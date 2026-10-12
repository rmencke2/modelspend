"""Token-first behaviour: unpriced models, token budgets, exact usage totals, store/dashboard/CLI, catalog sync."""
import json
import sqlite3
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from conftest import make_engine
from modelspend import BudgetTooLow, NoEligibleModel, RouteRequest
from modelspend.catalog import Catalog
from modelspend.cli import app, sync_patch
from modelspend.errors import ProviderError
from modelspend.providers import AnthropicAdapter
from modelspend.providers.base import ProviderResponse
from modelspend.providers.mock import ScriptedAdapter
from modelspend.server.app import create_app
from modelspend.store import Store
from modelspend.types import Usage

runner = CliRunner()


def R(text="Classify this: great", **kw):
    kw.setdefault("providers", ["xai"])
    return RouteRequest(messages=[{"role": "user", "content": text}], **kw)


def resp(text, i=100, o=10, **kw):
    return ProviderResponse(text=text, usage=Usage(input_tokens=i, output_tokens=o), **kw)


# ---------------------------------------------------------------- unpriced models
async def test_unpriced_model_excluded_by_default_included_when_allowed(engine_factory):
    eng, _ = engine_factory()
    only_unpriced = ["xai/synthetic-xai-small", "xai/synthetic-xai-mid", "xai/synthetic-xai-large"]
    with pytest.raises(NoEligibleModel):  # the unpriced model is the only one left and is excluded by default
        await eng.plan(R("Write a function to add numbers", deny_models=only_unpriced))
    plan = await eng.plan(R("Write a function to add numbers", allow_unpriced=True))
    assert plan.ranking_basis == "tier_then_tokens"          # costs are not comparable, so cost is not used to rank
    assert plan.candidates[0].tier == min(c.tier for c in plan.candidates)
    unpriced = next(c for c in plan.candidates if c.model == "synthetic-xai-unpriced")
    assert unpriced.expected_cost_usd is None and unpriced.worst_case_cost_usd is None
    assert unpriced.expected_total_tokens > 0 and unpriced.worst_case_total_tokens >= unpriced.expected_total_tokens
    assert "cost unknown" in " ".join(unpriced.pricing_warnings)


async def test_unpriced_run_reports_exact_tokens_and_unknown_cost(engine_factory):
    eng, ad = engine_factory({"xai": lambda r, n: resp("ok", 120, 30)})
    res = await eng.run(R("Write a function to add numbers", providers=["xai"], allow_unpriced=True,
                          deny_models=["xai/synthetic-xai-small", "xai/synthetic-xai-mid", "xai/synthetic-xai-large"]))
    assert res.model == "synthetic-xai-unpriced"
    assert res.actual_cost_usd is None and res.estimated_cost_usd is None
    assert (res.usage.input_tokens, res.usage.output_tokens, res.total_tokens) == (120, 30, 150)
    assert res.attempts[0].cost_usd is None and res.attempts[0].cost_basis == "unpriced"
    assert any("cost unknown" in u for u in res.uncertainty)


async def test_usd_budget_cannot_gate_unpriced_models(engine_factory):
    eng, _ = engine_factory()
    plan_req = R("Write a function", allow_unpriced=True, max_budget_usd=1.0,
                 deny_models=["xai/synthetic-xai-small", "xai/synthetic-xai-mid", "xai/synthetic-xai-large"])
    with pytest.raises(BudgetTooLow) as ei:
        await eng.plan(plan_req)
    assert any(e.stage == "budget" and "token" in e.reason for e in ei.value.details["plan"].excluded)


# ---------------------------------------------------------------- token budget
async def test_token_budget_filters_candidates_at_plan_time(engine_factory):
    eng, _ = engine_factory()
    plan = await eng.plan(R("Classify this", providers=["openai"], max_output_tokens=500, max_budget_tokens=900))
    assert plan.candidates and all(c.worst_case_total_tokens <= 900 for c in plan.candidates)
    with pytest.raises(BudgetTooLow):
        await eng.plan(R("Classify this", providers=["openai"], max_output_tokens=500, max_budget_tokens=100))


