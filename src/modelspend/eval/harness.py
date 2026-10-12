"""Evaluation harness: premium baseline vs cheapest-eligible baseline vs ModelSpend.

All three arms get identical tasks and are graded by the same task-specific, deterministic
validators. Calibration and held-out fixtures live in separate directories; `calibrate` reads only
the calibration split and `evaluate` defaults to held-out. Any savings figure is computed from the
run's own measurements and labelled SIMULATED or LIVE.
"""
from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Callable

from ..catalog.models import EvidenceItem
from ..engine import Engine, Ledger
from ..errors import ModelSpendError, ProviderError
from ..types import Candidate, RouteRequest, TaskCategory, ValidatorSpec
from ..validation import run_spec

MAX_OUT = 400
ARMS = ("premium", "cheapest_eligible", "modelspend")


@dataclass
class EvalTask:
    id: str
    category: str
    difficulty: int
    messages: list[dict[str, Any]]
    validators: list[dict[str, Any]]
    reference: str
    wrong: str
    sandbox_validators: list[dict[str, Any]] = field(default_factory=list)
    split: str = ""

    @property
    def prompt(self) -> str:
        return next(m["content"] for m in reversed(self.messages) if m["role"] == "user")


def fixtures_root(path: str | Path | None = None) -> Path:
    return Path(path) if path else Path(str(resources.files("modelspend.eval").joinpath("fixtures")))


def load_tasks(split: str, fixtures_dir: str | Path | None = None) -> list[EvalTask]:
    if split not in ("calibration", "heldout"):
        raise ValueError("split must be 'calibration' or 'heldout'")
    out = []
    for f in sorted((fixtures_root(fixtures_dir) / split).glob("*.jsonl")):
        for line in f.read_text().splitlines():
            if line.strip():
                out.append(EvalTask(split=split, **json.loads(line)))
    return out


def grade(task: EvalTask, text: str, sandbox_runner: Callable | None = None, use_sandbox: bool = False) -> tuple[bool, list[str]]:
    specs = list(task.validators) + (task.sandbox_validators if use_sandbox else [])
    fails = []
    for s in specs:
        r = run_spec(ValidatorSpec(**s), text, sandbox_runner)
        if not r.passed:
            fails.append(f"{r.name}: {r.detail}")
    return not fails, fails


def wilson_interval(passes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 0.0
    p = passes / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return max(0.0, (c - m) / d), min(1.0, (c + m) / d)


@dataclass
class Outcome:
    task_id: str
    category: str
    arm: str
    passed: bool
    cost_usd: float
    latency_ms: int
    escalated: bool = False
    provider: str = ""
    model: str = ""
    error: str | None = None
    fails: list[str] = field(default_factory=list)


def _percentile(xs: list[float], q: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))]


def _eligible(engine: Engine, req: RouteRequest) -> list[Candidate]:
    try:
        plan = engine.router.plan(req)
        return plan.candidates + plan.unsuitable
    except ModelSpendError as e:
        plan = e.details.get("plan")
        return (plan.candidates + plan.unsuitable) if plan else []


def default_premium(engine: Engine, providers: list[str]) -> str:
    c = [m for m in engine.catalog.for_providers(providers) if m.tier == 3 and m.pricing and m.pricing.known and m.availability == "available"]
    if not c:
        raise ValueError("no tier-3 priced model among the selected providers; pass --premium provider/model")
    return max(c, key=lambda m: m.pricing.output_per_mtok).ref  # type: ignore[union-attr]


