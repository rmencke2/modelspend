"""Which models clear YOUR bar for which task category: measured results (from `modelspend eval calibrate` on your own cases)
against the evidence bar that applies (the profile's, or the one you set with `modelspend thresholds set`).

Uses the same functions as routing (`policy.bar_for` / `judge_evidence`), so this table and the router cannot disagree."""
from __future__ import annotations

from typing import Any

from .catalog import Catalog
from .config import Config
from .policy import PROFILES, bar_for, judge_evidence
from .types import Profile, TaskCategory

VERDICT_TEXT = {
    "clears": "clears the bar",
    "below_bar": "below the bar",
    "needs_more_cases": "needs more cases",
    "bar_unreachable": "bar unreachable at this n",
    "none": "no evidence",
}


def _blended_price(m) -> float | None:
    p = m.pricing
    return None if p is None or not p.known else (p.input_per_mtok + p.output_per_mtok) / 2


def build(catalog: Catalog, cfg: Config, profile: str = "balanced", category: str | None = None, model: str | None = None) -> dict[str, Any]:
    prof = Profile(profile)
    pol = PROFILES[prof]
    cats = [c for c in TaskCategory if c != TaskCategory.unknown and (category is None or c.value == category)]
    if category and not cats:
        raise ValueError(f"unknown category '{category}'")
    rows: list[dict[str, Any]] = []
    cheapest: dict[str, dict[str, Any]] = {}
    bars: dict[str, dict[str, Any]] = {}
    for c in cats:
        bar = bar_for(pol, c, cfg.thresholds)
        bars[c.value] = {"min_n": bar.n, "min_pass_rate": bar.rate, "basis": bar.basis, "require_evidence": bar.require_evidence,
                         "source": bar.source, "describe": bar.describe()}
        clearing = []
        for m in catalog.models:
            if model and m.ref != model:
                continue
            ev = m.evidence.get(c.value)
            if ev is None:
                continue
            verdict, detail, lb = judge_evidence(ev, bar)
            rows.append({"model": m.ref, "category": c.value, "n": ev.n, "passes": ev.passes, "observed": ev.passes / ev.n if ev.n else None,
                         "lower_bound": lb, "dataset": ev.dataset, "measured_on": str(ev.measured_on) if ev.measured_on else None,
                         "verdict": verdict, "detail": detail})
            if verdict == "clears":
                clearing.append((m, _blended_price(m)))
        if clearing:
            priced = [(m, p) for m, p in clearing if p is not None]
            pick = min(priced, key=lambda x: x[1])[0] if priced else min(clearing, key=lambda x: x[0].tier or 9)[0]
            cheapest[c.value] = {"model": pick.ref, "blended_usd_per_mtok": _blended_price(pick), "clearing_models": sorted(m.ref for m, _ in clearing)}
    rows.sort(key=lambda r: (r["category"], r["model"]))
    return {"profile": profile, "bars": bars, "rows": rows, "cheapest_clearing": cheapest,
            "note": "Measured on your cases; the pass/fail checks are only as good as your validators. The router uses exactly these verdicts."}


def to_table(rep: dict[str, Any]) -> str:
    lines = [f"Evidence against the {rep['profile']} profile's bars (and any you set with `modelspend thresholds set`):", ""]
    for cat, b in rep["bars"].items():
        lines.append(f"  {cat:<15} bar: {b['describe']}" + ("" if b["source"] == "profile" else f"   [{b['source']}]"))
    if not rep["rows"]:
        return "\n".join(lines + ["", "No measured evidence yet. Measure your own cases with `modelspend eval calibrate --mode live --budget 1 --fixtures <dir> --write`."])
    w = max(len(r["model"]) for r in rep["rows"])
    lines += ["", f"{'category':<15}{'model':<{w + 2}}{'passed':>9}{'observed':>10}{'lower bd':>10}  verdict"]
    for r in rep["rows"]:
        lines.append(f"{r['category']:<15}{r['model']:<{w + 2}}{str(r['passes']) + '/' + str(r['n']):>9}{r['observed']:>10.2f}{r['lower_bound']:>10.2f}  {VERDICT_TEXT[r['verdict']]}"
                     + (f"  ({r['detail'].split('; ')[-1]})" if r["verdict"] in ("bar_unreachable",) else ""))
    if rep["cheapest_clearing"]:
        lines += ["", "Cheapest model that clears the bar (by blended list price):"]
        for c, v in rep["cheapest_clearing"].items():
            price = f"${v['blended_usd_per_mtok']:.2f}/M" if v["blended_usd_per_mtok"] is not None else "unpriced"
            lines.append(f"  {c:<15}{v['model']}  ({price}; {len(v['clearing_models'])} model(s) clear it)")
    lines += ["", rep["note"]]
    return "\n".join(lines)
