"""Model aliases: pick the model by the kind of work, by name.

    modelspend alias set coding anthropic/claude-sonnet-5-5
    modelspend alias set classify openai/gpt-4.1-nano
    modelspend route "..." --model classify              # CLI
    client.chat.completions.create(model="coding", ...)  # any OpenAI client against the local server
    ms.route("...", pin="coding")                         # SDK

An alias is a pinned model: ModelSpend runs exactly that model (no routing, failover or escalation) and still records tokens, cost and
tags, so per-project / per-agent reports and budgets work for people who prefer to choose the model themselves. Aliases live in
`~/.modelspend/settings.json`.
"""
from __future__ import annotations

import re
from typing import Any

from .config import Config, load_settings, save_settings

_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
RESERVED = {"auto"}
MAX_ALIASES = 100


def clean_name(name: str) -> str:
    n = str(name).strip().lower()
    if n in RESERVED or n == "modelspend":
        raise ValueError(f"'{n}' is reserved")
    if not _NAME.match(n):
        raise ValueError("alias names are 1-32 characters: lowercase letters, digits, - and _ (no '/', so they cannot be mistaken for provider/model)")
    return n


def clean_ref(ref: str) -> str:
    from .types import normalize_provider
    r = str(ref).strip()
    if "/" not in r:
        raise ValueError("an alias points to 'provider/model-id', e.g. anthropic/claude-sonnet-5-5")
    prov, mid = r.split("/", 1)
    if not mid.strip():
        raise ValueError("model id is empty")
    return f"{normalize_provider(prov)}/{mid.strip()}"


def resolve(name: str | None, aliases: dict[str, str]) -> str | None:
    """'coding' -> 'anthropic/claude-sonnet-5-5'; anything that is not an alias (including provider/model refs) comes back unchanged."""
    if name is None:
        return None
    return aliases.get(str(name).strip().lower(), name) if "/" not in str(name) else name


def set_alias(cfg: Config, name: str, ref: str, catalog: Any = None) -> tuple[str, str]:
    """Save an alias. With a catalog, the target must be in it (unpriced is fine). Updates `cfg.aliases` in place."""
    n, r = clean_name(name), clean_ref(ref)
    if catalog is not None and not any(m.ref == r for m in catalog.models):
        raise ValueError(f"{r} is not in your catalog. Add it first: modelspend catalog sync --provider {r.split('/', 1)[0]} --add-new --only {r.split('/', 1)[1]} --exact --write")
    s = load_settings(cfg.home)
    al = dict(s.get("aliases") or {})
    if n not in al and len(al) >= MAX_ALIASES:
        raise ValueError(f"at most {MAX_ALIASES} aliases")
    al[n] = r
    s["aliases"] = al
    save_settings(cfg.home, s)
    cfg.aliases[n] = r
    return n, r


def remove_alias(cfg: Config, name: str) -> bool:
    n = str(name).strip().lower()
    s = load_settings(cfg.home)
    al = dict(s.get("aliases") or {})
    if n not in al:
        return False
    del al[n]
    s["aliases"] = al
    save_settings(cfg.home, s)
    cfg.aliases.pop(n, None)
    return True


def describe(cfg: Config, catalog: Any, routability: Any = None) -> list[dict[str, Any]]:
    """Each alias with whether its target is in the catalog and usable right now."""
    known = {m.ref: m for m in catalog.models}
    out = []
    for n, ref in sorted(cfg.aliases.items()):
        m = known.get(ref)
        out.append({"alias": n, "model": ref, "in_catalog": m is not None,
                    "priced": bool(m and m.pricing is not None and m.pricing.known),
                    "has_key": ref.split("/", 1)[0] in cfg.api_keys})
    return out
