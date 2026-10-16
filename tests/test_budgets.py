"""0.3: persistent per-project / per-agent budgets (hard and soft), atomic reservation, retention."""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from conftest import make_engine
from modelspend import RouteRequest
from modelspend.budgets import (HOLD_TTL_S, BudgetGuard, PeriodBudgetExceeded, list_budgets, remove_budget, set_budget, status, window)
from modelspend.providers.base import ProviderResponse
from modelspend.server.app import create_app
from modelspend.store import Store
from modelspend.types import Usage


def R(text="Classify this: great", **kw):
    kw.setdefault("providers", ["openai"])
    return RouteRequest(messages=[{"role": "user", "content": text}], **kw)


def resp(text="positive", i=100, o=10, **kw):
    return ProviderResponse(text=text, usage=Usage(input_tokens=i, output_tokens=o), **kw)


def ts(y, m, d, h=0):
    return datetime(y, m, d, h, tzinfo=timezone.utc).timestamp()


# ------------------------------------------------------------------ windows
def test_windows_are_utc_calendar_days_and_months():
    assert window("daily", ts(2026, 10, 14, 15)) == (ts(2026, 10, 14), ts(2026, 10, 15))
    assert window("monthly", ts(2026, 10, 14, 15)) == (ts(2026, 10, 1), ts(2026, 11, 1))
    assert window("monthly", ts(2026, 12, 31, 23)) == (ts(2026, 12, 1), ts(2027, 1, 1))  # year rollover
    assert window("daily", ts(2026, 2, 28, 23))[1] == ts(2026, 3, 1)
    with pytest.raises(ValueError):
        window("weekly")


# ------------------------------------------------------------------ admin
def test_set_list_remove_and_upsert(store):
    b = set_budget(store, project="Shop", agent="Bot", period="monthly", limit_usd=50, mode="hard")
    assert (b.project, b.agent) == ("shop", "bot")
    again = set_budget(store, project="shop", agent="bot", period="monthly", limit_usd=75, limit_tokens=1_000_000, mode="soft")
    assert again.id == b.id and len(list_budgets(store)) == 1  # same scope+period replaces
    daily = set_budget(store, project="shop", period="daily", limit_tokens=5000)
    assert daily.id != b.id and daily.agent is None
    assert remove_budget(store, b.id) and not remove_budget(store, b.id)
    assert [x.id for x in list_budgets(store)] == [daily.id]


@pytest.mark.parametrize("kw", [
    {}, {"limit_usd": 0}, {"limit_usd": -1}, {"limit_usd": True}, {"limit_tokens": 0}, {"limit_tokens": 1.5},
    {"limit_usd": 1, "period": "weekly"}, {"limit_usd": 1, "mode": "maybe"}, {"limit_usd": 1, "project": "has space"},
    {"limit_usd": float("inf")},
])
def test_set_budget_rejects_bad_input(store, kw):
    with pytest.raises(ValueError):
        set_budget(store, **kw)
    assert list_budgets(store) == []


def test_scope_matching_uses_wildcards(store):
    shop_any = set_budget(store, project="shop", limit_usd=1)
    any_bot = set_budget(store, agent="bot", limit_usd=1)
    everything = set_budget(store, limit_usd=1)
    both = set_budget(store, project="shop", agent="bot", limit_usd=1)
    bs = {b.id: b for b in list_budgets(store)}
    hit = lambda p, a: {i for i, b in bs.items() if b.matches(p, a)}  # noqa: E731
    assert hit("shop", "bot") == {shop_any.id, any_bot.id, everything.id, both.id}
    assert hit("shop", "other") == {shop_any.id, everything.id}
    assert hit("blog", "bot") == {any_bot.id, everything.id}
    assert hit(None, None) == {everything.id}


# ------------------------------------------------------------------ enforcement in the engine
async def test_hard_usd_budget_refuses_before_any_provider_call(engine_factory, store):
    set_budget(store, project="shop", period="daily", limit_usd=0.000001, mode="hard")
    eng, ad = engine_factory({"openai": lambda r, n: resp()})
    with pytest.raises(PeriodBudgetExceeded) as ei:
        await eng.run(R(project="shop"))
    assert not ad["openai"].calls  # nothing was sent, so nothing was spent
    e = ei.value
    assert e.code == "period_budget_exceeded" and e.http_status == 429
    assert "daily hard budget for project shop" in e.message and "resets" in e.message and e.details["limit_usd"] == 0.000001
    row = store.db.execute("select status, project from requests").fetchone()
    assert tuple(row) == ("period_budget_exceeded", "shop")  # the refusal is recorded, with its tags


