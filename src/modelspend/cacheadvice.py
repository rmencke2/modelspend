"""Prompt-caching advice from your own traffic: which long prompt starts repeat, whether the provider is caching them, and what the misses cost.

Inputs (all local): the prefix sightings from prefixes.py (keyed hashes and token estimates, never text), the cached tokens each provider reported on your
requests, and the catalog's cached-input price. A repeat counts as cacheable when the same prefix was seen on the same model within `window` seconds
before. The shortest window a major provider documents is about five minutes, so that is the default; `--window` changes it.

What it says it knows, and what it does not:
- OpenAI and DeepSeek cache repeated prefixes automatically; Anthropic caches only where the request carries explicit cache markers, which ModelSpend does not
  send yet; for every other provider the behaviour is "unknown" and the report says to test it with `modelspend verify <provider> --cache`.
- Token counts of prefixes are estimates (exact only with the optional tiktoken extra), and a provider's real cache hit also depends on its own rules and load.
- Savings are a projection from your catalog prices; realized savings use the cached tokens the provider reported.
"""
from __future__ import annotations

import re
import time
from typing import Any

from .catalog import Catalog
from .config import Config
from .savings import COUNTED
from .shadow import TOOLING_PROJECTS
from .store import Store

DEFAULT_WINDOW_S = 300.0
KIND = {"openai": "automatic", "deepseek": "automatic", "anthropic": "explicit"}  # everything else: unknown (not assumed)


def parse_window(text: str) -> float:
    m = re.fullmatch(r"(\d{1,5})\s*([smh])", (text or "").strip().lower())
    if not m:
        raise ValueError("window must look like 90s, 5m or 1h")
    n = int(m.group(1)) * {"s": 1, "m": 60, "h": 3600}[m.group(2)]
    if n <= 0:
        raise ValueError("window must be positive")
    return float(n)


def _events(store: Store, since: float | None, project: str | None):
    marks = ",".join("?" for _ in TOOLING_PROJECTS)
    where, params = [f"coalesce(r.project, '') not in ({marks})", f"r.status in ({','.join('?' for _ in COUNTED)})"], [*TOOLING_PROJECTS, *COUNTED]
    if since is not None:
        where.append("r.ts >= ?")
        params.append(since)
    if project:
        where.append("r.project = ?")
        params.append(project.lower())
    q = ("select p.request_id rid, r.ts ts, p.provider provider, p.model model, p.level level, p.h h, p.hn hn, p.tokens tokens, coalesce(r.cache_read_tokens, 0) cached "
         f"from prefix_stats p join requests r on r.id = p.request_id where {' and '.join(where)} order by r.ts, p.request_id")
    return store.db.execute(q, params)


