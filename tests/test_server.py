import json

import pytest
from fastapi.testclient import TestClient

from modelspend.providers.base import ProviderResponse
from modelspend.server.app import check_bind, create_app
from modelspend.types import ToolCall, Usage

from conftest import make_engine


def client(catalog, cfg, scripts=None, token=None, store=None):
    cfg.api_token = token
    cfg.default_providers = ["openai", "anthropic"]
    eng, ad = make_engine(catalog, cfg, scripts, store=store)
    return TestClient(create_app(eng, cfg)), ad, eng


BODY = {"model": "auto", "messages": [{"role": "user", "content": "Classify: great"}]}


def test_chat_completion_shape_and_extension(catalog, cfg, store):
    c, ad, _ = client(catalog, cfg, {"openai": "positive", "anthropic": "positive"}, store=store)
    r = c.post("/v1/chat/completions", json={**BODY, "modelspend": {"providers": ["openai"], "max_budget_usd": 0.05}})
    j = r.json()
    assert r.status_code == 200 and j["object"] == "chat.completion"
    assert j["choices"][0]["message"] == {"role": "assistant", "content": "positive"}
    assert j["choices"][0]["finish_reason"] == "stop" and j["model"].startswith("synthetic-openai")
    assert set(j["usage"]) >= {"prompt_tokens", "completion_tokens", "total_tokens"}
    ms = j["modelspend"]
    assert ms["provider"] == "openai" and ms["actual_cost_usd"] > 0 and ms["catalog_version"] and ms["policy_version"]
    assert ms["attempts"] and ms["explanation"] and ms["validation"]["status"] == "not_configured"


def test_only_auto_model_and_unsupported_params_rejected(catalog, cfg):
    c, _, _ = client(catalog, cfg)
    r = c.post("/v1/chat/completions", json={**BODY, "model": "gpt-whatever"})
    assert r.status_code == 400 and r.json()["error"]["param"] == "model"
    for p, v in [("n", 2), ("logprobs", True), ("top_p", 0.5), ("audio", {})]:
        r = c.post("/v1/chat/completions", json={**BODY, p: v})
        assert r.status_code == 400 and r.json()["error"]["param"] == p, p
    r = c.post("/v1/chat/completions", json={**BODY, "bogus_field": 1})
    assert r.status_code == 400 and "unknown parameter" in r.json()["error"]["message"]


def test_image_content_rejected_explicitly(catalog, cfg):
    c, ad, _ = client(catalog, cfg)
    r = c.post("/v1/chat/completions", json={"model": "auto", "messages": [{"role": "user", "content": [
        {"type": "text", "text": "x"}, {"type": "image_url", "image_url": {"url": "http://x"}}]}]})
    assert r.status_code == 400 and r.json()["error"]["code"] == "unsupported_input"
    assert not any(a.calls for a in ad.values())


def test_allowlist_required_when_unconfigured(catalog, cfg):
    c, _, _ = client(catalog, cfg)
    cfg.default_providers = []
    r = c.post("/v1/chat/completions", json=BODY)
    assert r.status_code == 400 and r.json()["error"]["code"] == "provider_allowlist_required"


def test_preview_makes_no_provider_call_and_explains(catalog, cfg):
    c, ad, _ = client(catalog, cfg)
    r = c.post("/v1/route/preview", json={"messages": [{"role": "user", "content": "Summarize this report"}],
                                           "providers": ["google", "xai"], "max_budget_usd": 0.01})
    j = r.json()
    assert r.status_code == 200 and not any(a.calls for a in ad.values())
    assert j["candidates"][0]["expected_cost_usd"] <= j["candidates"][-1]["expected_cost_usd"]
    assert j["explanation"] and j["excluded"] is not None and j["assumptions"] and j["task"]["category"] == "summarization"


def test_no_eligible_model_error_carries_plan(catalog, cfg):
    c, _, _ = client(catalog, cfg)
    r = c.post("/v1/route/preview", json={"messages": [{"role": "user", "content": "classify"}], "providers": ["openai"],
                                           "profile": "high_assurance", "response_schema": {"type": "string"}})
    assert r.status_code == 422 and r.json()["error"]["code"] == "no_eligible_model" and "plan" in r.json()["error"]["modelspend"]