async def evaluate(
    engine: Engine, tasks: list[EvalTask], providers: list[str], premium: str | None = None,
    budget_usd: float | None = None, mode: str = "simulated", use_sandbox: bool = False,
) -> dict[str, Any]:
    premium = premium or default_premium(engine, providers)
    prem_provider = premium.split("/", 1)[0]
    spent_total, outcomes, done, stopped = 0.0, [], 0, None

    for t in tasks:
        remaining = None if budget_usd is None else budget_usd - spent_total
        base = dict(messages=t.messages, max_output_tokens=MAX_OUT, profile="economy")
        prem_req = RouteRequest(providers=[prem_provider], allow_models=[premium], **base)
        cheap_req = RouteRequest(providers=providers, **base)
        ms_req = RouteRequest(providers=providers, messages=t.messages, max_output_tokens=MAX_OUT, profile="balanced",
                              validators=t.validators + (t.sandbox_validators if use_sandbox else []))
        prem_c = next((c for c in _eligible(engine, prem_req) if f"{c.provider}/{c.model}" == premium), None)
        cheap_c = sorted(_eligible(engine, cheap_req), key=lambda c: c.expected_cost_usd)[:1]
        if prem_c is None or not cheap_c:
            raise ValueError(f"task {t.id}: premium model '{premium}' or a cheapest-eligible model is not eligible under hard constraints")
        if remaining is not None:  # stop before a task whose worst case could overrun the budget
            ms_plan = engine.router.plan(ms_req)
            worst = prem_c.worst_case_cost_usd + cheap_c[0].worst_case_cost_usd + ms_plan.candidates[0].worst_case_cost_usd + (
                max(c.worst_case_cost_usd for c in ms_plan.candidates) if ms_plan.escalation_limit else 0)
            if worst > remaining:
                stopped = f"stopped before task {t.id}: worst-case cost ${worst:.6f} exceeds remaining budget ${remaining:.6f}"
                break
        task_outcomes = []
        for arm, cand in (("premium", prem_c), ("cheapest_eligible", cheap_c[0])):
            ledger = Ledger(remaining)
            req = prem_req if arm == "premium" else cheap_req
            try:
                resp, lat = await engine.run_single(req, cand, ledger)
                text = resp.text if resp else ""
                ok, fails = grade(t, text, engine.sandbox_runner, use_sandbox) if resp else (False, ["skipped"])
                o = Outcome(t.id, t.category, arm, ok, ledger.used, lat, provider=cand.provider, model=cand.model, fails=fails)
            except ProviderError as e:
                o = Outcome(t.id, t.category, arm, False, ledger.used, 0, provider=cand.provider, model=cand.model, error=e.kind)
            task_outcomes.append(o)
        ms_req.max_budget_usd = None if remaining is None else max(1e-9, remaining - sum(o.cost_usd for o in task_outcomes))
        try:
            r = await engine.run(ms_req)
            ok, fails = grade(t, r.text, engine.sandbox_runner, use_sandbox)
            task_outcomes.append(Outcome(t.id, t.category, "modelspend", ok, r.actual_cost_usd + r.uncertain_cost_usd, r.latency_ms,
                                         escalated=r.escalations > 0, provider=r.provider, model=r.model, fails=fails))
        except ModelSpendError as e:
            task_outcomes.append(Outcome(t.id, t.category, "modelspend", False, float(e.details.get("cost", 0.0) or 0.0), 0, error=e.code))
        outcomes += task_outcomes
        spent_total += sum(o.cost_usd for o in task_outcomes)
        done += 1
    return build_report(outcomes, mode, premium, providers, len(tasks), done, stopped, budget_usd, spent_total, engine)


def build_report(outcomes: list[Outcome], mode: str, premium: str, providers: list[str], n_tasks: int, done: int,
                 stopped: str | None, budget: float | None, spent: float, engine: Engine) -> dict[str, Any]:
    arms: dict[str, Any] = {}
    for arm in ARMS:
        os_ = [o for o in outcomes if o.arm == arm]
        n, p = len(os_), sum(o.passed for o in os_)
        total = sum(o.cost_usd for o in os_)
        lat = [o.latency_ms for o in os_ if o.latency_ms]
        lo, hi = wilson_interval(p, n)
        by_cat: dict[str, dict[str, float]] = {}
        for o in os_:
            c = by_cat.setdefault(o.category, {"n": 0, "passes": 0})
            c["n"] += 1
            c["passes"] += o.passed
        arms[arm] = {
            "tasks": n, "passes": p, "pass_rate": p / n if n else 0.0, "pass_rate_ci95": [lo, hi],
            "total_spend_usd": total, "cost_per_success_usd": (total / p) if p else None,
            "latency_ms": {"p50": _percentile(lat, .5), "p95": _percentile(lat, .95), "mean": statistics.fmean(lat) if lat else 0.0},
            "escalation_rate": (sum(o.escalated for o in os_) / n) if n else 0.0, "by_category": by_cat,
            "models_used": sorted({f"{o.provider}/{o.model}" for o in os_ if o.model}),
            "errors": sum(1 for o in os_ if o.error),
        }
    prem, ms, cheap = arms["premium"], arms["modelspend"], arms["cheapest_eligible"]
    tradeoff = {}
    for name, a in (("modelspend", ms), ("cheapest_eligible", cheap)):
        tradeoff[name] = {
            "spend_vs_premium_pct": ((a["total_spend_usd"] / prem["total_spend_usd"] - 1) * 100) if prem["total_spend_usd"] else None,
            "pass_rate_vs_premium_pp": (a["pass_rate"] - prem["pass_rate"]) * 100,
        }
    return {
        "mode": mode, "simulated": mode == "simulated", "premium_model": premium, "providers": providers,
        "tasks_planned": n_tasks, "tasks_completed": done, "stopped_early": stopped, "budget_usd": budget,
        "total_spend_usd": spent, "arms": arms, "tradeoff": tradeoff,
        "catalog_version": engine.catalog.version, "policy_version": __import__("modelspend").POLICY_VERSION,
        "outcomes": [o.__dict__ for o in outcomes],
    }