def build_cache_advice(store: Store, catalog: Catalog, cfg: Config, *, since: float | None = None, project: str | None = None,
                       window_s: float = DEFAULT_WINDOW_S) -> dict[str, Any]:
    by_req: dict[str, dict[str, Any]] = {}
    for r in _events(store, since, project):
        x = by_req.setdefault(r["rid"], {"ts": r["ts"], "provider": r["provider"], "model": r["model"], "cached": r["cached"], "levels": []})
        x["levels"].append((r["level"], r["h"], r["tokens"], r["hn"]))
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    seen: dict[tuple[str, str, str], float] = {}  # (provider, model, prefix hash) -> last time it was sent
    shapes: dict[tuple[str, str, str], float] = {}  # (provider, model, shape hash) -> last time a prefix of that shape was sent
    drifts: dict[tuple[str, str, str], dict[str, Any]] = {}

    def group(prov: str, model: str, lv: tuple[str, str, int, str | None]) -> dict[str, Any]:
        return groups.setdefault((prov, model, lv[1]), {"provider": prov, "model": model, "level": lv[0], "id": lv[1][:8], "tokens": lv[2], "requests": 0,
                                                         "repeats": 0, "repeat_tokens": 0, "cached_tokens": 0})

    for rid, x in sorted(by_req.items(), key=lambda kv: (kv[1]["ts"], kv[0])):
        prov, model = x["provider"], x["model"]
        for lv in x["levels"]:
            group(prov, model, lv)["requests"] += 1  # every prefix of this request was sent once more
        repeats = [lv for lv in x["levels"] if x["ts"] - seen.get((prov, model, lv[1]), -1e18) <= window_s]
        # a cache serves the longest prefix it holds: repeats, cached tokens and savings belong to that one, never to both the system prompt and the context
        best = max(repeats or x["levels"], key=lambda lv: lv[2])
        g = group(prov, model, best)
        g["cached_tokens"] += min(x["cached"], best[2])
        if repeats:
            g["repeats"] += 1
            g["repeat_tokens"] += best[2]
        # the same prefix with only numbers, dates or ids changed: a provider cannot cache it, and it is the commonest way to lose the cache
        moved = [lv for lv in x["levels"] if lv[3] and lv not in repeats and x["ts"] - shapes.get((prov, model, lv[3]), -1e18) <= window_s]
        if moved:
            lv = max(moved, key=lambda v: v[2])
            d = drifts.setdefault((prov, model, lv[3]), {"provider": prov, "model": model, "level": lv[0], "id": lv[3][:8], "tokens": lv[2], "requests": 0,
                                                         "drifts": 0, "drift_tokens": 0, "cached_tokens": 0})
            d["drifts"] += 1
            d["drift_tokens"] += lv[2]
            d["cached_tokens"] += min(x["cached"], lv[2])
        for lv in x["levels"]:
            seen[(prov, model, lv[1])] = x["ts"]
            if lv[3]:
                shapes[(prov, model, lv[3])] = x["ts"]
    rows = []
    for g in groups.values():
        if g["requests"] < 2:
            continue
        rows.append(_assess(g, catalog, window_s))
    for d in drifts.values():
        d["requests"] = d["drifts"] + 1  # the first sighting is not a drift but was a request
        rows.append(_assess_drift(d, catalog))
    rows.sort(key=lambda r: -(r["potential_saving_usd"] or 0.0) - (r["realized_saving_usd"] or 0.0) * 1e-6 - r["missed_tokens"] * 1e-12)
    ones = sum(1 for g in groups.values() if g["requests"] < 2)
    return {
        "window_s": window_s, "since": since, "project": project, "rows": rows, "seen_once": ones,
        "totals": {"realized_saving_usd": sum(r["realized_saving_usd"] or 0.0 for r in rows), "potential_saving_usd": sum(r["potential_saving_usd"] or 0.0 for r in rows),
                   "unpriced_rows": sum(1 for r in rows if r["potential_saving_usd"] is None), "priced_rows": sum(1 for r in rows if r["potential_saving_usd"] is not None), "prefixes": len(rows), "tracking": cfg.track_prefixes},
        "notes": [
            "Prefix sizes are token estimates, and prefixes are identified by keyed hashes: ModelSpend does not keep the text.",
            f"A repeat counts as cacheable when the same prefix was sent to the same model within {int(window_s // 60)} min {int(window_s % 60)} s of the last time; "
            "providers differ, so change this with --window.",
            "Potential saving is a projection from your catalog prices. Realized saving uses the cached tokens the provider reported.",
            "A growing chat history is not detected as a repeat (each turn's context is new), though a provider may still cache it.",
        ], "generated": time.time()}


def _price(catalog: Catalog, g: dict[str, Any]):
    try:
        m = catalog.get(f"{g['provider']}/{g['model']}")
        return m.pricing if m.pricing and m.pricing.known else None
    except KeyError:
        return None


def _assess_drift(d: dict[str, Any], catalog: Catalog) -> dict[str, Any]:
    kind = KIND.get(d["provider"], "unknown")
    p = _price(catalog, d)
    missed = max(0, d["drift_tokens"] - d["cached_tokens"])
    realized = potential = None
    if p and p.cached_input_per_mtok is not None:
        diff = (p.input_per_mtok - p.cached_input_per_mtok) / 1_000_000
        realized, potential = d["cached_tokens"] * diff, missed * diff
    advice = ("the start of this prompt differs between calls only in numbers, dates or ids (a timestamp, request id, counter), so the provider cannot cache it. "
              "Move the changing text after the fixed text")
    if p is None:
        advice += "; no price in your catalog, so no dollar figure"
    elif p.cached_input_per_mtok is None:
        advice += "; no cached-input price in your catalog, so no dollar figure"
    return {"model": f"{d['provider']}/{d['model']}", "prefix": d["id"], "level": d["level"], "prefix_tokens": d["tokens"], "requests": d["requests"], "repeats": d["drifts"],
            "repeat_tokens": d["drift_tokens"], "cached_tokens": d["cached_tokens"], "missed_tokens": missed, "kind": kind, "drifting": True,
            "realized_saving_usd": realized, "potential_saving_usd": potential, "advice": advice}


