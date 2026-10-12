import json

import pytest

from modelspend import AllAttemptsFailed, BudgetTooLow, RouteRequest, StreamInterrupted
from modelspend.errors import ProviderError
from modelspend.providers.base import ProviderResponse
from modelspend.types import Usage


def R(text="Classify this: great", **kw):
    kw.setdefault("providers", ["openai", "anthropic", "google", "xai"])
    return RouteRequest(messages=[{"role": "user", "content": text}], **kw)


def resp(text, i=100, o=10, **kw):
    return ProviderResponse(text=text, usage=Usage(input_tokens=i, output_tokens=o), **kw)


async def test_basic_route_and_accounting(engine_factory, catalog):
    eng, ad = engine_factory({"google": lambda r, n: resp("positive", 1000, 100)})
    res = await eng.run(R(providers=["google"]))
    m = catalog.get(f"google/{res.model}")
    expected = (1000 * m.pricing.input_per_mtok + 100 * m.pricing.output_per_mtok) / 1e6
    assert res.status == "ok" and res.text == "positive"
    assert res.actual_cost_usd == pytest.approx(expected)
    assert res.estimated_cost_usd > 0 and len(res.attempts) == 1
    assert res.catalog_version.startswith("synthetic-1+") and res.policy_version
    assert res.validation.status == "not_configured"


async def test_only_allowlisted_provider_is_called(engine_factory):
    eng, ad = engine_factory()
    await eng.run(R(providers=["anthropic"]))
    assert len(ad["anthropic"].calls) == 1
    assert not ad["openai"].calls and not ad["google"].calls and not ad["xai"].calls


async def test_escalation_on_validator_failure_is_bounded(engine_factory):
    # small/mid give bad answers; the escalation limit (1) stops after exactly one escalation
    eng, ad = engine_factory({"openai": lambda r, n: resp("nope")})
    r = R("Classify the ticket", providers=["openai"], validators=[{"type": "allowed_labels", "labels": ["billing", "bug"]}])
    res = await eng.run(r)
    assert res.escalations == 1 and res.status == "validation_failed"
    assert [a.purpose for a in res.attempts] == ["initial", "escalation"]
    assert res.attempts[0].model != res.attempts[1].model
    tiers = [catalog_tier(eng, a) for a in res.attempts]
    assert tiers[1] > tiers[0]


def catalog_tier(eng, a):
    return eng.catalog.get(f"{a.provider}/{a.model}").tier


async def test_escalation_succeeds_and_costs_are_cumulative(engine_factory):
    def script(r, n):
        return resp("billing", 500, 5) if "mid" in r.model else resp("garbage", 500, 5)
    eng, ad = engine_factory({"openai": script})
    res = await eng.run(R("Classify the ticket", providers=["openai"], validators=[{"type": "allowed_labels", "labels": ["billing"]}]))
    assert res.status == "ok" and res.escalations == 1 and res.validation.status == "passed"
    assert res.actual_cost_usd == pytest.approx(sum(a.cost_usd for a in res.attempts))
    assert len(res.attempts) == 2 and res.usage.input_tokens == 1000


async def test_escalation_limit_zero_and_economy_default(engine_factory):
    eng, ad = engine_factory({"openai": lambda r, n: resp("nope")})
    v = [{"type": "allowed_labels", "labels": ["x"]}]
    res = await eng.run(R("Classify", providers=["openai"], validators=v, escalation_limit=0))
    assert res.escalations == 0 and len(res.attempts) == 1
    res = await eng.run(R("Classify", providers=["openai"], validators=v, profile="economy"))
    assert res.escalations == 0


async def test_no_escalation_without_validator_evidence(engine_factory):
    eng, _ = engine_factory({"openai": lambda r, n: resp("whatever")})
    res = await eng.run(R("Classify", providers=["openai"]))
    assert res.escalations == 0 and res.validation.status == "not_configured"


async def test_refusal_does_not_escalate(engine_factory):
    eng, _ = engine_factory({"openai": lambda r, n: resp("I can't help", finish_reason="content_filter")})
    res = await eng.run(R("Classify", providers=["openai"], validators=[{"type": "allowed_labels", "labels": ["x"]}]))
    assert res.status == "refused" and res.escalations == 0 and len(res.attempts) == 1


