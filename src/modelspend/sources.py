"""Where each provider's official pricing page lives, and a way to notice when it changed.

Resolution order for a provider's pricing URL (first match wins):
  1. env  MODELSPEND_PRICING_URL_<PROVIDER>   (e.g. MODELSPEND_PRICING_URL_OPENAI)
  2. user ~/.modelspend/sources.json          (managed by `modelspend sources set`)
  3. shipped catalog/data/sources.json
`sources check` fetches each page and compares a hash with the one you last accepted; it reports
that a page CHANGED so you re-verify prices. It never parses or applies prices by itself.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .config import Config
from .types import PROVIDERS


@dataclass
class Source:
    provider: str
    pricing_url: str
    origin: str  # env | user | shipped


def clean_url(url: str) -> str:
    """Drop tracking parameters (utm_*, ref, fbclid...) and fragments so a pasted link stays stable."""
    u = urlparse(url.strip())
    if u.scheme not in ("http", "https") or not u.netloc:
        raise ValueError(f"not a valid http(s) URL: {url!r}")
    q = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True) if not re.match(r"(utm_|fbclid|gclid|ref$|ref_)", k, re.I)]
    return urlunparse((u.scheme, u.netloc, u.path, u.params, urlencode(q), ""))


def shipped_sources() -> dict[str, str]:
    raw = json.loads(Path(str(resources.files("modelspend.catalog").joinpath("data", "sources.json"))).read_text())
    return {p: raw[p]["pricing_url"] for p in PROVIDERS if p in raw}


def user_sources_path(cfg: Config) -> Path:
    return cfg.home / "sources.json"


def load_user_sources(cfg: Config) -> dict[str, str]:
    p = user_sources_path(cfg)
    return {k: v for k, v in json.loads(p.read_text()).items() if k in PROVIDERS} if p.exists() else {}


def resolve_sources(cfg: Config | None = None, env: dict[str, str] | None = None) -> dict[str, Source]:
    import os
    env = dict(os.environ if env is None else env)
    shipped = shipped_sources()
    user = load_user_sources(cfg) if cfg is not None else {}
    out: dict[str, Source] = {}
    for p in PROVIDERS:
        e = env.get(f"MODELSPEND_PRICING_URL_{p.upper()}")
        if e:
            out[p] = Source(p, clean_url(e), "env")
        elif p in user:
            out[p] = Source(p, user[p], "user")
        elif p in shipped:
            out[p] = Source(p, shipped[p], "shipped")
    return out


def set_user_source(cfg: Config, provider: str, url: str | None) -> str | None:
    """Set (or with url=None remove) the user's pricing URL for a provider; returns the stored cleaned URL."""
    data = load_user_sources(cfg)
    stored = None
    if url is None:
        data.pop(provider, None)
    else:
        stored = data[provider] = clean_url(url)
    p = user_sources_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, sort_keys=True))
    return stored


# ---- change detection -----------------------------------------------------------------------
def snapshots_path(cfg: Config) -> Path:
    return cfg.home / "source_snapshots.json"


def load_snapshots(cfg: Config) -> dict[str, dict]:
    p = snapshots_path(cfg)
    return json.loads(p.read_text()) if p.exists() else {}


def fetch(url: str, timeout: float = 20.0) -> tuple[int, str, str]:
    """(status, final_url, normalized_text). Network is only touched by `sources check`."""
    import httpx
    r = httpx.get(url, timeout=timeout, follow_redirects=True, headers={"user-agent": "modelspend-source-check/0.1"})
    return r.status_code, str(r.url), r.text


def text_hash(text: str) -> str:
    body = re.sub(r"<(script|style)\b.*?</\1>", " ", text, flags=re.S | re.I)
    body = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body)).strip()
    return hashlib.sha256(body.encode()).hexdigest()


def check_source(cfg: Config, src: Source, accept: bool = False, fetcher=None) -> dict:
    fetcher = fetcher or fetch
    snaps = load_snapshots(cfg)
    prev = snaps.get(src.provider)
    try:
        status, final_url, text = fetcher(src.pricing_url)
    except Exception as e:  # noqa: BLE001
        return {"provider": src.provider, "url": src.pricing_url, "state": "unreachable", "detail": f"{type(e).__name__}: {str(e)[:120]}"}
    if status >= 400:
        return {"provider": src.provider, "url": src.pricing_url, "state": "http_error", "detail": f"HTTP {status}"}
    h = text_hash(text)
    if prev is None or prev.get("url") != src.pricing_url:
        state = "no_baseline"
    else:
        state = "unchanged" if prev["hash"] == h else "CHANGED"
    res = {"provider": src.provider, "url": src.pricing_url, "final_url": final_url, "state": state,
           "redirected": final_url.rstrip("/") != src.pricing_url.rstrip("/")}
    if accept:
        from datetime import date
        snaps[src.provider] = {"url": src.pricing_url, "hash": h, "accepted_on": str(date.today())}
        p = snapshots_path(cfg)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(snaps, indent=2, sort_keys=True))
        res["accepted"] = True
    return res