def _assess(g: dict[str, Any], catalog: Catalog, window_s: float) -> dict[str, Any]:
    kind = KIND.get(g["provider"], "unknown")
    missed = max(0, g["repeat_tokens"] - g["cached_tokens"])
    p = _price(catalog, g)
    realized = potential = None
    if p and p.cached_input_per_mtok is not None:
        diff = (p.input_per_mtok - p.cached_input_per_mtok) / 1_000_000
        realized = g["cached_tokens"] * diff
        potential = missed * diff
        if kind == "explicit" and p.cache_write_per_mtok is not None:  # an explicit cache charges extra every time it is (re)written
            potential = max(0.0, potential - (g["requests"] - g["repeats"]) * g["tokens"] * (p.cache_write_per_mtok - p.input_per_mtok) / 1_000_000)
    if g["repeats"] == 0:
        advice = f"repeats are more than {int(window_s // 60)} min apart, so a cache would have expired between them"
    elif kind == "explicit":
        advice = "needs cache markers in the request; ModelSpend does not send them yet (shown for information)"
    elif missed <= 0.1 * g["repeat_tokens"]:
        advice = "caching is working"
    elif g["cached_tokens"] == 0 and kind == "automatic":
        advice = ("the same prompt start was sent again but no cached tokens were reported. The text did not change, so the usual causes are a prompt below this model's "
                  "minimum cacheable size (our token count is an estimate; some models need more than 1,024), a model that does not cache, or a cache that expired. "
                  "Test it with `modelspend verify " + g["provider"] + " --cache`")
    elif g["cached_tokens"] == 0:
        advice = "no cached tokens reported: this provider may not cache automatically; test it with `modelspend verify " + g["provider"] + " --cache`"
    else:
        advice = "partly cached: some of the prefix varies between calls"
    if p is None:
        advice += "; no price in your catalog, so no dollar figure"
    elif p.cached_input_per_mtok is None:
        advice += "; no cached-input price in your catalog, so no dollar figure"
    return {"model": f"{g['provider']}/{g['model']}", "prefix": g["id"], "level": g["level"], "prefix_tokens": g["tokens"], "requests": g["requests"], "repeats": g["repeats"],
            "repeat_tokens": g["repeat_tokens"], "cached_tokens": g["cached_tokens"], "missed_tokens": missed, "kind": kind, "drifting": False,
            "realized_saving_usd": realized, "potential_saving_usd": potential, "advice": advice}


def _usd(v: float | None) -> str:
    return "-" if v is None else f"${v:,.2f}" if abs(v) >= 0.10 else f"${v:,.4f}"


def to_table(rep: dict[str, Any]) -> str:
    t = rep["totals"]
    if not t["tracking"] and not rep["rows"]:
        return "Prefix tracking is off (track_prefixes is false in settings.json), so there is nothing to analyse."
    if not rep["rows"]:
        return ("No repeated long prompt prefix found in this window"
                + (f" ({rep['seen_once']} long prefix(es) were seen only once)" if rep["seen_once"] else "")
                + ". Only prompts whose start is at least 1,024 tokens long and sent more than once are analysed, and only requests made through ModelSpend since this version are tracked.")
    head = ["prefix", "model", "tokens", "requests", "repeats", "cached", "missed", "saved so far", "could save", "what to do"]
    rows = [head] + [[f"{r['prefix']} ({r['level']})", r["model"].split("/", 1)[-1] + f" [{r['model'].split('/', 1)[0]}]", f"{r['prefix_tokens']:,}", str(r["requests"]), str(r["repeats"]),
                      f"{r['cached_tokens']:,}", f"{r['missed_tokens']:,}", _usd(r["realized_saving_usd"]), _usd(r["potential_saving_usd"]), r["advice"]] for r in rep["rows"]]
    widths = [max(len(x[i]) for x in rows) for i in range(len(head))]
    lines = []
    for n, x in enumerate(rows):
        lines.append("  ".join(c.ljust(widths[i]) if i in (0, 1, 9) else c.rjust(widths[i]) for i, c in enumerate(x)).rstrip())
        if n == 0:
            lines.append("  ".join("-" * w for w in widths))
    if t["priced_rows"]:
        tail = (f"\n\nSaved so far through caching: {_usd(t['realized_saving_usd'])}.  Still on the table: {_usd(t['potential_saving_usd'])}"
                + (f" (+ {t['unpriced_rows']} prefix(es) with no cached price)" if t["unpriced_rows"] else "") + ".")
    else:
        tail = "\n\nNo dollar figure: your catalog has no cached-input price for these models (set one with `modelspend catalog set <ref> pricing.cached_input_per_mtok=<usd/M>` from the provider's page)."
    out = "\n".join(lines) + tail
    return out + "\n\n" + "\n".join(f"* {n}" for n in rep["notes"])
