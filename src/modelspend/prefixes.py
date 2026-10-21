"""Remember which long prompt prefixes repeat, without remembering what they say.

Provider prompt caching bills a repeated prompt start (a system prompt, few-shot examples, a document) at a fraction of the normal input price, but only
when the start is byte-identical and long enough. To tell you where that is being missed, ModelSpend needs to know which long prefixes repeat. For
each request it keeps, per reusable prefix, a keyed hash (HMAC-SHA256 with a random key that lives only in your own database, truncated) and a token
estimate, plus a second keyed hash of the same prefix with its numbers, dates and ids blanked, which is how a timestamp near the top is recognised. The hash cannot be reversed or matched against anything outside this database; the text is never stored. Turn it off with
`"track_prefixes": false` in settings.json (or MODELSPEND_TRACK_PREFIXES=0); `modelspend caching --forget` erases what is stored.

Two prefixes are tracked per request, each including the tools and response schema (which are part of what a provider caches):
- `system`: the leading system message(s);
- `context`: everything except the last message (few-shot examples, a document, earlier turns).
A growing chat history is not detected as a repeat (each turn's context is new); providers may still cache it, and the report shows what they reported.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from typing import Any

from .tokens import estimate_input_tokens
from .types import Message, ToolSpec

log = logging.getLogger("modelspend")
MIN_PREFIX_TOKENS = 1024  # no major provider caches less than this
MIN_PREFIX_CHARS = 3000  # cheap pre-filter: roughly 750 tokens; below it nothing is tokenized at all


def _ser(messages: list[Message], schema: dict[str, Any] | None, tools: list[ToolSpec] | None) -> str:
    parts = [json.dumps(schema, sort_keys=True) if schema else "", "\x1e".join(t.name + "\x1f" + t.description + "\x1f" + json.dumps(t.parameters, sort_keys=True)
                                                                           for t in tools or [])]
    for m in messages:
        parts.append(m.role + "\x1f" + (m.content or "") + "".join("\x1f" + tc.name + tc.arguments for tc in m.tool_calls or []))
    return "\x1d".join(parts)


_VOLATILE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"  # uuid
    r"|\b(?=[0-9a-f]*\d)[0-9a-f]{8,}\b"  # long hex id
    r"|\d+"  # any number: timestamps, counters, dates
    r"|\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|January|February|March|April|June|July|August|September|October|November|December)\b")


def _shape(text: str) -> str:
    """The prefix with numbers, dates and ids blanked: two prefixes with the same shape differ only in things like a timestamp or request id."""
    return _VOLATILE.sub("#", text)


def prefix_levels(messages: list[Message], schema: dict[str, Any] | None, tools: list[ToolSpec] | None, salt: bytes) -> list[dict[str, Any]]:
    """[{level, h, hn, tokens}] for each reusable prefix long enough to be cached. Empty for short prompts (nothing is tokenized)."""
    lead = 0
    while lead < len(messages) and messages[lead].role == "system":
        lead += 1
    cuts = []
    if lead:
        cuts.append(("system", lead))
    if len(messages) - 1 > lead:  # a context beyond the system messages
        cuts.append(("context", len(messages) - 1))
    elif not lead and len(messages) > 1:
        cuts.append(("context", len(messages) - 1))
    out = []
    for level, n in cuts:
        text = _ser(messages[:n], schema, tools)
        if len(text) < MIN_PREFIX_CHARS:
            continue
        tokens = estimate_input_tokens(messages[:n], schema, tools)[0]
        if tokens < MIN_PREFIX_TOKENS:
            continue
        out.append({"level": level, "h": hmac.new(salt, text.encode("utf-8", "replace"), hashlib.sha256).hexdigest()[:16], "tokens": tokens,
                    "hn": hmac.new(salt, b"shape:" + _shape(text).encode("utf-8", "replace"), hashlib.sha256).hexdigest()[:16]})
    return out


def track(store: Any, enabled: bool, req: Any, provider: str, model: str, request_id: str) -> None:
    """Called after a request is recorded. Must never affect the request: any failure is swallowed."""
    if not enabled or store is None:
        return
    try:
        levels = prefix_levels(req.messages, req.response_schema, req.tools, store.salt())
        if levels:
            store.record_prefixes(request_id, provider, model, levels)
    except Exception:  # noqa: BLE001
        log.debug("prefix tracking skipped", exc_info=True)
