"""Adapters tested against fakes that mirror the official SDK response shapes.
These do NOT prove live behaviour; see docs/limitations.md."""
import json
from types import SimpleNamespace as NS

import pytest

from modelspend.errors import InvalidRequest, ProviderError
from modelspend.providers import AnthropicAdapter, GoogleAdapter, OpenAICompatAdapter, XAIAdapter
from modelspend.providers.base import ProviderRequest, StreamChunk, StreamEnd, classify_exception, is_strict_schema
from modelspend.types import Message, ToolCall, ToolSpec


def preq(**kw):
    kw.setdefault("model", "m")
    kw.setdefault("messages", [Message(role="system", content="sys"), Message(role="user", content="hi")])
    kw.setdefault("max_output_tokens", 50)
    return ProviderRequest(**kw)


async def agen(items):
    for i in items:
        yield i


# ------------------------------------------------------------------ OpenAI / xAI
class FakeOpenAI:
    def __init__(self, response=None, stream=None, exc=None):
        self.calls = []
        self.chat = NS(completions=NS(create=self._create))
        self.models = NS(list=self._list)
        self._r, self._s, self._exc = response, stream, exc

    async def _create(self, **kw):
        self.calls.append(kw)
        if self._exc:
            raise self._exc
        return agen(self._s) if kw.get("stream") else self._r

    async def _list(self):
        return NS(data=[NS(id="b"), NS(id="a")])


def oa_response(text="hello", finish="stop", tool_calls=None, refusal=None):
    msg = NS(content=text, tool_calls=tool_calls, refusal=refusal)
    usage = NS(prompt_tokens=100, completion_tokens=20, prompt_tokens_details=NS(cached_tokens=40),
               completion_tokens_details=NS(reasoning_tokens=5))
    return NS(id="r1", choices=[NS(message=msg, finish_reason=finish)], usage=usage)


async def test_openai_generate_and_params():
    c = FakeOpenAI(oa_response())
    a = OpenAICompatAdapter("k", client=c)
    r = await a.generate(preq(temperature=0.2, response_schema={"type": "object", "properties": {"a": {"type": "string"}},
                                                                "required": ["a"], "additionalProperties": False},
                              options={"seed": 1}))
    p = c.calls[0]
    assert p["max_completion_tokens"] == 50 and p["temperature"] == 0.2 and p["seed"] == 1
    assert p["response_format"]["json_schema"]["strict"] is True
    assert p["messages"][0] == {"role": "system", "content": "sys"}
    assert (r.usage.input_tokens, r.usage.cache_read_tokens, r.usage.output_tokens) == (60, 40, 20)
    assert r.text == "hello" and r.finish_reason == "stop"


async def test_openai_tool_calls_preserve_ids():
    tcs = [NS(id="call_abc", function=NS(name="get", arguments='{"x":1}'))]
    a = OpenAICompatAdapter("k", client=FakeOpenAI(oa_response(text=None, finish="tool_calls", tool_calls=tcs)))
    r = await a.generate(preq(tools=[ToolSpec(name="get")]))
    assert r.tool_calls == [ToolCall(id="call_abc", name="get", arguments='{"x":1}')] and r.finish_reason == "tool_calls"


async def test_openai_tool_roundtrip_message_shape():
    c = FakeOpenAI(oa_response())
    msgs = [Message(role="user", content="q"),
            Message(role="assistant", tool_calls=[ToolCall(id="c1", name="f", arguments="{}")]),
            Message(role="tool", tool_call_id="c1", content="42")]
    await OpenAICompatAdapter("k", client=c).generate(preq(messages=msgs, tools=[ToolSpec(name="f")]))
    sent = c.calls[0]["messages"]
    assert sent[1]["tool_calls"][0]["id"] == "c1" and sent[2] == {"role": "tool", "content": "42", "tool_call_id": "c1"}


async def test_openai_refusal_maps_to_content_filter():
    a = OpenAICompatAdapter("k", client=FakeOpenAI(oa_response(text="", refusal="no")))
    assert (await a.generate(preq())).finish_reason == "content_filter"


async def test_openai_stream_with_usage():
    chunks = [NS(id="s", usage=None, choices=[NS(delta=NS(content="he", tool_calls=None), finish_reason=None)]),
              NS(id="s", usage=None, choices=[NS(delta=NS(content="llo", tool_calls=None), finish_reason="stop")]),
              NS(id="s", usage=NS(prompt_tokens=10, completion_tokens=2, prompt_tokens_details=None, completion_tokens_details=None), choices=[])]
    c = FakeOpenAI(stream=chunks)
    evs = [e async for e in OpenAICompatAdapter("k", client=c).stream(preq())]
    assert c.calls[0]["stream_options"] == {"include_usage": True}
    assert "".join(e.text for e in evs if isinstance(e, StreamChunk)) == "hello"
    end = evs[-1]
    assert isinstance(end, StreamEnd) and end.response.usage.output_tokens == 2