async def test_runtime_token_gate_blocks_escalation_the_plan_alone_would_allow(engine_factory):
    eng, ad = engine_factory({"openai": lambda r, n: resp("nope", 10, 200)})
    base = dict(providers=["openai"], max_output_tokens=200, validators=[{"type": "allowed_labels", "labels": ["x"]}])
    plan = await eng.plan(R("Classify", **base))
    mid = next(c for c in plan.candidates if c.model == "synthetic-openai-mid")
    budget = mid.worst_case_total_tokens + 50          # mid fits at plan time...
    res = await eng.run(R("Classify", max_budget_tokens=budget, **base))
    assert res.escalations == 0 and len(ad["openai"].calls) == 1   # ...but 210 tokens are already spent, so it cannot start
    assert res.total_tokens == 210 and res.budget_tokens_remaining == budget - 210
    ad["openai"].calls.clear()
    res = await eng.run(R("Classify", max_budget_tokens=budget * 4, **base))
    assert res.escalations == 1 and res.total_tokens == 420


async def test_total_tokens_sum_every_attempt_including_judge_and_failures(engine_factory):
    def script(r, n):
        if n == 1:
            return ProviderError("openai", "rate_limit", "429", 429)
        return resp('{"pass": true, "reason": "ok"}', 50, 20) if r.max_output_tokens == 120 else resp("answer", 200, 40)
    eng, _ = engine_factory({"openai": script})
    res = await eng.run(R("Rewrite this politely", providers=["openai"], judge=True, acceptance_criteria=["polite"]))
    assert [a.kind for a in res.attempts] == ["generation", "generation", "judge"]
    assert res.total_tokens == 200 + 40 + 50 + 20 and res.usage.input_tokens == 250 and res.usage.output_tokens == 60


async def test_timeout_charges_uncertain_tokens_against_token_budget(engine_factory):
    eng, ad = engine_factory({"openai": lambda r, n: ProviderError("openai", "timeout", "t") if n == 1 else resp("positive", 10, 5)})
    res = await eng.run(R("Classify this", providers=["openai"], max_budget_tokens=5000))
    assert res.total_tokens == 15                      # exact tokens reported by the provider
    assert res.budget_tokens_remaining < 5000 - 15     # plus the upper-bound input estimate of the failed call


# ---------------------------------------------------------------- store / dashboard
async def test_store_and_dashboard_lead_with_tokens(catalog, cfg, store):
    cfg.default_providers = ["xai"]
    eng, _ = make_engine(catalog, cfg, {"xai": lambda r, n: resp("ok", 120, 30)}, store=store)
    c = TestClient(create_app(eng, cfg))
    deny = ["xai/synthetic-xai-small", "xai/synthetic-xai-mid", "xai/synthetic-xai-large"]
    for _ in range(2):
        r = c.post("/v1/chat/completions", json={"model": "auto", "messages": [{"role": "user", "content": "Write a function to add"}],
                                                  "modelspend": {"allow_unpriced": True, "deny_models": deny}})
        assert r.status_code == 200 and r.json()["usage"]["total_tokens"] == 150 and r.json()["modelspend"]["actual_cost_usd"] is None
    d = c.get("/dashboard/data").json()
    t = d["totals"]
    assert (t["n"], t["input_tokens"], t["output_tokens"], t["total_tokens"]) == (2, 240, 60, 300)
    assert t["unpriced_requests"] == 2 and d["by_provider"][0]["tokens"] == 300 and d["by_model"][0]["cost"] is None
    assert d["recent"][0]["input_tokens"] == 120 and "reasoning_tokens" in d["recent"][0]
    html = c.get("/dashboard").text
    assert "Total tokens" in html and "Tokens by model" in html


