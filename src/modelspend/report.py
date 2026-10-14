"""Cost and token reports per project, agent, model, ... built from the usage store (metadata only; no prompts).

Everything is computed from the `attempts` table (one row per provider call, including retries, failovers, escalations,
classifier and judge calls), joined to `requests` for the tags. Grouping dimensions come from a fixed whitelist: user text is
never interpolated into SQL.

Definitions
- tokens: input (uncached) + cache read + cache write + output. Reasoning tokens are a memo field already inside output.
- cost: sum of the costs ModelSpend computed *when each call happened* (price in effect then). Calls on models without a price
  contribute tokens but no cost; they are counted in `unpriced_attempts` and the group is flagged `cost_complete: false`.
- overhead: attempts that did not produce the final answer (failed calls, retries, superseded answers, classifier and judge calls).
"""
from __future__ import annotations

import csv
import io
import re
import time
from datetime import datetime, timezone
from typing import Any

from .store import Store
from .types import _LABEL_KEY

UNTAGGED = "(untagged)"
NO_RUN = "(none)"

# dimension -> SQL expression (fixed strings; `label.<key>` is handled separately with a bound parameter)
DIMENSIONS: dict[str, str] = {
    "project": f"coalesce(r.project, '{UNTAGGED}')",
    "agent": f"coalesce(r.agent, '{UNTAGGED}')",
    "run": f"coalesce(r.run_id, '{NO_RUN}')",
    "provider": "a.provider",
    "model": "a.provider || '/' || a.model",
    "task": "r.task",
    "profile": "r.profile",
    "day": "strftime('%Y-%m-%d', r.ts, 'unixepoch')",
    "purpose": "a.purpose",
}

# an attempt is "final" when it is the last generation attempt that returned an answer for its request
_FINAL = ("a.idx = (select max(x.idx) from attempts x where x.request_id = a.request_id "
          "and x.kind = 'generation' and x.outcome in ('ok', 'refused'))")