async def test_xai_uses_xai_base_url_and_reasoning_flag():
    a = XAIAdapter("k", client=FakeOpenAI(oa_response()))
    assert a.name == "xai" and a._base_url == "https://api.x.ai/v1"
    r = await a.generate(preq(reasoning_in_output=False))
    assert r.usage.output_tokens == 25  # completion 20 + separately reported reasoning 5


async def test_error_normalization():
    class RateLimitError(Exception):
        status_code = 429
    a = OpenAICompatAdapter("k", client=FakeOpenAI(exc=RateLimitError("slow down")))
    with pytest.raises(ProviderError) as ei:
        await a.generate(preq())
    assert ei.value.kind == "rate_limit" and ei.value.retryable
    for status, kind in [(401, "auth"), (500, "server"), (400, "bad_request")]:
        e = type("E", (Exception,), {"status_code": status})("x")
        assert classify_exception("openai", e).kind == kind
    quota = type("RateLimitError", (Exception,), {"status_code": 429})("Error code: 429 - {'code': 'credit_balance_exhausted', 'type': 'insufficient_quota'}")
    err = classify_exception("openai", quota)
    assert err.kind == "quota" and not err.retryable and err.kind in ProviderError.FAILOVER  # no credits: never retried, may fail over
    assert classify_exception("x", type("APITimeoutError", (Exception,), {})("t")).kind == "timeout"


def test_api_key_never_in_repr_or_error():
    a = OpenAICompatAdapter("sk-SECRET-123", client=FakeOpenAI(exc=Exception("boom")))
    assert "SECRET" not in repr(a)


def test_strict_schema_detection():
    assert is_strict_schema({"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"], "additionalProperties": False})
    assert not is_strict_schema({"type": "object", "properties": {"a": {"type": "string"}}})


async def test_list_models():
    assert await OpenAICompatAdapter("k", client=FakeOpenAI()).list_models() == ["a", "b"]


# ------------------------------------------------------------------ Anthropic
class FakeAnthropic:
    def __init__(self, message=None, events=None, final=None):
        self.calls = []
        self.messages = NS(create=self._create, stream=self._stream, count_tokens=self._count)
        self._m, self._events, self._final = message, events, final

    async def _create(self, **kw):
        self.calls.append(kw)
        return self._m

    async def _count(self, **kw):
        return NS(input_tokens=77)

    def _stream(self, **kw):
        self.calls.append(kw)
        outer = self

        class CM:
            async def __aenter__(s):
                s.events = agen(outer._events)
                return s

            async def __aexit__(s, *a):
                return False

            def __aiter__(s):
                return s.events.__aiter__()

            async def get_final_message(s):
                return outer._final
        return CM()


def an_message(blocks, stop="end_turn"):
    return NS(id="m1", content=blocks, stop_reason=stop,
              usage=NS(input_tokens=100, output_tokens=30, cache_read_input_tokens=50, cache_creation_input_tokens=10))


async def test_anthropic_generate_system_and_usage():
    c = FakeAnthropic(an_message([NS(type="text", text="hi there")]))
    r = await AnthropicAdapter("k", client=c).generate(preq())
    p = c.calls[0]
    assert p["system"] == "sys" and p["max_tokens"] == 50 and p["messages"] == [{"role": "user", "content": "hi"}]
    assert (r.usage.input_tokens, r.usage.cache_read_tokens, r.usage.cache_write_tokens, r.usage.output_tokens) == (100, 50, 10, 30)
    assert r.text == "hi there"


async def test_anthropic_structured_output_via_forced_tool():
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    c = FakeAnthropic(an_message([NS(type="tool_use", id="t1", name="structured_response", input={"a": "x"})], "tool_use"))
    r = await AnthropicAdapter("k", client=c).generate(preq(response_schema=schema))
    assert c.calls[0]["tool_choice"] == {"type": "tool", "name": "structured_response"}
    assert json.loads(r.text) == {"a": "x"} and r.tool_calls == [] and r.finish_reason == "stop"


async def test_anthropic_tool_use_preserves_ids_and_results_roundtrip():
    c = FakeAnthropic(an_message([NS(type="tool_use", id="toolu_9", name="f", input={"q": 1})], "tool_use"))
    a = AnthropicAdapter("k", client=c)
    r = await a.generate(preq(tools=[ToolSpec(name="f")]))
    assert r.tool_calls[0].id == "toolu_9" and r.finish_reason == "tool_calls"
    msgs = [Message(role="user", content="q"), Message(role="assistant", tool_calls=r.tool_calls),
            Message(role="tool", tool_call_id="toolu_9", content="ans")]
    p = a.build_params(preq(messages=msgs, tools=[ToolSpec(name="f")]))
    assert p["messages"][1]["content"][0] == {"type": "tool_use", "id": "toolu_9", "name": "f", "input": {"q": 1}}
    assert p["messages"][2]["content"][0]["tool_use_id"] == "toolu_9"