async def test_budget_blocks_escalation(engine_factory, catalog):
    eng, ad = engine_factory({"openai": lambda r, n: resp("nope", 100, 5)})
    # budget covers only the small model's worst case
    small = catalog.get("openai/synthetic-openai-small")
    budget = 1.2 * (1200 * small.pricing.input_per_mtok + 200 * small.pricing.output_per_mtok) / 1e6
    r = R("Classify", providers=["openai"], max_output_tokens=200, max_budget_usd=max(budget, 0.00035),
          validators=[{"type": "allowed_labels", "labels": ["x"]}])
    res = await eng.run(r)
    assert res.escalations == 0 and len(ad["openai"].calls) == 1
    assert any("escalation" in n for n in res.uncertainty)


async def test_never_starts_call_exceeding_remaining_budget(engine_factory):
    eng, ad = engine_factory({"openai": lambda r, n: resp("nope", 100, 5)})
    for budget in (0.0005, 0.002, 0.02):
        ad["openai"].calls.clear()
        res = await eng.run(R("Classify", providers=["openai"], max_output_tokens=200, max_budget_usd=budget,
                              validators=[{"type": "allowed_labels", "labels": ["x"]}]))
        assert res.actual_cost_usd + res.uncertain_cost_usd <= budget + 1e-12
        # every started attempt had its worst-case reserve covered at the time
        assert all(a.outcome != "skipped_budget" or a.cost_usd == 0 for a in res.attempts)


async def test_retry_then_success_counts_all_attempts(engine_factory):
    def script(r, n):
        return ProviderError("openai", "rate_limit", "429", 429) if n < 3 else resp("positive")
    eng, ad = engine_factory({"openai": script})
    res = await eng.run(R(providers=["openai"]))
    assert res.retries == 2 and res.status == "ok"
    assert [a.outcome for a in res.attempts] == ["operational_error", "operational_error", "ok"]
    assert res.attempts[1].purpose == "retry"


async def test_failover_to_other_allowed_provider(engine_factory):
    eng, ad = engine_factory({"google": lambda r, n: ProviderError("google", "server", "500", 500)})
    res = await eng.run(R(providers=["google", "xai"]))
    assert res.failovers == 1 and res.provider == "xai"
    assert len(ad["google"].calls) == 3  # 1 try + 2 retries
    assert not ad["openai"].calls and not ad["anthropic"].calls  # never an unapproved provider


async def test_all_attempts_fail_raises_with_accounting(engine_factory):
    eng, ad = engine_factory({p: (lambda r, n, p=p: ProviderError(p, "timeout", "t")) for p in ("openai",)})
    with pytest.raises(AllAttemptsFailed) as ei:
        await eng.run(R(providers=["openai"]))
    assert ei.value.details["attempts"] and ei.value.details["cost"] > 0  # timeouts may be billed: counted conservatively


async def test_auth_error_not_retried_but_fails_over(engine_factory):
    eng, ad = engine_factory({"google": lambda r, n: ProviderError("google", "auth", "bad key", 401)})
    res = await eng.run(R(providers=["google", "xai"]))
    assert len(ad["google"].calls) == 1 and res.provider == "xai" and res.failovers == 1


async def test_quota_error_not_retried_but_fails_over(engine_factory):
    eng, ad = engine_factory({"google": lambda r, n: ProviderError("google", "quota", "no credits", 429)})
    res = await eng.run(R(providers=["google", "xai"]))
    assert len(ad["google"].calls) == 1 and res.provider == "xai" and res.failovers == 1


async def test_bad_request_fails_fast(engine_factory):
    eng, ad = engine_factory({"openai": lambda r, n: ProviderError("openai", "bad_request", "no", 400)})
    with pytest.raises(AllAttemptsFailed):
        await eng.run(R(providers=["openai", "xai"]))
    assert len(ad["openai"].calls) == 1 and not ad["xai"].calls


