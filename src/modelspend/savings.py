"""Savings projection: what you could save by moving each kind of work to a cheaper model, and how sure ModelSpend is.

Inputs, all from your own usage:
- **traffic**: what you actually spent per task category in the window, from the usage ledger (ModelSpend's own eval and shadow runs excluded);
- **shadow evidence**: for each cheaper model, in how many sampled real requests it gave an answer as good as the one you used (see shadow.py).

A cheaper model is shown as *clears* only when the same bar routing uses (your thresholds, else the profile's) is met by the 95% Wilson lower bound
of its pass rate: a handful of lucky samples never qualifies. Everything else is shown with what is missing (more samples, or "below the bar").

The projected saving is a **projection, not a measurement**. It assumes the sampled requests are typical of the traffic that would move, and it
scales your spend in that category by the cost ratio measured on those samples (candidate cost / cost of the model you used, for the same
requests, from reported token counts and catalog prices). Prices change, traffic changes, and agreement with your current model is not the same as
correctness; the report says so.
"""
from __future__ import annotations

import time
from typing import Any

from .catalog.models import EvidenceItem
from .config import Config
from .policy import PROFILES, bar_for, judge_evidence, min_samples_for
from .shadow import TOOLING_PROJECTS, aggregate, spent_this_month
from .store import Store
from .types import Profile, TaskCategory

COUNTED = ("ok", "validation_failed", "truncated", "refused")  # requests that produced an answer you paid for


def _traffic(store: Store, since: float | None, project: str | None) -> dict[str, dict[str, Any]]:
    marks = ",".join("?" for _ in TOOLING_PROJECTS)
    where, params = [f"coalesce(project, '') not in ({marks})", f"status in ({','.join('?' for _ in COUNTED)})"], [*TOOLING_PROJECTS, *COUNTED]
    if since is not None:
        where.append("ts >= ?")
        params.append(since)
    if project:
        where.append("project = ?")
        params.append(project.lower())
    q = (f"select task, provider || '/' || model ref, count(*) n, sum(actual_cost) cost, sum(case when actual_cost is null then 1 else 0 end) unpriced "
         f"from requests where {' and '.join(where)} group by 1, 2")
    out: dict[str, dict[str, Any]] = {}
    for r in store.db.execute(q, params):
        c = out.setdefault(r["task"], {"requests": 0, "cost": 0.0, "unpriced": 0, "models": {}})
        c["requests"] += r["n"]
        c["cost"] += r["cost"] or 0.0
        c["unpriced"] += r["unpriced"]
        c["models"][r["ref"]] = {"requests": r["n"], "cost": r["cost"] or 0.0}
    return out


def _needed(verdict: str, n: int, bar: Any) -> int:
    """How many more scored comparisons would let the bar be judged (0 when it already can be)."""
    if verdict in ("none", "needs_more_cases"):
        return max(0, bar.n - n)
    if verdict == "bar_unreachable":
        return max(0, min_samples_for(bar.rate) - n)
    return 0


