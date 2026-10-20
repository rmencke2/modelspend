"""Adapters tested against fakes that mirror the official SDK response shapes.
These do NOT prove live behaviour; see docs/limitations.md."""
import asyncio
import json
import time
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


def test_no_credit_is_a_quota_error_whatever_wording_the_provider_uses():
    """Found live (DeepSeek, 2026-10-20): HTTP 402 'Insufficient Balance' was classified 'unknown', which does not fail over to another provider."""
    e402 = type("APIStatusError", (Exception,), {"status_code": 402})("Error code: 402 - {'error': {'message': 'Insufficient Balance'}}")
    k = classify_exception("deepseek", e402)
    assert k.kind == "quota" and k.status_code == 402 and k.kind in ProviderError.FAILOVER and not k.retryable
    assert classify_exception("x", type("E", (Exception,), {"status_code": 402})("pay up")).kind == "quota"  # the status alone is enough
    assert classify_exception("x", type("E", (Exception,), {"status_code": 400})("Insufficient Balance")).kind == "quota"  # and so is the wording alone
    assert classify_exception("x", type("E", (Exception,), {"status_code": 400})("model not found")).kind == "bad_request"  # nothing else changed


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


def test_google_style_errors_are_classified_by_what_they_mean_not_by_their_status():
    """Found rehearsing Gemini with the real SDK (2026-10-20): a bad key is HTTP 400, and a per-minute limit uses the 'exceeded your current quota' wording."""
    bad_key = type("ClientError", (Exception,), {"code": 400})("400 INVALID_ARGUMENT. {'error': {'message': 'API key not valid. Please pass a valid API key.', 'status': 'INVALID_ARGUMENT'}}")
    k = classify_exception("google", bad_key)
    assert k.kind == "auth" and k.kind in ProviderError.FAILOVER  # so a broken Google key fails over instead of failing the request
    assert classify_exception("google", type("E", (Exception,), {"code": 400})("API key expired. Please renew the API key.")).kind == "auth"
    free_tier = type("ClientError", (Exception,), {"code": 429})("429 RESOURCE_EXHAUSTED. You exceeded your current quota, please check your plan and billing details. Please retry in 22.5s.")
    rl = classify_exception("google", free_tier)
    assert rl.kind == "rate_limit" and rl.retryable  # a limit that says when to try again is not an empty account
    daily = type("ClientError", (Exception,), {"code": 429})("429 RESOURCE_EXHAUSTED. You exceeded your current quota, please check your plan and billing details.")
    assert classify_exception("google", daily).kind == "quota"  # no retry hint: still treated as no credit
    assert classify_exception("openai", type("E", (Exception,), {"status_code": 429})("insufficient_quota: you exceeded your current quota")).kind == "quota"


def test_the_retry_hint_survives_a_long_message_because_the_classifier_reads_the_whole_of_it():
    """Found by the second Gemini rehearsal: the hint sits after ~400 characters of explanation and used to be cut off, so a rate limit read as no credit."""
    long = "429 RESOURCE_EXHAUSTED. You exceeded your current quota, please check your plan and billing details. " + "More detail about quotas and limits. " * 12 + "Please retry in 22.5s."
    assert len(long) > 450
    e = classify_exception("google", type("ClientError", (Exception,), {"code": 429})(long))
    assert e.kind == "rate_limit" and e.retry_after == 22.5 and len(e.message) < 400
    assert classify_exception("google", type("ClientError", (Exception,), {"code": 429})("429 RESOURCE_EXHAUSTED. You exceeded your current quota.")).retry_after is None


# ---- the Gemini adapter never waits forever ----
class HangingGoogle(FakeGoogle):
    """A client whose calls never finish (the google-genai SDK has no default timeout), or whose stream stalls after its first chunk."""

    def __init__(self, hang="generate"):
        super().__init__()
        self.hang = hang
        self.aio = NS(models=NS(generate_content=self._gen, generate_content_stream=self._stream, count_tokens=self._count, list=self._list))

    async def _gen(self, **kw):
        await asyncio.sleep(3)

    async def _stream(self, **kw):
        if self.hang == "stream_start":
            await asyncio.sleep(3)

        async def chunks():
            yield g_response([NS(text="a", thought=False, function_call=None)], usage=False)
            await asyncio.sleep(3)  # stalls midway
            yield g_response([NS(text="b", thought=False, function_call=None)])
        return chunks()

    async def _list(self, **kw):
        await asyncio.sleep(3)

    async def _count(self, **kw):
        await asyncio.sleep(3)


async def test_google_generate_gives_up_after_the_request_timeout_instead_of_waiting_forever():
    with pytest.raises(ProviderError) as ei:
        await GoogleAdapter("k", client=HangingGoogle()).generate(preq(timeout_s=0.05))
    assert ei.value.kind == "timeout" and ei.value.retryable and "0.05" in ei.value.message  # retryable, so ModelSpend's own failover can act on it


async def test_google_stream_times_out_whether_it_never_starts_or_stalls_midway():
    got = []
    with pytest.raises(ProviderError) as ei:
        async for e in GoogleAdapter("k", client=HangingGoogle("stream_start")).stream(preq(timeout_s=0.05)):
            got.append(e)
    assert ei.value.kind == "timeout" and got == []
    got2 = []
    with pytest.raises(ProviderError) as ei2:
        async for e in GoogleAdapter("k", client=HangingGoogle("midway")).stream(preq(timeout_s=0.05)):
            got2.append(e)
    assert ei2.value.kind == "timeout" and "stalled" in ei2.value.message and [x.text for x in got2] == ["a"]  # what arrived before the stall is still delivered


async def test_google_list_and_count_calls_are_bounded_too(monkeypatch):
    import modelspend.providers.google_ as g
    monkeypatch.setattr(g, "LIST_TIMEOUT_S", 0.05)
    a = GoogleAdapter("k", client=HangingGoogle())
    for call in (a.list_models, a.list_model_info):
        with pytest.raises(ProviderError) as ei:
            await call()
        assert ei.value.kind == "timeout"
    t0 = time.monotonic()
    assert await a.count_tokens(preq()) is None  # counting is best effort: a stall just means no count
    assert time.monotonic() - t0 < 1.0  # ... and it must not take the stall's whole length to find that out


def test_a_limit_of_zero_is_recognised_even_when_it_sits_past_the_truncation_point():
    """Google's free-tier message names the metric first and 'limit: 0' about 400 characters in."""
    long = ("429 RESOURCE_EXHAUSTED. You exceeded your current quota, please check your plan and billing details. " + "More detail about quotas. " * 12
            + "* Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 0, model: gemini-2.5-flash Please retry in 4.2s.")
    zero = classify_exception("google", type("ClientError", (Exception,), {"code": 429})(long))
    assert zero.zero_quota and zero.kind == "rate_limit" and zero.retry_after == 4.2
    five = classify_exception("google", type("ClientError", (Exception,), {"code": 429})(long.replace("limit: 0,", "limit: 5,")))
    assert not five.zero_quota and five.kind == "rate_limit"
    assert not classify_exception("openai", type("E", (Exception,), {"status_code": 400})("model not found")).zero_quota
