"""Reconcile ModelSpend's own usage records with usage the provider reports (imported from a file you export yourself).

What this can and cannot tell you
- ModelSpend only sees calls made through it. A provider's numbers cover everything on that key, project or organization. So
  "provider higher" is normal unless the export is for a key used only by ModelSpend (`exclusive`).
- "ModelSpend higher" is the finding that matters: ModelSpend recorded more than the provider billed. Likely causes are token
  estimates, a different key/project/organization than the one exported, or provider data that has not caught up yet.
- Days are UTC. Calls close to midnight can land on different days in the two systems; compare ranges, not single days, first.
- Only the days the export covers are compared, per provider.
Nothing here is sent anywhere; the imported file is reduced to numbers (no prompts or text are ever read or stored).
"""
from __future__ import annotations

import csv
import io
import json
import re
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import registry
from .store import Store

MAX_BYTES = 50 * 1024 * 1024
MAX_ROWS = 500_000
# Providers differ on whether the input-token number already contains cached tokens (from the provider registry, so DeepSeek and your own are covered).

_ALIASES: dict[str, tuple[str, ...]] = {
    "day": ("day", "date", "usage_date", "start_date", "start_time", "starting_at", "bucket_start", "timestamp", "time"),
    "model": ("model", "model_id", "model_name", "snapshot_id", "model_version"),
    "provider": ("provider", "vendor"),
    "input_tokens": ("input_tokens", "uncached_input_tokens", "prompt_tokens", "n_context_tokens_total"),
    "output_tokens": ("output_tokens", "completion_tokens", "generated_tokens", "n_generated_tokens_total"),
    "cache_read": ("cache_read", "cache_read_tokens", "cache_read_input_tokens", "input_cached_tokens", "cached_tokens", "cached_input_tokens"),
    "cache_write": ("cache_write", "cache_write_tokens", "cache_creation_input_tokens", "cache_creation_tokens", "input_cache_write_tokens"),
    "requests": ("requests", "num_model_requests", "n_requests", "request_count"),
    "cost": ("cost_usd", "cost", "amount_usd", "amount", "spend", "spend_usd", "total_cost"),
}
NO_USAGE = "(no usage)"  # placeholder row for a day the provider reports as empty: it still counts as covered
_DATE_SUFFIX = re.compile(r"-(\d{4}-\d{2}-\d{2}|\d{8})$")


class ReconcileError(ValueError):
    pass


def canon_model(model: str) -> str:
    """gpt-4.1-nano-2025-04-14 and gpt-4.1-nano are the same model for accounting purposes."""
    return _DATE_SUFFIX.sub("", model.strip())


def _num(v: Any, as_int: bool = True) -> float | int | None:
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        raise ReconcileError(f"unexpected boolean value {v!r}")
    if isinstance(v, dict):
        return sum(_num(x, as_int) or 0 for x in v.values())
    try:
        x = float(str(v).replace(",", "").replace("$", "").strip()) if not isinstance(v, (int, float)) else float(v)
    except ValueError:
        raise ReconcileError(f"not a number: {str(v)[:40]!r}") from None
    if x != x or x in (float("inf"), float("-inf")) or x < 0:
        raise ReconcileError(f"invalid number: {x!r}")
    return int(round(x)) if as_int else x


def _day(v: Any) -> str:
    if isinstance(v, (int, float)) or (isinstance(v, str) and re.fullmatch(r"\d{9,13}(\.\d+)?", v.strip())):
        t = float(v)
        if t > 1e11:  # milliseconds
            t /= 1000
        return datetime.fromtimestamp(t, timezone.utc).date().isoformat()
    s = str(v).strip()
    try:
        return date.fromisoformat(s[:10]).isoformat() if len(s) >= 10 and s[4] == "-" else _parse_other(s)
    except ValueError:
        raise ReconcileError(f"cannot read a date from {s[:40]!r} (use YYYY-MM-DD, an ISO timestamp or epoch seconds)") from None


