"""Find requests that were sent more than once, word for word, and what the repeats cost.

A repeated identical request is billed in full each time (a provider's prompt cache discounts the repeated *input*, never the answer you pay for again),
so it is pure waste when the answer could not have changed: a client that resends after a timeout, a retry loop, an agent asking the same question every
step, a job that re-processes the same input. Sometimes it is on purpose (sampling at a temperature above zero, evals, tests), so this reports and does not
judge.

To do that, ModelSpend keeps per request a keyed hash (HMAC-SHA256 with a random key that lives only in your database, truncated) of the messages, tools,
response schema and temperature, never the text. Requests shorter than 200 characters are not tracked at all: a short prompt could be guessed and
confirmed against its hash, and repeating one costs little. Turn tracking off with `"track_duplicates": false` in settings.json (or
MODELSPEND_TRACK_DUPLICATES=0); `modelspend duplicates --forget` erases it. Requests ModelSpend makes for its own eval and shadow runs are left out.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import statistics
import time
from typing import Any

from .prefixes import _ser
from .savings import COUNTED
from .shadow import TOOLING_PROJECTS
from .store import Store

log = logging.getLogger("modelspend")
MIN_CHARS = 200
BURST_S = 60.0


def request_hash(req: Any, salt: bytes) -> str | None:
    text = _ser(req.messages, req.response_schema, req.tools) + "\x1e" + repr(req.temperature)
    if len(text) < MIN_CHARS:
        return None
    return hmac.new(salt, b"dup:" + text.encode("utf-8", "replace"), hashlib.sha256).hexdigest()[:16]


def track(store: Any, enabled: bool, req: Any, request_id: str) -> None:
    """Called after a request is recorded. Must never affect the request: any failure is swallowed."""
    if not enabled or store is None:
        return
    try:
        h = request_hash(req, store.salt())
        if h:
            store.record_duplicate(request_id, h)
    except Exception:  # noqa: BLE001
        log.debug("duplicate tracking skipped", exc_info=True)


def _events(store: Store, since: float | None, project: str | None):
    marks = ",".join("?" for _ in TOOLING_PROJECTS)
    where, params = [f"coalesce(r.project, '') not in ({marks})", f"r.status in ({','.join('?' for _ in COUNTED)})"], [*TOOLING_PROJECTS, *COUNTED]
    if since is not None:
        where.append("r.ts >= ?")
        params.append(since)
    if project:
        where.append("r.project = ?")
        params.append(project.lower())
    q = ("select d.h h, r.id rid, r.ts ts, r.project project, r.agent agent, r.provider provider, r.model model, r.actual_cost cost, "
         f"coalesce(r.input_tokens, 0) + coalesce(r.output_tokens, 0) tokens from dup_stats d join requests r on r.id = d.request_id where {' and '.join(where)} order by d.h, r.ts, r.id")
    return store.db.execute(q, params)


def _gap(s: float) -> str:
    return f"{s:.0f} s" if s < 90 else f"{s / 60:.0f} min" if s < 5400 else f"{s / 3600:.1f} h" if s < 172800 else f"{s / 86400:.1f} days"


def _advice(pattern: str, n: int, median_gap: float) -> str:
    if pattern == "burst":
        return (f"sent {n} times within about a minute of each other: usually a retry loop or a client that resends after a timeout. Check the caller. "
                "Each repeat was billed in full")
    if pattern == "later":
        return f"the same request again, a median of {_gap(median_gap)} later: if the answer cannot have changed, keep the first answer in your app instead of asking again"
    return f"sent {n} times, some in quick succession and some much later: check the caller for a retry loop, and whether the later repeats could reuse the first answer"


def build_duplicates(store: Store, *, since: float | None = None, project: str | None = None, min_requests: int = 2) -> dict[str, Any]:
    groups: dict[str, list[Any]] = {}
    total_cost, total_requests = 0.0, 0
    for r in _events(store, since, project):
        groups.setdefault(r["h"], []).append(r)
        total_cost += float(r["cost"] or 0.0)
        total_requests += 1
    rows = []
    for h, evs in groups.items():
        if len(evs) < max(2, min_requests):
            continue
        reps = evs[1:]
        gaps = [b["ts"] - a["ts"] for a, b in zip(evs, evs[1:])]
        burst = sum(1 for g in gaps if g <= BURST_S)
        pattern = "burst" if burst == len(gaps) else "later" if burst == 0 else "mixed"
        med = statistics.median(gaps)
        known = [float(r["cost"]) for r in reps if r["cost"] is not None]
        rows.append({
            "request": h[:8], "requests": len(evs), "repeats": len(reps), "pattern": pattern, "median_gap_s": med,
            "repeat_cost_usd": sum(known) if known else None, "cost_complete": len(known) == len(reps), "repeat_tokens": sum(r["tokens"] for r in reps),
            "project": evs[0]["project"], "agent": evs[0]["agent"], "model": f"{evs[0]['provider']}/{evs[0]['model']}",
            "first_ts": evs[0]["ts"], "last_ts": evs[-1]["ts"], "advice": _advice(pattern, len(evs), med)})
    rows.sort(key=lambda r: (-(r["repeat_cost_usd"] or 0.0), -r["repeats"], r["request"]))
    wasted = sum(r["repeat_cost_usd"] or 0.0 for r in rows)
    return {
        "since": since, "project": project, "rows": rows,
        "totals": {"distinct_requests": len(rows), "repeat_requests": sum(r["repeats"] for r in rows), "repeat_cost_usd": wasted, "repeat_tokens": sum(r["repeat_tokens"] for r in rows),
                   "complete": all(r["cost_complete"] for r in rows), "requests_considered": total_requests, "cost_considered_usd": total_cost,
                   "share_of_cost": (wasted / total_cost) if total_cost else 0.0},
        "notes": ["Only requests of at least 200 characters sent through ModelSpend since duplicate tracking began are compared, and they are identified by keyed hashes: the text is not kept.",
                  "Two requests count as the same only if their messages, tools, response schema and temperature are identical.",
                  "A repeat can be on purpose (sampling above temperature 0, evals, tests); this lists what happened and does not say it was wrong.",
                  "Cost is the repeats' recorded cost, after the first of each; your provider's invoice is authoritative."],
        "generated": time.time()}


def _usd(v: float | None) -> str:
    return "unknown" if v is None else f"${v:,.2f}" if abs(v) >= 0.10 or v == 0 else f"${v:,.4f}"


def to_table(rep: dict[str, Any], tracking: bool = True) -> str:
    t = rep["totals"]
    if not tracking and not rep["rows"]:
        return "Duplicate tracking is off (track_duplicates is false in settings.json), so there is nothing to analyse."
    if not rep["rows"]:
        return (f"No request was sent more than once, word for word, in this window ({t['requests_considered']:,} request(s) compared). "
                "Only requests of at least 200 characters sent through ModelSpend since duplicate tracking began are compared.")
    head = ["request", "sent", "repeats", "repeat cost", "pattern", "project / agent", "model", "what to do"]
    rows = [head] + [[r["request"], str(r["requests"]), str(r["repeats"]), _usd(r["repeat_cost_usd"]) + ("" if r["cost_complete"] else "+"), r["pattern"],
                      f"{r['project'] or '(untagged)'} / {r['agent'] or '(untagged)'}", r["model"], r["advice"]] for r in rep["rows"]]
    widths = [max(len(x[i]) for x in rows) for i in range(len(head))]
    lines = []
    for n, x in enumerate(rows):
        lines.append("  ".join(c.ljust(widths[i]) if i in (0, 4, 5, 6, 7) else c.rjust(widths[i]) for i, c in enumerate(x)).rstrip())
        if n == 0:
            lines.append("  ".join("-" * w for w in widths))
    tail = (f"\n\n{t['repeat_requests']:,} repeated request(s) across {t['distinct_requests']:,} distinct request(s) cost {_usd(t['repeat_cost_usd'])}"
            + ("" if t["complete"] else " (partial: some repeats have no price)") + f", {t['share_of_cost']:.1%} of the {_usd(t['cost_considered_usd'])} compared.")
    return "\n".join(lines) + tail + "\n\n" + "\n".join(f"* {n}" for n in rep["notes"])
