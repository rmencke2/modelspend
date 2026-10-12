"""Anthropic Messages adapter using the official `anthropic` SDK.

Structured output is implemented with a forced tool whose input_schema is the caller's schema
(a documented technique); the tool input is returned as the JSON text, not as a tool call.
LIVE-UNVERIFIED in this repository's CI: tested against recorded-shape fakes only.
"""
from __future__ import annotations

import json
from typing import Any, AsyncIterator

from ..errors import InvalidRequest, ProviderError
from ..types import ToolCall, Usage
from .base import ProviderAdapter, ProviderRequest, ProviderResponse, StreamChunk, StreamEnd, classify_exception

STRUCT_TOOL = "structured_response"
_STOP = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length", "tool_use": "tool_calls",
         "refusal": "content_filter", "pause_turn": "other"}


def normalize_usage(u: Any) -> Usage:
    if u is None:
        return Usage()
    # Anthropic reports input_tokens EXCLUDING cache reads/writes; output_tokens include thinking.
    return Usage(
        input_tokens=getattr(u, "input_tokens", 0) or 0,
        cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
        cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
        output_tokens=getattr(u, "output_tokens", 0) or 0,
    )


def _blocks(m, tool_names: dict[str, str]) -> dict[str, Any]:
    if m.role == "tool":
        return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.content or ""}]}
    if m.role == "assistant" and m.tool_calls:
        blocks: list[dict[str, Any]] = []
        if m.content:
            blocks.append({"type": "text", "text": m.content})
        for t in m.tool_calls:
            try:
                args = json.loads(t.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            blocks.append({"type": "tool_use", "id": t.id, "name": t.name, "input": args})
        return {"role": "assistant", "content": blocks}
    return {"role": m.role, "content": m.content or ""}


class AnthropicAdapter(ProviderAdapter):
    name = "anthropic"

    def __init__(self, api_key: str, client: Any = None):
        self._api_key = api_key
        self._client = client

    def _get_client(self) -> Any:
        if self._client is None:
            from anthropic import AsyncAnthropic

            self._client = AsyncAnthropic(api_key=self._api_key, max_retries=0)
        return self._client

    def build_params(self, req: ProviderRequest) -> dict[str, Any]:
        if req.tools and req.response_schema:
            raise InvalidRequest("anthropic adapter cannot combine tools with response_schema")
        system = "\n\n".join(m.content or "" for m in req.messages if m.role == "system")
        msgs = []
        for m in req.messages:
            if m.role == "system":
                continue
            b = _blocks(m, {})
            # merge consecutive tool_result user turns into one user message
            if msgs and b["role"] == "user" and msgs[-1]["role"] == "user" and isinstance(b["content"], list) and isinstance(msgs[-1]["content"], list):
                msgs[-1]["content"].extend(b["content"])
            else:
                msgs.append(b)
        p: dict[str, Any] = {"model": req.model, "messages": msgs, "max_tokens": req.max_output_tokens, "timeout": req.timeout_s}
        if system:
            p["system"] = system
        if req.temperature is not None:
            p["temperature"] = req.temperature
        if req.tools:
            p["tools"] = [{"name": t.name, "description": t.description, "input_schema": t.parameters} for t in req.tools]
            if req.tool_choice:
                p["tool_choice"] = {"auto": {"type": "auto"}, "required": {"type": "any"}, "none": {"type": "none"}}.get(
                    req.tool_choice, {"type": "tool", "name": req.tool_choice})
        if req.response_schema:
            p["tools"] = [{"name": STRUCT_TOOL, "description": "Return the final answer in this schema.", "input_schema": req.response_schema}]
            p["tool_choice"] = {"type": "tool", "name": STRUCT_TOOL}
        p.update(req.options)
        return p

    @staticmethod
    def _convert(msg: Any) -> ProviderResponse:
        text, calls, structured = [], [], None
        for b in msg.content:
            if b.type == "text":
                text.append(b.text)
            elif b.type == "tool_use":
                if b.name == STRUCT_TOOL:
                    structured = json.dumps(b.input)
                else:
                    calls.append(ToolCall(id=b.id, name=b.name, arguments=json.dumps(b.input)))
        finish = _STOP.get(msg.stop_reason or "end_turn", "other")
        if structured is not None:
            finish = "stop" if finish == "tool_calls" else finish
        return ProviderResponse(
            text=structured if structured is not None else "".join(text), tool_calls=calls, finish_reason=finish,
            usage=normalize_usage(msg.usage), response_id=getattr(msg, "id", None), raw_finish_reason=msg.stop_reason,
        )

    async def generate(self, req: ProviderRequest) -> ProviderResponse:
        params = self.build_params(req)
        try:
            msg = await self._get_client().messages.create(**params)
        except Exception as e:  # noqa: BLE001
            raise classify_exception(self.name, e) from e
        return self._convert(msg)

    async def stream(self, req: ProviderRequest) -> AsyncIterator[StreamChunk | StreamEnd]:
        params = self.build_params(req)
        try:
            async with self._get_client().messages.stream(**params) as s:
                async for ev in s:
                    if ev.type == "content_block_delta":
                        d = ev.delta
                        if d.type == "text_delta":
                            yield StreamChunk(text=d.text)
                        elif d.type == "input_json_delta" and req.response_schema:
                            yield StreamChunk(text=d.partial_json)
                final = await s.get_final_message()
        except ProviderError:
            raise
        except Exception as e:  # noqa: BLE001
            raise classify_exception(self.name, e) from e
        yield StreamEnd(response=self._convert(final))

    async def list_models(self) -> list[str]:
        try:
            page = await self._get_client().models.list(limit=100)
        except Exception as e:  # noqa: BLE001
            raise classify_exception(self.name, e) from e
        return sorted(m.id for m in page.data)

    async def count_tokens(self, req: ProviderRequest) -> int | None:
        p = self.build_params(req)
        kw = {k: p[k] for k in ("model", "messages", "system", "tools") if k in p}
        try:
            r = await self._get_client().messages.count_tokens(**kw)
            return int(r.input_tokens)
        except Exception:  # noqa: BLE001
            return None
