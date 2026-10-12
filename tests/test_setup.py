"""Keys, providers, models, sources and status: how someone who just installed the repo sets things up."""
import json
import os
import stat

import pytest
from typer.testing import CliRunner

from modelspend import Catalog, Config
from modelspend.cli import app
from modelspend.errors import ProviderError
from modelspend.providers.mock import ScriptedAdapter
from modelspend.routing import Router
from modelspend.sources import Source, check_source, clean_url, resolve_sources, set_user_source
from modelspend.types import RouteRequest

runner = CliRunner()
SECRET = "sk-ant-SUPER-SECRET-1234"


def home():
    return Config.from_env().home


# ---------------------------------------------------------------- keys
def test_keys_set_prompts_hidden_stores_0600_and_never_echoes():
    r = runner.invoke(app, ["keys", "set", "anthropic"], input=SECRET + "\n")
    assert r.exit_code == 0 and SECRET not in r.output
    p = home() / "keys.env"
    assert p.read_text().count(SECRET) == 1 and "ANTHROPIC_API_KEY=" in p.read_text()
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    cfg = Config.from_env()
    assert cfg.api_keys["anthropic"] == SECRET and cfg.key_sources["anthropic"] == "keys file"
    assert SECRET not in repr(cfg)
    out = runner.invoke(app, ["keys", "list"]).output
    assert SECRET not in out and "anthropic" in out and "set (keys file)" in out and "not set" in out


def test_environment_overrides_keys_file(monkeypatch):
    runner.invoke(app, ["keys", "set", "openai", "--stdin"], input="from-file\n")
    assert Config.from_env().api_keys["openai"] == "from-file"
    monkeypatch.setenv("OPENAI_API_KEY", "from-env")
    cfg = Config.from_env()
    assert cfg.api_keys["openai"] == "from-env" and cfg.key_sources["openai"] == "environment"
    assert "takes precedence" in runner.invoke(app, ["keys", "set", "openai", "--stdin"], input="x\n").output


def test_keys_remove_and_google_alias():
    runner.invoke(app, ["keys", "set", "gemini", "--stdin"], input="g-key\n")
    assert Config.from_env().api_keys["google"] == "g-key"  # 'gemini' accepted as an alias for google
    assert "removed" in runner.invoke(app, ["keys", "remove", "google"]).output
    assert "google" not in Config.from_env().api_keys
    assert runner.invoke(app, ["keys", "set", "nonsense"], input="k\n").exit_code == 2
    assert runner.invoke(app, ["keys", "set", "openai", "--stdin"], input="\n").exit_code == 2  # empty key refused


def test_keys_test_reports_ok_and_failure_without_leaking(monkeypatch):
    class Bad(ScriptedAdapter):
        async def list_models(self):
            raise ProviderError("openai", "auth", f"invalid key {SECRET}", 401)
    ad = {"anthropic": ScriptedAdapter("anthropic", lambda r, n: "x", models=["a", "b"]), "openai": Bad("openai", lambda r, n: "x")}
    monkeypatch.setattr("modelspend.cli.build_adapters", lambda cfg: ad)
    ok = runner.invoke(app, ["keys", "test", "anthropic"])
    assert ok.exit_code == 0 and "OK (2 models" in ok.output
    bad = runner.invoke(app, ["keys", "test"])
    assert bad.exit_code == 1 and "FAILED: auth" in bad.output


# ---------------------------------------------------------------- providers and models
def test_providers_enable_disable_persist_and_feed_config(monkeypatch):
    out = runner.invoke(app, ["providers", "enable", "anthropic", "openai"]).output
    assert "anthropic, openai" in out and "no API key for openai" in out
    assert json.loads((home() / "settings.json").read_text())["providers"] == ["anthropic", "openai"]
    assert Config.from_env().default_providers == ["anthropic", "openai"]
    runner.invoke(app, ["providers", "disable", "openai"])
    assert Config.from_env().default_providers == ["anthropic"]
    monkeypatch.setenv("MODELSPEND_PROVIDERS", "xai")  # the environment overrides the saved setting
    assert Config.from_env().default_providers == ["xai"]
    assert "provider" in runner.invoke(app, ["providers", "list", "--catalog", "synthetic"]).output


