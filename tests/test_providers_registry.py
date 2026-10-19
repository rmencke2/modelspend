"""0.8: more providers. DeepSeek is a preset and anything else that speaks the OpenAI protocol (Groq, Together, Mistral, OpenRouter, a local
Ollama, ...) is described in settings.json. No models and no prices are shipped for them: those come from the provider on the user's machine.
All of this is tested against fakes that mirror the SDK shape; none of it has been run against a live DeepSeek endpoint."""
from __future__ import annotations

import json
from types import SimpleNamespace as NS

import pytest
from typer.testing import CliRunner

from modelspend import Catalog, Config, RouteRequest, registry
from modelspend.catalog import shipped_catalog_path
from modelspend.cli import app
from modelspend.engine import Engine
from modelspend.providers import OpenAICompatAdapter, build_adapters, make_compat_adapter
from modelspend.providers.openai_compat import normalize_usage
from modelspend.store import Store
from modelspend.types import TaskCategory, normalize_provider
from test_adapters import FakeOpenAI, preq


def ds_usage(hit=70, miss=30, completion=20, reasoning=5):
    return NS(prompt_tokens=hit + miss, completion_tokens=completion, prompt_cache_hit_tokens=hit, prompt_cache_miss_tokens=miss,
              completion_tokens_details=NS(reasoning_tokens=reasoning))


def ds_response(text="billing", usage=None):
    return NS(id="r1", choices=[NS(message=NS(content=text, tool_calls=None, refusal=None, reasoning_content="thinking..."), finish_reason="stop")],
              usage=usage or ds_usage())


# ---- the registry ----
def test_builtins_keep_their_order_and_deepseek_is_a_preset():
    assert registry.all_providers() == ("anthropic", "openai", "google", "xai", "deepseek")
    s = registry.spec("deepseek")
    assert (s.kind, s.base_url, s.key_env, s.max_tokens_param, s.cached_included) == ("preset", "https://api.deepseek.com", ("DEEPSEEK_API_KEY",), "max_tokens", True)
    assert registry.spec("anthropic").cached_included is False and registry.spec("openai").cached_included is True
    assert registry.key_env()["google"] == ("GEMINI_API_KEY", "GOOGLE_API_KEY")


def test_normalize_accepts_aliases_and_names_every_known_provider_in_the_error():
    assert [normalize_provider(x) for x in ("Claude", " GEMINI ", "grok", "DeepSeek")] == ["anthropic", "google", "xai", "deepseek"]
    with pytest.raises(ValueError, match="supported: anthropic, openai, google, xai, deepseek"):
        normalize_provider("mistral")


GOOD = {"groq": {"base_url": "https://api.groq.com/openai/v1"},
        "ollama": {"base_url": "http://localhost:11434/v1", "no_key": True},
        "together": {"base_url": "https://api.together.xyz/v1/", "key_env": "TOGETHER_KEY", "max_tokens_param": "max_completion_tokens",
                     "cached_included": False, "pricing_url": "https://example.test/pricing"}}


def test_valid_custom_providers_get_sensible_defaults():
    out = registry.parse_custom(GOOD)
    g, o, t = out["groq"], out["ollama"], out["together"]
    assert (g.kind, g.key_env, g.max_tokens_param, g.cached_included, g.needs_key, g.pricing_url) == ("custom", ("GROQ_API_KEY",), "max_tokens", True, True, None)
    assert (o.base_url, o.needs_key) == ("http://localhost:11434/v1", False)
    assert (t.base_url, t.key_env, t.max_tokens_param, t.cached_included, t.pricing_url) == (
        "https://api.together.xyz/v1", ("TOGETHER_KEY",), "max_completion_tokens", False, "https://example.test/pricing")
    assert registry.parse_custom(None) == {} == registry.parse_custom({})


