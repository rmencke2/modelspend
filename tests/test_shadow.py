"""0.7: shadow evaluation. Real requests are sometimes also tried on cheaper models in the background and compared with the answer
that was used. These tests pin down what is compared, what is stored (never text), what it costs, and that it can never hurt the request."""
from __future__ import annotations

import random

import pytest

from conftest import make_engine
from modelspend import RouteRequest
from modelspend import shadow as sh
from modelspend.config import ShadowSettings, parse_shadow
from modelspend.errors import ProviderError
from modelspend.providers.base import ProviderResponse
from modelspend.store import Store
from modelspend.types import TaskCategory, Usage, ValidatorSpec

LARGE = "openai/synthetic-openai-large"
SECRET_PROMPT = "PROMPT-SECRET-4711"
SECRET_ANSWER = "ANSWER-SECRET-0815"


def req(text=f"Classify this review: {SECRET_PROMPT} was great", **kw):
    kw.setdefault("providers", ["openai"])
    kw.setdefault("pin", LARGE)
    kw.setdefault("task_type", TaskCategory.classification)
    return RouteRequest(messages=[{"role": "user", "content": text}], **kw)


def judge_prompt(r) -> bool:
    return any("You compare two answers" in (m.content or "") for m in r.messages)


def make(catalog, cfg, script, store=None, **settings):
    settings.setdefault("sample_rate", 1.0)
    cfg.shadow = ShadowSettings(enabled=True, **settings)
    eng, _ = make_engine(catalog, cfg, {"openai": script}, store=store or Store(":memory:"))
    return eng


async def run(eng, r=None):
    res = await eng.run(r or req())
    await eng.shadow.drain()
    return res


def rows(store):
    return [dict(r) for r in store.db.execute("select * from shadow_results order by id")]


# ---- settings ----
def test_settings_parse_validate_and_reject_typos(monkeypatch):
    s = parse_shadow({"enabled": True, "sample_rate": 0.2, "monthly_budget_usd": 3})
    assert (s.enabled, s.sample_rate, s.monthly_budget_usd, s.max_candidates, s.judge, s.use_as_evidence) == (True, 0.2, 3.0, 2, True, False)
    assert parse_shadow(None).enabled is False
    assert parse_shadow({}, "true").enabled is True and parse_shadow({"enabled": True}, "false").enabled is False
    for bad, msg in [({"sample_rat": 1}, "unknown shadow setting"), ({"enabled": "yes"}, "true or false"), ({"sample_rate": 2}, "between 0 and 1"),
                     ({"monthly_budget_usd": -1}, "zero or more"), ({"max_candidates": 9}, "between 1 and 5"), ({"target_n": 3}, "between 10"),
                     ({"sample_rate": "lots"}, "bad shadow setting"), ("on", "must be an object")]:
        with pytest.raises(ValueError, match=msg):
            parse_shadow(bad)


# ---- what is tried, and when ----
async def test_a_pinned_request_is_also_tried_on_cheaper_models_and_compared(catalog, cfg):
    store = Store(":memory:")
    eng = make(catalog, cfg, "positive", store)
    res = await run(eng)
    assert res.status == "ok" and res.model == "synthetic-openai-large"
    got = rows(store)
    assert {(r["cand_model"], r["verdict"], r["reason"], r["method"]) for r in got} == {
        ("synthetic-openai-small", "pass", "exact_match", "exact"), ("synthetic-openai-mid", "pass", "exact_match", "exact")}  # the deprecated 'legacy' model is not eligible
    r = got[0]
    assert (r["category"], r["used_model"], r["project"]) == ("classification", "synthetic-openai-large", None)
    assert r["used_cost"] > r["cand_cost"] > 0 and r["cand_input"] > 0 and r["cand_output"] > 0


async def test_nothing_stored_anywhere_contains_the_prompt_or_any_answer(catalog, cfg):
    store = Store(":memory:")

    def script(r, n):
        if judge_prompt(r):
            return '{"pass": true}'
        return SECRET_ANSWER if "large" in r.model else f"{SECRET_ANSWER} but different words"

    eng = make(catalog, cfg, script, store)
    await run(eng)
    assert rows(store) and store.db.execute("select count(*) from requests where project = 'modelspend-shadow'").fetchone()[0] > 0
    for (table,) in store.db.execute("select name from sqlite_master where type = 'table'").fetchall():
        for row in store.db.execute(f"select * from {table}"):
            blob = " ".join(str(v) for v in tuple(row))
            assert SECRET_PROMPT not in blob and SECRET_ANSWER not in blob, f"text leaked into {table}"


