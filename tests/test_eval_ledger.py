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