def test_auth_required_when_token_set(catalog, cfg):
    c, _, _ = client(catalog, cfg, token="s3cret")
    assert c.get("/healthz").status_code == 200
    assert c.post("/v1/chat/completions", json=BODY).status_code == 401
    assert c.get("/dashboard/data").status_code == 401
    assert c.post("/v1/chat/completions", json=BODY, headers={"Authorization": "Bearer wrong"}).status_code == 401
    ok = c.post("/v1/chat/completions", json=BODY, headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200


def test_bind_guard():
    check_bind("127.0.0.1", None)
    with pytest.raises(SystemExit):
        check_bind("0.0.0.0", None)
    check_bind("0.0.0.0", "tok")


def test_streaming_sse(catalog, cfg, store):
    c, _, _ = client(catalog, cfg, {"openai": "hello world"}, store=store)
    with c.stream("POST", "/v1/chat/completions", json={**BODY, "stream": True, "modelspend": {"providers": ["openai"]}}) as r:
        lines = [l for l in r.iter_lines() if l]
    assert lines[-1] == "data: [DONE]"
    chunks = [json.loads(l[6:]) for l in lines[:-1]]
    text = "".join(ch["choices"][0]["delta"].get("content", "") for ch in chunks)
    assert text == "hello world" and chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert chunks[-1]["modelspend"]["actual_cost_usd"] > 0 and chunks[0]["modelspend"]["routing"]["provider"] == "openai"


def test_streaming_interrupted_emits_error_event(catalog, cfg):
    c, ad, _ = client(catalog, cfg, {"openai": "partial text here"})
    ad["openai"].fail_stream_after = 1
    with c.stream("POST", "/v1/chat/completions", json={**BODY, "stream": True, "modelspend": {"providers": ["openai", "anthropic"]}}) as r:
        lines = [l for l in r.iter_lines() if l]
    err = json.loads(lines[-2][6:])["error"]
    assert err["code"] == "stream_interrupted" and err["partial_text"] and lines[-1] == "data: [DONE]"
    assert not ad["anthropic"].calls


def test_tool_calls_pass_through_with_ids(catalog, cfg):
    tc = ToolCall(id="call_7", name="lookup", arguments='{"q": 1}')
    resp = ProviderResponse(text="", tool_calls=[tc], finish_reason="tool_calls", usage=Usage(input_tokens=5, output_tokens=3))
    c, _, _ = client(catalog, cfg, {"openai": lambda r, n: resp})
    r = c.post("/v1/chat/completions", json={**BODY, "tools": [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}],
                                              "modelspend": {"providers": ["openai"]}})
    m = r.json()["choices"][0]
    assert m["finish_reason"] == "tool_calls" and m["message"]["tool_calls"][0] == {
        "id": "call_7", "type": "function", "function": {"name": "lookup", "arguments": '{"q": 1}'}}


def test_response_format_json_schema_routes_validation(catalog, cfg):
    schema = {"type": "object", "required": ["a"]}
    c, ad, _ = client(catalog, cfg, {"openai": lambda r, n: '{"a":1}' if "mid" in r.model else "bad"})
    r = c.post("/v1/chat/completions", json={**BODY, "response_format": {"type": "json_schema", "json_schema": {"name": "x", "schema": schema}},
                                              "modelspend": {"providers": ["openai"]}})
    ms = r.json()["modelspend"]
    assert ms["escalations"] == 1 and ms["validation"]["status"] == "passed"


def test_dashboard_and_usage(catalog, cfg, store):
    c, _, _ = client(catalog, cfg, store=store)
    c.post("/v1/chat/completions", json={**BODY, "modelspend": {"providers": ["openai"]}})
    assert "ModelSpend" in c.get("/dashboard").text
    d = c.get("/dashboard/data").json()
    assert d["totals"]["n"] == 1 and d["by_provider"][0]["provider"] == "openai" and d["validation"]
    assert c.get("/v1/catalog").json()["catalog_version"].startswith("synthetic-1")
    assert "balanced" in c.get("/v1/policy").json()["profiles"]
