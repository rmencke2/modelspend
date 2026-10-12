"""Input-token estimation. Always labelled with the method used."""
from __future__ import annotations

import json
import math
from typing import Any

from .types import Message, ToolSpec

PER_MESSAGE_OVERHEAD = 4
APPROX_SAFETY_MARGIN = 1.2  # applied to reservations only when the count is an approximation


def _tiktoken_count(text: str) -> int | None:
    try:
        import tiktoken  # optional extra
    except Exception:
        return None
    try:
        return len(tiktoken.get_encoding("o200k_base").encode(text))
    except Exception:
        return None


def count_text(text: str) -> tuple[int, str]:
    n = _tiktoken_count(text)
    if n is not None:
        return n, "tiktoken o200k_base (approximate for non-OpenAI models)"
    return math.ceil(len(text) / 4), "characters/4 approximation"


def estimate_input_tokens(
    messages: list[Message], schema: dict[str, Any] | None = None, tools: list[ToolSpec] | None = None
) -> tuple[int, str]:
    blob = []
    for m in messages:
        blob.append(m.content or "")
        for tc in m.tool_calls or []:
            blob.append(tc.name + tc.arguments)
    if schema:
        blob.append(json.dumps(schema))
    for t in tools or []:
        blob.append(t.name + t.description + json.dumps(t.parameters))
    total, method = count_text("\n".join(blob))
    return total + PER_MESSAGE_OVERHEAD * len(messages) + 3, method


def reservation_input_tokens(estimate: int, method: str) -> int:
    """Conservative input figure for budget reservation."""
    return estimate if method.startswith("provider") else math.ceil(estimate * APPROX_SAFETY_MARGIN)
