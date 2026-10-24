"""Decide when to ask Anthropic to cache a prompt prefix.

OpenAI and DeepSeek cache a repeated prompt start on their own. Anthropic caches only what a request marks with `cache_control`, so without markers a
Claude call never gets a cache hit however often the same long system prompt is sent. This module decides, per request, whether to mark one prefix and
which. Off by default: marking changes what a call costs (a cache write is billed above the normal input price, a cache read far below it), so it is a
choice you make, with `"anthropic_cache_markers"` in settings.json (or MODELSPEND_ANTHROPIC_CACHE_MARKERS):

- `repeats`: mark a prefix only when the same prefix was already sent to the same model within the cache lifetime (five minutes). The first request is
  never marked, so a prompt that never repeats never pays the write premium. Needs prefix tracking (the keyed hashes `modelspend caching` uses).
- `always`: mark every long prefix, including the first. The first request pays the write premium whether or not a second one follows.

Only one prefix is marked: the longest one that qualifies, the same one `modelspend caching` credits. A marker on a prefix below the model's minimum
cacheable size (1,024 tokens, more on some models) is ignored by Anthropic without error or charge, so no per-model table is needed here.
Nothing is stored beyond what prefix tracking already keeps: keyed hashes and token counts, never text.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from .prefixes import prefix_levels

log = logging.getLogger("modelspend")
CACHE_TTL_S = 300.0  # Anthropic's default cache lifetime, refreshed each time the cache is read


def choose(store: Any, mode: str, req: Any, provider: str, model: str, now: float | None = None) -> str | None:
    """"system", "context" or None. Never raises: a failure here must not affect the request."""
    if mode == "off" or provider != "anthropic" or store is None:
        return None
    try:
        levels = prefix_levels(req.messages, req.response_schema, req.tools, store.salt())
        if mode == "repeats":
            since = (time.time() if now is None else now) - CACHE_TTL_S
            levels = [lv for lv in levels if store.db.execute("select 1 from prefix_stats where provider = ? and model = ? and h = ? and ts >= ? limit 1",
                                                              (provider, model, lv["h"], since)).fetchone()]
        return max(levels, key=lambda lv: lv["tokens"])["level"] if levels else None
    except Exception:  # noqa: BLE001
        log.debug("cache marker choice skipped", exc_info=True)
        return None