def test_models_disable_removes_model_from_routing_and_explains(monkeypatch):
    cat = Catalog.load()
    ref = "anthropic/claude-haiku-5-5"
    runner.invoke(app, ["catalog", "set", ref, "availability=\"available\"", "limits.context_tokens=1000000", "limits.max_output_tokens=64000"])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    cfg = Config.from_env()
    req = RouteRequest(providers=["anthropic"], messages=[{"role": "user", "content": "Classify this ticket"}])
    plan = Router(Catalog.from_config(cfg), cfg).plan(req)
    assert plan.candidates[0].model == "claude-haiku-5-5"
    assert "turned off" in runner.invoke(app, ["models", "disable", ref]).output
    cfg = Config.from_env()
    assert cfg.deny_models == [ref]
    from modelspend import NoEligibleModel
    with pytest.raises(NoEligibleModel) as ei:
        Router(Catalog.from_config(cfg), cfg).plan(req)
    assert any(e.model == "claude-haiku-5-5" and "disabled" in e.reason for e in ei.value.details["plan"].excluded)
    listing = runner.invoke(app, ["models", "list"]).output
    assert "turned off by you" in listing
    runner.invoke(app, ["models", "enable", ref])
    assert Config.from_env().deny_models == []
    assert runner.invoke(app, ["models", "disable", "openai/nope"]).exit_code == 2


def test_allow_models_default_restricts_to_listed(monkeypatch):
    synth = Catalog.load(__import__("modelspend.catalog", fromlist=["x"]).shipped_catalog_path("synthetic_test_catalog.json"))
    cfg = Config(home=home(), api_keys={"openai": "k"}, allow_models=["openai/synthetic-openai-mid"])
    plan = Router(synth, cfg).plan(RouteRequest(providers=["openai"], messages=[{"role": "user", "content": "Classify"}]))
    assert [c.model for c in plan.candidates] == ["synthetic-openai-mid"]


def test_status_explains_what_is_missing():
    out = runner.invoke(app, ["status"]).output
    assert "routable models (0)" in out and "keys set" in out and "pricing sources" in out and "platform.claude.com" in out


# ---------------------------------------------------------------- pricing sources
def test_clean_url_strips_tracking_and_validates():
    dirty = "https://ai.google.dev/gemini-api/docs/pricing?utm_source=chatgpt.com&utm_medium=x&keep=1#frag"
    assert clean_url(dirty) == "https://ai.google.dev/gemini-api/docs/pricing?keep=1"
    assert clean_url("https://developers.openai.com/api/docs/pricing?utm_source=influzer.ai") == "https://developers.openai.com/api/docs/pricing"
    for bad in ("not a url", "ftp://x.com/p", "javascript:alert(1)"):
        with pytest.raises(ValueError):
            clean_url(bad)


def test_shipped_sources_cover_all_four_providers_with_clean_urls():
    s = resolve_sources(Config.from_env(), env={})
    assert set(s) == {"anthropic", "openai", "google", "xai"} and all("utm_" not in x.pricing_url and x.origin == "shipped" for x in s.values())
    assert s["xai"].pricing_url == "https://docs.x.ai/developers/pricing"