def render_markdown(rep: dict[str, Any]) -> str:
    sim = rep["simulated"]
    L = [f"# ModelSpend evaluation ({'SIMULATED' if sim else 'LIVE'})", ""]
    if sim:
        L += ["> **SIMULATED DATA.** Models are fake: a simulated model answers correctly iff its catalog tier >= the task's "
              "difficulty label. This verifies harness and routing *mechanics* only. It is **not** evidence of real model quality "
              "or real savings, and must not be quoted as a benchmark.", ""]
    else:
        L += ["> Live run against real provider APIs with the operator's keys. Spend is computed from provider-reported usage and "
              "catalog prices; the provider invoice is authoritative. Small task counts give wide intervals.", ""]
    L += [f"- Premium baseline: `{rep['premium_model']}`  |  providers: {', '.join(rep['providers'])}",
          f"- Tasks: {rep['tasks_completed']}/{rep['tasks_planned']} completed; total spend ${rep['total_spend_usd']:.6f}"
          + (f" (budget ${rep['budget_usd']:.4f})" if rep["budget_usd"] else ""),
          f"- Catalog `{rep['catalog_version']}`, policy `{rep['policy_version']}`"]
    if rep["stopped_early"]:
        L.append(f"- **Partial run:** {rep['stopped_early']}")
    L += ["", "| Arm | Tasks | Pass rate (95% CI) | Total spend | Cost / success | p50 ms | p95 ms | Escalation rate |", "|---|---|---|---|---|---|---|---|"]
    for arm, a in rep["arms"].items():
        cps = f"${a['cost_per_success_usd']:.6f}" if a["cost_per_success_usd"] is not None else "n/a"
        L.append(f"| {arm} | {a['tasks']} | {a['pass_rate']*100:.0f}% ({a['pass_rate_ci95'][0]*100:.0f}-{a['pass_rate_ci95'][1]*100:.0f}) | "
                 f"${a['total_spend_usd']:.6f} | {cps} | {a['latency_ms']['p50']:.0f} | {a['latency_ms']['p95']:.0f} | {a['escalation_rate']*100:.0f}% |")
    L += ["", "## Cost-quality trade-off vs premium baseline", "", "| Arm | Spend vs premium | Pass-rate vs premium |", "|---|---|---|"]
    for arm, t in rep["tradeoff"].items():
        sp = f"{t['spend_vs_premium_pct']:+.1f}%" if t["spend_vs_premium_pct"] is not None else "n/a"
        L.append(f"| {arm} | {sp} | {t['pass_rate_vs_premium_pp']:+.1f} pp |")
    L += ["", "## Pass rate by category (passes/tasks)", "", "| Category | " + " | ".join(rep["arms"]) + " |", "|---|" + "---|" * len(rep["arms"])]
    cats = sorted({c for a in rep["arms"].values() for c in a["by_category"]})
    for c in cats:
        L.append(f"| {c} | " + " | ".join(f"{int(a['by_category'].get(c, {}).get('passes', 0))}/{int(a['by_category'].get(c, {}).get('n', 0))}" for a in rep["arms"].values()) + " |")
    L += ["", "Notes: baselines get no validators and never escalate; ModelSpend receives the task's validators (that is the product's "
          "intended use) and may escalate once. All arms are graded by the same deterministic checks, which verify format and key facts, "
          "not full correctness."]
    return "\n".join(L) + "\n"


async def calibrate(engine: Engine, tasks: list[EvalTask], providers: list[str], budget_usd: float | None = None,
                    use_sandbox: bool = False) -> tuple[dict[str, dict[str, EvidenceItem]], float, str | None]:
    """Run every eligible model on every calibration task of each category; return measured evidence."""
    from datetime import date
    ev: dict[str, dict[str, EvidenceItem]] = {}
    spent, stopped = 0.0, None
    for t in tasks:
        req = RouteRequest(providers=providers, messages=t.messages, max_output_tokens=MAX_OUT, profile="economy")
        for cand in sorted(_eligible(engine, req), key=lambda c: (c.provider, c.model)):
            if budget_usd is not None and spent + cand.worst_case_cost_usd > budget_usd:
                stopped = f"stopped: next call's worst case would exceed the ${budget_usd:.4f} calibration budget"
                return ev, spent, stopped
            ledger = Ledger(None)
            try:
                resp, _ = await engine.run_single(req, cand, ledger)
                ok = grade(t, resp.text, engine.sandbox_runner, use_sandbox)[0] if resp else False
            except ProviderError:
                ok = False
            spent += ledger.used
            slot = ev.setdefault(f"{cand.provider}/{cand.model}", {}).setdefault(
                t.category, EvidenceItem(n=0, passes=0, dataset="modelspend-calibration-v1", measured_on=date.today()))
            slot.n += 1
            slot.passes += int(ok)
    return ev, spent, stopped
