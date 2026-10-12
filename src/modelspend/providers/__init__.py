from __future__ import annotations

from ..config import Config
from .anthropic_ import AnthropicAdapter
from .base import ProviderAdapter, ProviderRequest, ProviderResponse, StreamChunk, StreamEnd
from .google_ import GoogleAdapter
from .openai_compat import OpenAICompatAdapter, XAIAdapter

__all__ = [
    "ProviderAdapter", "ProviderRequest", "ProviderResponse", "StreamChunk", "StreamEnd",
    "OpenAICompatAdapter", "XAIAdapter", "AnthropicAdapter", "GoogleAdapter", "build_adapters",
]


def build_adapters(cfg: Config) -> dict[str, ProviderAdapter]:
    """One adapter per provider that has an API key. Keys stay inside the adapter objects."""
    out: dict[str, ProviderAdapter] = {}
    k = cfg.api_keys
    if "openai" in k:
        out["openai"] = OpenAICompatAdapter(k["openai"])
    if "anthropic" in k:
        out["anthropic"] = AnthropicAdapter(k["anthropic"])
    if "google" in k:
        out["google"] = GoogleAdapter(k["google"])
    if "xai" in k:
        out["xai"] = XAIAdapter(k["xai"])
    return out