async def test_budget_accumulates_across_requests_and_other_scopes_are_unaffected(engine_factory, store):
    set_budget(store, project="shop", period="daily", limit_usd=0.0005, mode="hard")
    big = resp("positive", 100, 1000)  # about $0.0004 on the small openai model
    eng, ad = engine_factory({"openai": lambda r, n: big})
    r1 = await eng.run(R(project="shop"))
    assert r1.actual_cost_usd > 0.0003
    with pytest.raises(PeriodBudgetExceeded):  # spent + this call's worst case no longer fits
        await eng.run(R(project="shop"))
    assert len(ad["openai"].calls) == 1
    await eng.run(R(project="blog"))   # a different project is not covered
    await eng.run(R())                 # untagged calls are not covered by a project budget
    assert len(ad["openai"].calls) == 3
    st = {s["scope"]: s for s in status(store)}["project shop / agent *"]
    assert st["spent_usd"] == pytest.approx(r1.actual_cost_usd) and st["in_flight_usd"] == 0


async def test_hard_token_budget(engine_factory, store):
    set_budget(store, agent="bot", period="monthly", limit_tokens=1500, mode="hard")
    eng, ad = engine_factory({"openai": lambda r, n: resp("positive", 100, 900)})
    await eng.run(R(agent="bot", max_output_tokens=900))
    with pytest.raises(PeriodBudgetExceeded) as ei:
        await eng.run(R(agent="bot", max_output_tokens=900))
    assert "tokens" in ei.value.message and len(ad["openai"].calls) == 1
    await eng.run(R(agent="other-bot", max_output_tokens=900))  # (*, bot) covers that agent only


async def test_soft_budget_never_refuses_but_warns(engine_factory, store):
    set_budget(store, project="shop", period="daily", limit_usd=0.0001, mode="soft")
    eng, ad = engine_factory({"openai": lambda r, n: resp("positive", 100, 1000)})
    res = await eng.run(R(project="shop"))
    assert res.status == "ok" and len(ad["openai"].calls) == 1
    assert any("soft budget" in w and "project shop" in w and "over its limit" in w for w in res.budget_warnings)
    res2 = await eng.run(R(project="shop"))
    assert res2.status == "ok" and res2.budget_warnings  # still not refused, still warned
    assert status(store)[0]["state"] == "over"


async def test_soft_budget_does_not_warn_about_a_cheap_call_that_merely_had_a_big_worst_case(engine_factory, store):
    # seen live: 73 of 150 tokens spent, worst case of the next call 98, actual 30 -> 103 of 150 (69%) is not a warning
    set_budget(store, agent="bot", period="daily", limit_tokens=150, mode="soft")
    eng, _ = engine_factory({"openai": lambda r, n: resp("positive", 12, 18)})
    for _ in range(2):
        res = await eng.run(R(agent="bot", max_output_tokens=64))
    assert res.budget_warnings == [] and status(store)[0]["spent_tokens"] == 60  # 2 x 30 tokens = 40% of 150


async def test_soft_budget_warns_when_approaching(engine_factory, store):
    set_budget(store, project="shop", period="daily", limit_tokens=10_000, mode="soft")
    eng, _ = engine_factory({"openai": lambda r, n: resp("positive", 4000, 4500)})  # 8,500 of 10,000 = 85%
    await eng.run(R(project="shop"))
    res = await eng.run(R(project="shop", max_output_tokens=64))
    assert any("is at" in w or "over its limit" in w for w in res.budget_warnings), res.budget_warnings


async def test_hard_usd_limit_refuses_an_unpriced_model_but_a_token_limit_allows_it(catalog, cfg, store):
    catalog.get("openai/synthetic-openai-mid").pricing = None
    eng, ad = make_engine(catalog, cfg, {"openai": lambda r, n: resp("ok", 70, 7)}, store=store)
    set_budget(store, agent="a", period="daily", limit_usd=1.0, mode="hard")
    with pytest.raises(PeriodBudgetExceeded) as ei:
        await eng.run(R(agent="a", pin="openai/synthetic-openai-mid"))
    assert "no price" in ei.value.message and not ad["openai"].calls  # never guesses a cost it cannot know
    remove_budget(store, list_budgets(store)[0].id)
    set_budget(store, agent="a", period="daily", limit_tokens=10_000, mode="hard")
    res = await eng.run(R(agent="a", pin="openai/synthetic-openai-mid"))
    assert res.total_tokens == 77