async def test_shadow_calls_are_in_the_ledger_under_their_own_project(catalog, cfg):
    store = Store(":memory:")
    eng = make(catalog, cfg, "positive", store)
    await run(eng)
    shadow_rows = store.db.execute("select project, agent, task, status from requests where project = 'modelspend-shadow'").fetchall()
    assert len(shadow_rows) == 2 and {tuple(r) for r in shadow_rows} == {("modelspend-shadow", "shadow", "shadow", "ok")}
    assert store.db.execute("select count(*) from requests where project is null").fetchone()[0] == 1  # your own call, untouched
    label = store.db.execute("select labels from requests where project = 'modelspend-shadow' limit 1").fetchone()[0]
    assert "origin" in label


async def test_validators_decide_when_the_request_has_them(catalog, cfg):
    store = Store(":memory:")

    def script(r, n):
        return "billing" if "large" in r.model or "small" in r.model else "gibberish"  # small agrees, mid does not

    eng = make(catalog, cfg, script, store)
    await run(eng, req(validators=[ValidatorSpec(type="allowed_labels", labels=["billing", "bug"])]))
    by = {r["cand_model"]: (r["verdict"], r["method"], r["reason"]) for r in rows(store)}
    assert by["synthetic-openai-small"] == ("pass", "validators", "validators_passed")
    assert by["synthetic-openai-mid"] == ("fail", "validators", "validators_failed")


async def test_the_judge_compares_answers_that_differ(catalog, cfg):
    store = Store(":memory:")
    verdicts = iter(['{"pass": true}', '{"pass": false}'])

    def script(r, n):
        if judge_prompt(r):
            return next(verdicts)
        return "The refund was issued." if "large" in r.model else "We refunded you."

    eng = make(catalog, cfg, script, store)
    await run(eng)
    got = sorted((r["verdict"], r["reason"], r["method"]) for r in rows(store))
    assert got == [("fail", "judge_fail", "judge"), ("pass", "judge_pass", "judge")]
    assert all(r["judge_cost"] > 0 for r in rows(store))
    assert store.db.execute("select count(*) from attempts where kind = 'judge'").fetchone()[0] == 2  # the judge calls are in the ledger too


@pytest.mark.parametrize("judge_reply,reason", [("not json at all", "judge_unparseable"), ('{"pass": "maybe"}', "judge_unparseable")])
async def test_an_unreadable_judge_verdict_is_unscored_not_counted(catalog, cfg, judge_reply, reason):
    store = Store(":memory:")
    eng = make(catalog, cfg, lambda r, n: judge_reply if judge_prompt(r) else ("a" if "large" in r.model else "b"), store)
    await run(eng)
    assert {(r["verdict"], r["reason"]) for r in rows(store)} == {("unscored", reason)}
    assert sh.counts(store, "classification") == {}  # unscored comparisons are not evidence
    assert all(a["n"] == 0 and a["unscored"] == 1 for a in sh.aggregate(store)) and sh.shadow_evidence(store) == {}


async def test_without_a_judge_differing_answers_are_unscored(catalog, cfg):
    store = Store(":memory:")
    eng = make(catalog, cfg, lambda r, n: "a" if "large" in r.model else "b", store, judge=False)
    await run(eng)
    assert {(r["verdict"], r["reason"], r["judge_cost"]) for r in rows(store)} == {("unscored", "no_comparison", 0)}
    assert store.db.execute("select count(*) from attempts where kind = 'judge'").fetchone()[0] == 0


async def test_a_candidate_cut_off_at_the_cap_fails_as_truncated(catalog, cfg):
    store = Store(":memory:")

    def script(r, n):
        if "large" in r.model:
            return "ok answer"
        return ProviderResponse(text="", finish_reason="length", usage=Usage(input_tokens=10, output_tokens=r.max_output_tokens))

    eng = make(catalog, cfg, script, store)
    await run(eng)
    assert {(r["verdict"], r["reason"]) for r in rows(store)} == {("fail", "truncated")}


async def test_a_failing_candidate_call_is_unscored_and_the_next_one_still_runs(catalog, cfg):
    store = Store(":memory:")

    def script(r, n):
        if "small" in r.model:
            return ProviderError("openai", "auth", "bad key", retryable=False)
        return "positive"

    eng = make(catalog, cfg, script, store)
    res = await run(eng)
    assert res.status == "ok"
    assert {(r["cand_model"], r["verdict"], r["reason"]) for r in rows(store)} == {
        ("synthetic-openai-small", "unscored", "provider_error"), ("synthetic-openai-mid", "pass", "exact_match")}