@pytest.mark.parametrize("raw,msg", [
    ("groq", "must be an object of name"),
    ({"Groq": {"base_url": "https://x.test"}}, "custom provider name"),
    ({"g": {"base_url": "https://x.test"}}, "custom provider name"),
    ({"openai": {"base_url": "https://x.test"}}, "already a built-in"),
    ({"claude": {"base_url": "https://x.test"}}, "already a built-in"),
    ({"deepseek": {"base_url": "https://x.test"}}, "already a built-in"),
    ({"groq": "https://x.test"}, "must be an object"),
    ({"groq": {}}, "base_url is required"),
    ({"groq": {"base_url": "https://x.test", "key": "sk-1"}}, "unknown setting"),
    ({"groq": {"base_url": "http://api.groq.com/v1"}}, "must use https"),
    ({"groq": {"base_url": "http://192.168.1.5:8000/v1"}}, "must use https"),
    ({"groq": {"base_url": "https://user:secret@api.x.test/v1"}}, "without credentials"),
    ({"groq": {"base_url": "https://api.x.test/v1?key=1"}}, "without credentials"),
    ({"groq": {"base_url": "ftp://x.test"}}, "must use https"),
    ({"groq": {"base_url": 5}}, "must be a URL string"),
    ({"groq": {"base_url": "https://x.test", "key_env": "my key"}}, "environment variable name"),
    ({"groq": {"base_url": "https://x.test", "key_env": "lower"}}, "environment variable name"),
    ({"groq": {"base_url": "https://x.test", "max_tokens_param": "max_new_tokens"}}, "max_tokens_param"),
    ({"groq": {"base_url": "https://x.test", "no_key": "yes"}}, "no_key must be true or false"),
    ({"groq": {"base_url": "https://x.test", "pricing_url": "http://x.test/p"}}, "must use https"),
])
def test_invalid_custom_providers_are_rejected_loudly(raw, msg):
    with pytest.raises(ValueError, match=msg):
        registry.parse_custom(raw)


# ---- config ----
def write_settings(home, **s):
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(json.dumps(s))


def test_config_registers_custom_providers_and_reads_their_keys(tmp_path):
    home = tmp_path / "home"
    write_settings(home, custom_providers=GOOD)
    (home / "keys.env").write_text("GROQ_API_KEY=from-file\n")
    cfg = Config.from_env({"MODELSPEND_HOME": str(home), "TOGETHER_KEY": "from-env", "DEEPSEEK_API_KEY": "ds-env"})
    assert cfg.api_keys["groq"] == "from-file" and cfg.key_sources["groq"] == "keys file"
    assert cfg.api_keys["together"] == "from-env" and cfg.key_sources["together"] == "environment"
    assert cfg.api_keys["deepseek"] == "ds-env"
    assert cfg.api_keys["ollama"] == "no-key-required" and cfg.key_sources["ollama"] == "not required"  # a local server needs no key
    assert normalize_provider("Groq") == "groq" and cfg.configured_providers() == ["deepseek", "groq", "ollama", "together"]  # built-ins, presets, then yours in the order written
    assert "ds-env" not in repr(cfg) and "from-file" not in repr(cfg)  # keys never appear in the config's repr


def test_an_invalid_custom_provider_stops_config_loading_before_any_key_is_read(tmp_path):
    home = tmp_path / "home"
    write_settings(home, custom_providers={"groq": {"base_url": "http://api.groq.com/v1"}})
    with pytest.raises(ValueError, match="settings.json: custom_providers.groq: base_url must use https"):
        Config.from_env({"MODELSPEND_HOME": str(home), "GROQ_API_KEY": "k"})


def test_the_registry_is_replaced_not_accumulated_when_a_new_config_is_built(tmp_path):
    Config(home=tmp_path, custom_providers=GOOD)
    assert "groq" in registry.all_providers()
    Config(home=tmp_path)
    assert "groq" not in registry.all_providers()
    with pytest.raises(ValueError, match="unknown provider"):
        normalize_provider("groq")


# ---- adapters ----
def test_build_adapters_creates_compat_adapters_for_presets_and_custom_providers(tmp_path):
    cfg = Config(home=tmp_path, api_keys={"openai": "k1", "deepseek": "k2", "groq": "k3", "ollama": "x"}, custom_providers={
        "groq": GOOD["groq"], "ollama": GOOD["ollama"], "unused": {"base_url": "https://u.test/v1"}})
    ad = build_adapters(cfg)
    assert set(ad) == {"openai", "deepseek", "groq", "ollama"}  # nothing for a provider without a key
    assert type(ad["openai"]) is OpenAICompatAdapter and ad["openai"].name == "openai"
    for name, url, mtp in (("deepseek", "https://api.deepseek.com", "max_tokens"), ("groq", "https://api.groq.com/openai/v1", "max_tokens"), ("ollama", "http://localhost:11434/v1", "max_tokens")):
        a = ad[name]
        assert isinstance(a, OpenAICompatAdapter) and (a.name, a._base_url, a.max_tokens_param) == (name, url, mtp)
    assert "k2" not in repr(ad["deepseek"].__dict__.get("name"))