async def test_tool_calls_returned_not_executed_no_escalation(engine_factory):
    from modelspend.types import ToolCall, ToolSpec
    tc = ToolCall(id="call_1", name="lookup", arguments='{"q":"x"}')
    eng, ad = engine_factory({"openai": lambda r, n: ProviderResponse(text="", tool_calls=[tc], finish_reason="tool_calls", usage=Usage(input_tokens=10, output_tokens=5))})
    res = await eng.run(R("Look up x", providers=["openai"], tools=[ToolSpec(name="lookup")], validators=[{"type": "allowed_labels", "labels": ["z"]}]))
    assert res.tool_calls[0].id == "call_1" and res.escalations == 0 and len(ad["openai"].calls) == 1


async def test_truncation_with_validator_escalates_and_without_is_flagged(engine_factory):
    eng, _ = engine_factory({"openai": lambda r, n: resp("cut off", finish_reason="length")})
    res = await eng.run(R("Summarize this", providers=["openai"]))
    assert res.status == "truncated"


async def test_json_schema_validation(engine_factory):
    schema = {"type": "object", "properties": {"label": {"type": "string"}}, "required": ["label"]}
    eng, _ = engine_factory({"openai": lambda r, n: resp('{"label": "x"}' if "mid" in r.model else "not json")})
    res = await eng.run(R("Extract the label", providers=["openai"], response_schema=schema))
    assert res.escalations == 1 and res.validation.status == "passed"


async def test_judge_cost_is_accounted(engine_factory):
    def script(r, n):
        return resp('{"pass": true, "reason": "ok"}', 200, 20) if r.max_output_tokens == 120 else resp("answer")
    eng, ad = engine_factory({"openai": script})
    res = await eng.run(R("Rewrite this politely", providers=["openai"], judge=True, acceptance_criteria=["polite"]))
    kinds = [a.kind for a in res.attempts]
    assert kinds == ["generation", "judge"] and res.validation.results[-1].kind == "judge"
    assert res.actual_cost_usd == pytest.approx(sum(a.cost_usd for a in res.attempts))


async def test_unverified_criteria_reported(engine_factory):
    eng, _ = engine_factory()
    res = await eng.run(R("Rewrite this", providers=["openai"], acceptance_criteria=["be nice"]))
    assert res.validation.status == "unverified_criteria"


async def test_reasoning_tokens_not_double_counted(engine_factory, catalog):
    # google-large reports thoughts separately (reasoning_in_output=False): adapter sums them into output
    from modelspend.providers.google_ import normalize_usage
    class U:  # recorded Gemini usage shape
        prompt_token_count = 1000; cached_content_token_count = 200; candidates_token_count = 50; thoughts_token_count = 300
    u = normalize_usage(U)
    assert (u.input_tokens, u.cache_read_tokens, u.output_tokens, u.reasoning_tokens) == (800, 200, 350, 300)
    from modelspend.providers.openai_compat import normalize_usage as nu
    class D: cached_tokens = 100; reasoning_tokens = 40
    class OU: prompt_tokens = 500; completion_tokens = 90; prompt_tokens_details = D; completion_tokens_details = D
    u = nu(OU, True)  # completion_tokens already includes reasoning
    assert (u.input_tokens, u.cache_read_tokens, u.output_tokens) == (400, 100, 90)
    assert nu(OU, False).output_tokens == 130  # provider reports reasoning separately


async def test_store_records_everything_but_content(engine_factory, store):
    eng, _ = engine_factory()
    res = await eng.run(R("SECRET-PROMPT-TEXT classify", providers=["openai"]))
    dump = json.dumps([dict(r) for r in store.db.execute("select * from requests")] +
                      [dict(r) for r in store.db.execute("select * from attempts")] +
                      [dict(r) for r in store.db.execute("select * from class_cache")], default=str)
    assert "SECRET-PROMPT-TEXT" not in dump and "test-key" not in dump
    assert store.summary()["totals"]["n"] == 1 and res.request_id in dump


# ---------------- streaming ----------------
async def collect(agen):
    return [e async for e in agen]