def build_savings(store: Store, cfg: Config, *, profile: str = "balanced", since: float | None = None, project: str | None = None) -> dict[str, Any]:
    pol = PROFILES[Profile(profile)]
    traffic = _traffic(store, since, project)
    shadow = aggregate(store)
    rows: list[dict[str, Any]] = []
    for cat, t in sorted(traffic.items(), key=lambda kv: -kv[1]["cost"]):
        try:
            category = TaskCategory(cat)
        except ValueError:
            continue
        if category == TaskCategory.unknown:
            continue
        bar = bar_for(pol, category, cfg.thresholds)
        top = max(t["models"].items(), key=lambda kv: kv[1]["cost"])[0] if t["models"] else None
        cands = []
        for a in (x for x in shadow if x["category"] == cat):
            ref = f"{a['provider']}/{a['model']}"
            verdict, detail, lb = judge_evidence(EvidenceItem(n=a["n"], passes=a["passes"], dataset="shadow"), bar) if a["n"] else \
                ("none", f"no scored comparisons yet (need {bar.n})", None)
            ratio = (a["cand_cost"] / a["used_cost"]) if a["cost_rows"] and a["used_cost"] else None
            movable = t["cost"] - t["models"].get(ref, {"cost": 0.0})["cost"]  # spend on this model already needs no move
            saving = movable * (1 - ratio) if ratio is not None and ratio < 1 else 0.0
            cands.append({"model": ref, "n": a["n"], "passes": a["passes"], "unscored": a["unscored"], "truncated": a["truncated"], "lower_bound": lb, "verdict": verdict, "detail": detail,
                          "cost_ratio": ratio, "projected_saving_usd": saving, "samples_needed": _needed(verdict, a["n"], bar)})
        clearing = [c for c in cands if c["verdict"] == "clears" and c["projected_saving_usd"] > 0]
        best = max(clearing, key=lambda c: c["projected_saving_usd"]) if clearing else None
        if best:
            state = "ready"
        elif any(c["verdict"] in ("none", "needs_more_cases", "bar_unreachable") for c in cands) or not cands:
            state = "collecting"
        else:
            state = "no_safe_cheaper_model"
        rows.append({"category": cat, "requests": t["requests"], "cost": t["cost"], "cost_complete": not t["unpriced"], "top_model": top,
                     "bar": bar.describe(), "state": state, "recommended": best, "candidates": sorted(cands, key=lambda c: -(c["projected_saving_usd"]))})
    total = sum(r["cost"] for r in rows)
    saving = sum(r["recommended"]["projected_saving_usd"] for r in rows if r["recommended"])
    month = spent_this_month(store)
    return {
        "profile": profile, "since": since, "project": project, "rows": rows,
        "totals": {"cost": total, "projected_saving_usd": saving, "projected_saving_pct": (saving / total) if total else 0.0,
                   "categories_ready": sum(1 for r in rows if r["state"] == "ready"), "categories": len(rows),
                   "shadow_spend_this_month": month, "shadow_budget": cfg.shadow.monthly_budget_usd, "shadow_enabled": cfg.shadow.enabled},
        "notes": [
            "This is a projection, not a measurement. It assumes the sampled requests are typical of the traffic that would move.",
            "'Passes' means the cheaper model gave an answer as good as the one you used (your validators where you have them, otherwise an exact match or a judge model). It does not prove the answer is correct.",
            "Savings scale your recorded spend by the cost ratio measured on the samples; prices and traffic change.",
            "ModelSpend's own eval and shadow runs are excluded from your traffic. Shadow comparisons cost money: see `modelspend shadow status`.",
        ],
        "generated": time.time(),
    }


def _usd(v: float | None) -> str:
    return "-" if v is None else f"${v:,.4f}"


def to_table(rep: dict[str, Any]) -> str:
    t = rep["totals"]
    head = ["task", "spend now", "top model", "state", "best cheaper model", "passed", "lower bound", "cost vs now", "projected saving"]
    rows = [head]
    cut: list[str] = []
    for r in rep["rows"]:
        b = r["recommended"]
        pick = b or (r["candidates"][0] if r["candidates"] else None)
        state = {"ready": "READY", "collecting": "collecting evidence", "no_safe_cheaper_model": "no safe cheaper model yet"}[r["state"]]
        rows.append([r["category"], _usd(r["cost"]) + ("" if r["cost_complete"] else " +"), (r["top_model"] or "-").split("/", 1)[-1], state,
                     (pick["model"].split("/", 1)[-1] if pick else "-"), (f"{pick['passes']}/{pick['n']}" if pick else "-"),
                     (f"{pick['lower_bound']:.2f}" if pick and pick["lower_bound"] is not None else "-"),
                     (f"{pick['cost_ratio'] - 1:+.0%}" if pick and pick["cost_ratio"] is not None else "-"),
                     (f"{_usd(b['projected_saving_usd'])} ({b['projected_saving_usd'] / r['cost']:.0%})" if b and r["cost"] else "-")])
        if pick and pick["truncated"]:
            cut.append(f"{pick['model']}: {pick['truncated']} comparison(s) on {r['category']} were cut off at your max_output_tokens and are not counted")
    widths = [max(len(x[i]) for x in rows) for i in range(len(head))]
    lines = []
    for n, x in enumerate(rows):
        lines.append("  ".join(c.ljust(widths[i]) if i in (0, 2, 3, 4) else c.rjust(widths[i]) for i, c in enumerate(x)))
        if n == 0:
            lines.append("  ".join("-" * w for w in widths))
    out = "\n".join(lines) if rep["rows"] else "No usage recorded in this window."
    out += (f"\n\nSpend in window: {_usd(t['cost'])}.  Projected saving where the evidence clears the bar: {_usd(t['projected_saving_usd'])} "
            f"({t['projected_saving_pct']:.0%}), in {t['categories_ready']} of {t['categories']} task types.")
    if not t["shadow_enabled"]:
        out += "\nShadow evaluation is OFF, so no new evidence is being collected. Turn it on with: modelspend shadow on"
    else:
        out += f"\nShadow spend this month: {_usd(t['shadow_spend_this_month'])} of {_usd(t['shadow_budget'])}."
    if cut:
        out += "\n\nNote: " + "; ".join(cut) + ". Reasoning models spend output tokens thinking: give them more room (a larger max_output_tokens) to measure them."
    return out + "\n\n" + "\n".join(f"* {n}" for n in rep["notes"])