def _parse_other(s: str) -> str:
    return datetime.strptime(s, "%m/%d/%Y").date().isoformat()


def _flatten_json(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, dict):
        data = data.get("data", data.get("results", [data]))
    if not isinstance(data, list):
        raise ReconcileError("JSON must be a list of rows, or an object with a 'data' list")
    rows: list[dict[str, Any]] = []
    for b in data:
        if not isinstance(b, dict):
            raise ReconcileError("JSON rows must be objects")
        inner = b.get("results")
        if isinstance(inner, list):  # time buckets: {start_time|starting_at, results: [...]}
            for r in inner:
                if isinstance(r, dict):
                    rows.append({**{k: v for k, v in b.items() if k != "results"}, **r})
        else:
            rows.append(b)
    return rows


def _read_rows(text: str, name: str) -> list[dict[str, Any]]:
    t = text.lstrip("﻿").strip()
    if not t:
        raise ReconcileError("the file is empty")
    if t[0] in "[{":
        try:
            return _flatten_json(json.loads(t))
        except json.JSONDecodeError:
            pass  # maybe JSON lines
        try:
            return _flatten_json([json.loads(ln) for ln in t.splitlines() if ln.strip()])
        except json.JSONDecodeError as e:
            raise ReconcileError(f"{name}: not valid JSON or JSON lines ({e.msg} at line {e.lineno})") from None
    rd = csv.DictReader(io.StringIO(t))
    if not rd.fieldnames:
        raise ReconcileError("no header row found")
    return [dict(r) for r in rd]


