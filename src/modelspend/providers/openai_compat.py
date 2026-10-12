"""OpenAI Chat Completions adapter; also serves xAI, whose API is OpenAI-compatible
(base_url https://api.x.ai/v1). Uses the official `openai` SDK.

LIVE-UNVERIFIED in this repository's CI: tested against recorded-shape fakes only.
"""
from __future__ import annotations

from typing import Any, AsyncIterator

from ..errors import ProviderError
from ..types import ToolCall, Usage
from .base import (
    ProviderAdapter, ProviderRequest, ProviderResponse, StreamChunk, StreamEnd, classify_exception, is_strict_schema,
)

_FINISH = {"stop": "stop", "length": "length", "tool_calls": "tool_calls", "function_call": "tool_calls",
           "content_filter": "content_filter"}


def _g(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def normalize_usage(u: Any, reasoning_in_output: bool) -> Usage:
    if u is None:
        return Usage()
    prompt = _g(u, "prompt_tokens", 0) or 0
    completion = _g(u, "completion_tokens", 0) or 0
    cached = _g(_g(u, "prompt_tokens_details"), "cached_tokens", 0) or 0
    reasoning = _g(_g(u, "completion_tokens_details"), "reasoning_tokens", 0) or 0
    # prompt_tokens includes cached tokens; split them so each is billed once.
    out = completion if reasoning_in_output else completion + reasoning
    return Usage(input_tokens=max(0, prompt - cached), cache_read_tokens=cached, output_tokens=out, reasoning_tokens=reasoning)


class OpenAICompatAdapter(ProviderAdapter):
    name = "openai"
    base_url: str | None = None
    max_tokens_param = "max_completion_tokens"

    def __init__(self, api_key: str, client: Any = None, base_url: str | None = None):
        self._api_key = api_key
        self._client = client
        self._base_url = base_url or self.base_url

    def _get_client(self) -> Any:
        if self._client is None:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(api_key=self._api_key, base_url=self._base_url, max_retries=0)
        return self._client

    # ---- request building -----------------------------------------------------------
    def build_params(self, req: ProviderRequest, stream: bool = False) -> dict[str, Any]:
        msgs: list[dict[str, Any]] = []
        for m in req.messages:
            d: dict[str, Any] = {"role": m.role, "content": m.content if m.content is not None else ""}
            if m.tool_calls:
                d["tool_calls"] = [
                    {"id": t.id, "type": "function", "function": {"name": t.name, "arguments": t.arguments}}
                    for t in m.tool_calls
                ]
                if not m.content:
                    d["content"] = None
            if m.role == "tool":
                d["tool_call_id"] = m.tool_call_id
            if m.name and m.role != "tool":
                d["name"] = m.name
            msgs.append(d)
        p: dict[str, Any] = {"model": req.model, "messages": msgs, self.max_tokens_param: req.max_output_tokens}
        if req.temperature is not None:
            p["temperature"] = req.temperature
        if req.response_schema:
            p["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "response", "schema": req.response_schema, "strict": is_strict_schema(req.response_schema)},
            }
        if req.tools:
            p["tools"] = [{"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}} for t in req.tools]
            if req.tool_choice:
                p["tool_choice"] = req.tool_choice if req.tool_choice in ("auto", "none", "required") else {"type": "function", "function": {"name": req.tool_choice}}
        p.update(req.options)
        if stream:
            p["stream"] = True
            p["stream_options"] = {"include_usage": True}
        p["timeout"] = req.timeout_s
        return p

    # ---- calls ----------------------------------------------------------------------
    async def generate(self, req: ProviderRequest) -> ProviderResponse:
        try:
            r = await self._get_client().chat.completions.create(**self.build_params(req))
        except Exception as e:  # noqa: BLE001
            raise classify_exception(self.name, e) from e
        choice = r.choices[0]
        msg = choice.message
        calls = [ToolCall(id=t.id, name=t.function.name, arguments=t.function.arguments) for t in (msg.tool_calls or [])]
        text = msg.content or ""
        finish = _FINISH.get(choice.finish_reason or "stop", "other")
        if getattr(msg, "refusal", None) and not text:
            finish, text = "content_filter", msg.refusal
        return ProviderResponse(
            text=text, tool_calls=calls, finish_reason=finish, response_id=getattr(r, "id", None),
            usage=normalize_usage(r.usage, req.reasoning_in_output), raw_finish_reason=choice.finish_reason,
        )

    async def stream(self, req: ProviderRequest) -> AsyncIterator[StreamChunk | StreamEnd]:
        text, calls, finish, usage, rid = [], {}, "stop", None, None
        try:
            s = await self._get_client().chat.completions.create(**self.build_params(req, stream=True))
            async for ch in s:
                rid = rid or getattr(ch, "id", None)
                if getattr(ch, "usage", None):
                    usage = ch.usage
                if not ch.choices:
                    continue
                c = ch.choices[0]
                d = c.delta
                if d is not None and d.content:
                    text.append(d.content)
                    yield StreamChunk(text=d.content)
                for tc in (getattr(d, "tool_calls", None) or []):
                    slot = calls.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
                    slot["id"] = tc.id or slot["id"]
                    if tc.function:
                        slot["name"] += tc.function.name or ""
                        slot["arguments"] += tc.function.arguments or ""
                if c.finish_reason:
                    finish = _FINISH.get(c.finish_reason, "other")
        except ProviderError:
            raise
        except Exception as e:  # noqa: BLE001
            raise classify_exception(self.name, e) from e
        yield StreamEnd(response=ProviderResponse(
            text="".join(text), finish_reason=finish, response_id=rid,
            tool_calls=[ToolCall(**calls[i]) for i in sorted(calls)],
            usage=normalize_usage(usage, req.reasoning_in_output),
        ))

    async def list_models(self) -> list[str]:
        try:
            page = await self._get_client().models.list()
        except Exception as e:  # noqa: BLE001
            raise classify_exception(self.name, e) from e
        return sorted(m.id for m in page.data)


class XAIAdapter(OpenAICompatAdapter):
    name = "xai"
    base_url = "https://api.x.ai/v1"