async def test_escalation_blocked_by_budget_returns_the_first_answer(engine_factory, store):
    # first call fits, the escalation does not: the earlier answer is returned with the reason, never an error
    set_budget(store, project="shop", period="daily", limit_tokens=2000, mode="hard")
    eng, ad = engine_factory({"openai": lambda r, n: resp("positive", 100, 1000)})
    res = await eng.run(R(project="shop", providers=["openai"], max_output_tokens=1000,
                          validators=[{"type": "allowed_labels", "labels": ["billing", "bug"]}]))
    assert res.status == "validation_failed" and res.escalations == 0 and len(ad["openai"].calls) == 1
    assert any("budget" in u.lower() for u in res.uncertainty)


async def test_stream_is_refused_before_the_first_chunk(engine_factory, store):
    set_budget(store, project="shop", period="daily", limit_usd=0.000001, mode="hard")
    eng, ad = engine_factory({"openai": "streamed"})
    with pytest.raises(PeriodBudgetExceeded):
        async for _ in eng.stream(R(project="shop")):
            pass
    assert not ad["openai"].calls


async def test_in_flight_calls_are_held_and_released(engine_factory, store):
    set_budget(store, project="shop", period="daily", limit_usd=1.0, mode="hard")
    seen = {}

    def during(r, n):
        s = status(store)[0]
        seen["in_flight_usd"], seen["holds"] = s["in_flight_usd"], store.db.execute("select count(*) from budget_holds").fetchone()[0]
        return resp()
    eng, _ = engine_factory({"openai": during})
    await eng.run(R(project="shop"))
    assert seen["holds"] == 1 and seen["in_flight_usd"] > 0  # the worst case of the running call is reserved
    assert store.db.execute("select count(*) from budget_holds").fetchone()[0] == 0  # released when the request ended


async def test_hold_shrinks_to_actual_spend_after_each_call(engine_factory, store):
    # between calls (here: while a validator runs) other requests must see what was really spent, not the worst case
    set_budget(store, project="shop", period="daily", limit_usd=1.0, mode="hard")
    seen = {}

    def check(text):
        seen["in_flight"] = status(store)[0]["in_flight_usd"]
        return True
    eng, _ = engine_factory({"openai": lambda r, n: resp("positive", 100, 10)})
    res = await eng.run(R(project="shop", callable_validators=[check]))
    assert seen["in_flight"] == pytest.approx(res.actual_cost_usd) and seen["in_flight"] < 0.0001  # not the ~$0.0004 worst case


async def test_request_level_budget_also_leaves_a_failed_answer_marked_failed(engine_factory, store):
    # the escalation is not affordable under the request's own token budget: the earlier answer is returned as validation_failed
    eng, ad = engine_factory({"openai": lambda r, n: resp("positive", 100, 1000)})
    res = await eng.run(R(max_budget_tokens=1500, max_output_tokens=1000, validators=[{"type": "allowed_labels", "labels": ["billing", "bug"]}]))
    assert res.status == "validation_failed" and res.escalations == 0 and len(ad["openai"].calls) == 1


async def test_failed_request_still_releases_its_hold(engine_factory, store):
    from modelspend.errors import ProviderError
    set_budget(store, project="shop", period="daily", limit_usd=1.0, mode="hard")
    eng, _ = engine_factory({"openai": lambda r, n: ProviderError("openai", "bad_request", "no", 400)})
    with pytest.raises(Exception):
        await eng.run(R(project="shop"))
    assert store.db.execute("select count(*) from budget_holds").fetchone()[0] == 0


def test_stale_holds_expire(store):
    set_budget(store, project="shop", period="daily", limit_usd=0.01, mode="hard")
    store.db.execute("insert into budget_holds values('crashed','shop',null,0.0099,0,?)", (time.time() - HOLD_TTL_S - 5,))
    store.db.commit()
    g = BudgetGuard.for_request(store, R(project="shop"))
    assert g.reserve(0.005, 100, 0.0, 0) is None  # the crashed request's reservation no longer blocks anyone
    assert store.db.execute("select count(*) from budget_holds where hold_id='crashed'").fetchone()[0] == 0
    # but a fresh hold does count
    store.db.execute("insert into budget_holds values('live','shop',null,0.0099,0,?)", (time.time(),))
    store.db.commit()
    other = BudgetGuard.for_request(store, R(project="shop"))
    assert other.reserve(0.005, 100, 0.0, 0) is not None