# ---- when it does NOT run ----
async def test_it_is_off_by_default(catalog, cfg):
    store = Store(":memory:")
    eng, _ = make_engine(catalog, cfg, {"openai": "positive"}, store=store)
    assert eng.shadow is None
    await eng.run(req())
    assert rows(store) == [] and store.db.execute("select count(*) from requests").fetchone()[0] == 1


async def test_it_can_be_disabled_per_engine_even_when_enabled_in_settings(catalog, cfg):
    cfg.shadow = ShadowSettings(enabled=True, sample_rate=1.0)
    from modelspend.engine import Engine
    eng = Engine(catalog, cfg, make_engine(catalog, cfg, {})[0].adapters, Store(":memory:"), shadow=False)
    assert eng.shadow is None


@pytest.mark.parametrize("why,kw,settings", [
    ("sample rate zero", {}, {"sample_rate": 0.0}),
    ("sensitive request", {"sensitive": True}, {}),
    ("ModelSpend's own eval traffic", {"project": "modelspend-eval"}, {}),
    ("ModelSpend's own shadow traffic", {"project": "modelspend-shadow"}, {}),
    ("unknown task type", {"task_type": None, "pin": LARGE}, {}),
])
async def test_requests_that_must_not_be_shadowed_are_not(catalog, cfg, why, kw, settings):
    store = Store(":memory:")
    cfg.sensitive_providers = ["openai"]  # so that a sensitive request itself is allowed to run
    eng = make(catalog, cfg, "positive", store, **settings)
    r = req(**kw)
    if why == "unknown task type":
        r = req(text="hmm", task_type=None)
    await run(eng, r)
    assert rows(store) == [], why


async def test_sensitive_requests_are_shadowed_only_when_you_allow_it(catalog, cfg):
    store = Store(":memory:")
    eng = make(catalog, cfg, "positive", store, include_sensitive=True)
    cfg.sensitive_providers = ["openai"]
    await run(eng, req(sensitive=True))
    assert rows(store)


async def test_failed_validation_and_tool_calls_are_not_used_as_the_reference(catalog, cfg):
    store = Store(":memory:")
    eng = make(catalog, cfg, "gibberish", store)
    await run(eng, req(validators=[ValidatorSpec(type="allowed_labels", labels=["billing"])], escalation_limit=0))
    assert rows(store) == []  # the answer that was used failed its own validators: nothing to compare against
    eng2 = make(catalog, cfg, lambda r, n: ProviderResponse(text="x", tool_calls=[{"id": "1", "name": "f", "arguments": "{}"}], usage=Usage(input_tokens=5, output_tokens=5)), Store(":memory:"))
    await run(eng2, req())
    assert rows(eng2.store) == []


# ---- money ----
async def test_the_monthly_budget_is_a_ceiling_checked_before_each_call(catalog, cfg):
    store = Store(":memory:")
    eng = make(catalog, cfg, "positive", store, monthly_budget_usd=0.0)
    await run(eng)
    assert rows(store) == [] and sh.spent_this_month(store) == 0.0  # a zero budget means nothing is spent

    # the check uses each call's WORST case (answer + judge), so size the budget from that: room for one comparison, not two
    probe = make(catalog, cfg, "positive", Store(":memory:"), monthly_budget_usd=100.0)
    r = req()
    plan = await probe.plan(r)
    pool = probe.shadow._pool(r, plan)
    picks = sh.pick_candidates(pool, plan.candidates[0], {}, 2, 100)
    worst = [(c.worst_case_cost_usd or 0) + (probe.shadow._judge_candidate(pool, plan.candidates[0], "x", r).worst_case_cost_usd or 0) for c in picks]
    budget = worst[0] * 1.2
    assert budget < worst[0] + worst[1]

    store3 = Store(":memory:")
    tight = make(catalog, cfg, "positive", store3, monthly_budget_usd=budget, max_candidates=2)
    for _ in range(4):
        await run(tight)
    spent = sh.spent_this_month(store3)
    assert 0 < spent <= budget and 1 <= len(rows(store3)) < 8  # some comparisons ran, then the ceiling stopped them
    assert tight.shadow._reserved == 0.0  # nothing is left reserved after the comparisons finish


async def test_spend_from_earlier_months_does_not_count(catalog, cfg):
    store = Store(":memory:")
    eng = make(catalog, cfg, "positive", store)
    await run(eng)
    assert sh.spent_this_month(store) > 0
    assert sh.spent_this_month(store, now=sh.month_start() + 40 * 86400) == 0.0
    assert sh.month_start(1791590400.0) == 1790812800.0  # 2026-10-01 00:00 UTC