def test_store_migrates_old_schema(tmp_path):
    p = tmp_path / "old.db"
    db = sqlite3.connect(p)
    db.execute("create table requests(id text primary key, ts real, profile text, task text, task_basis text, provider text, model text, "
               "status text, validation text, retries int, failovers int, escalations int, estimated_cost real, actual_cost real, "
               "uncertain_cost real, budget real, input_tokens int, output_tokens int, latency_ms int, catalog_version text, policy_version text)")
    db.commit()
    db.close()
    s = Store(p)
    cols = {r[1] for r in s.db.execute("pragma table_info(requests)")}
    assert {"cache_read_tokens", "reasoning_tokens", "total_tokens"} <= cols
    assert s.summary()["totals"]["total_tokens"] == 0


# ---------------------------------------------------------------- CLI
def _patch_adapters(monkeypatch, script):
    ad = {"openai": ScriptedAdapter("openai", script)}
    monkeypatch.setattr("modelspend.cli.build_adapters", lambda cfg: ad)
    return ad


def test_cli_route_prints_tokens_before_cost(monkeypatch, tmp_path):
    _patch_adapters(monkeypatch, lambda r, n: resp("positive", 80, 12))
    env = {"MODELSPEND_HOME": str(tmp_path), "OPENAI_API_KEY": "k"}
    r = runner.invoke(app, ["route", "Classify this", "--providers", "openai", "--catalog", "synthetic", "--budget-tokens", "5000"], env=env)
    assert r.exit_code == 0, r.output
    lines = [l for l in r.output.splitlines() if l.startswith("---")]
    assert lines[1].startswith("--- TOKENS: in 80 | out 12 | total 92") and "token budget left" in lines[1]
    assert lines[2].startswith("--- cost:")
    u = runner.invoke(app, ["usage"], env=env).output
    assert u.index("TOKENS total 92") < u.index("cost:") and "by model" in u


def test_cli_preview_unpriced_shows_unknown_cost():
    r = runner.invoke(app, ["preview", "Write a function to add", "--providers", "xai", "--catalog", "synthetic", "--assume-keys",
                            "--allow-unpriced"])
    assert r.exit_code == 0 and "tier_then_tokens" in r.output and "unknown" in r.output


# ---------------------------------------------------------------- catalog sync / set
def test_sync_patch_never_touches_prices_and_suggests_tier():
    p = sync_patch(True, "m", "anthropic", {"context_tokens": 1_000_000, "max_output_tokens": 128_000, "line": "sonnet",
                                           "lifecycle": "active", "structured_output": True}, ["streaming", "tools"], "2026-10-12", False)
    assert "pricing" not in p and p["tier"] == 2 and "not a measurement" in p["tier_note"]
    assert p["limits"] == {"context_tokens": 1_000_000, "max_output_tokens": 128_000}
    assert p["capabilities"] == {"text": True, "structured_output": True, "streaming": True, "tool_calling": True}
    assert sync_patch(True, "m", "a", {"lifecycle": "retired"}, [], "2026-10-12", False) is None
    assert "tier" not in sync_patch(True, "m", "a", {"line": "haiku"}, [], "2026-10-12", True)  # existing tier is kept