def parse_usage(text: str, provider: str | None, name: str = "file", mapping: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Rows of provider usage -> [{provider, model, day, input_tokens, output_tokens, cache_read, cache_write, requests, cost}], summed per (provider, model, day)."""
    if provider is not None and provider not in registry.all_providers():
        raise ReconcileError(f"unknown provider {provider!r}; choose from {', '.join(registry.all_providers())}")
    raw = _read_rows(text, name)
    if len(raw) > MAX_ROWS:
        raise ReconcileError(f"too many rows ({len(raw):,}; the limit is {MAX_ROWS:,})")
    if not raw:
        raise ReconcileError("no data rows found")
    mapping = mapping or {}
    cols = {k.strip().lower(): k for r in raw[:50] for k in r}  # case-insensitive lookup of the file's own headers

    def pick(field: str) -> str | None:
        if field in mapping:
            want = mapping[field].strip().lower()
            if want not in cols:
                raise ReconcileError(f"--map {field}={mapping[field]}: no such column; the file has {', '.join(sorted(cols))}")
            return cols[want]
        for a in _ALIASES[field]:
            if a in cols:
                return cols[a]
        return None

    c = {f: pick(f) for f in _ALIASES}
    for need in ("day", "model"):
        if not c[need]:
            raise ReconcileError(f"no '{need}' column found (looked for {', '.join(_ALIASES[need])}); map it with --map {need}=<column>")
    if not (c["input_tokens"] or c["output_tokens"] or c["cost"]):
        raise ReconcileError("no token or cost columns found; map them with --map input_tokens=<column> / output_tokens / cost")
    if provider is None and not c["provider"]:
        raise ReconcileError("say which provider this is: --provider openai|anthropic|google|xai (or include a 'provider' column)")
    agg: dict[tuple[str, str, str], dict[str, Any]] = {}
    for n, r in enumerate(raw, 1):
        try:
            p = (str(r.get(c["provider"]) or "").strip().lower() if c["provider"] else "") or provider
            if p not in registry.all_providers():
                raise ReconcileError(f"unknown provider {p!r}")
            model = str(r.get(c["model"]) or "").strip()
            row = {f: _num(r.get(c[f]), f != "cost") if c[f] else None
                   for f in ("input_tokens", "output_tokens", "cache_read", "cache_write", "requests", "cost")}
            if not model and all(v is None for v in row.values()):
                model = NO_USAGE  # a time bucket with no usage at all (providers write these for quiet days)
            elif not model or len(model) > 200:
                raise ReconcileError("missing or oversized model name")
            key = (p, canon_model(model) if model != NO_USAGE else model, _day(r.get(c["day"])))
        except ReconcileError as e:
            raise ReconcileError(f"row {n}: {e}") from None
        a = agg.setdefault(key, {"provider": key[0], "model": key[1], "day": key[2], "input_tokens": 0, "output_tokens": 0,
                                 "cache_read": 0, "cache_write": 0, "requests": 0, "cost": None})
        for f in ("input_tokens", "output_tokens", "cache_read", "cache_write", "requests"):
            a[f] += row[f] or 0
        if row["cost"] is not None:
            a["cost"] = (a["cost"] or 0.0) + row["cost"]
    return sorted(agg.values(), key=lambda x: (x["provider"], x["model"], x["day"]))


def load_rows(path: Path, provider: str | None, mapping: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Read and parse a usage export file (size-capped, UTF-8 only). Writes nothing."""
    path = Path(path)
    if not path.is_file():
        raise ReconcileError(f"no such file: {path}")
    if path.stat().st_size > MAX_BYTES:
        raise ReconcileError(f"file is larger than {MAX_BYTES // 1024 // 1024} MB")
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ReconcileError("the file is not UTF-8 text") from None
    return parse_usage(text, provider, path.name, mapping)


def import_file(store: Store, path: Path, provider: str | None, source: str | None = None, mapping: dict[str, str] | None = None,
                now: float | None = None) -> dict[str, Any]:
    path = Path(path)
    src = (source or path.name).strip()[:80]
    if not re.fullmatch(r"[A-Za-z0-9._ -]+", src):
        raise ReconcileError("--source may only contain letters, digits, space, '.', '_' and '-'")
    rows = load_rows(path, provider, mapping)
    ts = time.time() if now is None else now
    with store.lock:
        # warn when another source already covers some of the same provider-days: summing two exports of the same period double counts
        pairs = {(r["provider"], r["day"]) for r in rows}
        overlap = sorted(pd for pd in pairs
                         if store.db.execute("select 1 from provider_usage where source != ? and provider = ? and day = ? limit 1",
                                             (src, *pd)).fetchone())
        store.db.execute("delete from provider_usage where source = ?", (src,))  # re-importing a source replaces it
        store.db.executemany(
            "insert into provider_usage(source, provider, model, day, input_tokens, output_tokens, cache_read, cache_write, requests, cost, imported_ts) "
            "values (?,?,?,?,?,?,?,?,?,?,?)",
            [(src, r["provider"], r["model"], r["day"], r["input_tokens"], r["output_tokens"], r["cache_read"], r["cache_write"],
              r["requests"], r["cost"], ts) for r in rows])
        store.db.commit()
    days = sorted({r["day"] for r in rows})
    empty = sorted({r["day"] for r in rows if r["model"] == NO_USAGE})
    return {"source": src, "rows": len(rows) - len(empty), "empty_days": empty, "providers": sorted({r["provider"] for r in rows}), "first_day": days[0], "last_day": days[-1],
            "models": len({(r["provider"], r["model"]) for r in rows if r["model"] != NO_USAGE}), "has_cost": any(r["cost"] is not None for r in rows),
            "overlap": [{"provider": p, "day": d} for p, d in overlap]}


def list_sources(store: Store) -> list[dict[str, Any]]:
    q = ("select source, provider, min(day) first_day, max(day) last_day, count(*) rows, sum(input_tokens + output_tokens + cache_read + cache_write) tokens, "
         "max(imported_ts) imported_ts from provider_usage group by source, provider order by source, provider")
    return [dict(r) for r in store.db.execute(q)]


def remove_source(store: Store, source: str) -> int:
    with store.lock:
        n = store.db.execute("delete from provider_usage where source = ?", (source,)).rowcount
        store.db.commit()
    return n


# ---- comparison -----------------------------------------------------------------------------------------------------
def _allowed(provider_value: float, tol: float, floor: float) -> float:
    return max(tol * provider_value, floor)


def judge(ms: float | None, prov: float | None, tol: float, floor: float, exclusive: bool) -> dict[str, Any]:
    """One number compared with the provider's. `attention` is True when this needs a human look."""
    if prov is None and ms is None:
        return {"status": "n/a", "attention": False, "diff": None, "note": ""}
    if prov is None and not ms:
        return {"status": "n/a", "attention": False, "diff": None,
                "note": "ModelSpend recorded calls here that reported no tokens (failed before the provider billed anything): nothing to compare."}
    if prov is None:
        return {"status": "modelspend_only", "attention": True, "diff": None,
                "note": "ModelSpend recorded this, the export has nothing for it: other key/project/org, or the export is not complete yet."}
    if not ms:
        return {"status": "provider_only", "attention": exclusive, "diff": -prov,
                "note": "Provider reports usage ModelSpend did not make." + ("" if exclusive else " Normal if other tools use this key.")}
    diff = ms - prov
    ok = abs(diff) <= _allowed(prov, tol, floor)
    if ok:
        return {"status": "match", "attention": False, "diff": diff, "note": ""}
    if diff > 0:
        return {"status": "modelspend_higher", "attention": True, "diff": diff,
                "note": "ModelSpend counted more than the provider billed: estimates, a different key/project/org, or provider data lag."}
    return {"status": "provider_higher", "attention": exclusive, "diff": diff,
            "note": "Provider reports more than ModelSpend." + ("" if exclusive else " Normal if other tools or keys use this account.")}


def compare(store: Store, *, by_day: bool = False, tolerance: float = 0.02, exclusive: bool = False, source: str | None = None,
            provider: str | None = None, cached_included: dict[str, bool] | None = None) -> dict[str, Any]:
    if not 0 <= tolerance <= 1:
        raise ReconcileError("tolerance must be between 0 and 1 (0.02 = 2%)")
    incl = {**{n: s.cached_included for n, s in registry.all_specs().items()}, **(cached_included or {})}
    where, params = ["1=1"], []
    if source:
        where.append("source = ?")
        params.append(source)
    if provider:
        where.append("provider = ?")
        params.append(provider)
    imported = [dict(r) for r in store.db.execute(f"select * from provider_usage where {' and '.join(where)}", params)]
    if not imported:
        raise ReconcileError("no imported provider usage; run: modelspend reconcile import <file> --provider <name>")
    span: dict[str, tuple[str, str]] = {}
    for r in imported:
        lo, hi = span.get(r["provider"], (r["day"], r["day"]))
        span[r["provider"]] = (min(lo, r["day"]), max(hi, r["day"]))

    def key(p: str, model: str, day: str) -> tuple:
        return (p, model, day) if by_day else (p, model)

    prov: dict[tuple, dict[str, Any]] = {}
    for r in imported:
        if r["model"] == NO_USAGE:
            continue  # only widens the covered days
        k = key(r["provider"], r["model"], r["day"])
        a = prov.setdefault(k, {"input": 0, "output": 0, "cost": None, "requests": 0})
        side = r["input_tokens"] + (r["cache_write"] + r["cache_read"] if not incl[r["provider"]] else 0)
        a["input"] += side
        a["output"] += r["output_tokens"]
        a["requests"] += r["requests"]
        if r["cost"] is not None:
            a["cost"] = (a["cost"] or 0.0) + r["cost"]

    ms: dict[tuple, dict[str, Any]] = {}
    for p, (lo, hi) in span.items():
        start = datetime.fromisoformat(lo).replace(tzinfo=timezone.utc).timestamp()
        end = (datetime.fromisoformat(hi).replace(tzinfo=timezone.utc) + timedelta(days=1)).timestamp()
        q = ("select a.model, strftime('%Y-%m-%d', r.ts, 'unixepoch') day, a.input_tokens i, a.cache_read cr, a.cache_write cw, a.output_tokens o, a.cost c, a.outcome oc, a.cost_basis cb "
             "from attempts a join requests r on r.id = a.request_id "
             "where a.provider = ? and a.outcome != 'skipped_budget' and r.ts >= ? and r.ts < ?")
        for r in store.db.execute(q, (p, start, end)):
            k = key(p, canon_model(r["model"]), r["day"])
            a = ms.setdefault(k, {"input": 0, "output": 0, "cost": 0.0, "unpriced": 0, "calls": 0})
            a["input"] += (r["i"] or 0) + (r["cr"] or 0) + (r["cw"] or 0)
            a["output"] += r["o"] or 0
            if r["oc"] in ("ok", "refused") or r["cb"] == "uncertain_upper_estimate":  # failed calls that never reached the provider's meter are not requests
                a["calls"] += 1
            if r["c"] is None:
                a["unpriced"] += 1
            else:
                a["cost"] += r["c"]

    rows: list[dict[str, Any]] = []
    for k in sorted(set(prov) | set(ms)):
        pv, mv = prov.get(k), ms.get(k)
        pt = (pv["input"] + pv["output"]) if pv else None
        mt = (mv["input"] + mv["output"]) if mv else None
        tok = judge(mt, pt, tolerance, 100, exclusive)
        cost_p = pv["cost"] if pv else None
        partial = bool(mv and mv["unpriced"])
        cost = judge(None if partial else (mv["cost"] if mv else None), cost_p, tolerance, 0.0005, exclusive) if cost_p is not None \
            else {"status": "n/a", "attention": False, "diff": None, "note": "The export has no cost column."}
        if partial and cost_p is not None:
            cost = {"status": "n/a", "attention": False, "diff": None, "note": "Some ModelSpend calls have no price, so cost is partial."}
        row = {"provider": k[0], "model": k[1], **({"day": k[2]} if by_day else {}),
               "provider_tokens": pt, "modelspend_tokens": mt, "tokens": tok,
               "provider_input": pv["input"] if pv else None, "provider_output": pv["output"] if pv else None,
               "modelspend_input": mv["input"] if mv else None, "modelspend_output": mv["output"] if mv else None,
               "provider_cost": cost_p, "modelspend_cost": None if (not mv or partial) else mv["cost"], "cost": cost,
               "provider_requests": pv["requests"] if pv else None, "modelspend_calls": mv["calls"] if mv else None}
        row["attention"] = tok["attention"] or cost["attention"]
        rows.append(row)
    summary = {"rows": len(rows), "match": sum(1 for r in rows if r["tokens"]["status"] == "match"),
               "attention": sum(1 for r in rows if r["attention"]),
               "provider_tokens": sum(r["provider_tokens"] or 0 for r in rows), "modelspend_tokens": sum(r["modelspend_tokens"] or 0 for r in rows),
               "provider_cost": sum(r["provider_cost"] or 0 for r in rows) if any(r["provider_cost"] is not None for r in rows) else None,
               "modelspend_cost": sum(r["modelspend_cost"] or 0 for r in rows) if any(r["modelspend_cost"] is not None for r in rows) else None}
    summary["ok"] = summary["attention"] == 0
    summary["other_usage_rows"] = sum(1 for r in rows if r["tokens"]["status"] in ("provider_higher", "provider_only") and not r["attention"])
    notes = ["Only calls made through ModelSpend are counted; the provider's numbers cover everything on the exported key/project/organization.",
             "Compared only for the days the export covers, per provider. Days are UTC.",
             "The provider's invoice is authoritative. This check shows where ModelSpend's numbers and the provider's disagree; it does not prove either right."]
    if not exclusive:
        notes.append("'provider higher' is not flagged unless you pass --exclusive (this key/project is used only by ModelSpend).")
    return {"by_day": by_day, "tolerance": tolerance, "exclusive": exclusive, "coverage": {p: {"first_day": a, "last_day": b} for p, (a, b) in span.items()},
            "rows": rows, "summary": summary, "notes": notes}


# ---- formatting -----------------------------------------------------------------------------------------------------
def _n(v: int | None) -> str:
    return "-" if v is None else f"{v:,}"


def _c(v: float | None) -> str:
    return "-" if v is None else f"${v:,.4f}"


def _d(v: float | None, money: bool = False) -> str:
    if v is None:
        return "-"
    return f"{v:+,.4f}" if money else f"{v:+,.0f}"


def to_table(rep: dict[str, Any]) -> str:
    head = ["provider", "model"] + (["day"] if rep["by_day"] else []) + ["provider tok", "ModelSpend tok", "diff", "tokens", "calls prov/MS",
                                                                     "provider $", "ModelSpend $", "diff $", "cost"]
    rows = [head]
    for r in rep["rows"]:
        rows.append([r["provider"], r["model"]] + ([r["day"]] if rep["by_day"] else []) +
                    [_n(r["provider_tokens"]), _n(r["modelspend_tokens"]), _d(r["tokens"]["diff"]), r["tokens"]["status"] + ("  <-- check" if r["attention"] else ""),
                     f"{_n(r['provider_requests'])}/{_n(r['modelspend_calls'])}",
                     _c(r["provider_cost"]), _c(r["modelspend_cost"]), _d(r["cost"]["diff"], True), r["cost"]["status"]])
    left = 2 + (1 if rep["by_day"] else 0)
    widths = [max(len(x[i]) for x in rows) for i in range(len(head))]
    lines = []
    for n, x in enumerate(rows):
        lines.append("  ".join(c.ljust(widths[i]) if i < left else c.rjust(widths[i]) for i, c in enumerate(x)))
        if n == 0:
            lines.append("  ".join("-" * w for w in widths))
    s = rep["summary"]
    if not s["ok"]:
        verdict = f"{s['attention']} row(s) need a look"
    elif s["other_usage_rows"]:
        verdict = (f"nothing needs a look: ModelSpend is never above the provider; in {s['other_usage_rows']} row(s) the provider reports more, "
                   "which is expected if anything else uses this key (not flagged; use --exclusive if it does not)")
    else:
        verdict = "nothing needs a look"
    cov = "; ".join(f"{p}: {c['first_day']}..{c['last_day']}" for p, c in rep["coverage"].items())
    out = "\n".join(lines) + f"\n\nCompared days (UTC): {cov}\nResult: {s['match']} of {s['rows']} rows match within {rep['tolerance']:.0%}; {verdict}.\n"
    seen: set[str] = set()
    for r in rep["rows"]:
        for part in (r["tokens"], r["cost"]):
            if part["note"] and part["note"] not in seen and part["attention"]:
                seen.add(part["note"])
                out += f"* {part['note']}\n"
    return out + "\n" + "\n".join(f"* {n}" for n in rep["notes"])


def to_csv(rep: dict[str, Any]) -> str:
    from .report import _csv_safe
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    cols = ["provider", "model"] + (["day"] if rep["by_day"] else [])
    w.writerow(cols + ["provider_tokens", "modelspend_tokens", "token_diff", "token_status", "provider_cost", "modelspend_cost", "cost_diff", "cost_status", "provider_requests", "modelspend_calls", "attention"])
    for r in rep["rows"]:
        w.writerow([_csv_safe(r[c]) for c in cols] + [r["provider_tokens"], r["modelspend_tokens"], r["tokens"]["diff"], r["tokens"]["status"],
                                                       r["provider_cost"], r["modelspend_cost"], r["cost"]["diff"], r["cost"]["status"], r["provider_requests"], r["modelspend_calls"], r["attention"]])
    return out.getvalue()
