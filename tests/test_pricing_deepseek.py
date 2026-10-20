"""Pricing refresh for a provider whose page names models differently from its API (DeepSeek's page is expected to print `deepseek-v4-flash` where the API
lists `deepseek-flash`), and for providers that have no pricing page at all. The numbers in the page below are FAKE test values, not DeepSeek's prices."""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from conftest import make_engine
from modelspend import Catalog, Config
from modelspend.cli import _print_result, app
from modelspend.engine import Engine
from modelspend.overrides import apply_assignments, save_overrides
from modelspend.pricing_refresh import apply_proposal, describe_unmatched, get_proposal, no_page_detail, refresh_provider, suggest_refs
from modelspend.providers.base import ProviderResponse
from modelspend.providers.mock import ScriptedAdapter
from modelspend.server.app import create_app
from modelspend.store import Store
from modelspend.types import Usage

runner = CliRunner()
FILLER = "Pricing for the DeepSeek API. All prices are in USD per 1M tokens. The model names below are the ones accepted by the API. " * 3
PAGE = (f"<html><body><p>{FILLER}</p><table>"
        "<tr><th>MODEL</th><td>deepseek-v4-flash</td><td>deepseek-v4-pro</td></tr>"
        "<tr><th>1M INPUT TOKENS (CACHE HIT)</th><td>$0.011</td><td>$0.022</td></tr>"
        "<tr><th>1M INPUT TOKENS (CACHE MISS)</th><td>$0.123</td><td>$0.456</td></tr>"
        "<tr><th>1M OUTPUT TOKENS</th><td>$0.234</td><td>$0.789</td></tr></table></body></html>")
EXTRACTION = {"models": [
    {"name": "deepseek-v4-flash", "input_per_mtok": 0.123, "output_per_mtok": 0.234, "cached_input_per_mtok": 0.011},
    {"name": "deepseek-v4-pro", "input_per_mtok": 0.456, "output_per_mtok": 0.789, "cached_input_per_mtok": 0.022}]}


class Rig:
    def __init__(self, tmp_path):
        self.cfg = Config(home=tmp_path / "h", api_keys={"anthropic": "k", "deepseek": "k"}, retry_backoff_s=0.0)
        save_overrides(self.cfg, {"add_models": [
            {"provider": "deepseek", "id": i, "availability": "available", "verified_on": "2026-10-20", "capabilities": {"text": True}}
            for i in ("deepseek-flash", "deepseek-v4-pro")]})  # what `catalog sync --add-new` writes: no prices
        apply_assignments(self.cfg, "anthropic/claude-haiku-5-5", ['availability="available"', "limits.context_tokens=1000000", "limits.max_output_tokens=64000",
                                                                   "capabilities.structured_output=true"])  # the extractor
        self.extractor = ScriptedAdapter("anthropic", lambda r, n: ProviderResponse(text=json.dumps(EXTRACTION), usage=Usage(input_tokens=2000, output_tokens=200)))
        self.engine = Engine(Catalog.from_config(self.cfg), self.cfg, {"anthropic": self.extractor, "deepseek": ScriptedAdapter("deepseek", lambda r, n: "x")},
                             Store(":memory:"), sandbox_runner=None)

    async def refresh(self, provider="deepseek", **kw):
        return await refresh_provider(self.engine, self.cfg, provider, fetcher=lambda url: (200, url, PAGE), providers=["anthropic"], **kw)

    def price(self, ref):
        p = Catalog.from_config(self.cfg).get(ref).pricing
        return None if p is None or not p.known else (p.input_per_mtok, p.output_per_mtok, p.cached_input_per_mtok)


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


# ---- a page that names a model differently from the API ----
def test_a_row_whose_printed_name_is_not_the_api_id_is_unmatched_with_a_suggestion_and_the_exact_row_that_matches_is_proposed(rig):
    r = asyncio.run(rig.refresh())
    assert r["state"] == "proposed" and r["changes"] == 1 and r["unmatched"] == 1  # deepseek-v4-pro matches exactly; deepseek-v4-flash does not
    row = r["unmatched_rows"][0]
    assert row["name"] == "deepseek-v4-flash" and row["suggest"] == ["deepseek/deepseek-flash"] and row["input_per_mtok"] == 0.123


def test_the_suggestion_is_a_ready_command_with_the_verified_prices_and_is_never_applied_by_itself(rig):
    r = asyncio.run(rig.refresh())
    lines = describe_unmatched(r["unmatched_rows"][0], "deepseek")
    assert "input $0.123" in lines[0] and "cached input $0.011" in lines[0] and "verified in the page text" in lines[0]
    assert lines[1].strip() == "if that is deepseek/deepseek-flash: modelspend catalog price deepseek/deepseek-flash --input 0.123 --output 0.234 --cached 0.011"
    apply_proposal(rig.cfg, r["proposal_id"])
    assert rig.price("deepseek/deepseek-v4-pro") == (0.456, 0.789, 0.022)  # the exact match was applied after review
    assert rig.price("deepseek/deepseek-flash") is None  # the guess was not: that is your call


