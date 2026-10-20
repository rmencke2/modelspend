"""Google Gemini adapter using the official `google-genai` SDK (Gemini Developer API).

Usage semantics: `candidates_token_count` EXCLUDES thinking; `thoughts_token_count` is reported
separately and billed as output, so the adapter sums them. `prompt_token_count` INCLUDES cached
content, so cached tokens are split out. Gemini does not always return tool-call ids; ids are
synthesized as `call_<n>` and documented.
LIVE-UNVERIFIED in this repository's CI: tested against recorded-shape fakes only.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

from ..errors import InvalidRequest, ProviderError
from ..types import ToolCall, Usage
from .base import ProviderAdapter, ProviderRequest, ProviderResponse, StreamChunk, StreamEnd, classify_exception

_FINISH = {"STOP": "stop", "MAX_TOKENS": "length", "SAFETY": "content_filter", "RECITATION": "content_filter",
           "BLOCKLIST": "content_filter", "PROHIBITED_CONTENT": "content_filter", "SPII": "content_filter",
           "IMAGE_SAFETY": "content_filter"}


def normalize_usage(u: Any) -> Usage:
    if u is None:
        return Usage()
    prompt = getattr(u, "prompt_token_count", 0) or 0
    cached = getattr(u, "cached_content_token_count", 0) or 0
    cand = getattr(u, "candidates_token_count", 0) or 0
    thoughts = getattr(u, "thoughts_token_count", 0) or 0
    return Usage(input_tokens=max(0, prompt - cached), cache_read_tokens=cached, output_tokens=cand + thoughts, reasoning_tokens=thoughts)


LIST_TIMEOUT_S = 30.0  # the model list and token count are small calls: a stall there is a failure, not something to wait for


class GoogleAdapter(ProviderAdapter):
    """Every network call here is bounded: the google-genai SDK has no default timeout, so without these a stalled request would wait forever
    and ModelSpend's own timeout and failover would never fire (found 2026-10-20 when `verify google` appeared to hang)."""
    name = "google"

    def __init__(self, api_key: str, client: Any = None):
        self._api_key = api_key
        self._client = client

    def _get_client(self) -> Any:
        if self._client is None:
            from google import genai

            self._client = genai.Client(api_key=self._api_key)
        return self._client

    def build_call(self, req: ProviderRequest) -> dict[str, Any]:
        from google.genai import types as T

        if req.tools and req.response_schema:
            raise InvalidRequest("google adapter cannot combine tools with response_schema")
        names: dict[str, str] = {}
        contents = []
        system = "\n\n".join(m.content or "" for m in req.messages if m.role == "system")
        for m in req.messages:
            if m.role == "system":
                continue
            if m.role == "assistant":
                parts = [T.Part(text=m.content)] if m.content else []
                for t in m.tool_calls or []:
                    names[t.id] = t.name
                    parts.append(T.Part(function_call=T.FunctionCall(id=t.id, name=t.name, args=json.loads(t.arguments or "{}"))))
                contents.append(T.Content(role="model", parts=parts))
            elif m.role == "tool":
                nm = m.name or names.get(m.tool_call_id or "", "tool")
                try:
                    payload = json.loads(m.content or "{}")
                    if not isinstance(payload, dict):
                        payload = {"result": payload}
                except json.JSONDecodeError:
                    payload = {"result": m.content}
                contents.append(T.Content(role="user", parts=[T.Part(function_response=T.FunctionResponse(id=m.tool_call_id, name=nm, response=payload))]))
            else:
                contents.append(T.Content(role="user", parts=[T.Part(text=m.content or "")]))
        cfg: dict[str, Any] = {"max_output_tokens": req.max_output_tokens}
        if system:
            cfg["system_instruction"] = system
        if req.temperature is not None:
            cfg["temperature"] = req.temperature
        if req.response_schema:
            cfg["response_mime_type"] = "application/json"
            cfg["response_json_schema"] = req.response_schema
        if req.tools:
            cfg["tools"] = [T.Tool(function_declarations=[
                T.FunctionDeclaration(name=t.name, description=t.description, parameters_json_schema=t.parameters) for t in req.tools])]
        for k, v in req.options.items():
            cfg[k] = v
        return {"model": req.model, "contents": contents, "config": T.GenerateContentConfig(**cfg)}

    @staticmethod
    def _convert(r: Any, counter_start: int = 0) -> ProviderResponse:
        text, calls = [], []
        finish = "stop"
        fb = getattr(r, "prompt_feedback", None)
        if fb is not None and getattr(fb, "block_reason", None):
            finish = "content_filter"
        for c in (getattr(r, "candidates", None) or [])[:1]:
            fr = getattr(c, "finish_reason", None)
            if fr is not None:
                finish = _FINISH.get(getattr(fr, "name", str(fr)), "other")
            for p in (getattr(getattr(c, "content", None), "parts", None) or []):
                if getattr(p, "function_call", None):
                    fc = p.function_call
                    calls.append(ToolCall(id=fc.id or f"call_{counter_start + len(calls)}", name=fc.name, arguments=json.dumps(dict(fc.args or {}))))
                elif getattr(p, "text", None) and not getattr(p, "thought", False):
                    text.append(p.text)
        if calls and finish == "stop":
            finish = "tool_calls"
        return ProviderResponse(text="".join(text), tool_calls=calls, finish_reason=finish,
                                usage=normalize_usage(getattr(r, "usage_metadata", None)),
                                response_id=getattr(r, "response_id", None))

    async def generate(self, req: ProviderRequest) -> ProviderResponse:
        try:
            r = await asyncio.wait_for(self._get_client().aio.models.generate_content(**self.build_call(req)), req.timeout_s)
        except InvalidRequest:
            raise
        except asyncio.TimeoutError:
            raise ProviderError(self.name, "timeout", f"no answer within {req.timeout_s:g}s") from None
        except Exception as e:  # noqa: BLE001
            raise classify_exception(self.name, e) from e
        return self._convert(r)

    async def stream(self, req: ProviderRequest) -> AsyncIterator[StreamChunk | StreamEnd]:
        text, calls, finish, usage = [], [], "stop", None
        try:
            s = await asyncio.wait_for(self._get_client().aio.models.generate_content_stream(**self.build_call(req)), req.timeout_s)
            it = s.__aiter__()
            while True:
                try:
                    ch = await asyncio.wait_for(it.__anext__(), req.timeout_s)  # the wait applies to each chunk, so a stream that stalls midway is caught too
                except StopAsyncIteration:
                    break
                part = self._convert(ch, len(calls))
                if ch is not None and getattr(ch, "usage_metadata", None):
                    usage = ch.usage_metadata
                if part.text:
                    text.append(part.text)
                    yield StreamChunk(text=part.text)
                calls.extend(part.tool_calls)
                if part.finish_reason != "stop" or part.tool_calls:
                    finish = part.finish_reason
        except (InvalidRequest, ProviderError):
            raise
        except asyncio.TimeoutError:
            raise ProviderError(self.name, "timeout", f"the stream stalled for {req.timeout_s:g}s") from None
        except Exception as e:  # noqa: BLE001
            raise classify_exception(self.name, e) from e
        yield StreamEnd(response=ProviderResponse(text="".join(text), tool_calls=calls, finish_reason=finish, usage=normalize_usage(usage)))

    async def list_models(self) -> list[str]:
        async def collect() -> list[str]:
            out = []
            pager = await self._get_client().aio.models.list()
            async for m in pager:
                out.append(m.name.removeprefix("models/"))
            return sorted(out)
        try:
            return await asyncio.wait_for(collect(), LIST_TIMEOUT_S)
        except asyncio.TimeoutError:
            raise ProviderError(self.name, "timeout", f"the model list did not arrive within {LIST_TIMEOUT_S:g}s") from None
        except Exception as e:  # noqa: BLE001
            raise classify_exception(self.name, e) from e

    async def list_model_info(self) -> dict[str, dict]:
        async def collect() -> dict[str, dict]:
            out: dict[str, dict] = {}
            pager = await self._get_client().aio.models.list()
            async for m in pager:
                if "generateContent" not in (getattr(m, "supported_actions", None) or ["generateContent"]):
                    continue
                out[m.name.removeprefix("models/")] = {
                    "display_name": getattr(m, "display_name", None), "context_tokens": getattr(m, "input_token_limit", None),
                    "max_output_tokens": getattr(m, "output_token_limit", None)}
            return out
        try:
            return await asyncio.wait_for(collect(), LIST_TIMEOUT_S)
        except asyncio.TimeoutError:
            raise ProviderError(self.name, "timeout", f"the model list did not arrive within {LIST_TIMEOUT_S:g}s") from None
        except Exception as e:  # noqa: BLE001
            raise classify_exception(self.name, e) from e

    async def count_tokens(self, req: ProviderRequest) -> int | None:
        try:
            call = self.build_call(req)
            r = await asyncio.wait_for(self._get_client().aio.models.count_tokens(model=call["model"], contents=call["contents"]), LIST_TIMEOUT_S)
            return int(r.total_tokens)
        except Exception:  # noqa: BLE001
            return None