async def test_compat_providers_send_their_own_token_limit_parameter():
    spec = registry.spec("deepseek")
    fake = FakeOpenAI(response=ds_response())
    await make_compat_adapter(spec, "k", client=fake).generate(preq(model="deepseek-test", max_output_tokens=77))
    assert fake.calls[0]["max_tokens"] == 77 and "max_completion_tokens" not in fake.calls[0]
    fake2 = FakeOpenAI(response=ds_response())
    custom = registry.parse_custom({"tg": {"base_url": "https://tg.test/v1", "max_tokens_param": "max_completion_tokens"}})["tg"]
    await make_compat_adapter(custom, "k", client=fake2).generate(preq(max_output_tokens=33))
    assert fake2.calls[0]["max_completion_tokens"] == 33 and "max_tokens" not in fake2.calls[0]


def test_deepseek_cache_hits_count_as_cached_input_and_openai_usage_is_unchanged():
    u = normalize_usage(ds_usage(hit=70, miss=30, completion=20, reasoning=5), True)
    assert (u.input_tokens, u.cache_read_tokens, u.output_tokens, u.reasoning_tokens) == (30, 70, 20, 5)  # prompt_tokens includes the hits: billed once each
    assert normalize_usage(ds_usage(hit=0, miss=100), True).cache_read_tokens == 0
    openai = NS(prompt_tokens=100, completion_tokens=20, prompt_tokens_details=NS(cached_tokens=40), completion_tokens_details=NS(reasoning_tokens=5))
    v = normalize_usage(openai, True)
    assert (v.input_tokens, v.cache_read_tokens, v.output_tokens) == (60, 40, 20)
    both = NS(prompt_tokens=100, completion_tokens=1, prompt_tokens_details=NS(cached_tokens=40), prompt_cache_hit_tokens=99)
    assert normalize_usage(both, True).cache_read_tokens == 40  # the standard field wins when both are present


# ---- end to end: a routed call to a DeepSeek-style provider, cost included ----
def deepseek_catalog(tmp_path):
    d = json.load(open(shipped_catalog_path("synthetic_test_catalog.json")))
    m = next(x for x in d["models"] if x["id"] == "synthetic-openai-mid")
    m = {**m, "provider": "deepseek", "id": "deepseek-test", "display_name": "SYNTHETIC deepseek"}
    m["pricing"] = {**m["pricing"], "long_context_tiers": []}
    d["models"] = [m]
    p = tmp_path / "ds.json"
    p.write_text(json.dumps(d))
    return Catalog.load(p)


async def test_a_routed_call_to_deepseek_is_priced_with_its_cache_hits(tmp_path):
    cat = deepseek_catalog(tmp_path)
    cfg = Config(home=tmp_path, api_keys={"deepseek": "k"}, retry_backoff_s=0.0)
    fake = FakeOpenAI(response=ds_response("billing"))
    eng = Engine(cat, cfg, {"deepseek": make_compat_adapter(registry.spec("deepseek"), "k", client=fake)}, store=Store(":memory:"), sandbox_runner=None)
    res = await eng.run(RouteRequest(providers=["deepseek"], messages=[{"role": "user", "content": "Classify: charged twice"}], task_type=TaskCategory.classification))
    assert (res.provider, res.model, res.status, res.text) == ("deepseek", "deepseek-test", "ok", "billing")  # reasoning_content is never returned as the answer
    u = res.usage
    assert (u.input_tokens, u.cache_read_tokens, u.output_tokens, u.reasoning_tokens) == (30, 70, 20, 5)
    price = cat.get("deepseek/deepseek-test").pricing
    want = (30 * price.input_per_mtok + 70 * price.cached_input_per_mtok + 20 * price.output_per_mtok) / 1e6
    assert res.actual_cost_usd == pytest.approx(want) and want > 0
    assert fake.calls[0]["max_tokens"] > 0 and "max_completion_tokens" not in fake.calls[0]


def test_provider_options_follow_each_providers_allowlist():
    from modelspend.errors import InvalidRequest
    from modelspend.routing import validate_provider_options
    msgs = [{"role": "user", "content": "x"}]
    validate_provider_options(RouteRequest(messages=msgs, providers=["deepseek"], provider_options={"deepseek": {"seed": 1, "top_p": 0.5}}))
    with pytest.raises(InvalidRequest, match="unsupported key"):
        validate_provider_options(RouteRequest(messages=msgs, providers=["deepseek"], provider_options={"deepseek": {"top_k": 5}}))
    Config(home=".", custom_providers={"groq": GOOD["groq"]})
    validate_provider_options(RouteRequest(messages=msgs, providers=["groq"], provider_options={"groq": {"stop": ["x"]}}))


