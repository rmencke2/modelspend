"""Import OpenRouter's model list into your catalog: ids, prices and limits, from OpenRouter's own API.

Why this exists: an OpenAI-style model list gives ids only, so every model needs a hand-typed price, context length and output limit before
ModelSpend will route to it. OpenRouter's models endpoint publishes all of that (USD per token, context length, the top provider's output
limit, the parameters a model accepts), so one command can bring in the models you name, with prices that come from the party that bills you.

What it deliberately does NOT do
- It never imports the whole list: you name the models (`--only`), because the list has hundreds, most of them not what you want.
- It never invents a tier. A tier is your own prior about a model's strength; OpenRouter does not publish one. Models arrive without a tier,
  which means routing will not pick them on a prior, but **shadow evaluation can still measure them** and let evidence decide.
- It never trusts the response: it is untrusted network data. Sizes are capped, every number is checked, names are cleaned, odd entries are
  skipped with a reason, and nothing from the response is executed or stored beyond the fields below.

Verified against: OpenRouter's SDK documentation for the response shape (not yet against the live API, which this sandbox cannot reach).
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

MAX_BYTES = 15 * 1024 * 1024
MAX_PRICE_PER_MTOK = 1000.0  # above this, a price is almost certainly a unit mistake, not a real model
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/-]{0,127}$")
_CTRL = re.compile(r"[\x00-\x1f\x7f<>`]")


class OpenRouterError(ValueError):
    pass


@dataclass
class Entry:
    id: str
    name: str
    context: int | None
    max_output: int | None
    input_per_mtok: float
    output_per_mtok: float
    cached_per_mtok: float | None = None
    cache_write_per_mtok: float | None = None
    free: bool = False
    deprecated: bool = False
    capabilities: dict[str, bool] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


@dataclass
class Skip:
    id: str
    reason: str


def fetch(base_url: str, api_key: str | None = None, timeout: float = 30.0, client: Any = None) -> dict:
    """GET {base_url}/models. The URL is the registry's fixed OpenRouter address, never user input. The body is size-capped."""
    import httpx
    own = client is None
    client = client or httpx.Client(timeout=timeout, follow_redirects=False)
    try:
        headers = {"Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        with client.stream("GET", f"{base_url.rstrip('/')}/models", headers=headers) as r:
            if r.status_code != 200:
                raise OpenRouterError(f"OpenRouter's model list answered HTTP {r.status_code}")
            buf = bytearray()
            for chunk in r.iter_bytes():
                buf += chunk
                if len(buf) > MAX_BYTES:
                    raise OpenRouterError(f"the model list is larger than {MAX_BYTES // 1024 // 1024} MB; refusing to read it")
    except httpx.HTTPError as e:
        raise OpenRouterError(f"could not reach OpenRouter ({type(e).__name__}); check your network, or import a saved response with --file") from None
    finally:
        if own:
            client.close()
    return load_payload(bytes(buf))


def load_payload(raw: bytes | str) -> dict:
    if isinstance(raw, bytes) and len(raw) > MAX_BYTES:
        raise OpenRouterError(f"the file is larger than {MAX_BYTES // 1024 // 1024} MB")
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise OpenRouterError("that is not valid JSON") from None
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise OpenRouterError("unexpected shape: expected an object with a 'data' list of models")
    return data


def _per_mtok(v: Any) -> float | None:
    """OpenRouter publishes USD per TOKEN as a string. None for missing, negative (dynamic pricing) or malformed values."""
    if v is None or isinstance(v, bool) or v == "":
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(x) or x < 0:
        return None
    return round(x * 1_000_000, 6)


def _pos_int(v: Any) -> int | None:
    return int(v) if isinstance(v, int) and not isinstance(v, bool) and v > 0 else None


def _text_only(arch: Any) -> bool:
    if not isinstance(arch, dict):
        return False
    ins, outs = arch.get("input_modalities"), arch.get("output_modalities")
    if isinstance(ins, list) and isinstance(outs, list):
        return "text" in ins and outs == ["text"]
    modality = arch.get("modality")  # older shape: "text->text", "text+image->text"
    if isinstance(modality, str) and "->" in modality:
        left, right = modality.split("->", 1)
        return "text" in left.split("+") and right.strip() == "text"
    return False


def parse(payload: dict) -> tuple[list[Entry], list[Skip]]:
    """Entries ModelSpend can use, plus every model that was skipped and why."""
    entries: list[Entry] = []
    skips: list[Skip] = []
    seen: set[str] = set()
    for m in payload.get("data", []):
        mid = m.get("id") if isinstance(m, dict) else None
        if not isinstance(mid, str) or not _ID.match(mid):
            skips.append(Skip(str(mid)[:60], "unusable id"))
            continue
        if mid in seen:
            continue
        seen.add(mid)
        if not _text_only(m.get("architecture")):
            skips.append(Skip(mid, "not a text-in, text-out model"))
            continue
        pr = m.get("pricing") if isinstance(m.get("pricing"), dict) else {}
        inp, out = _per_mtok(pr.get("prompt")), _per_mtok(pr.get("completion"))
        if inp is None or out is None:
            skips.append(Skip(mid, "no fixed price (dynamic or missing): a router entry, or not priced per token"))
            continue
        if inp > MAX_PRICE_PER_MTOK or out > MAX_PRICE_PER_MTOK:
            skips.append(Skip(mid, f"price over ${MAX_PRICE_PER_MTOK:,.0f} per million tokens looks like a unit mistake"))
            continue
        top = m.get("top_provider") if isinstance(m.get("top_provider"), dict) else {}
        ctx = _pos_int(top.get("context_length")) or _pos_int(m.get("context_length"))
        params = m.get("supported_parameters") if isinstance(m.get("supported_parameters"), list) else []
        caps = {"text": True}
        if "tools" in params:
            caps["tool_calling"] = True
        if "structured_outputs" in params:
            caps["structured_output"] = True
        if "temperature" in params:
            caps["temperature"] = True
        warnings: list[str] = []
        reasoning = _per_mtok(pr.get("internal_reasoning"))
        if reasoning and abs(reasoning - out) > 1e-9:
            warnings.append(f"reasoning tokens are priced separately (${reasoning:g}/M); ModelSpend counts them as output")
        name = _CTRL.sub("", str(m.get("name") or mid))[:100].strip() or mid
        entries.append(Entry(
            id=mid, name=name, context=ctx, max_output=_pos_int(top.get("max_completion_tokens")), input_per_mtok=inp, output_per_mtok=out,
            cached_per_mtok=_per_mtok(pr.get("input_cache_read")), cache_write_per_mtok=_per_mtok(pr.get("input_cache_write")),
            free=mid.endswith(":free") or (inp == 0 and out == 0), deprecated=bool(m.get("expiration_date")), capabilities=caps, warnings=warnings))
    return entries, skips


def select(entries: list[Entry], only: list[str], exact: bool = False, include_free: bool = False, min_context: int = 0) -> tuple[list[Entry], list[Skip]]:
    """The models you asked for (substring match on the id, or the whole id with exact). Free-tier models only when you ask for them."""
    picked: list[Entry] = []
    left: list[Skip] = []
    for e in entries:
        if only and not any((o == e.id.lower()) if exact else (o in e.id.lower()) for o in only):
            continue
        if e.free and not include_free:
            left.append(Skip(e.id, "free-tier model: rate limited and may be logged by the provider; add --include-free to import it"))
        elif min_context and (e.context or 0) < min_context:
            left.append(Skip(e.id, f"context length below {min_context}"))
        else:
            picked.append(e)
    return picked, left


def catalog_entry(e: Entry, today: str, default_max_output: int | None = None) -> dict:
    """A raw `add_models` entry for overrides.json. No tier: that is your call, or shadow evaluation's."""
    limits = {k: v for k, v in (("context_tokens", e.context), ("max_output_tokens", e.max_output or default_max_output)) if v}
    pricing: dict[str, Any] = {"currency": "USD", "input_per_mtok": e.input_per_mtok, "output_per_mtok": e.output_per_mtok, "effective_date": today,
                               "verified_on": today, "source_url": "https://openrouter.ai/api/v1/models", "reasoning_in_output": True}
    if e.cached_per_mtok is not None:
        pricing["cached_input_per_mtok"] = e.cached_per_mtok
    if e.cache_write_per_mtok is not None:
        pricing["cache_write_per_mtok"] = e.cache_write_per_mtok
    out: dict[str, Any] = {"provider": "openrouter", "id": e.id, "display_name": e.name, "availability": "deprecated" if e.deprecated else "available",
                           "verified_on": today, "availability_source_url": f"OpenRouter models API on {today}", "capabilities": dict(e.capabilities), "pricing": pricing}
    if limits:
        out["limits"] = limits
    return out


def merge(existing: dict | None, fresh: dict) -> dict:
    """Refresh an entry you imported before: new prices, limits and availability win; what you set yourself (tier, capabilities you marked) stays."""
    if not existing:
        return fresh
    out = {**existing, **{k: v for k, v in fresh.items() if k not in ("capabilities", "limits")}}
    out["capabilities"] = {**fresh.get("capabilities", {}), **existing.get("capabilities", {})}
    out["limits"] = {**fresh.get("limits", {}), **{k: v for k, v in existing.get("limits", {}).items() if k not in fresh.get("limits", {})}}
    if not out["limits"]:
        out.pop("limits")
    return out