async def test_pairs_with_enough_comparisons_are_left_alone(catalog, cfg):
    store = Store(":memory:")
    eng = make(catalog, cfg, "positive", store, target_n=10, max_candidates=3)
    for _ in range(12):
        await run(eng)
    n = sh.counts(store, "classification")
    assert max(n.values()) == 10  # sampling stops at target_n
    assert len(rows(store)) == sum(n.values())


def test_candidate_choice_explores_thin_evidence_first_and_skips_what_it_cannot_compare():
    from modelspend.types import Candidate

    def c(model, cost, worst=1.0):
        return Candidate(provider="openai", model=model, tier=2, suitability_basis="prior", suitability_detail="", input_tokens_est=1,
                         expected_output_tokens=1, worst_case_output_tokens=1, expected_cost_usd=cost, worst_case_cost_usd=worst)

    used = c("big", 10.0)
    pool = [c("a", 1.0), c("b", 2.0), c("c", 3.0), c("big", 10.0), c("pricey", 20.0), c("unpriced", None), c("noworst", 0.5, worst=None)]
    have = {"openai/a": 5, "openai/b": 0, "openai/c": 0}
    assert [x.model for x in sh.pick_candidates(pool, used, have, 3, 100)] == ["b", "c", "a"]  # fewest comparisons first, then cheapest
    assert [x.model for x in sh.pick_candidates(pool, used, have, 3, 5)] == ["b", "c"]  # 'a' reached target_n
    assert sh.pick_candidates(pool, c("big", None), have, 3, 100) == []  # nothing to compare cost against


# ---- it can never hurt the request ----
async def test_a_failure_inside_shadowing_never_reaches_the_caller(catalog, cfg, monkeypatch):
    store = Store(":memory:")
    eng = make(catalog, cfg, "positive", store)

    async def boom(*a, **k):
        raise RuntimeError(f"{SECRET_PROMPT} exploded")

    monkeypatch.setattr(eng.shadow, "_shadow", boom)
    res = await run(eng)
    assert res.status == "ok" and res.text == "positive" and rows(store) == []


async def test_the_caller_does_not_wait_for_the_comparison(catalog, cfg):
    store = Store(":memory:")
    eng = make(catalog, cfg, "positive", store)
    res = await eng.run(req())  # returns without draining
    assert res.status == "ok" and len(eng.shadow._tasks) >= 1  # the comparison is still pending in the background
    await eng.shadow.drain()
    assert len(rows(store)) == 2


async def test_too_many_comparisons_in_flight_are_dropped_not_queued(catalog, cfg):
    store = Store(":memory:")
    eng = make(catalog, cfg, "positive", store)
    for _ in range(sh.MAX_PENDING + 3):
        await eng.run(req())
    assert eng.shadow.dropped >= 3
    await eng.shadow.drain()


async def test_the_sample_rate_is_honoured(catalog, cfg):
    store = Store(":memory:")
    eng = make(catalog, cfg, "positive", store, sample_rate=0.25)
    eng.shadow.rng = random.Random(7)
    for _ in range(40):
        await eng.run(req())
        await eng.shadow.drain()
    sampled = len({r["request_id"] for r in rows(store)})
    assert 4 <= sampled <= 16  # about a quarter of 40


# ---- reading it back ----
async def test_aggregate_evidence_and_prune(catalog, cfg):
    store = Store(":memory:")
    verdicts = iter(['{"pass": true}', '{"pass": false}'] * 20)
    eng = make(catalog, cfg, lambda r, n: next(verdicts) if judge_prompt(r) else ("a" if "large" in r.model else "b"), store, max_candidates=1)
    for _ in range(4):
        await run(eng)
    agg = sh.aggregate(store)  # with max_candidates=1 it alternates between the two least-tested cheaper models
    assert {a["model"] for a in agg} == {"synthetic-openai-small", "synthetic-openai-mid"} and {a["category"] for a in agg} == {"classification"}
    assert sum(a["n"] for a in agg) == 4 and sum(a["passes"] for a in agg) == 2 and sum(a["unscored"] for a in agg) == 0
    assert all(a["cost_rows"] == a["n"] and 0 < a["cand_cost"] < a["used_cost"] for a in agg)
    ev = sh.shadow_evidence(store)
    assert set(ev) == {("openai/synthetic-openai-small", "classification"), ("openai/synthetic-openai-mid", "classification")}
    assert all(i.dataset == "shadow" and i.source == "modelspend shadow" and i.n == 2 for i in ev.values())
    store.prune(1e12)
    assert rows(store) == []
