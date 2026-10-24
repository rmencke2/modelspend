"""A short spend digest: what the last day (or week) cost, what changed against the period before it, which budgets are close, and anything that looks off.

It is built from the same ledger as `modelspend report`, so the numbers agree with it: only calls made through ModelSpend, costed at the prices in effect when
each call ran, and your provider's invoice is authoritative. It contains numbers, project names, model names and task labels, never prompt text.

Printing it is free and local. `--post` sends it to one webhook you configured yourself (Slack, Discord or a generic JSON endpoint). That is the only thing in
ModelSpend that sends your usage metadata anywhere, so it is off until you set a webhook, project names are left out of what is posted unless you pass
`--include-projects`, redirects are not followed, and the webhook address (which usually contains a secret) is never printed or logged.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, Callable

from . import budgets
from .registry import _check_url
from .report import UNTAGGED, build_report
from .savings import COUNTED
from .shadow import TOOLING_PROJECTS
from .store import Store

PERIODS = {"day": (86400.0, "24 h"), "week": (7 * 86400.0, "7 days")}
FORMATS = ("slack", "discord", "json")
SPIKE_FACTOR = 2.0
SPIKE_MIN_USD = 0.50
OVERHEAD_WARN = 0.30
ERROR_RATE_WARN = 0.20
MOVERS = 3


class DigestError(Exception):
    pass


def validate_webhook(url: str) -> str:
    return _check_url("digest webhook", "url", url, allow_http_loopback=True)


def _cost(row: dict[str, Any]) -> float:
    return float(row.get("cost") or 0.0)


def _pct(cur: float, prev: float) -> float | None:
    return None if prev <= 0 else (cur - prev) / prev


def _by(store: Store, dim: str, lo: float, hi: float) -> dict[str, dict[str, Any]]:
    return {r[dim]: r for r in build_report(store, [dim], since=lo, until=hi)["rows"]}


def _movers(cur: dict[str, dict[str, Any]], prev: dict[str, dict[str, Any]], skip: set[str]) -> list[dict[str, Any]]:
    out = []
    for name in set(cur) | set(prev):
        if name in skip:
            continue
        c, p = _cost(cur.get(name, {})), _cost(prev.get(name, {}))
        if round(c - p, 4) != 0:
            out.append({"name": name, "cost": c, "previous": p, "delta": c - p, "new": p == 0 and c > 0,
                        "complete": cur.get(name, {}).get("cost_complete", True)})
    return sorted(out, key=lambda m: (-abs(m["delta"]), m["name"]))[:MOVERS]


def build_digest(store: Store, *, period: str = "day", now: float | None = None, catalog: Any = None) -> dict[str, Any]:
    if period not in PERIODS:
        raise DigestError(f"period must be one of {', '.join(PERIODS)}")
    span, label = PERIODS[period]
    now = time.time() if now is None else now
    lo, mid = now - span, now - span
    prev_lo = now - 2 * span
    tot = build_report(store, since=lo, until=now + 1)["totals"]
    ptot = build_report(store, since=prev_lo, until=mid)["totals"]
    cost, pcost = _cost(tot), _cost(ptot)
    by_project, pby_project = _by(store, "project", lo, now + 1), _by(store, "project", prev_lo, mid)
    by_model = _by(store, "model", lo, now + 1)
    tooling = sum(_cost(r) for n, r in by_project.items() if n in TOOLING_PROJECTS)
    models = sorted(by_model.values(), key=lambda r: (-_cost(r), r["model"]))[:3]
    marks = ",".join("?" for _ in COUNTED)
    n_req = store.db.execute("select count(*) from requests where ts >= ? and ts < ?", (lo, now + 1)).fetchone()[0]
    failed = store.db.execute(f"select count(*) from requests where ts >= ? and ts < ? and status not in ({marks})", (lo, now + 1, *COUNTED)).fetchone()[0]
    bud = [b for b in budgets.status(store, now) if b["state"] != "ok"]
    flags: list[str] = []
    if pcost > 0 and cost >= SPIKE_FACTOR * pcost and cost - pcost >= SPIKE_MIN_USD:
        flags.append(f"spend is {cost / pcost:.1f}x the previous {label}")
    elif pcost == 0 and cost >= SPIKE_MIN_USD:
        flags.append(f"no spend in the previous {label}, then ${cost:,.2f}")
    if tot["unpriced_attempts"]:
        rows = store.db.execute("select a.provider || '/' || a.model m, count(*) c from attempts a join requests r on r.id = a.request_id where r.ts >= ? and r.ts < ? and a.cost is null "
                                "and a.outcome != 'skipped_budget' group by 1", (lo, now + 1)).fetchall()
        priced = {m.ref for m in catalog.models if m.pricing and m.pricing.known} if catalog is not None else set()
        early = sum(r["c"] for r in rows if r["m"] in priced)  # recorded before the model was priced: the price is set now, the old calls stay unknown
        still = tot["unpriced_attempts"] - early
        if still:
            flags.append(f"{still} call(s) used a model with no price, so the total is partial (price it with `modelspend catalog price`)")
        if early:
            flags.append(f"{early} earlier call(s) ran before their model was priced, so the total is a little low (new calls are costed)")
    if n_req >= 10 and failed / n_req > ERROR_RATE_WARN:
        flags.append(f"{failed} of {n_req} requests did not produce an answer")
    if tot["overhead_token_share"] > OVERHEAD_WARN and tot["total_tokens"]:
        flags.append(f"{tot['overhead_token_share']:.0%} of tokens went on retries, failovers, classifier or judge calls rather than final answers")
    return {
        "period": period, "label": label, "start": lo, "end": now, "requests": n_req, "failed_requests": failed,
        "cost": cost, "previous_cost": pcost, "change_pct": _pct(cost, pcost), "complete": tot["cost_complete"],
        "tooling_cost": tooling, "cache_hit_rate": tot["cache_hit_rate"],
        "projects": _movers(by_project, pby_project, set(TOOLING_PROJECTS)), "models": [{"name": r["model"], "cost": _cost(r), "share": (_cost(r) / cost) if cost else 0.0} for r in models],
        "budgets": [{"scope": b["scope"], "period": b["period"], "state": b["state"], "pct": max(p for p in (b["pct_usd"], b["pct_tokens"]) if p is not None) if (b["pct_usd"] is not None or b["pct_tokens"] is not None) else 0.0,
                     "spent_usd": b["spent_usd"], "limit_usd": b["limit_usd"], "resets": b["resets_at_text"]} for b in bud],
        "flags": flags,
    }


def _usd(v: float) -> str:
    return f"${v:,.2f}" if abs(v) >= 0.10 or v == 0 else f"${v:,.4f}"


def _ts(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def to_text(d: dict[str, Any], *, include_projects: bool = True) -> str:
    chg = "" if d["change_pct"] is None else f"  ({d['change_pct']:+.0%} on the previous {d['label']}: {_usd(d['previous_cost'])})"
    if d["change_pct"] is None and d["cost"] > 0:
        chg = f"  (nothing recorded in the previous {d['label']})"
    lines = [f"ModelSpend digest: the last {d['label']} ({_ts(d['start'])} to {_ts(d['end'])})", "",
             f"Spend    {_usd(d['cost'])}{'' if d['complete'] else ' (partial: some calls have no price)'}{chg}", f"Requests {d['requests']:,}"
             + (f", {d['failed_requests']:,} without an answer" if d["failed_requests"] else "")]
    if d["tooling_cost"]:
        lines.append(f"Of that, ModelSpend's own shadow and eval runs: {_usd(d['tooling_cost'])}")
    if d["requests"] == 0:
        lines.append("\nNothing went through ModelSpend in this period.")
    if include_projects and d["projects"]:
        lines += ["", "Biggest changes by project"] + [f"  {m['name']:<24}{_usd(m['cost']):>10}  ({'new' if m['new'] else ('+' if m['delta'] > 0 else '-') + _usd(abs(m['delta']))})" for m in d["projects"]]
    elif not include_projects:
        lines += ["", "Project names left out (add --include-projects to include them)."]
    if d["models"] and d["cost"]:
        lines += ["", "By model"] + [f"  {m['name']:<36}{_usd(m['cost']):>10}  {m['share']:.0%}" for m in d["models"]]
    if d["budgets"]:
        lines += ["", "Budgets near or over their limit"] + [
            f"  {b['scope']} ({b['period']}): {b['state'].upper()} at {b['pct']:.0%}" + (f", {_usd(b['spent_usd'])} of {_usd(b['limit_usd'])}" if b["limit_usd"] else "") + f", resets {b['resets']}" for b in d["budgets"]]
    if d["flags"]:
        lines += ["", "Worth a look"] + [f"  - {f}" for f in d["flags"]]
    lines += ["", "Only calls made through ModelSpend are counted; your provider's invoice is authoritative."]
    return "\n".join(lines)


def payload(fmt: str, text: str, d: dict[str, Any]) -> dict[str, Any]:
    body = "```\n" + text + "\n```"  # a code block keeps the columns aligned in Slack and Discord
    if fmt == "slack":
        return {"text": body}
    if fmt == "discord":
        return {"content": body[:1990]}  # Discord refuses messages over 2,000 characters
    return {"text": text, "digest": d}


def post(url: str, fmt: str, text: str, d: dict[str, Any], *, post_fn: Callable[..., Any] | None = None, timeout: float = 10.0) -> None:
    """Send the digest. Never follows a redirect, and never puts the webhook address in an error (it usually holds a secret)."""
    if fmt not in FORMATS:
        raise DigestError(f"digest format must be one of {', '.join(FORMATS)}")
    url = validate_webhook(url)
    try:
        if post_fn is None:
            import httpx
            r = httpx.post(url, json=payload(fmt, text, d), timeout=timeout, follow_redirects=False)
        else:
            r = post_fn(url, json=payload(fmt, text, d), timeout=timeout)
    except Exception as e:  # noqa: BLE001
        raise DigestError(f"could not reach the webhook ({type(e).__name__})") from None
    if not 200 <= r.status_code < 300:
        raise DigestError(f"the webhook answered HTTP {r.status_code}" + (" (a redirect: use the final address)" if 300 <= r.status_code < 400 else ""))


def to_json(d: dict[str, Any]) -> str:
    return json.dumps(d, indent=2)