def test_anthropic_rejects_tools_plus_schema():
    with pytest.raises(InvalidRequest):
        AnthropicAdapter("k", client=FakeAnthropic()).build_params(preq(tools=[ToolSpec(name="f")], response_schema={"type": "object"}))


async def test_anthropic_stream_and_refusal_mapping():
    ev = [NS(type="content_block_delta", delta=NS(type="text_delta", text="a")),
          NS(type="content_block_delta", delta=NS(type="text_delta", text="b")),
          NS(type="message_stop")]
    c = FakeAnthropic(events=ev, final=an_message([NS(type="text", text="ab")]))
    out = [e async for e in AnthropicAdapter("k", client=c).stream(preq())]
    assert [e.text for e in out if isinstance(e, StreamChunk)] == ["a", "b"] and out[-1].response.text == "ab"
    r = await AnthropicAdapter("k", client=FakeAnthropic(an_message([NS(type="text", text="")], "refusal"))).generate(preq())
    assert r.finish_reason == "content_filter"


async def test_anthropic_count_tokens():
    assert await AnthropicAdapter("k", client=FakeAnthropic()).count_tokens(preq()) == 77


# ------------------------------------------------------------------ Google
class FakeGoogle:
    def __init__(self, response=None, chunks=None):
        self.calls = []
        self.aio = NS(models=NS(generate_content=self._gen, generate_content_stream=self._stream, count_tokens=self._count))
        self._r, self._c = response, chunks

    async def _gen(self, **kw):
        self.calls.append(kw)
        return self._r

    async def _stream(self, **kw):
        self.calls.append(kw)
        return agen(self._c)

    async def _count(self, **kw):
        return NS(total_tokens=12)


def g_response(parts, finish="STOP", usage=True):
    cand = NS(content=NS(parts=parts), finish_reason=NS(name=finish))
    u = NS(prompt_token_count=100, cached_content_token_count=20, candidates_token_count=10, thoughts_token_count=30) if usage else None
    return NS(candidates=[cand], usage_metadata=u, prompt_feedback=None, response_id="g1")


async def test_google_generate_params_and_usage():
    c = FakeGoogle(g_response([NS(text="yo", thought=False, function_call=None)]))
    r = await GoogleAdapter("k", client=c).generate(preq(temperature=0.1, response_schema={"type": "object"}))
    cfg = c.calls[0]["config"]
    assert cfg.max_output_tokens == 50 and cfg.system_instruction == "sys" and cfg.response_mime_type == "application/json"
    assert cfg.response_json_schema == {"type": "object"}
    assert (r.usage.input_tokens, r.usage.cache_read_tokens, r.usage.output_tokens, r.usage.reasoning_tokens) == (80, 20, 40, 30)
    assert r.text == "yo"


async def test_google_function_call_ids_and_thought_parts_skipped():
    parts = [NS(text="thinking...", thought=True, function_call=None),
             NS(text=None, thought=False, function_call=NS(id=None, name="f", args={"a": 1}))]
    r = await GoogleAdapter("k", client=FakeGoogle(g_response(parts))).generate(preq(tools=[ToolSpec(name="f")]))
    assert r.text == "" and r.tool_calls[0].id == "call_0" and json.loads(r.tool_calls[0].arguments) == {"a": 1}
    assert r.finish_reason == "tool_calls"


async def test_google_safety_block_maps_to_content_filter():
    r = await GoogleAdapter("k", client=FakeGoogle(g_response([], finish="SAFETY"))).generate(preq())
    assert r.finish_reason == "content_filter"


async def test_google_stream_accumulates_and_reports_usage():
    chunks = [g_response([NS(text="a", thought=False, function_call=None)], usage=False),
              g_response([NS(text="b", thought=False, function_call=None)])]
    out = [e async for e in GoogleAdapter("k", client=FakeGoogle(chunks=chunks)).stream(preq())]
    assert "".join(e.text for e in out if isinstance(e, StreamChunk)) == "ab"
    assert out[-1].response.usage.output_tokens == 40


async def test_google_rejects_tools_plus_schema_and_counts_tokens():
    a = GoogleAdapter("k", client=FakeGoogle())
    with pytest.raises(InvalidRequest):
        await a.generate(preq(tools=[ToolSpec(name="f")], response_schema={"type": "object"}))
    assert await a.count_tokens(preq()) == 12
