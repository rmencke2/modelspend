"""`modelspend doctor`: one offline pass over a setup that says what is wrong, what is merely worth knowing, and the command that fixes each thing.

Nothing here touches the network or spends money: it reads your settings, keys file (never printing a key), catalog, the last `modelspend verify` results
and the usage ledger. Each finding has a level:

- `problem`: something is broken or unsafe (no key, a provider with no usable model, a failed live check, a key file other users can read);
- `warn`: something will probably give you a wrong or missing number (stale or missing prices, calls with no price, a high error rate);
- `info`: worth knowing, nothing to fix (no traffic yet, a provider never verified, a feature that is off);
- `ok`: checked and fine.

`modelspend verify <provider>` is the live counterpart: doctor tells you which providers have never been through it, and reports what the last run found.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

from .config import Config, is_loopback, keys_path

VERIFY_FILE = "verify-results.json"
VERIFY_FRESH_DAYS = 30
ERROR_RATE_WARN = 0.2
LEDGER_DAYS = 30
ONLY_HINT = {"openai": "gpt", "google": "gemini", "xai": "grok", "deepseek": "deepseek"}  # catalog sync --add-new needs an id filter
LEVELS = ("problem", "warn", "info", "ok")


@dataclass
class Finding:
    level: str
    area: str
    message: str
    fix: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"level": self.level, "area": self.area, "message": self.message, "fix": self.fix}


# ---------------------------------------------------------------- remembering `verify` (verdicts only: no keys, no text)
def save_verify(home: Path, reports: list[Any], today: date | None = None) -> None:
    """Keep the last result per provider so doctor can say what was found and how long ago. Best effort: never fails a verify run."""
    try:
        p = home / VERIFY_FILE
        data = load_verify(home)
        for r in reports:
            data[r.provider] = {"model": r.model, "status": r.status, "date": str(today or date.today()),
                                "problems": [c.name for c in r.checks if c.status in ("fail", "warn")],
                                "skipped": [c.name for c in r.checks if c.status == "skip"]}
        p.write_text(json.dumps(data, indent=2, sort_keys=True))
    except Exception:  # noqa: BLE001
        pass


def load_verify(home: Path) -> dict[str, dict[str, Any]]:
    try:
        d = json.loads((home / VERIFY_FILE).read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


# ---------------------------------------------------------------- the checks
def _loose(path: Path) -> bool:
    try:
        return bool(path.stat().st_mode & 0o077)
    except OSError:
        return False


def _keys(cfg: Config, out: list[Finding]) -> list[str]:
    have = [p for p in cfg.configured_providers() if cfg.key_sources.get(p) != "not required"]
    if not have:
        out.append(Finding("problem", "keys", "no provider API key is configured, so ModelSpend cannot call anything", "modelspend keys set openai"))
        return have
    out.append(Finding("ok", "keys", "keys for " + ", ".join(f"{p} ({cfg.key_sources.get(p, '?')})" for p in have)))
    kp = keys_path(cfg.home)
    if kp.exists() and (_loose(kp) or _loose(cfg.home)):
        out.append(Finding("problem", "keys", f"your keys file or its folder can be read by other users on this machine ({kp})", f"chmod 600 {kp} && chmod 700 {cfg.home}"))
    return have


def _models(cfg: Config, catalog: Any, have: list[str], routability: Callable[[Any], list[str]], out: list[Finding]) -> list[Any]:
    routable_all: list[Any] = []
    for p in have:
        ms = [m for m in catalog.models if m.provider == p]
        if not ms:
            out.append(Finding("warn", "models", f"{p}: you have a key but your catalog has no {p} models, so nothing can be sent to it", f"modelspend catalog sync --provider {p} --add-new --only {ONLY_HINT.get(p, p)} --write   # --only picks which model ids to add: narrow it for fewer"))
            continue
        ok = [m for m in ms if not routability(m)]
        routable_all += ok
        if ok:
            out.append(Finding("ok", "models", f"{p}: {len(ok)} of {len(ms)} models routable"))
            continue
        if all("provider not enabled" in routability(m) for m in ms):  # your own allowlist, not a fault: say how to change it
            out.append(Finding("info", "models", f"{p} has a key but is not in your enabled providers, so its {len(ms)} models are not used", f"modelspend providers enable {p}"))
            continue
        reasons: dict[str, int] = {}
        for m in ms:
            for r in routability(m):
                key = r.split(" (")[0]
                reasons[key] = reasons.get(key, 0) + 1
        top = max(reasons, key=lambda k: reasons[k])
        fix = {"availability unverified": f"modelspend catalog sync --provider {p} --write", "no pricing": "modelspend sources list   # shows the provider's pricing page; enter prices with `modelspend catalog price`",
               "limits unknown": f"modelspend catalog sync --provider {p} --write", "no tier": "modelspend models list   # see which models have a tier"}.get(top)
        out.append(Finding("warn", "models", f"{p}: none of its {len(ms)} models can be used right now; most common reason: {top} ({reasons[top]} models)", fix))
    if not routable_all:
        first = next((f for f in out if f.area == "models" and f.level in ("warn", "info") and f.fix), None)
        out.append(Finding("problem", "models", "no provider has a model ModelSpend can use right now, so every request would be refused", first.fix if first else None))
    for p in cfg.default_providers:
        if p not in cfg.api_keys:
            out.append(Finding("warn", "models", f"{p} is in your enabled-providers list but has no key", f"modelspend keys set {p}"))
    return routable_all


def _prices(cfg: Config, catalog: Any, routable: list[Any], store: Any, out: list[Finding]) -> None:
    refs = {m.ref for m in routable}
    stale = [m for m in catalog.stale(max_age_days=cfg.max_pricing_age_days) if m.ref in refs]
    if stale:
        out.append(Finding("warn", "prices", f"{len(stale)} routable model(s) have prices older than {cfg.max_pricing_age_days} days or no date: " + ", ".join(m.ref for m in stale[:4]) + (" ..." if len(stale) > 4 else ""),
                           "modelspend catalog stale   # lists them; check the provider's pricing page and re-enter with `modelspend catalog price`"))
    elif routable:
        out.append(Finding("ok", "prices", "prices of the routable models are dated within " + str(cfg.max_pricing_age_days) + " days"))
    if store is None:
        return
    since = time.time() - LEDGER_DAYS * 86400
    rows = store.db.execute("select provider, model, sum(coalesce(cache_read_tokens, 0)) cached from requests where ts >= ? group by 1, 2", (since,)).fetchall()
    by_ref = {m.ref: m for m in catalog.models}
    for r in rows:
        m = by_ref.get(f"{r['provider']}/{r['model']}")
        if m and r["cached"] and m.pricing and m.pricing.known and m.pricing.cached_input_per_mtok is None:
            out.append(Finding("warn", "prices", f"{m.ref} reported {r['cached']:,} cached tokens in the last {LEDGER_DAYS} days but your catalog has no cached-input price, so they are costed as ordinary input and the cost is overstated",
                               f"modelspend catalog set {m.ref} pricing.cached_input_per_mtok=<usd per million>   # the cache-read price on the provider's pricing page (modelspend sources list shows the link)"))


def _verify(have: list[str], results: dict[str, dict[str, Any]], today: date, out: list[Finding]) -> None:
    never = [p for p in have if p not in results]
    if never:
        out.append(Finding("info", "verify", "not checked live on this machine yet: " + ", ".join(never) + " (a few tiny requests each, a fraction of a cent; it asks before spending)",
                           "modelspend verify --cache" if len(never) == len(have) else "modelspend verify " + " ".join(never) + " --cache"))
    for p in have:
        r = results.get(p)
        if r is None:
            continue
        try:
            age = (today - date.fromisoformat(r.get("date", ""))).days
        except ValueError:
            age = None
        what = f"{p}: last live check {r.get('date', '?')} on {r.get('model') or 'no model'}"
        if r["status"] == "fail":
            out.append(Finding("problem", "verify", f"{what} FAILED ({', '.join(r.get('problems') or ['see the report'])}): a number ModelSpend shows for this provider may be wrong", f"modelspend verify {p} --cache --report check.md"))
        elif r["status"] in ("warn", "skip"):
            out.append(Finding("warn", "verify", f"{what} was NOT VERIFIED" + (f" ({', '.join(r['skipped'])} skipped)" if r.get("skipped") else ""), f"modelspend verify {p} --cache"))
        elif age is not None and age > VERIFY_FRESH_DAYS:
            out.append(Finding("info", "verify", f"{what} passed, {age} days ago", f"modelspend verify {p} --cache"))
        else:
            out.append(Finding("ok", "verify", f"{what} passed"))


def _ledger(store: Any, catalog: Any, out: list[Finding]) -> None:
    if store is None:
        return
    since = time.time() - LEDGER_DAYS * 86400
    n = store.db.execute("select count(*) from requests where ts >= ?", (since,)).fetchone()[0]
    if n == 0:
        out.append(Finding("info", "ledger", f"no requests recorded in the last {LEDGER_DAYS} days: nothing has gone through ModelSpend yet", "modelspend serve --background   # then point your client at http://127.0.0.1:8787/v1"))
        return
    from .savings import COUNTED
    marks = ",".join("?" for _ in COUNTED)
    failed = store.db.execute(f"select count(*) from requests where ts >= ? and status not in ({marks})", (since, *COUNTED)).fetchone()[0]
    if n >= 10 and failed / n > ERROR_RATE_WARN:
        kinds = store.db.execute("select coalesce(a.error_kind, a.outcome) k, count(*) c from attempts a join requests r on r.id = a.request_id where r.ts >= ? and a.outcome != 'ok' group by 1 order by 2 desc limit 3", (since,)).fetchall()
        out.append(Finding("warn", "ledger", f"{failed} of {n} requests in the last {LEDGER_DAYS} days did not produce an answer" + (" (most common: " + ", ".join(f"{k['k']} x{k['c']}" for k in kinds) + ")" if kinds else ""),
                           "modelspend report --since 7d"))
    else:
        out.append(Finding("ok", "ledger", f"{n} request(s) in the last {LEDGER_DAYS} days, {failed} without an answer"))
    unpriced = store.db.execute(f"select provider, model, count(*) c from requests where ts >= ? and actual_cost is null and status in ({marks}) group by 1, 2", (since, *COUNTED)).fetchall()
    priced = {m.ref for m in catalog.models if m.pricing and m.pricing.known}
    still = [r for r in unpriced if f"{r['provider']}/{r['model']}" not in priced]
    past = [r for r in unpriced if f"{r['provider']}/{r['model']}" in priced]
    if still:
        out.append(Finding("warn", "ledger", "calls with no price, so their cost is unknown: " + ", ".join(f"{r['provider']}/{r['model']} x{r['c']}" for r in still[:4]),
                           f"modelspend catalog price {still[0]['provider']}/{still[0]['model']} --input <usd per million> --output <usd per million>   # the numbers on the provider's pricing page (modelspend sources list shows the link)"))
    if past:
        out.append(Finding("info", "ledger", "earlier calls with no cost recorded, from before the model was priced (new calls are costed): " + ", ".join(f"{r['provider']}/{r['model']} x{r['c']}" for r in past[:4])))


def _caching(cfg: Config, have: list[str], store: Any, out: list[Finding]) -> None:
    if not cfg.track_prefixes:
        out.append(Finding("info", "caching", "prefix tracking is off, so `modelspend caching` has nothing to show", "set MODELSPEND_TRACK_PREFIXES=1 (or remove track_prefixes from settings.json)"))
    if "anthropic" in have and cfg.anthropic_cache_markers == "off":
        used = store is not None and store.db.execute("select count(*) from requests where provider = 'anthropic'").fetchone()[0]
        if used:
            out.append(Finding("info", "caching", "Claude calls are going through ModelSpend with cache markers off: a long system prompt that repeats is billed in full every time. `modelspend caching` shows whether that applies to you",
                               'add "anthropic_cache_markers": "repeats" to settings.json (see docs/caching.md)'))


def _security_and_setup(cfg: Config, out: list[Finding]) -> None:
    if not is_loopback(cfg.host) and not cfg.api_token:
        out.append(Finding("problem", "security", f"the server is set to listen on {cfg.host} with no API token: it will refuse to start, and without that check anyone who can reach it could spend your keys", "set MODELSPEND_API_TOKEN to a long random value, or use MODELSPEND_HOST=127.0.0.1"))
    if cfg.db_path and Path(cfg.db_path).exists() and _loose(Path(cfg.db_path)):
        out.append(Finding("warn", "security", f"your ledger database can be read by other users on this machine ({cfg.db_path})", f"chmod 600 {cfg.db_path}"))
    if cfg.exact_token_counts:
        try:
            import tiktoken  # noqa: F401
        except ImportError:
            out.append(Finding("problem", "setup", "exact token counts are on but the optional tiktoken package is not installed", "pip install tiktoken"))


def diagnose(cfg: Config, catalog: Any, store: Any, *, routability: Callable[[Any], list[str]], verify_results: dict[str, dict[str, Any]] | None = None,
             today: date | None = None) -> list[Finding]:
    out: list[Finding] = []
    have = _keys(cfg, out)
    routable = _models(cfg, catalog, have, routability, out) if have else []
    _prices(cfg, catalog, routable, store, out)
    _verify(have, verify_results or {}, today or date.today(), out)
    _ledger(store, catalog, out)
    _caching(cfg, have, store, out)
    _security_and_setup(cfg, out)
    return out


def summary(findings: list[Finding]) -> dict[str, int]:
    return {lv: sum(1 for f in findings if f.level == lv) for lv in LEVELS}


def to_text(findings: list[Finding]) -> str:
    tag = {"problem": "PROBLEM", "warn": "warn", "info": "info", "ok": "ok"}
    lines = ["ModelSpend doctor (offline: nothing was sent to any provider, nothing was changed)", ""]
    area = None
    for f in sorted(findings, key=lambda f: (list(dict.fromkeys(x.area for x in findings)).index(f.area), LEVELS.index(f.level))):
        if f.area != area:
            area = f.area
            lines.append(area)
        lines.append(f"  {tag[f.level]:<8}{f.message}")
        if f.fix:
            lines.append(f"  {'':<8}-> {f.fix}")
    s = summary(findings)
    lines += ["", f"{s['problem']} problem(s), {s['warn']} warning(s), {s['info']} note(s), {s['ok']} check(s) fine."
              + (" Fix the problems first." if s["problem"] else "" if not s["warn"] else " The warnings are numbers that may be wrong or missing.")]
    return "\n".join(lines)
