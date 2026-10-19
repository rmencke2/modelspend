"""Provider registry: the one place that knows which providers exist and how to reach them.

Three kinds:
- **builtin**: OpenAI, Anthropic, Google, xAI (native adapters, shipped before this module existed).
- **preset**: an OpenAI-compatible provider ModelSpend knows the address of (DeepSeek).
- **custom**: any other OpenAI-compatible endpoint you describe in `settings.json` under `custom_providers` (Groq, Together, Mistral, OpenRouter,
  a local Ollama or vLLM, ...). Nothing is hard-coded about them beyond what you write.

What this module does NOT do is decide prices or model lists: those come from the provider's own pages and Models API on your machine
(`modelspend catalog sync`, `modelspend pricing refresh`), never from a list shipped here. Model names and prices change too fast to ship.

A leaf module: it imports nothing else from ModelSpend, so every other module may depend on it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

BUILTIN = ("anthropic", "openai", "google", "xai")  # display order
ALIASES = {"gemini": "google", "claude": "anthropic", "grok": "xai"}

_OPENAI_OPTS = frozenset({"reasoning_effort", "seed", "top_p", "frequency_penalty", "presence_penalty", "stop", "user"})
_COMPAT_OPTS = frozenset({"seed", "top_p", "frequency_penalty", "presence_penalty", "stop", "user"})
_NAME = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
_ENV = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}
_CUSTOM_KEYS = {"base_url", "key_env", "max_tokens_param", "cached_included", "no_key", "pricing_url"}


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    kind: str  # builtin | preset | custom
    key_env: tuple[str, ...]
    base_url: str | None = None
    max_tokens_param: str = "max_completion_tokens"
    cached_included: bool = True  # does the provider's input-token count already include cached tokens?
    options: frozenset[str] = _COMPAT_OPTS  # provider_options keys a request may forward
    pricing_url: str | None = None
    needs_key: bool = True
    note: str = ""


_BUILTIN_SPECS = {
    "anthropic": ProviderSpec("anthropic", "builtin", ("ANTHROPIC_API_KEY",), cached_included=False, options=frozenset({"top_p", "top_k", "stop_sequences"})),
    "openai": ProviderSpec("openai", "builtin", ("OPENAI_API_KEY",), options=_OPENAI_OPTS),
    "google": ProviderSpec("google", "builtin", ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
                           options=frozenset({"top_p", "top_k", "seed", "stop_sequences", "presence_penalty", "frequency_penalty"})),
    "xai": ProviderSpec("xai", "builtin", ("XAI_API_KEY",), base_url="https://api.x.ai/v1", options=_OPENAI_OPTS),
}

# OpenAI-compatible providers ModelSpend knows the address of. No models and no prices are shipped for them.
PRESETS = {
    "deepseek": ProviderSpec(
        "deepseek", "preset", ("DEEPSEEK_API_KEY",), base_url="https://api.deepseek.com", max_tokens_param="max_tokens", cached_included=True,
        pricing_url="https://api-docs.deepseek.com/quick_start/pricing",
        note="Reports cache hits as prompt_cache_hit_tokens. Not yet exercised against the live API: check `modelspend keys test deepseek` and "
             "`modelspend sources check deepseek`, and take prices from DeepSeek's own page."),
}

_custom: dict[str, ProviderSpec] = {}


def parse_custom(raw: object) -> dict[str, ProviderSpec]:
    """Validate the `custom_providers` block of settings.json. A typo here would send your API key to the wrong place or silently drop a provider,
    so anything unexpected is an error (ValueError), never ignored."""
    if raw in (None, {}):
        return {}
    if not isinstance(raw, dict):
        raise ValueError("settings.json: 'custom_providers' must be an object of name -> settings")
    out: dict[str, ProviderSpec] = {}
    for name, d in raw.items():
        where = f"settings.json: custom_providers.{name}"
        if not isinstance(name, str) or not _NAME.match(name):
            raise ValueError(f"settings.json: custom provider name {name!r} must be 2-32 characters: lowercase letters, digits, '-' or '_', starting with a letter")
        if name in BUILTIN or name in PRESETS or name in ALIASES:
            raise ValueError(f"{where}: '{name}' is already a built-in provider name; choose another")
        if not isinstance(d, dict):
            raise ValueError(f"{where} must be an object")
        extra = sorted(set(d) - _CUSTOM_KEYS)
        if extra:
            raise ValueError(f"{where}: unknown setting(s) {', '.join(extra)}; valid: {', '.join(sorted(_CUSTOM_KEYS))}")
        if "base_url" not in d:
            raise ValueError(f"{where}: base_url is required (the OpenAI-compatible endpoint, e.g. https://api.example.com/v1)")
        base = _check_url(where, "base_url", d["base_url"], allow_http_loopback=True)
        key_env = d.get("key_env", name.upper().replace("-", "_") + "_API_KEY")
        if not isinstance(key_env, str) or not _ENV.match(key_env):
            raise ValueError(f"{where}: key_env must be an environment variable name like GROQ_API_KEY")
        mtp = d.get("max_tokens_param", "max_tokens")
        if mtp not in ("max_tokens", "max_completion_tokens"):
            raise ValueError(f"{where}: max_tokens_param must be 'max_tokens' or 'max_completion_tokens'")
        for flag in ("cached_included", "no_key"):
            if flag in d and not isinstance(d[flag], bool):
                raise ValueError(f"{where}: {flag} must be true or false")
        pricing = _check_url(where, "pricing_url", d["pricing_url"], allow_http_loopback=False) if d.get("pricing_url") else None
        out[name] = ProviderSpec(name, "custom", (key_env,), base_url=base, max_tokens_param=mtp, cached_included=d.get("cached_included", True),
                                 pricing_url=pricing, needs_key=not d.get("no_key", False))
    return out


def _check_url(where: str, field: str, value: object, allow_http_loopback: bool) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{where}: {field} must be a URL string")
    u = urlsplit(value.strip())
    host = (u.hostname or "").lower()
    if not host or u.username or u.password or u.query or u.fragment:
        raise ValueError(f"{where}: {field} must be a plain URL without credentials, query or fragment (API keys go in the keys file or environment, never in a URL)")
    if u.scheme == "https":
        return value.strip().rstrip("/")
    if u.scheme == "http" and allow_http_loopback and host in _LOOPBACK:
        return value.strip().rstrip("/")  # a local server (Ollama, vLLM): no key crosses a network
    raise ValueError(f"{where}: {field} must use https" + (" (plain http is allowed only for localhost)" if allow_http_loopback else ""))


def register_custom(specs: dict[str, ProviderSpec]) -> None:
    """Make the user's custom providers known process-wide (called when a Config is built). Replaces the previous set."""
    _custom.clear()
    _custom.update(specs)


def all_specs() -> dict[str, ProviderSpec]:
    return {**_BUILTIN_SPECS, **PRESETS, **_custom}


def all_providers() -> tuple[str, ...]:
    return tuple(all_specs())  # builtins first (display order), then presets, then custom in the order written


def spec(name: str) -> ProviderSpec:
    return all_specs()[name]


def key_env() -> dict[str, tuple[str, ...]]:
    return {n: s.key_env for n, s in all_specs().items()}


def normalize(name: str) -> str:
    n = ALIASES.get(name.strip().lower(), name.strip().lower())
    if n not in all_specs():
        raise ValueError(f"unknown provider '{name}'; supported: {', '.join(all_providers())}")
    return n
