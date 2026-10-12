"""Normalized provider interface."""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any, AsyncIterator

from pydantic import BaseModel, Field

from ..errors import ProviderError
from ..types import Message, ToolCall, ToolSpec, Usage


class ProviderRequest(BaseModel):
    model: str
    messages: list[Message]
    max_output_tokens: int
    temperature: float | None = None
    response_schema: dict[str, Any] | None = None
    tools: list[ToolSpec] = Field(default_factory=list)
    tool_choice: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)  # already-validated namespaced options
    timeout_s: float = 60.0
    # From the catalog: do provider-reported output tokens already include reasoning tokens?
    reasoning_in_output: bool = True


class ProviderResponse(BaseModel):
    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    finish_reason: str = "stop"  # stop | length | tool_calls | content_filter | other
    usage: Usage = Field(default_factory=Usage)
    response_id: str | None = None
    raw_finish_reason: str | None = None


class StreamChunk(BaseModel):
    text: str


class StreamEnd(BaseModel):
    response: ProviderResponse


class ProviderAdapter(ABC):
    name: str

    def __repr__(self) -> str:  # never include credentials
        return f"<{type(self).__name__} {self.name}>"

    @abstractmethod
    async def generate(self, req: ProviderRequest) -> ProviderResponse: ...

    @abstractmethod
    def stream(self, req: ProviderRequest) -> AsyncIterator[StreamChunk | StreamEnd]: ...

    async def list_models(self) -> list[str]:
        raise NotImplementedError

    async def count_tokens(self, req: ProviderRequest) -> int | None:
        return None

    async def list_model_info(self) -> dict[str, dict]:
        """id -> whatever metadata the provider's list endpoint exposes (never prices). Default: IDs only."""
        return {i: {} for i in await self.list_models()}


# Account has no credit / hit a spending cap: retrying the same provider cannot help (unlike a transient 429).
_QUOTA_MARKERS = ("insufficient_quota", "credit_balance", "credit balance", "exceeded your current quota", "billing_hard_limit")


def classify_exception(provider: str, exc: BaseException) -> ProviderError:
    """Map SDK exceptions onto normalized kinds without importing the SDKs here."""
    if isinstance(exc, ProviderError):
        return exc
    name = type(exc).__name__
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    status = status if isinstance(status, int) else None
    from ..redact import scrub
    msg = scrub(str(exc))[:300]  # scrub BEFORE truncating, so a cut can never leave half a key behind
    low = name.lower()
    text = msg.lower()
    if any(m in text for m in _QUOTA_MARKERS):
        kind = "quota"
    elif "timeout" in low or "timedout" in low:
        kind = "timeout"
    elif "connection" in low or "connect" in low:
        kind = "connection"
    elif status in (401, 403):
        kind = "auth"
    elif status == 429 or "ratelimit" in low:
        kind = "rate_limit"
    elif status in (408, 409):
        kind = "timeout" if status == 408 else "server"
    elif status is not None and status >= 500:
        kind = "server"
    elif status in (400, 404, 413, 422):
        kind = "bad_request"
    else:
        kind = "unknown"
    return ProviderError(provider, kind, f"{name}: {msg}", status)


def is_strict_schema(schema: dict[str, Any]) -> bool:
    """True if the schema meets OpenAI strict-mode rules (all props required, no extras)."""
    if schema.get("type") == "object":
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is not False:
            return False
        if set(schema.get("required", [])) != set(props):
            return False
        return all(is_strict_schema(v) for v in props.values())
    if schema.get("type") == "array":
        return is_strict_schema(schema.get("items", {}))
    return True


def safe_json_loads(s: str) -> Any:
    try:
        return json.loads(s)
    except Exception:
        return {"_raw": s}