def test_sources_set_list_reset_and_env_override(monkeypatch):
    r = runner.invoke(app, ["sources", "set", "openai", "https://example.test/new-pricing?utm_source=x"])
    assert r.exit_code == 0 and "tracking parameters removed" in r.output
    assert resolve_sources(Config.from_env(), env={})["openai"].pricing_url == "https://example.test/new-pricing"
    assert "origin: user" in runner.invoke(app, ["sources", "list"]).output
    monkeypatch.setenv("MODELSPEND_PRICING_URL_OPENAI", "https://env.test/p")
    assert resolve_sources(Config.from_env())["openai"].origin == "env"
    monkeypatch.delenv("MODELSPEND_PRICING_URL_OPENAI")
    runner.invoke(app, ["sources", "reset", "openai"])
    assert resolve_sources(Config.from_env(), env={})["openai"].origin == "shipped"
    assert runner.invoke(app, ["sources", "set", "openai", "nonsense"]).exit_code == 2


def test_catalog_source_url_follows_the_configured_pricing_page():
    ref = "anthropic/claude-haiku-5-5"
    assert Catalog.from_config(Config.from_env()).get(ref).pricing.source_url == "https://platform.claude.com/docs/en/about-claude/pricing"
    runner.invoke(app, ["sources", "set", "anthropic", "https://docs.example.test/anthropic-pricing"])
    assert Catalog.from_config(Config.from_env()).get(ref).pricing.source_url == "https://docs.example.test/anthropic-pricing"
    t = runner.invoke(app, ["catalog", "template", "anthropic/some-model"])
    assert json.loads(t.output)["add_models"][0]["pricing"]["source_url"] == "https://docs.example.test/anthropic-pricing"
    # an entry that carries its own source_url keeps it
    Catalog.load(overrides={"models": {ref: {"pricing": {"source_url": "https://mine.test/p"}}}}, sources={"anthropic": "https://x.test"})


def test_source_check_detects_changes_and_failures():
    cfg = Config.from_env()
    src = Source("openai", "https://example.test/pricing", "shipped")
    pages = {"v": "<html><script>x=1</script><p>input  $1</p></html>"}
    fetcher = lambda url: (200, url, pages["v"])  # noqa: E731
    assert check_source(cfg, src, fetcher=fetcher)["state"] == "no_baseline"
    assert check_source(cfg, src, accept=True, fetcher=fetcher)["accepted"] is True
    assert check_source(cfg, src, fetcher=fetcher)["state"] == "unchanged"
    pages["v"] = "<html><script>x=2</script><p>input   $1</p></html>"  # scripts and whitespace do not count as a change
    assert check_source(cfg, src, fetcher=fetcher)["state"] == "unchanged"
    pages["v"] = "<html><p>input $2</p></html>"
    assert check_source(cfg, src, fetcher=fetcher)["state"] == "CHANGED"
    assert check_source(cfg, src, fetcher=lambda u: (404, u, ""))["state"] == "http_error"
    def boom(u):
        raise OSError("getaddrinfo ENOTFOUND")
    assert check_source(cfg, src, fetcher=boom)["state"] == "unreachable"
    moved = check_source(cfg, src, fetcher=lambda u: (200, "https://example.test/new-home", "x"))
    assert moved["redirected"] is True
    # a baseline for a different URL is not reused
    assert check_source(cfg, Source("openai", "https://other.test/p", "user"), fetcher=fetcher)["state"] == "no_baseline"


def test_cli_sources_check_exit_code_signals_change(monkeypatch):
    pages = {"v": "price 1"}
    monkeypatch.setattr("modelspend.sources.fetch", lambda url, timeout=20.0: (200, url, pages["v"]))
    assert runner.invoke(app, ["sources", "check", "xai", "--accept"]).exit_code == 0
    assert runner.invoke(app, ["sources", "check", "xai"]).exit_code == 0
    pages["v"] = "price 2"
    r = runner.invoke(app, ["sources", "check", "xai"])
    assert r.exit_code == 1 and "CHANGED" in r.output and "re-verify" in r.output


