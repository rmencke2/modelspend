from __future__ import annotations

from .. import registry
from ..config import Config
from .anthropic_ import AnthropicAdapter
from .base import ProviderAdapter, ProviderRequest, ProviderResponse, StreamChunk, StreamEnd
from .google_ import GoogleAdapter
from .openai_compat import OpenAICompatAdapter, XAIAdapter, make_compat_adapter

__all__ = [
    "ProviderAdapter", "ProviderRequest", "ProviderResponse", "StreamChunk", "StreamEnd",
    "OpenAICompatAdapter", "XAIAdapter", "AnthropicAdapter", "GoogleAdapter", "build_adapters", "make_compat_adapter",
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
    for name, spec in registry.all_specs().items():  # presets (DeepSeek) and the user's own OpenAI-compatible providers
        if spec.kind != "builtin" and name in k:
            out[name] = make_compat_adapter(spec, k[name])
    return out
