"""What-if price table: what the tokens you already used would have cost on each model in your catalog.

It replays the token counts in your own usage ledger (input, cache reads, cache writes and output of every billed attempt) against each model's
catalog price, with the same arithmetic that priced the real calls (`pricing.cost_for`), so the table and your actual spend cannot disagree about a
model you really used.

What it is not: a promise. It assumes the other model would have read and written the same number of tokens (models differ, reasoning models
think more) and it says nothing about quality. The columns beside the price carry what ModelSpend has measured about quality: the shadow
evidence, judged against the same bar routing uses. A model with no evidence is shown as unmeasured, not as safe.
Models without a known USD price are listed apart and never guessed.
"""
from __future__ import annotations

import time
from typing import Any

from .catalog import Catalog
from .catalog.models import EvidenceItem, Model
from .config import Config
from .policy import PROFILES, bar_for, judge_evidence
from . import registry
from .pricing import cost_for
from .reconcile import NO_USAGE
from .savings import COUNTED
from .shadow import TOOLING_PROJECTS, aggregate
from .store import Store
from .types import Profile, TaskCategory, Usage

LISTED = ("available", "preview", "unverified")  # unverified: shipped before a `catalog sync` confirmed it; flagged in the table, not hidden


def _attempts(store: Store, since: float | None, project: str | None):
    marks = ",".join("?" for _ in TOOLING_PROJECTS)
    where, params = [f"coalesce(r.project, '') not in ({marks})", f"r.status in ({','.join('?' for _ in COUNTED)})",
                     "(a.input_tokens + a.cache_read + a.cache_write + a.output_tokens) > 0"], [*TOOLING_PROJECTS, *COUNTED]
    if since is not None:
        where.append("r.ts >= ?")
        params.append(since)
    if project:
        where.append("r.project = ?")
        params.append(project.lower())
    q = ("select r.task task, a.provider || '/' || a.model ref, coalesce(a.input_tokens, 0) i, coalesce(a.cache_read, 0) cr, "
         "coalesce(a.cache_write, 0) cw, coalesce(a.output_tokens, 0) o, a.cost cost "
         f"from attempts a join requests r on r.id = a.request_id where {' and '.join(where)}")
    return store.db.execute(q, params)


def build_whatif(store: Store, catalog: Catalog, cfg: Config, *, profile: str = "balanced", since: float | None = None, project: str | None = None,
                 only: list[str] | None = None, providers: list[str] | None = None) -> dict[str, Any]:
    traffic: dict[str, list[tuple[int, int, int, int]]] = {}  # task -> token tuples, one per billed attempt
    actual: dict[str, float] = {}
    unpriced = 0
    for r in _attempts(store, since, project):
        traffic.setdefault(r["task"], []).append((r["i"], r["cr"], r["cw"], r["o"]))
        actual[r["task"]] = actual.get(r["task"], 0.0) + (r["cost"] or 0.0)
        unpriced += r["cost"] is None
    rep = _price(store, catalog, cfg, profile, traffic, actual, _used_refs(store, since, project), only, providers, tiers=True)
    rep.update({"since": since, "project": project, "actual_complete": not unpriced, "source": None})
    return rep


def build_whatif_export(store: Store, catalog: Catalog, cfg: Config, rows: list[dict[str, Any]], *, profile: str = "balanced",
                        only: list[str] | None = None, providers: list[str] | None = None) -> dict[str, Any]:
    """The same table from a provider's own usage export (rows as `reconcile.parse_usage` returns them), for the day before you have any traffic here.

    An export has totals per model per day, not per request, so long-context price tiers cannot be applied (they depend on each request's size)
    and context-limit checks are skipped; there is no task type either, so the shadow evidence is listed without a 'moved only there' total.
    Nothing is written to the database."""
    traffic: dict[str, list[tuple[int, int, int, int]]] = {"(export)": []}
    spend, estimated, unknown, used, missing = 0.0, 0, 0, set(), set()
    calls, days = 0, set()
    for r in rows:
        if r["model"] == NO_USAGE:
            continue
        ref = f"{r['provider']}/{r['model']}"
        used.add(ref)
        days.add(r["day"])
        cr, cw = r["cache_read"], r["cache_write"]
        inp = r["input_tokens"]
        if registry.spec(r["provider"]).cached_included:  # these providers count cached tokens inside input: split them out so they are priced once
            inp = max(0, inp - cr)
        traffic["(export)"].append((inp, cr, cw, r["output_tokens"]))
        calls += r["requests"]
        if r["cost"] is not None:
            spend += r["cost"]
            continue
        try:
            spend += cost_for(_flat(catalog.get(ref)), Usage(input_tokens=inp, cache_read_tokens=cr, cache_write_tokens=cw, output_tokens=r["output_tokens"]))
            estimated += 1
        except (KeyError, ValueError):
            unknown += 1
            missing.add(ref)
    rep = _price(store, catalog, cfg, profile, traffic, {"(export)": spend}, used, only, providers, tiers=False)
    rep.update({"since": None, "project": None, "calls": calls, "actual_complete": not unknown,
                "source": {"kind": "export", "first_day": min(days) if days else None, "last_day": max(days) if days else None, "models": len(used),
                           "cost_estimated_rows": estimated, "cost_unknown_rows": unknown, "no_catalog_price": sorted(missing)}})
    rep["notes"].insert(0, "Built from a provider usage export: totals per model per day. Long-context price tiers and per-request context limits cannot be applied, so a model "
                           "with a long-context surcharge may cost more than shown on long prompts. Nothing from the file was stored.")
    return rep