# ---- sources, reconcile, CLI ----
def test_pricing_pages_come_from_the_preset_and_from_your_own_settings(tmp_path):
    from modelspend.sources import resolve_sources
    cfg = Config(home=tmp_path, custom_providers={"together": GOOD["together"], "groq": GOOD["groq"]})
    src = resolve_sources(cfg, env={})
    assert src["deepseek"].pricing_url == "https://api-docs.deepseek.com/quick_start/pricing" and src["deepseek"].origin == "shipped"
    assert src["together"].pricing_url == "https://example.test/pricing" and "groq" not in src  # no page given: set one with `modelspend sources set groq <url>`


def test_reconcile_knows_each_providers_cached_token_convention(tmp_path):
    from modelspend import reconcile as rc
    Config(home=tmp_path, custom_providers={"together": GOOD["together"]})
    assert rc.parse_usage("date,model,input_tokens\n2026-10-10,deepseek-chat,5\n", "deepseek")[0]["provider"] == "deepseek"
    assert rc.parse_usage("date,model,input_tokens\n2026-10-10,m,5\n", "together")[0]["provider"] == "together"
    with pytest.raises(rc.ReconcileError, match="choose from anthropic, openai, google, xai, deepseek, together"):
        rc.parse_usage("date,model,input_tokens\n2026-10-10,m,5\n", "nope")
    store = Store(":memory:")
    store.db.execute("insert into requests(id, ts, provider, model, status) values ('r', 1791590400.0 + 60, 'together', 'm', 'ok')")
    store.db.execute("insert into attempts(request_id, idx, kind, purpose, provider, model, outcome, input_tokens, cache_read, cache_write, output_tokens) "
                     "values ('r', 0, 'generation', 'answer', 'together', 'm', 'ok', 600, 400, 0, 100)")
    store.db.commit()
    store.db.execute("insert into provider_usage(source, provider, model, day, input_tokens, output_tokens, cache_read, cache_write, requests, cost, imported_ts) "
                     "values ('s', 'together', 'm', '2026-10-10', 1000, 100, 400, 0, 1, null, 1)")
    store.db.commit()
    assert rc.compare(store)["rows"][0]["tokens"]["status"] == "provider_higher"  # together is configured as 'input excludes cached': 1000 + 400 + 100 = 1500 on the provider side vs 1100 recorded
    assert rc.compare(store, cached_included={"together": True})["rows"][0]["tokens"]["status"] == "match"  # overridden: the provider's 1000 input already includes the 400 cached, so 1100 = 1100


def cli(*a, env=None, input=None):
    return CliRunner().invoke(app, list(a), env=env, input=input)


def test_cli_keys_and_providers_cover_deepseek_and_your_own(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("MODELSPEND_HOME", str(home))
    monkeypatch.setenv("MODELSPEND_DB", str(tmp_path / "u.db"))
    write_settings(home, custom_providers={"groq": GOOD["groq"]})
    r = cli("keys", "set", "deepseek", "--stdin", input="ds-secret\n")
    assert r.exit_code == 0 and "saved deepseek key" in r.output and "ds-secret" not in r.output
    assert cli("keys", "set", "groq", "--stdin", input="gq-secret\n").exit_code == 0
    saved = (home / "keys.env").read_text()
    assert "DEEPSEEK_API_KEY=ds-secret" in saved and "GROQ_API_KEY=gq-secret" in saved
    out = cli("keys", "list").output
    assert "deepseek" in out and "DEEPSEEK_API_KEY" in out and "groq" in out and "GROQ_API_KEY" in out and "secret" not in out
    write_settings(home, custom_providers={"groq": GOOD["groq"], "ollama": GOOD["ollama"]})
    local = [ln for ln in cli("keys", "list").output.splitlines() if ln.startswith("ollama")][0]
    assert "not required" in local and "no key needed" in local and "OLLAMA_API_KEY" not in local
    assert "deepseek" in cli("providers", "list").output and "groq" in cli("providers", "list").output
    assert cli("keys", "remove", "groq").exit_code == 0 and "GROQ_API_KEY" not in (home / "keys.env").read_text()
    bad = cli("keys", "set", "mistral", "--stdin", input="k\n")
    assert bad.exit_code != 0 and "unknown provider" in bad.output
