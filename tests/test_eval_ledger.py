"""Live eval and calibration calls spend real money, so they belong in the usage ledger (found by reconciling with OpenAI's export:
about 35,000 tokens a day were missing from `report`). They are tagged project=modelspend-eval so they can be filtered in or out."""
from __future__ import annotations

import pytest

from conftest import make_engine
from modelspend import RouteRequest
from modelspend.config import Config
from modelspend.errors import AllAttemptsFailed, ProviderError
from modelspend.eval.harness import calibrate, evaluate, load_tasks
from modelspend.eval.sim import build_sim_engine
from modelspend.report import build_report
from modelspend.store import Store


def totals(store, **filters):
    return build_report(store, [], filters=filters)["totals"]


async def test_calibration_calls_are_recorded_under_the_eval_project(catalog):
    store = Store(":memory:")
    tasks = load_tasks("calibration")[:6]
    eng = build_sim_engine(catalog, tasks, providers=["openai"], store=store)
    ev, spent, _ = await calibrate(eng, tasks, ["openai"])
    rows = store.db.execute("select project, agent, task, status, count(*) n from requests group by 1,2,3,4").fetchall()
    assert {(r["project"], r["agent"], r["task"], r["status"]) for r in rows} == {("modelspend-eval", "calibrate", "eval", "ok")}
    n_calls = sum(i.n for cats in ev.values() for i in cats.values())
    assert rows[0]["n"] == n_calls > 0  # one ledger row per model call, none skipped
    t = totals(store, project="modelspend-eval", agent="calibrate")
    assert t["total_tokens"] > 0 and t["cost"] == pytest.approx(spent) and t["requests"] == n_calls


async def test_eval_run_records_baselines_and_the_modelspend_arm(catalog):
    store = Store(":memory:")
    tasks = load_tasks("heldout")[:4]
    eng = build_sim_engine(catalog, tasks, providers=["openai", "anthropic"], store=store)
    rep = await evaluate(eng, tasks, ["openai", "anthropic"])
    t = totals(store, project="modelspend-eval", agent="eval-run")
    assert t["cost"] == pytest.approx(sum(a["total_spend_usd"] for a in rep["arms"].values()))  # the ledger holds every arm's spend
    assert store.db.execute("select count(*) from requests where task = 'eval'").fetchone()[0] == 2 * len(tasks)
    assert store.db.execute("select count(*) from requests where project != 'modelspend-eval'").fetchone()[0] == 0


async def test_recording_never_changes_routing_stickiness_is_off_for_evals(catalog):
    cfg = Config(api_keys={"openai": "sim"}, retry_backoff_s=0.0, sticky=True)  # a user who turned stickiness on globally
    store = Store(":memory:")
    tasks = load_tasks("calibration")[:4]
    eng = build_sim_engine(catalog, tasks, providers=["openai"], store=store)
    eng.cfg = cfg
    await calibrate(eng, tasks, ["openai"])
    await evaluate(eng, load_tasks("heldout")[:3], ["openai"])
    assert store.db.execute("select count(*) from conversations").fetchone()[0] == 0


async def test_failed_baseline_calls_are_recorded_too(catalog, cfg):
    store = Store(":memory:")

    def boom(req, n):
        raise ProviderError("openai", "server_error", "down", retryable=False)

    eng, _ = make_engine(catalog, cfg, {"openai": boom}, store=store)
    req = RouteRequest(providers=["openai"], messages=[{"role": "user", "content": "x"}], project="modelspend-eval", agent="eval-run", sticky=False)
    from modelspend.eval.harness import _eligible
    cand = _eligible(eng, req)[0]
    from modelspend.engine import Ledger
    with pytest.raises(ProviderError):
        await eng.run_single(req, cand, Ledger(None))
    r = store.db.execute("select status, project from requests").fetchone()
    assert (r["status"], r["project"]) == ("provider_error", "modelspend-eval")
    assert store.db.execute("select count(*) from attempts").fetchone()[0] >= 1


async def test_run_single_without_a_store_still_works(catalog, cfg):
    eng, _ = make_engine(catalog, cfg, {"openai": "ok"})
    from modelspend.engine import Ledger
    from modelspend.eval.harness import _eligible
    req = RouteRequest(providers=["openai"], messages=[{"role": "user", "content": "x"}])
    resp, _lat = await eng.run_single(req, _eligible(eng, req)[0], Ledger(None))
    assert resp.text == "ok"


# ---- reasoning models that run out of output tokens must not be scored as if they answered wrongly ----
def cut_off(eng, below=1000, always=False):
    """Make every adapter behave like a reasoning model: with a small output cap it thinks until the limit and returns nothing."""
    for ad in eng.adapters.values():
        orig = ad.generate

        async def generate(req, orig=orig):
            r = await orig(req)
            if always or (req.max_output_tokens or 0) < below:
                return r.model_copy(update={"text": "", "finish_reason": "length"})
            return r
        ad.generate = generate
    return eng