def test_cli_sync_then_route_preview_end_to_end(monkeypatch, tmp_path):
    info = {"claude-haiku-5-5": {"context_tokens": 1_000_000, "max_output_tokens": 128_000, "line": "haiku", "lifecycle": "active",
                                 "structured_output": True, "display_name": "Haiku"},
            "claude-unknown-x": {"line": "sonnet"}}

    class A(ScriptedAdapter):
        async def list_model_info(self):
            return info
    monkeypatch.setattr("modelspend.cli.build_adapters", lambda cfg: {"anthropic": A("anthropic", lambda r, n: "ok")})
    env = {"MODELSPEND_HOME": str(tmp_path), "ANTHROPIC_API_KEY": "k"}
    dry = runner.invoke(app, ["catalog", "sync", "--provider", "anthropic"], env=env)
    assert "update anthropic/claude-haiku-5-5" in dry.output and "claude-unknown-x" not in dry.output and not (tmp_path / "overrides.json").exists()
    # not routable before sync (availability unverified)
    pre = runner.invoke(app, ["preview", "Classify this ticket", "--providers", "anthropic"], env=env)
    assert pre.exit_code == 2
    w = runner.invoke(app, ["catalog", "sync", "--provider", "anthropic", "--assume", "streaming", "--write"], env=env)
    assert "wrote 1 model(s)" in w.output
    ov = json.loads((tmp_path / "overrides.json").read_text())["models"]["anthropic/claude-haiku-5-5"]
    assert ov["limits"]["max_output_tokens"] == 128_000 and "pricing" not in ov and ov["capabilities"]["streaming"] is True
    post = runner.invoke(app, ["preview", "Classify this ticket", "--providers", "anthropic", "--json"], env=env)
    plan = json.loads(post.output[post.output.index("{"):])
    c = plan["candidates"][0]
    assert post.exit_code == 0 and c["model"] == "claude-haiku-5-5" and c["expected_cost_usd"] > 0 and c["expected_total_tokens"] > 0
    # discover the newly-added model too
    add = runner.invoke(app, ["catalog", "sync", "--provider", "anthropic", "--add-new", "--write"], env=env)
    assert "add    anthropic/claude-unknown-x" in add.output
    cat = Catalog.from_config(__import__("modelspend").Config.from_env({**env}))
    assert cat.get("anthropic/claude-unknown-x").pricing is None  # added without prices


def test_cli_set_patches_and_reverts_invalid(tmp_path):
    env = {"MODELSPEND_HOME": str(tmp_path)}
    ok = runner.invoke(app, ["catalog", "set", "anthropic/claude-haiku-5-5", "tier=2", "capabilities.streaming=true", "pricing.input_per_mtok=0.2"], env=env)
    assert ok.exit_code == 0
    m = Catalog.from_config(__import__("modelspend").Config.from_env(env)).get("anthropic/claude-haiku-5-5")
    assert m.tier == 2 and m.capabilities.streaming is True and m.pricing.input_per_mtok == 0.2 and m.pricing.origin == "user_override"
    bad = runner.invoke(app, ["catalog", "set", "anthropic/claude-haiku-5-5", "tier=9"], env=env)
    assert bad.exit_code == 2 and "reverted" in bad.output
    assert Catalog.from_config(__import__("modelspend").Config.from_env(env)).get("anthropic/claude-haiku-5-5").tier == 2
    assert runner.invoke(app, ["catalog", "set", "openai/nope", "tier=1"], env=env).exit_code == 2


# ---------------------------------------------------------------- adapters
class FakeModels:
    def __init__(self, data):
        self.models = NS(list=self._list)
        self._d = data

    async def _list(self, **kw):
        return NS(data=self._d)


async def test_anthropic_list_model_info_maps_fields():
    m = NS(id="claude-x", display_name="X", max_input_tokens=1000, max_tokens=64, lifecycle="active", line="opus",
           capabilities=NS(structured_outputs=NS(supported=True)))
    info = await AnthropicAdapter("k", client=FakeModels([m])).list_model_info()
    assert info["claude-x"] == {"display_name": "X", "context_tokens": 1000, "max_output_tokens": 64, "lifecycle": "active",
                                "line": "opus", "structured_output": True}


def test_long_context_tier_cache_write_price(catalog):
    from modelspend.pricing import cost_for
    m = Catalog.load().get("anthropic/claude-haiku-5-5")
    small = cost_for(m, Usage(input_tokens=1000, cache_write_tokens=1000, cache_read_tokens=1000, output_tokens=1000))
    assert small == pytest.approx((1000 * 0.10 + 1000 * 0.125 + 1000 * 0.01 + 1000 * 0.50) / 1e6)
    big = cost_for(m, Usage(input_tokens=60000, cache_write_tokens=30000, cache_read_tokens=20000, output_tokens=1000))
    assert big == pytest.approx((60000 * 0.50 + 30000 * 0.625 + 20000 * 0.05 + 1000 * 2.50) / 1e6)