def test_providers_list_does_not_claim_enabled_when_no_default_allowlist():
    out = runner.invoke(app, ["providers", "list"]).output
    assert out.count("per request") == 4
    assert "per request" in out
    runner.invoke(app, ["providers", "enable", "anthropic"])
    rows = {l.split()[0]: l.split() for l in runner.invoke(app, ["providers", "list"]).output.splitlines()[1:5]}
    assert rows["anthropic"][2] == "yes" and rows["openai"][2] == "no"


def test_sources_listed_in_the_same_order_as_providers():
    out = runner.invoke(app, ["sources", "list"]).output
    order = [l.split()[0] for l in out.splitlines() if l and not l.startswith(" ")]
    assert order == ["anthropic", "openai", "google", "xai"]


# ---------------------------------------------------------------- adding a new vendor's models
def _fake_listing(monkeypatch, provider, info):
    class A(ScriptedAdapter):
        async def list_model_info(self):
            return info
    monkeypatch.setattr("modelspend.cli.build_adapters", lambda cfg: {provider: A(provider, lambda r, n: "ok")})
    monkeypatch.setenv(f"{ {'openai': 'OPENAI', 'xai': 'XAI', 'google': 'GEMINI'}[provider] }_API_KEY", "k")


def test_sync_add_new_requires_only_and_filters(monkeypatch):
    _fake_listing(monkeypatch, "openai", {"gpt-test-1": {}, "gpt-test-mini": {}, "text-embedding-x": {}, "whisper-y": {}})
    refuse = runner.invoke(app, ["catalog", "sync", "--provider", "openai", "--add-new", "--write"])
    assert refuse.exit_code == 2 and "--only" in refuse.output and not (home() / "overrides.json").exists()
    ok = runner.invoke(app, ["catalog", "sync", "--provider", "openai", "--add-new", "--only", "gpt-test", "--assume", "streaming", "--write"])
    assert ok.exit_code == 0 and "text-embedding-x" not in ok.output and "whisper-y" not in ok.output
    ids = {m["id"] for m in json.loads((home() / "overrides.json").read_text())["add_models"]}
    assert ids == {"gpt-test-1", "gpt-test-mini"}


def test_full_manual_onboarding_of_a_new_vendor_model(monkeypatch):
    """The path someone follows for OpenAI/xAI, whose list endpoints give IDs only."""
    _fake_listing(monkeypatch, "openai", {"gpt-test-1": {}})
    runner.invoke(app, ["catalog", "sync", "--provider", "openai", "--add-new", "--only", "gpt-test-1", "--assume", "streaming", "--write"])
    ref = "openai/gpt-test-1"
    assert "no pricing" in runner.invoke(app, ["models", "list", "--provider", "openai"]).output
    r = runner.invoke(app, ["catalog", "price", ref, "--input", "1.25", "--output", "10", "--cached", "0.125", "--reasoning-separate"])
    assert r.exit_code == 0, r.output
    m = Catalog.from_config(Config.from_env()).get(ref)
    assert (m.pricing.input_per_mtok, m.pricing.output_per_mtok, m.pricing.cached_input_per_mtok) == (1.25, 10.0, 0.125)
    assert m.pricing.reasoning_in_output is False and m.pricing.verified_on is not None
    assert m.pricing.source_url == "https://developers.openai.com/api/docs/pricing"  # the configured pricing page
    out = runner.invoke(app, ["models", "list", "--provider", "openai"]).output
    assert "no pricing" not in out and "limits unknown" in out and "no tier" in out
    runner.invoke(app, ["catalog", "set", ref, "limits.context_tokens=400000", "limits.max_output_tokens=128000", "tier=2"])
    assert runner.invoke(app, ["models", "list", "--provider", "openai"]).output.startswith("ROUTABLE")
    plan = runner.invoke(app, ["preview", "Classify this ticket", "--providers", "openai", "--json"])
    assert json.loads(plan.output[plan.output.index("{"):])["candidates"][0]["model"] == "gpt-test-1"
    assert runner.invoke(app, ["catalog", "price", "openai/missing", "--input", "1", "--output", "2"]).exit_code == 2