def test_spend_outside_the_window_does_not_count(store):
    set_budget(store, project="shop", period="monthly", limit_usd=1.0)

    class A:
        index, kind, purpose, provider, model, outcome, error_kind = 0, "generation", "initial", "openai", "m", "ok", None
        usage = Usage(input_tokens=1000, output_tokens=100)
        estimated_cost_usd = cost_usd = 0.9
        cost_basis, latency_ms = "reported_usage", 1
    now = time.time()
    store.record_row("old", "balanced", "classification", "rules", "openai", "m", "ok", "x", 0, 0, 0, 0.9, 0.9, 0, None, 0, 0, 1, "c", "p", [A()],
                     tags={"project": "shop"})
    store.db.execute("update requests set ts=? where id='old'", (window("monthly", now)[0] - 86400,))  # last month
    store.record_row("new", "balanced", "classification", "rules", "openai", "m", "ok", "x", 0, 0, 0, 0.1, 0.1, 0, None, 0, 0, 1, "c", "p", [A()],
                     tags={"project": "shop"})
    store.db.commit()
    assert status(store, now)[0]["spent_usd"] == pytest.approx(0.9)  # only "new" (the same 0.9 fixture) is inside this month


def test_uncertain_failed_calls_count_toward_the_budget(store):
    set_budget(store, project="shop", period="daily", limit_usd=0.001)

    class A:
        index, kind, purpose, provider, model, outcome, error_kind = 0, "generation", "initial", "openai", "m", "operational_error", "timeout"
        usage = Usage()
        estimated_cost_usd, cost_usd, cost_basis, latency_ms = 0.0004, 0.0004, "uncertain_upper_estimate", 1
    store.record_row("t", "balanced", "classification", "rules", "openai", "m", "all_attempts_failed", "n/a", 0, 0, 0, 0.0004, 0.0004, 0, None, 0, 0, 1,
                     "c", "p", [A()], tags={"project": "shop"})
    assert status(store)[0]["spent_usd"] == pytest.approx(0.0004)  # a call that failed but may have been billed is still counted