def _flat(m: Model) -> Model:
    """The model priced without long-context tiers (those need each request's size, which an export does not have)."""
    return m.model_copy(update={"pricing": m.pricing.model_copy(update={"long_context_tiers": []})}) if m.pricing else m


def _price(store: Store, catalog: Catalog, cfg: Config, profile: str, traffic: dict[str, list[tuple[int, int, int, int]]], actual: dict[str, float],
           used: set[str], only: list[str] | None, providers: list[str] | None, tiers: bool) -> dict[str, Any]:
    pol = PROFILES[Profile(profile)]
    tot = {k: sum(sum(t[n] for t in v) for v in traffic.values()) for n, k in enumerate(("input", "cache_read", "cache_write", "output"))}
    calls = sum(len(v) for v in traffic.values())
    spend = sum(actual.values())
    shadow = {(f"{a['provider']}/{a['model']}", a["category"]): a for a in aggregate(store)}

    def listed(m: Model) -> bool:
        if m.availability not in LISTED:
            return False
        if providers and m.provider not in providers:
            return False
        return not only or any(o in m.ref.lower() for o in only)

    rows: list[dict[str, Any]] = []
    unpriced_models: list[str] = []
    for m in catalog.models:
        if not listed(m):
            continue
        p = m.pricing
        if p is None or not p.known or p.currency != "USD":
            unpriced_models.append(m.ref)
            continue
        priced = m if tiers else _flat(m)
        cost, by_task, no_fit, no_room = 0.0, {}, 0, 0
        ctx, cap = m.limits.context_tokens, m.limits.max_output_tokens
        for task, toks in traffic.items():
            c = 0.0
            for i, cr, cw, o in toks:
                c += cost_for(priced, Usage(input_tokens=i, cache_read_tokens=cr, cache_write_tokens=cw, output_tokens=o))
                if tiers:
                    no_fit += bool(ctx and i + cr + cw + o > ctx)
                    no_room += bool(cap and o > cap)
            by_task[task] = c
            cost += c
        clears, below = [], []
        n_all = passes_all = 0
        for task in (traffic if tiers else sorted({c for (ref, c) in shadow if ref == m.ref})):  # an export has no task types: list every task the evidence covers
            a = shadow.get((m.ref, task))
            if not a or not a["n"]:
                continue
            n_all, passes_all = n_all + a["n"], passes_all + a["passes"]
            try:
                bar = bar_for(pol, TaskCategory(task), cfg.thresholds)
            except ValueError:
                continue
            verdict = judge_evidence(EvidenceItem(n=a["n"], passes=a["passes"], dataset="shadow"), bar)[0]
            (clears if verdict == "clears" else below).append(task)
        moved = spend - sum(actual[t] for t in clears) + sum(by_task[t] for t in clears) if clears and tiers else None  # only the tasks where it clears the bar move
        rows.append({"model": m.ref, "cost": cost, "cost_if_moved_where_clear": moved, "free": p.input_per_mtok == 0 and p.output_per_mtok == 0, "vs_actual": (cost - spend) if spend else None, "ratio": (cost / spend) if spend else None,
                     "by_task": by_task, "too_large": no_fit, "over_output_cap": no_room, "tier": m.tier, "used": m.ref in used, "availability": m.availability,
                     "evidence_n": n_all, "evidence_passes": passes_all, "clears": sorted(clears), "not_cleared": sorted(below),
                     "price_verified_on": p.verified_on.isoformat() if p.verified_on else None})
    rows.sort(key=lambda r: r["cost"])
    return {"profile": profile, "calls": calls, "tokens": tot, "actual_cost": spend, "rows": rows, "unpriced_models": sorted(unpriced_models),
            "tasks": {t: {"calls": len(v), "actual_cost": actual[t]} for t, v in sorted(traffic.items(), key=lambda kv: -actual[kv[0]])},
            "notes": [
                "Same tokens, other prices: it assumes each model would have read and written exactly the tokens you recorded. Models differ, and reasoning models often write more.",
                "Price says nothing about quality. 'Evidence' is the shadow comparison against the answer you used (see `modelspend savings`); no evidence means unmeasured, not safe.",
                "Prices are the catalog's. Check the verified date; `modelspend pricing refresh` updates them from the providers' own pages.",
                "ModelSpend's own eval and shadow runs are excluded from your traffic.",
            ], "generated": time.time()}