async def test_a_call_cut_off_at_the_cap_is_retried_once_and_judged_on_the_retry(catalog):
    store = Store(":memory:")
    tasks = load_tasks("calibration")[:6]
    eng = cut_off(build_sim_engine(catalog, tasks, providers=["openai"], store=store))
    stats: dict = {}
    fails: list = []
    ev, spent, _ = await calibrate(eng, tasks, ["openai"], None, False, "x", fails, stats)
    calls = sum(i.n for cats in ev.values() for i in cats.values())
    assert stats["retried"] == calls and stats.get("still_truncated", 0) == 0  # every call was cut off once, then answered
    assert store.db.execute("select count(*) from requests").fetchone()[0] == 2 * calls  # both calls are in the ledger
    plain = build_sim_engine(catalog, tasks, providers=["openai"])
    ev2, _s, _ = await calibrate(plain, tasks, ["openai"], None, False, "x")
    assert {(m, c): (i.n, i.passes) for m, cats in ev.items() for c, i in cats.items()} == \
           {(m, c): (i.n, i.passes) for m, cats in ev2.items() for c, i in cats.items()}  # same verdicts as a model that was never cut off
    assert totals(store, project="modelspend-eval")["cost"] == pytest.approx(spent)


async def test_still_cut_off_after_the_retry_is_a_labelled_failure(catalog):
    tasks = load_tasks("calibration")[:3]
    eng = cut_off(build_sim_engine(catalog, tasks, providers=["openai"]), always=True)
    stats: dict = {}
    fails: list = []
    ev, _s, _ = await calibrate(eng, tasks, ["openai"], None, False, "x", fails, stats)
    calls = sum(i.n for cats in ev.values() for i in cats.values())
    assert sum(i.passes for cats in ev.values() for i in cats.values()) == 0
    assert stats["retried"] == calls and stats["still_truncated"] == calls
    assert len(fails) == calls and all(f["reason"].startswith("truncated at the output limit") for f in fails)


async def test_the_retry_is_skipped_when_it_would_break_the_budget(catalog):
    tasks = load_tasks("calibration")[:1]
    eng = cut_off(build_sim_engine(catalog, tasks, providers=["openai"]))
    req = RouteRequest(providers=["openai"], messages=tasks[0].messages, max_output_tokens=400, profile="economy")
    from modelspend.eval.harness import _eligible
    worst = max(c.worst_case_cost_usd for c in _eligible(eng, req))
    stats: dict = {}
    ev, spent, _ = await calibrate(eng, tasks, ["openai"], worst * 1.001, False, "x", [], stats)
    assert stats["still_truncated"] >= 1 and spent <= worst * 1.001  # the big model's retry did not fit and was skipped; the budget held


async def test_models_that_are_not_cut_off_cost_no_extra_calls(catalog):
    store = Store(":memory:")
    tasks = load_tasks("calibration")[:4]
    stats: dict = {}
    ev, _s, _ = await calibrate(build_sim_engine(catalog, tasks, providers=["openai"], store=store), tasks, ["openai"], None, False, "x", [], stats)
    assert stats == {} and store.db.execute("select count(*) from requests").fetchone()[0] == sum(i.n for cats in ev.values() for i in cats.values())


async def test_eval_run_baselines_get_the_same_retry(catalog):
    tasks = load_tasks("heldout")[:3]
    eng = cut_off(build_sim_engine(catalog, tasks, providers=["openai", "anthropic"]))
    rep = await evaluate(eng, tasks, ["openai", "anthropic"])
    assert rep["output_limit_retries"]["retried"] >= 2 * len(tasks) and rep["output_limit_retries"]["still_truncated"] == 0
    assert rep["arms"]["premium"]["pass_rate"] == 1.0  # the premium baseline is judged on its answers, not on the cap


async def test_a_ledger_reused_for_two_calls_records_each_call_once(catalog, cfg):
    """The retry shares one Ledger with the first call: the second record must not repeat the first call's attempts (double counting)."""
    from modelspend.engine import Ledger
    from modelspend.eval.harness import _eligible
    store = Store(":memory:")
    eng, _ = make_engine(catalog, cfg, {"openai": "ok"}, store=store)
    req = RouteRequest(providers=["openai"], messages=[{"role": "user", "content": "x"}], project="modelspend-eval", agent="calibrate", sticky=False)
    cand = _eligible(eng, req)[0]
    ledger = Ledger(None)
    await eng.run_single(req, cand, ledger)
    await eng.run_single(req, cand, ledger)
    assert store.db.execute("select count(*) from requests").fetchone()[0] == 2
    assert store.db.execute("select count(*) from attempts").fetchone()[0] == 2
    t = totals(store)
    assert t["total_tokens"] == ledger.tokens and t["cost"] == pytest.approx(ledger.spent)