def test_pricing_show_and_the_refresh_output_both_carry_the_suggestion(rig, monkeypatch, capsys):
    r = asyncio.run(rig.refresh())
    monkeypatch.setenv("MODELSPEND_HOME", str(rig.cfg.home))
    shown = runner.invoke(app, ["pricing", "show", r["proposal_id"]]).output
    assert "No catalog model matches" in shown and "modelspend catalog price deepseek/deepseek-flash --input 0.123 --output 0.234 --cached 0.011" in shown
    _print_result(r)
    assert "modelspend catalog price deepseek/deepseek-flash --input 0.123" in capsys.readouterr().out
    assert get_proposal(rig.cfg, r["proposal_id"])["unmatched"][0]["suggest"] == ["deepseek/deepseek-flash"]


def test_suggestions_need_two_shared_words_and_one_name_containing_the_other():
    cat = Catalog.load(overrides={"add_models": [{"provider": "deepseek", "id": i, "availability": "available", "verified_on": "2026-10-20", "capabilities": {"text": True}}
                                                  for i in ("deepseek-flash", "deepseek-v4-pro", "deepseek-reasoner")]})
    assert suggest_refs("deepseek-v4-flash", cat, "deepseek") == ["deepseek/deepseek-flash"]
    assert suggest_refs("DeepSeek V4 Flash (thinking mode)", cat, "deepseek") == ["deepseek/deepseek-flash"]
    assert suggest_refs("deepseek-chat", cat, "deepseek") == []  # 'deepseek' alone is not enough
    assert suggest_refs("deepseek-v4-flash", cat, "openai") == [] and suggest_refs("", cat, "deepseek") == []
    assert suggest_refs("deepseek-v4-pro", cat, "deepseek") == ["deepseek/deepseek-v4-pro"]
    short = Catalog.load(overrides={"add_models": [{"provider": "deepseek", "id": "chat", "availability": "available", "verified_on": "2026-10-20", "capabilities": {"text": True}}]})
    assert suggest_refs("deepseek-chat", short, "deepseek") == []  # a one-word id contained in the page name shares only one word: too weak to suggest
    lines = describe_unmatched({"name": "deepseek-chat", "input_per_mtok": 1.0, "output_per_mtok": 2.0}, "deepseek")
    assert "catalog sync --provider deepseek --add-new" in lines[1]  # nothing looks like it: say how to add the model


# ---- providers that have no pricing page ----
def test_a_provider_with_no_pricing_page_is_an_explained_error_not_a_crash(rig):
    r = asyncio.run(rig.refresh("openrouter"))
    assert r["state"] == "error" and "import-openrouter --refresh" in r["detail"]
    assert "sources set" in no_page_detail("ollama")


def test_refresh_without_a_provider_skips_those_with_no_pricing_page(tmp_path, monkeypatch):
    monkeypatch.setenv("MODELSPEND_HOME", str(tmp_path))
    (tmp_path / "settings.json").write_text(json.dumps({"custom_providers": {"ollama": {"base_url": "http://127.0.0.1:11434/v1", "no_key": True}}}))
    seen = {}

    async def fake(cfg, eng, targets, **kw):
        seen["targets"] = targets
        return []
    monkeypatch.setattr("modelspend.cli._refresh_many", fake)
    runner.invoke(app, ["pricing", "refresh"])
    assert "deepseek" in seen["targets"] and "openai" in seen["targets"]
    assert "openrouter" not in seen["targets"] and "ollama" not in seen["targets"]  # used to be a KeyError


def test_sources_commands_say_so_for_a_provider_with_no_pricing_page(tmp_path, monkeypatch):
    monkeypatch.setenv("MODELSPEND_HOME", str(tmp_path))
    r = runner.invoke(app, ["sources", "check", "openrouter"])
    assert r.exit_code == 2 and "import-openrouter" in r.output
    assert runner.invoke(app, ["sources", "reset", "openrouter"]).exit_code == 0  # used to be a KeyError after the reset


def test_the_server_answers_400_not_500_for_a_source_check_on_a_provider_with_no_page(tmp_path):
    cfg = Config(home=tmp_path, api_keys={"openai": "k"}, retry_backoff_s=0.0)
    cfg.api_token = None
    eng, _ = make_engine(Catalog.load(), cfg, {}, store=Store(":memory:"))
    c = TestClient(create_app(eng, cfg), base_url="http://127.0.0.1")
    r = c.post("/v1/sources/openrouter/check", json={})
    assert r.status_code == 400 and "import-openrouter" in r.text


def test_a_refused_extractor_says_why_and_how_to_fix_it_in_full(tmp_path):
    """Found live: `pricing refresh --extractor-model openai/gpt-4.1` failed with the reason cut off at 200 characters, hiding the fix the router had written."""
    r = Rig(tmp_path)
    apply_assignments(r.cfg, "anthropic/claude-haiku-5-5", ["capabilities.structured_output=false"])  # the extractor's structured output is not verified
    r.engine.catalog = Catalog.from_config(r.cfg)
    r.engine.router.catalog = r.engine.catalog
    out = asyncio.run(r.refresh(extractor_model="anthropic/claude-haiku-5-5"))
    assert out["state"] == "error" and len(out["detail"]) > 300
    assert "structured output not verified" in out["detail"] and "capabilities.structured_output=true" in out["detail"]