def _used_refs(store: Store, since: float | None, project: str | None) -> set[str]:
    return {r["ref"] for r in store.db.execute(
        "select distinct provider || '/' || model ref from requests where status in ('ok','validation_failed','truncated','refused') "
        "and coalesce(project,'') not in (?, ?) and (? is null or ts >= ?) and (? is null or project = ?)",
        (*TOOLING_PROJECTS, since, since, project and project.lower(), project and project.lower()))}


def _usd(v: float | None) -> str:
    return "-" if v is None else f"${v:,.4f}"


def to_table(rep: dict[str, Any], top: int = 15) -> str:
    if not any(rep["tokens"].values()):
        return "No usage found in the export." if rep.get("source") else "No usage recorded in this window."
    head = ["model", "would cost", "vs actual", "evidence", "clears the bar for", "total if moved only there", "notes"]
    rows = [head]
    shown = rep["rows"][:top]
    for r in shown:
        ev = f"{r['evidence_passes']}/{r['evidence_n']}" if r["evidence_n"] else "unmeasured"
        notes = []
        if r["used"]:
            notes.append("you used it")
        if r["availability"] == "unverified":
            notes.append("availability unverified (modelspend catalog sync)")
        if r["free"]:
            notes.append("no per-token price (local or free): hardware and quality not counted")
        if r["too_large"]:
            notes.append(f"{r['too_large']} call(s) exceed its context")
        if r["over_output_cap"]:
            notes.append(f"{r['over_output_cap']} call(s) exceed its output cap")
        rows.append([r["model"], _usd(r["cost"]), (f"{r['ratio'] - 1:+.0%}" if r["ratio"] is not None else "-"), ev,
                     ", ".join(r["clears"]) or "-",
                     _usd(r["cost_if_moved_where_clear"]) if r["cost_if_moved_where_clear"] is not None else "-", "; ".join(notes)])
    widths = [max(len(x[i]) for x in rows) for i in range(len(head))]
    lines = []
    for n, x in enumerate(rows):
        lines.append("  ".join(c.ljust(widths[i]) if i in (0, 3, 4, 6) else c.rjust(widths[i]) for i, c in enumerate(x)).rstrip())
        if n == 0:
            lines.append("  ".join("-" * w for w in widths))
    t = rep["tokens"]
    src = rep.get("source")
    if not rep["rows"]:
        return ("No model in your catalog has a known USD price yet, so there is nothing to compare. Add some: `modelspend keys set <provider>`, "
                "`modelspend catalog sync`, then `modelspend catalog price <ref> --input <usd/M> --output <usd/M>` from the provider's pricing page "
                "(or `modelspend pricing refresh`), or bring in OpenRouter's models with `modelspend catalog import-openrouter --only <ids>`.")
    what = (f"Your export ({src['first_day']} to {src['last_day']}, {src['models']} model(s)): " + (f"{rep['calls']:,} requests, " if rep["calls"] else "")
            if src else f"Your traffic: {rep['calls']:,} billed calls, ")
    basis = ""
    if src and src["cost_estimated_rows"]:
        basis = f" ({src['cost_estimated_rows']} row(s) had no cost column and are priced from the catalog)"
    out = (f"{what}{t['input']:,} input + {t['cache_read']:,} cached + {t['cache_write']:,} cache-write "
           f"input tokens and {t['output']:,} output tokens. Actual spend {('unknown (no cost column, and no catalog price for these models)' if not rep['actual_cost'] and not rep['actual_complete'] else _usd(rep['actual_cost']) + ('' if rep['actual_complete'] else ' (some rows unpriced, so this is a minimum)'))}{basis}.\n\n")
    out += "\n".join(lines)
    if len(rep["rows"]) > len(shown):
        out += f"\n... {len(rep['rows']) - len(shown)} more (cheapest first). Show all with --all."
    if src and src["no_catalog_price"]:
        out += (f"\n\nIn your export but with no price in your catalog, so they could not be priced (and are not in your actual spend unless the file has a cost column): "
                f"{', '.join(src['no_catalog_price'][:8])}. Add them with `modelspend catalog sync` (needs that provider's key), then "
                f"`modelspend catalog price <ref> --input <usd/M> --output <usd/M>` from the provider's pricing page.")
    if rep["unpriced_models"]:
        out += f"\n\nNo known price, so not in the table: {', '.join(rep['unpriced_models'][:8])}" + (" ..." if len(rep["unpriced_models"]) > 8 else "")
    return out + "\n\n" + "\n".join(f"* {n}" for n in rep["notes"])