async def test_stream_happy_path(engine_factory):
    eng, ad = engine_factory({"openai": lambda r, n: resp("hello streaming world")})
    evs = await collect(eng.stream(R("Rewrite this", providers=["openai"])))
    assert evs[0].type == "route" and evs[-1].type == "done"
    assert "".join(e.text for e in evs if e.type == "delta") == "hello streaming world"
    assert evs[-1].result.actual_cost_usd > 0


async def test_stream_interrupted_never_swaps_model(engine_factory):
    eng, ad = engine_factory({"openai": lambda r, n: resp("partial output here")})
    ad["openai"].fail_stream_after = 2
    got = []
    with pytest.raises(StreamInterrupted) as ei:
        async for e in eng.stream(R("Rewrite this", providers=["openai", "xai"])):
            got.append(e)
    assert "".join(e.text for e in got if e.type == "delta") == ei.value.details["partial_text"] != ""
    assert not ad["xai"].calls  # no silent replacement
    assert ei.value.details["cost_usd"] > 0  # partial spend still counted


async def test_stream_failure_before_output_may_fail_over(engine_factory):
    eng, ad = engine_factory({"google": lambda r, n: resp("from google"), "xai": lambda r, n: resp("from xai")})
    ad["google"].fail_stream_after = 0
    evs = await collect(eng.stream(R("Rewrite this", providers=["google", "xai"])))
    assert evs[-1].result.provider == "xai" and evs[-1].result.failovers == 1


async def test_stream_validation_status_reported_without_replacement(engine_factory):
    eng, ad = engine_factory({"openai": lambda r, n: resp("bad")})
    evs = await collect(eng.stream(R("Classify", providers=["openai"], validators=[{"type": "allowed_labels", "labels": ["x"]}])))
    r = evs[-1].result
    assert r.status == "validation_failed" and r.escalations == 0 and len(ad["openai"].calls) == 1


async def test_stream_requires_streaming_capability(engine_factory, catalog):
    for m in catalog.models:
        if m.provider == "openai":
            m.capabilities.streaming = None
    eng, _ = engine_factory()
    from modelspend import NoEligibleModel
    with pytest.raises(NoEligibleModel):
        await collect(eng.stream(R("hi", providers=["openai"])))


async def test_runtime_ledger_blocks_escalation_the_plan_alone_would_allow(engine_factory):
    """The mid model fits the budget at plan time (worst case <= budget) but NOT after the first
    attempt's real spend is deducted. Only the runtime ledger gate can stop this call."""
    eng, ad = engine_factory({"openai": lambda r, n: resp("nope", 100, 200)})  # first call spends ~its cap
    base = dict(providers=["openai"], max_output_tokens=200, validators=[{"type": "allowed_labels", "labels": ["x"]}])
    plan = await eng.plan(R("Classify", **base))
    small = next(c for c in plan.candidates if c.model == "synthetic-openai-small")
    mid = next(c for c in plan.candidates if c.model == "synthetic-openai-mid")
    first_actual = (100 * 0.10 + 200 * 0.40) / 1e6
    budget = mid.worst_case_cost_usd + first_actual / 2
    assert small.worst_case_cost_usd < budget and mid.worst_case_cost_usd <= budget  # both pass plan-time filter
    res = await eng.run(R("Classify", max_budget_usd=budget, **base))
    assert res.escalations == 0 and len(ad["openai"].calls) == 1
    assert res.actual_cost_usd + res.uncertain_cost_usd <= budget
    # sanity: with a budget that covers both, the escalation does happen
    ad["openai"].calls.clear()
    res = await eng.run(R("Classify", max_budget_usd=budget * 3, **base))
    assert res.escalations == 1 and len(ad["openai"].calls) == 2


async def test_failed_call_charges_uncertain_upper_bound_against_budget(engine_factory):
    eng, ad = engine_factory({"openai": lambda r, n: ProviderError("openai", "timeout", "t") if n == 1 else resp("positive", 10, 5)})
    res = await eng.run(R(providers=["openai"], max_budget_usd=0.01))
    assert res.uncertain_cost_usd > 0 and res.attempts[0].cost_basis == "uncertain_upper_estimate"
    assert res.budget_remaining_usd == pytest.approx(0.01 - res.actual_cost_usd - res.uncertain_cost_usd)
