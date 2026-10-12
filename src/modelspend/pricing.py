"""Cost arithmetic. One place, so estimates and accounting cannot drift apart.

Rules:
* `usage.output_tokens` is TOTAL billable output including reasoning; reasoning is never re-added.
* Long-context tiers replace the rates for the whole request when the prompt is over the threshold.
* Cache reads/writes use their own rates when the catalog has them; otherwise they fall back to
  the plain input rate (never to zero) so cost is not under-reported.
"""
from __future__ import annotations

from .catalog.models import Model, Pricing
from .types import Usage

M = 1_000_000


def _rates(p: Pricing, prompt_tokens: int) -> tuple[float, float, float | None, float | None]:
    inp, out, cached, write = p.input_per_mtok, p.output_per_mtok, p.cached_input_per_mtok, p.cache_write_per_mtok
    for t in sorted(p.long_context_tiers, key=lambda t: t.prompt_tokens_over):
        if prompt_tokens > t.prompt_tokens_over:
            inp, out = t.input_per_mtok, t.output_per_mtok
            cached = t.cached_input_per_mtok if t.cached_input_per_mtok is not None else cached
            write = t.cache_write_per_mtok if t.cache_write_per_mtok is not None else write
    assert inp is not None and out is not None
    return inp, out, cached, write


def cost_for(model: Model, usage: Usage) -> float:
    p = model.pricing
    if p is None or not p.known:
        raise ValueError(f"{model.ref} has no known pricing")
    prompt = usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens
    inp, out, cached, write = _rates(p, prompt)
    read_rate = cached if cached is not None else inp
    write_rate = write if write is not None else inp
    return (
        usage.input_tokens * inp
        + usage.cache_read_tokens * read_rate
        + usage.cache_write_tokens * write_rate
        + usage.output_tokens * out
    ) / M


def estimate_cost(model: Model, input_tokens: int, output_tokens: int) -> float:
    """Pre-call estimate assuming no cache hits (conservative where cache reads are cheaper)."""
    return cost_for(model, Usage(input_tokens=input_tokens, output_tokens=output_tokens))