# ------------------------------------------------------------------ atomicity (threads, each with its own connection)
def test_concurrent_reservations_cannot_jointly_exceed_a_limit(tmp_path):
    path = tmp_path / "shared.db"
    admin = Store(path)
    set_budget(admin, project="shop", period="daily", limit_usd=0.05, mode="hard")
    n, results, barrier = 24, [], threading.Barrier(24)

    def worker():
        st = Store(path)  # own connection, like a separate process sharing the file
        g = BudgetGuard.for_request(st, R(project="shop"))
        barrier.wait()
        results.append(g.reserve(0.01, 100, 0.0, 0) is None)  # holds are never released here: each success consumes $0.01

    threads = [threading.Thread(target=worker) for _ in range(n)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(results) == n and sum(results) == 5  # exactly limit / worst-case, never 6
    held = admin.db.execute("select count(*), sum(usd) from budget_holds").fetchone()
    assert held[0] == 5 and held[1] == pytest.approx(0.05)


def test_concurrent_requests_through_separate_stores_respect_the_budget(tmp_path):
    """Two 'processes' (separate Store objects on one file) share one daily token budget."""
    import asyncio

    from conftest import _nosleep  # noqa: F401
    path = tmp_path / "shared2.db"
    set_budget(Store(path), project="shop", period="daily", limit_tokens=2200, mode="hard")
    outcomes = []

    def proc(catalog, cfg):
        st = Store(path)
        eng, _ = make_engine(catalog, cfg, {"openai": lambda r, n: resp("positive", 100, 900)}, store=st)
        for _ in range(3):
            try:
                asyncio.run(eng.run(R(project="shop", max_output_tokens=900)))
                outcomes.append("ok")
            except PeriodBudgetExceeded:
                outcomes.append("refused")

    from modelspend import Catalog, Config
    from modelspend.catalog import shipped_catalog_path
    cat = Catalog.load(shipped_catalog_path("synthetic_test_catalog.json"))
    cfg = Config(home=tmp_path, api_keys={"openai": "k"}, retry_backoff_s=0.0, max_retries=2)
    ts_ = [threading.Thread(target=proc, args=(cat, cfg)) for _ in range(2)]
    [t.start() for t in ts_]
    [t.join() for t in ts_]
    used = Store(path).db.execute("select coalesce(sum(a.input_tokens + a.output_tokens),0) from attempts a").fetchone()[0]
    assert outcomes.count("ok") == 2 and outcomes.count("refused") == 4  # 2 x 1,000 tokens fit under 2,200; the rest are refused
    assert used == 2000 and used <= 2200


# ------------------------------------------------------------------ retention
def test_prune_deletes_old_usage_but_keeps_budgets_and_recent_rows(store):
    set_budget(store, project="shop", period="daily", limit_usd=1.0)

    class A:
        index, kind, purpose, provider, model, outcome, error_kind = 0, "generation", "initial", "openai", "m", "ok", None
        usage = Usage(input_tokens=10, output_tokens=5)
        estimated_cost_usd = cost_usd = 0.001
        cost_basis, latency_ms = "reported_usage", 1
    for rid, age_days in (("old", 100), ("mid", 40), ("new", 1)):
        store.record_row(rid, "balanced", "classification", "rules", "openai", "m", "ok", "x", 0, 0, 0, 0.001, 0.001, 0, None, 0, 0, 1, "c", "p", [A()])
        store.db.execute("update requests set ts=? where id=?", (time.time() - age_days * 86400, rid))
    store.db.commit()
    cutoff = time.time() - 30 * 86400
    assert store.prune(cutoff, dry_run=True) == {"requests": 2, "attempts": 2, "shadow": 0}
    assert store.db.execute("select count(*) from requests").fetchone()[0] == 3  # dry run changes nothing
    assert store.prune(cutoff) == {"requests": 2, "attempts": 2, "shadow": 0}
    assert [r[0] for r in store.db.execute("select id from requests")] == ["new"]
    assert store.db.execute("select count(*) from attempts").fetchone()[0] == 1
    assert len(list_budgets(store)) == 1


# ------------------------------------------------------------------ server
def _client(catalog, cfg, scripts=None, store=None, token=None):
    cfg.api_token = token
    cfg.default_providers = ["openai", "anthropic"]
    eng, ad = make_engine(catalog, cfg, scripts, store=store)
    return TestClient(create_app(eng, cfg), base_url="http://127.0.0.1:8787"), ad


BODY = {"model": "auto", "messages": [{"role": "user", "content": "Classify: great"}], "modelspend": {"providers": ["openai"]}}


def test_budget_endpoints_roundtrip(catalog, cfg, store):
    c, _ = _client(catalog, cfg, store=store)
    r = c.put("/v1/budgets", json={"project": "Shop", "period": "monthly", "limit_usd": 25, "mode": "hard"})
    assert r.status_code == 200
    b = r.json()["budget"]
    assert b["project"] == "shop" and b["limit_usd"] == 25 and b["state"] == "ok" and b["spent_usd"] == 0
    listed = c.get("/v1/budgets").json()["budgets"]
    assert [x["id"] for x in listed] == [b["id"]]
    assert c.put("/v1/budgets", json={"project": "shop", "limit_usd": 30}).json()["budget"]["id"] == b["id"]  # upsert
    for bad in ({"period": "weekly", "limit_usd": 1}, {"project": "has space", "limit_usd": 1}, {}, {"limit_usd": 1, "bogus": 1},
                {"limit_usd": "lots"}, {"limit_tokens": -5}):
        assert c.put("/v1/budgets", json=bad).status_code == 400, bad
    json_hdr = {"Content-Type": "application/json"}  # writes need it (browser safety), as for every other write endpoint
    assert c.delete(f"/v1/budgets/{b['id']}").status_code == 415
    assert c.delete(f"/v1/budgets/{b['id']}", headers=json_hdr).status_code == 200
    assert c.delete(f"/v1/budgets/{b['id']}", headers=json_hdr).status_code == 400
    assert c.get("/v1/budgets").json()["budgets"] == []


def test_server_returns_429_with_a_clear_body_when_a_hard_budget_is_exceeded(catalog, cfg, store):
    c, ad = _client(catalog, cfg, {"openai": "positive"}, store)
    c.put("/v1/budgets", json={"project": "shop", "period": "daily", "limit_usd": 0.000001, "mode": "hard"})
    r = c.post("/v1/chat/completions", json=BODY, headers={"X-ModelSpend-Project": "shop"})
    assert r.status_code == 429
    e = r.json()["error"]
    assert e["code"] == "period_budget_exceeded" and "shop" in e["message"] and "resets" in e["message"]
    assert not ad["openai"].calls
    assert c.post("/v1/chat/completions", json=BODY, headers={"X-ModelSpend-Project": "blog"}).status_code == 200


def test_server_streaming_refusal_is_a_proper_error_before_any_chunk(catalog, cfg, store):
    c, ad = _client(catalog, cfg, {"openai": "streamed"}, store)
    c.put("/v1/budgets", json={"project": "shop", "period": "daily", "limit_usd": 0.000001, "mode": "hard"})
    r = c.post("/v1/chat/completions", json={**BODY, "stream": True}, headers={"X-ModelSpend-Project": "shop"})
    assert r.status_code == 429 and r.json()["error"]["code"] == "period_budget_exceeded" and not ad["openai"].calls


def test_soft_budget_warning_reaches_the_response_metadata(catalog, cfg, store):
    c, _ = _client(catalog, cfg, {"openai": "positive"}, store)
    c.put("/v1/budgets", json={"project": "shop", "period": "daily", "limit_usd": 0.000001, "mode": "soft"})
    j = c.post("/v1/chat/completions", json=BODY, headers={"X-ModelSpend-Project": "shop"}).json()
    assert j["modelspend"]["budget_warnings"] and "soft budget" in j["modelspend"]["budget_warnings"][0]


def test_budget_endpoints_are_protected(catalog, cfg, store):
    c, _ = _client(catalog, cfg, store=store, token="sekret")
    assert c.get("/v1/budgets").status_code == 401 and c.put("/v1/budgets", json={"limit_usd": 1}).status_code == 401
    ok = {"Authorization": "Bearer sekret"}
    assert c.put("/v1/budgets", json={"limit_usd": 1}, headers=ok).status_code == 200
    # without a token the server is local-only: foreign hosts and cross-origin writes are refused
    c2, _ = _client(catalog, cfg, store=Store(":memory:"))
    evil = TestClient(c2.app, base_url="http://attacker.test:8787")
    assert evil.get("/v1/budgets").status_code == 403
    assert c2.put("/v1/budgets", json={"limit_usd": 1}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert c2.put("/v1/budgets", content=b'{"limit_usd": 1}', headers={"Content-Type": "text/plain"}).status_code == 415


def test_dashboard_has_the_costs_tab_and_loads_nothing_external(catalog, cfg, store):
    c, _ = _client(catalog, cfg, store=store)
    html = c.get("/dashboard").text
    for needle in ('id="nav-costs"', 'id="costs"', 'id="c-bars"', 'id="c-days"', 'id="c-budgets"', 'id="b-add"', "/v1/usage/report", "/v1/budgets"):
        assert needle in html, needle
    assert 'src="http' not in html and "<link" not in html and "@import" not in html  # fully self-contained: no CDN, no tracking
    assert "new Set([days[0]" in html  # a one-day window shows one x label, not the same date three times
    assert "innerHTML" in html and "const esc=" in html  # dynamic strings go through esc(); tooltips use textContent


# ------------------------------------------------------------------ CLI
def test_cli_budget_and_prune(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from modelspend.cli import app
    monkeypatch.setenv("MODELSPEND_HOME", str(tmp_path))
    monkeypatch.setenv("MODELSPEND_DB", str(tmp_path / "usage.db"))
    run = CliRunner()
    assert "no budgets set" in run.invoke(app, ["budget", "list"]).output
    out = run.invoke(app, ["budget", "set", "--project", "shop", "--agent", "bot", "--period", "monthly", "--usd", "50", "--tokens", "2000000"])
    assert out.exit_code == 0 and "$50.00" in out.output and "2,000,000 tokens" in out.output and "project shop / agent bot" in out.output
    lst = run.invoke(app, ["budget", "list"])
    assert "ok" in lst.output and "monthly" in lst.output and "hard" in lst.output and "resets" in lst.output
    js = run.invoke(app, ["budget", "list", "--json"])
    assert '"limit_usd": 50.0' in js.output
    assert run.invoke(app, ["budget", "set", "--project", "has space", "--usd", "1"]).exit_code == 2
    assert run.invoke(app, ["budget", "set", "--project", "shop"]).exit_code == 2  # needs a limit
    assert run.invoke(app, ["budget", "remove", "99"]).exit_code == 2
    assert run.invoke(app, ["budget", "remove", "1"]).exit_code == 0
    dry = run.invoke(app, ["prune", "90d"])
    assert dry.exit_code == 0 and "would delete 0" in dry.output
    assert run.invoke(app, ["prune", "all"]).exit_code == 2 and run.invoke(app, ["prune", "soon"]).exit_code == 2