_TOK = "(a.input_tokens + a.cache_read + a.cache_write + a.output_tokens)"
METRICS = f"""
  count(distinct r.id) as requests,
  count(*) as attempts,
  coalesce(sum(a.input_tokens), 0) as input_tokens,
  coalesce(sum(a.cache_read), 0) as cache_read_tokens,
  coalesce(sum(a.cache_write), 0) as cache_write_tokens,
  coalesce(sum(a.output_tokens), 0) as output_tokens,
  coalesce(sum(a.reasoning_tokens), 0) as reasoning_tokens,
  coalesce(sum({_TOK}), 0) as total_tokens,
  sum(a.cost) as cost,
  sum(case when a.cost is null then 1 else 0 end) as unpriced_attempts,
  sum(case when a.cost_basis = 'uncertain_upper_estimate' then 1 else 0 end) as uncertain_attempts,
  coalesce(sum(case when {_FINAL} then 0 else 1 end), 0) as overhead_attempts,
  coalesce(sum(case when {_FINAL} then 0 else {_TOK} end), 0) as overhead_tokens,
  sum(case when {_FINAL} then null else a.cost end) as overhead_cost
"""
FILTERS = ("project", "agent", "run", "provider", "model", "task")
_REL = re.compile(r"^(\d+)([mhdw])$")
_UNIT = {"m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}


class ReportError(ValueError):
    pass


def parse_since(text: str | None, now: float | None = None) -> float | None:
    """'7d', '24h', '90m', '2w', an ISO date (UTC midnight) or 'all'/None -> epoch seconds (None = no lower bound)."""
    if text is None or text.strip().lower() in ("", "all"):
        return None
    t = text.strip().lower()
    m = _REL.match(t)
    if m:
        return (time.time() if now is None else now) - int(m.group(1)) * _UNIT[m.group(2)]
    try:
        return datetime.fromisoformat(text.strip()).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        raise ReportError(f"cannot parse --since {text!r}: use e.g. 7d, 24h, 2w or a date like 2026-10-01") from None


def _dim_sql(dim: str) -> tuple[str, list[Any]]:
    if dim in DIMENSIONS:
        return DIMENSIONS[dim], []
    if dim.startswith("label."):
        key = dim[len("label."):]
        if not _LABEL_KEY.match(key):
            raise ReportError(f"invalid label name in {dim!r}")
        return "coalesce(json_extract(r.labels, ?), '(none)')", [f"$.{key}"]
    raise ReportError(f"unknown dimension {dim!r}; choose from {', '.join(DIMENSIONS)} or label.<name>")


def _where(filters: dict[str, str], since: float | None, until: float | None) -> tuple[str, list[Any]]:
    clauses, params = ["a.outcome != 'skipped_budget'"], []
    for k, v in filters.items():
        if k not in FILTERS:
            raise ReportError(f"unknown filter {k!r}; choose from {', '.join(FILTERS)}")
        sql, p = _dim_sql(k)
        if v in (UNTAGGED, NO_RUN):
            col = {"project": "r.project", "agent": "r.agent", "run": "r.run_id"}.get(k)
            if col is None:
                raise ReportError(f"{v!r} is only valid for project, agent or run")
            clauses.append(f"{col} is null")
        else:
            clauses.append(f"{sql} = ?")
            params += p + [v.lower() if k in ("project", "agent") else v]
    if since is not None:
        clauses.append("r.ts >= ?")
        params.append(since)
    if until is not None:
        clauses.append("r.ts < ?")
        params.append(until)
    return " and ".join(clauses), params


def _finish(row: dict[str, Any]) -> dict[str, Any]:
    if not row["attempts"]:  # nothing recorded: the sums are SQL NULLs, but the honest answer is zero
        row["cost"] = 0.0
    if not row["overhead_attempts"]:  # no overhead calls at all is $0, not "unknown" (unknown means overhead calls without a price)
        row["overhead_cost"] = 0.0
    row["cost_complete"] = not row["unpriced_attempts"]
    t = row["total_tokens"] or 0
    row["overhead_token_share"] = (row["overhead_tokens"] / t) if t else 0.0
    inp = row["input_tokens"] + row["cache_read_tokens"] + row["cache_write_tokens"]
    row["cache_hit_rate"] = (row["cache_read_tokens"] / inp) if inp else 0.0
    return row


def build_report(store: Store, by: list[str] | None = None, *, filters: dict[str, str] | None = None,
                 since: float | None = None, until: float | None = None, limit: int = 200) -> dict[str, Any]:
    by = list(by or [])
    if len(by) > 4:
        raise ReportError("group by at most 4 dimensions")
    if len(set(by)) != len(by):
        raise ReportError("duplicate dimension in --by")
    where, wparams = _where(filters or {}, since, until)
    frm = f"from attempts a join requests r on r.id = a.request_id where {where}"
    totals = _finish(dict(store.db.execute(f"select {METRICS} {frm}", wparams).fetchone()))
    rows: list[dict[str, Any]] = []
    truncated = False
    if by:
        sels, gparams = [], []
        for i, d in enumerate(by):
            sql, p = _dim_sql(d)
            sels.append(f"{sql} as d{i}")
            gparams += p
        group = ", ".join(f"d{i}" for i in range(len(by)))
        q = (f"select {', '.join(sels)}, {METRICS} {frm} group by {group} "
             f"order by total_tokens desc, {group} limit ?")
        for r in store.db.execute(q, gparams + wparams + [limit + 1]):
            d = dict(r)
            row = {name: d.pop(f"d{i}") for i, name in enumerate(by)}
            row.update(d)
            rows.append(_finish(row))
        if len(rows) > limit:
            rows, truncated = rows[:limit], True
    notes = ["Only calls made through ModelSpend are counted.",
             "Cost uses the prices in effect when each call ran; tokens are provider-reported.",
             "Your provider's invoice is authoritative."]
    if totals["unpriced_attempts"]:
        notes.append(f"{totals['unpriced_attempts']} call(s) used models without a price: their tokens are counted, their cost is not (shown as partial).")
    return {"group_by": by, "filters": filters or {}, "since": since, "until": until, "rows": rows, "totals": totals,
            "truncated": truncated, "notes": notes}


# ---- formatting -----------------------------------------------------------------------------------------------------
COLUMNS = ["requests", "total_tokens", "input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens",
           "reasoning_tokens", "cost", "cost_complete", "overhead_tokens", "overhead_cost", "unpriced_attempts"]


def to_csv(rep: dict[str, Any]) -> str:
    by = rep["group_by"] or ["scope"]
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(by + COLUMNS)
    for r in rep["rows"]:
        w.writerow([_csv_safe(r[d]) for d in by] + [r[c] for c in COLUMNS])
    t = rep["totals"]
    w.writerow(["TOTAL"] + [""] * (len(by) - 1) + [t[c] for c in COLUMNS])
    return out.getvalue()


def _csv_safe(v: Any) -> Any:
    """Neutralize spreadsheet formula injection in text cells (tags are restricted, but model ids/labels come from outside)."""
    s = str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s


def _money(v: float | None, complete: bool) -> str:
    if v is None:
        return "unknown"
    return f"${v:,.6f}" + ("" if complete else " +")


def _cells(r: dict[str, Any]) -> list[str]:
    return [f"{r['requests']:,}", f"{r['total_tokens']:,}", f"{r['input_tokens']:,}", f"{r['cache_read_tokens']:,}",
            f"{r['output_tokens']:,}", f"{r['reasoning_tokens']:,}", _money(r["cost"], r["cost_complete"]),
            f"{r['overhead_token_share']:.0%}"]


def to_table(rep: dict[str, Any]) -> str:
    by = rep["group_by"]
    dims = by or ["scope"]
    rows = [dims + ["reqs", "tokens", "in", "cached", "out", "(reasoning)", "cost", "overhead"]]
    for r in rep["rows"]:
        rows.append([str(r[d]) for d in by] + _cells(r))
    if not by:
        rows.append(["all"] + _cells(rep["totals"]))
    else:
        rows.append(["TOTAL"] + [""] * (len(by) - 1) + _cells(rep["totals"]))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    lines = []
    for n, r in enumerate(rows):
        lines.append("  ".join(c.ljust(widths[i]) if i < len(dims) else c.rjust(widths[i]) for i, c in enumerate(r)))
        if n == 0 or (by and n == len(rows) - 2):
            lines.append("  ".join("-" * w for w in widths))
    out = "\n".join(lines) + "\n\n" + "\n".join(f"* {n}" for n in rep["notes"])
    if not rep["totals"]["attempts"]:
        out += "\n* No usage recorded in this window. Tag calls with --project / --agent and run them through ModelSpend."
    if rep["truncated"]:
        out += "\n* Output truncated: raise --top to see more groups."
    if any(not r["cost_complete"] for r in rep["rows"]) or not rep["totals"]["cost_complete"]:
        out += "\n* A trailing '+' means the cost is partial (some calls had no price)."
    return out
