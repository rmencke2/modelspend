"""0.2: attribution tags, observe-only (pinned) calls, schema migration and the per-project/agent report."""
from __future__ import annotations

import csv
import io
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from conftest import make_engine
from modelspend import RouteRequest
from modelspend.errors import NoEligibleModel, ProviderError
from modelspend.providers.base import ProviderResponse
from modelspend.report import ReportError, build_report, parse_since, to_csv, to_table
from modelspend.server.app import create_app
from modelspend.store import Store
from modelspend.types import Usage


def R(text="Classify this: great", **kw):
    kw.setdefault("providers", ["openai", "anthropic", "google", "xai"])
    return RouteRequest(messages=[{"role": "user", "content": text}], **kw)


def resp(text, i=100, o=10, cache=0, reasoning=0, **kw):
    return ProviderResponse(text=text, usage=Usage(input_tokens=i, cache_read_tokens=cache, output_tokens=o, reasoning_tokens=reasoning), **kw)


# ------------------------------------------------------------------ tag validation
def test_tags_are_normalized_and_validated():
    r = R(project="Billing", agent="Invoice-Bot", run_id="Run_42", labels={"env": "Prod"})
    assert (r.project, r.agent, r.run_id, r.labels) == ("billing", "invoice-bot", "Run_42", {"env": "prod"})
    assert R(project="  ").project is None  # blank means untagged


@pytest.mark.parametrize("kw", [
    {"project": "has space"}, {"agent": "x" * 65}, {"project": "semi;colon"}, {"agent": "Summarize this customer email please"},
    {"run_id": "a b"}, {"labels": {"Env": "x"}}, {"labels": {"env": "has space"}},
    {"labels": {f"k{i}": "v" for i in range(9)}}, {"project": "-leading-dash"},
])
def test_tags_reject_free_text(kw):
    # tags are identifiers: prompt-like text must never be able to reach the store or a report
    with pytest.raises(ValidationError):
        R(**kw)


def test_pin_validation():
    assert R(pin="Gemini/some-model").pin == "google/some-model"
    with pytest.raises(ValidationError):
        R(pin="no-provider-prefix")
    assert R(pin="anthropic/synthetic-anthropic-small", providers=[]).providers == ["anthropic"]  # a pin implies its provider


# ------------------------------------------------------------------ engine: tags are stored
async def test_tags_are_recorded_with_the_request(engine_factory, store):
    eng, _ = engine_factory({"google": lambda r, n: resp("positive", 1000, 100)})
    res = await eng.run(R(providers=["google"], project="billing", agent="invoice-bot", run_id="r1", labels={"env": "prod"}))
    assert (res.project, res.agent, res.run_id, res.labels, res.pinned) == ("billing", "invoice-bot", "r1", {"env": "prod"}, False)
    row = store.db.execute("select project, agent, run_id, labels, pinned from requests").fetchone()
    assert tuple(row) == ("billing", "invoice-bot", "r1", '{"env": "prod"}', 0)


async def test_failed_requests_keep_their_tags(engine_factory, store):
    eng, _ = engine_factory({"openai": lambda r, n: ProviderError("openai", "bad_request", "no", 400)})
    with pytest.raises(Exception):
        await eng.run(R(providers=["openai"], project="billing", agent="a1"))
    assert tuple(store.db.execute("select project, agent from requests").fetchone()) == ("billing", "a1")


# ------------------------------------------------------------------ pinned / observe-only
async def test_pinned_call_runs_exactly_that_model_without_routing(engine_factory, catalog):
    eng, ad = engine_factory({"openai": lambda r, n: resp("hi", 50, 5)})
    # a task that balanced routing would send to a mid/small model; the pin forces the large one
    res = await eng.run(R(providers=[], pin="openai/synthetic-openai-large", project="p", agent="a"))
    assert res.model == "synthetic-openai-large" and res.pinned and res.escalations == 0 and res.failovers == 0
    assert [c.model for c in ad["openai"].calls] == ["synthetic-openai-large"]
    m = catalog.get("openai/synthetic-openai-large")
    assert res.actual_cost_usd == pytest.approx((50 * m.pricing.input_per_mtok + 5 * m.pricing.output_per_mtok) / 1e6)
    assert "Pinned by the caller" in res.explanation
    plan = await eng.plan(R(providers=[], pin="openai/synthetic-openai-large"))
    assert plan.escalation_limit == 0 and [c.model for c in plan.candidates] == ["synthetic-openai-large"] and not plan.unsuitable


async def test_pinned_call_never_escalates_or_fails_over(engine_factory):
    eng, ad = engine_factory({"openai": lambda r, n: ProviderError("openai", "auth", "bad key", 401)})
    with pytest.raises(Exception):
        await eng.run(R(providers=["openai", "anthropic"], pin="openai/synthetic-openai-small"))
    assert not ad["anthropic"].calls  # observe-only: no silent swap to another provider


async def test_pinned_unknown_model_is_a_clear_error(engine_factory):
    eng, _ = engine_factory()
    with pytest.raises(NoEligibleModel) as ei:
        await eng.run(R(providers=["openai"], pin="openai/not-in-catalog"))
    assert "not in the catalog" in str(ei.value) and "catalog sync" in str(ei.value)


async def test_pinned_unpriced_model_still_records_tokens(catalog, cfg, store):
    # a model with no price: not routable normally, but observable when pinned (tokens exact, cost unknown)
    m = catalog.get("openai/synthetic-openai-mid")
    m.pricing = None
    eng, _ = make_engine(catalog, cfg, {"openai": lambda r, n: resp("ok", 70, 7)}, store=store)
    res = await eng.run(R(providers=["openai"], pin="openai/synthetic-openai-mid", agent="a"))
    assert res.actual_cost_usd is None and res.total_tokens == 77
    rep = build_report(store, ["agent"])
    assert rep["totals"]["total_tokens"] == 77 and rep["totals"]["cost"] is None and not rep["totals"]["cost_complete"]
    assert "without a price" in " ".join(rep["notes"])


# ------------------------------------------------------------------ migration
def test_database_from_0_1_opens_with_new_columns_and_keeps_data(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.executescript("""
    create table requests(id text primary key, ts real, profile text, task text, task_basis text, provider text, model text,
      status text, validation text, retries int, failovers int, escalations int, estimated_cost real, actual_cost real,
      uncertain_cost real, budget real, input_tokens int, output_tokens int, latency_ms int, catalog_version text, policy_version text);
    create table attempts(request_id text, idx int, kind text, purpose text, provider text, model text, outcome text, error_kind text,
      input_tokens int, cache_read int, cache_write int, output_tokens int, reasoning_tokens int, estimated_cost real, cost real,
      cost_basis text, latency_ms int);
    create table class_cache(key text primary key, category text, ts real);
    create table meta(k text primary key, v blob);
    insert into requests values('old1', 1.0, 'balanced', 'classification', 'rules', 'openai', 'm', 'ok', 'not_configured', 0,0,0,
      0.001, 0.001, 0, null, 10, 5, 100, 'c', 'p');
    insert into attempts values('old1', 0, 'generation', 'initial', 'openai', 'm', 'ok', null, 10, 0, 0, 5, 0, 0.001, 0.001, 'reported_usage', 100);
    """)
    db.commit()
    db.close()
    st = Store(path)
    assert {"project", "agent", "run_id", "labels", "pinned"} <= {r[1] for r in st.db.execute("pragma table_info(requests)")}
    rep = build_report(st, ["project"], since=None)
    assert rep["rows"][0]["project"] == "(untagged)" and rep["rows"][0]["total_tokens"] == 15  # old rows are reported, untagged
    Store(path)  # reopening is idempotent


# ------------------------------------------------------------------ report
def _seed(store: Store):
    """Hand-built requests so every expected number can be checked by eye."""
    def req(rid, project, agent, run, ts, atts, labels=None):
        class A:  # minimal attempt record
            def __init__(s, idx, kind, purpose, model, outcome, i, c, o, r, cost, basis="reported_usage", provider="openai"):
                s.index, s.kind, s.purpose, s.provider, s.model, s.outcome, s.error_kind = idx, kind, purpose, provider, model, outcome, None
                s.usage = Usage(input_tokens=i, cache_read_tokens=c, output_tokens=o, reasoning_tokens=r)
                s.estimated_cost_usd, s.cost_usd, s.cost_basis, s.latency_ms = cost, cost, basis, 10
        store.record_row(rid, "balanced", "classification", "rules", atts[-1][3] and "openai", "m", "ok", "not_configured", 0, 0, 0,
                         0.0, sum(a[8] or 0 for a in atts), 0.0, None, 0, 0, 10, "cat", "pol", [A(*a) for a in atts],
                         tags={"project": project, "agent": agent, "run_id": run, "labels": labels or {}, "pinned": False})
        store.db.execute("update requests set ts=? where id=?", (ts, rid))
        store.db.commit()
    now = time.time()
    # billing/invoice-bot: one clean call, one call that needed an escalation (first answer superseded)
    req("r1", "billing", "invoice-bot", "run1", now, [(0, "generation", "initial", "small", "ok", 100, 0, 10, 0, 0.001)])
    req("r2", "billing", "invoice-bot", "run2", now, [(0, "generation", "initial", "small", "ok", 100, 0, 10, 0, 0.001),
                                                      (1, "generation", "escalation", "mid", "ok", 100, 0, 20, 5, 0.004)],
        labels={"env": "prod"})
    # support/triage: cached input, plus a failed (maybe billed) attempt and a classifier call
    req("r3", "support", "triage", None, now - 3 * 86400, [(0, "classification", "routing", "small", "ok", 20, 0, 2, 0, 0.0001),
                                                          (1, "generation", "initial", "small", "operational_error", 0, 0, 0, 0, 0.0005, "uncertain_upper_estimate"),
                                                          (2, "generation", "retry", "small", "ok", 50, 200, 8, 0, 0.0007)])
    # untagged call on an unpriced model
    req("r4", None, None, None, now - 40 * 86400, [(0, "generation", "initial", "free", "ok", 30, 0, 3, 0, None, "unpriced")])


def test_report_groups_and_totals_are_consistent(store):
    _seed(store)
    rep = build_report(store, ["project"], since=None)
    by = {r["project"]: r for r in rep["rows"]}
    assert by["billing"]["requests"] == 2 and by["billing"]["attempts"] == 3
    assert by["billing"]["total_tokens"] == (100 + 10) + (100 + 10) + (100 + 20)
    assert by["billing"]["cost"] == pytest.approx(0.001 + 0.001 + 0.004)
    assert by["support"]["cache_read_tokens"] == 200 and by["support"]["total_tokens"] == 20 + 2 + 0 + 50 + 200 + 8
    assert by["(untagged)"]["cost"] is None and by["(untagged)"]["unpriced_attempts"] == 1 and not by["(untagged)"]["cost_complete"]
    # the invariant: group totals add up to the overall totals, for every grouping
    for dims in (["project"], ["agent"], ["model"], ["project", "agent"], ["day"], ["purpose"], ["provider", "task"], ["label.env"], ["run"]):
        r = build_report(store, dims, since=None)
        for col in ("requests", "attempts", "total_tokens", "input_tokens", "cache_read_tokens", "output_tokens", "reasoning_tokens", "overhead_tokens"):
            if col == "requests" and dims[0] in ("model", "purpose", "provider", "day"):
                continue  # a request with attempts on several models/purposes legitimately appears in each group
            assert sum(x[col] for x in r["rows"]) == r["totals"][col], (dims, col)
        known = [x["cost"] for x in r["rows"] if x["cost"] is not None]
        assert sum(known) == pytest.approx(r["totals"]["cost"]), dims


def test_overhead_counts_only_attempts_that_did_not_produce_the_answer(store):
    _seed(store)
    by = {r["project"]: r for r in build_report(store, ["project"], since=None)["rows"]}
    # billing: r2's first (superseded) answer = 110 tokens / $0.001; everything else is the final answer
    assert by["billing"]["overhead_tokens"] == 110 and by["billing"]["overhead_cost"] == pytest.approx(0.001)
    # support: classifier call (22 tokens) and the failed attempt (0 tokens, $0.0005 uncertain) are overhead; the retry is the answer
    assert by["support"]["overhead_tokens"] == 22 and by["support"]["overhead_cost"] == pytest.approx(0.0001 + 0.0005)
    assert by["support"]["uncertain_attempts"] == 1


def test_overhead_when_a_later_attempt_fails_after_an_earlier_answer(store):
    # engine returns the earlier output if a later (escalation) call fails: the earlier ok attempt is the answer, the failure is overhead
    class A:
        def __init__(s, idx, outcome, i, o, cost, basis="reported_usage"):
            s.index, s.kind, s.purpose, s.provider, s.model, s.outcome, s.error_kind = idx, "generation", "initial", "openai", "m", outcome, None
            s.usage = Usage(input_tokens=i, output_tokens=o)
            s.estimated_cost_usd, s.cost_usd, s.cost_basis, s.latency_ms = cost, cost, basis, 10
    store.record_row("late", "balanced", "classification", "rules", "openai", "m", "ok", "failed", 0, 0, 1, 0.0, 0.003, 0.0, None, 0, 0, 10,
                     "c", "p", [A(0, "ok", 100, 10, 0.001), A(1, "operational_error", 0, 0, 0.002, "uncertain_upper_estimate")],
                     tags={"project": "p", "agent": "a"})
    row = build_report(store, ["agent"], since=None)["rows"][0]
    assert row["total_tokens"] == 110 and row["overhead_tokens"] == 0 and row["overhead_cost"] == pytest.approx(0.002)


def test_report_filters_since_and_labels(store):
    _seed(store)
    week = {r["project"] for r in build_report(store, ["project"], since=parse_since("7d"))["rows"]}
    assert week == {"billing", "support"}  # the 40-day-old untagged call is outside the window
    assert {r["agent"] for r in build_report(store, ["agent"], filters={"project": "billing"}, since=None)["rows"]} == {"invoice-bot"}
    assert [r["project"] for r in build_report(store, ["project"], filters={"project": "(untagged)"}, since=None)["rows"]] == ["(untagged)"]
    lab = {r["label.env"]: r for r in build_report(store, ["label.env"], since=None)["rows"]}
    assert lab["prod"]["requests"] == 1 and "(none)" in lab
    days = build_report(store, ["day"], since=None)["rows"]
    assert len(days) == 3 and all(len(d["day"]) == 10 for d in days)


def test_report_rejects_unknown_dimensions_and_sql_text(store):
    for bad in (["nope"], ["project; drop table requests"], ["label.Bad Key"], ["a"] * 5):
        with pytest.raises(ReportError):
            build_report(store, bad)
    with pytest.raises(ReportError):
        build_report(store, ["project"], filters={"nope": "x"})
    # filter values are bound parameters, not SQL
    assert build_report(store, ["project"], filters={"project": "x' or '1'='1"}, since=None)["rows"] == []
    assert store.db.execute("select count(*) from requests").fetchone()[0] == 0


def test_parse_since():
    now = 1_000_000.0
    assert parse_since("2h", now) == now - 7200 and parse_since("1w", now) == now - 7 * 86400
    assert parse_since("all") is None and parse_since(None) is None
    assert parse_since("2026-10-01") == 1_790_812_800.0
    with pytest.raises(ReportError):
        parse_since("soon")


def test_csv_and_table_output(store):
    _seed(store)
    rep = build_report(store, ["project", "agent"], since=None)
    rows = list(csv.reader(io.StringIO(to_csv(rep))))
    assert rows[0][:2] == ["project", "agent"] and rows[-1][0] == "TOTAL" and len(rows) == len(rep["rows"]) + 2
    text = to_table(rep)
    assert "billing" in text and "invoice-bot" in text and "TOTAL" in text and "Only calls made through ModelSpend" in text
    assert "partial" in text  # the unpriced call is flagged, never silently dropped
    no_group = to_table(build_report(store, [], since=None))
    assert "all" in no_group


def test_empty_store_reports_zero_not_unknown(store):
    rep = build_report(store, ["project"], since=None)
    assert rep["rows"] == [] and rep["totals"]["cost"] == 0.0 and rep["totals"]["cost_complete"] and rep["totals"]["requests"] == 0
    assert "No usage recorded" in to_table(rep) and "unknown" not in to_table(rep)


def test_csv_neutralizes_formula_injection(store):
    _seed(store)
    store.db.execute("update attempts set provider='=cmd|x' where request_id='r1'")
    store.db.commit()
    out = to_csv(build_report(store, ["provider"], since=None))
    assert "'=cmd" in out and ",=cmd" not in out and not any(line.startswith("=") for line in out.splitlines())


async def test_end_to_end_two_agents_through_the_engine(engine_factory, store, catalog):
    eng, _ = engine_factory({"openai": lambda r, n: resp("positive", 400, 40, cache=100, reasoning=0),
                             "anthropic": lambda r, n: resp("positive", 300, 30)})
    await eng.run(R(providers=["openai"], project="shop", agent="tagger"))
    await eng.run(R(providers=["openai"], project="shop", agent="tagger"))
    await eng.run(R(providers=["anthropic"], project="shop", agent="replier"))
    rep = build_report(store, ["agent", "provider"], since=None)
    by = {(r["agent"], r["provider"]): r for r in rep["rows"]}
    assert by[("tagger", "openai")]["requests"] == 2 and by[("tagger", "openai")]["total_tokens"] == 2 * (400 + 100 + 40)
    assert by[("replier", "anthropic")]["total_tokens"] == 330
    assert rep["totals"]["total_tokens"] == sum(r["total_tokens"] for r in rep["rows"])
    # the report agrees with the per-request totals the engine returned
    assert rep["totals"]["requests"] == 3


# ------------------------------------------------------------------ server
def _client(catalog, cfg, scripts=None, store=None, token=None):
    cfg.api_token = token
    cfg.default_providers = ["openai", "anthropic"]
    eng, ad = make_engine(catalog, cfg, scripts, store=store)
    return TestClient(create_app(eng, cfg), base_url="http://127.0.0.1:8787"), ad, eng


BODY = {"model": "auto", "messages": [{"role": "user", "content": "Classify: great"}]}


def test_server_reads_tags_from_headers_and_body_wins(catalog, cfg, store):
    c, _, _ = _client(catalog, cfg, {"openai": "positive", "anthropic": "positive"}, store)
    r = c.post("/v1/chat/completions", json={**BODY, "modelspend": {"providers": ["openai"], "agent": "from-body"}},
               headers={"X-ModelSpend-Project": "Shop", "X-ModelSpend-Agent": "from-header", "X-ModelSpend-Run": "run-7",
                        "X-ModelSpend-Labels": "env=prod, team=growth"})
    assert r.status_code == 200
    ms = r.json()["modelspend"]
    assert (ms["project"], ms["agent"], ms["run_id"], ms["labels"]) == ("shop", "from-body", "run-7", {"env": "prod", "team": "growth"})


def test_server_rejects_bad_tag_headers(catalog, cfg, store):
    c, _, _ = _client(catalog, cfg, store=store)
    for hdr in ({"X-ModelSpend-Project": "has space"}, {"X-ModelSpend-Labels": "novalue"}):
        r = c.post("/v1/chat/completions", json=BODY, headers=hdr)
        assert r.status_code == 400
    assert store.db.execute("select count(*) from requests").fetchone()[0] == 0


def test_server_model_pin_by_catalog_id(catalog, cfg, store):
    c, ad, _ = _client(catalog, cfg, {"anthropic": "hello"}, store)
    r = c.post("/v1/chat/completions", json={"model": "synthetic-anthropic-large", "messages": BODY["messages"]},
               headers={"X-ModelSpend-Agent": "legacy-bot"})
    j = r.json()
    assert r.status_code == 200 and j["model"] == "synthetic-anthropic-large" and j["modelspend"]["pinned"] is True
    assert j["modelspend"]["escalations"] == 0 and [x.model for x in ad["anthropic"].calls] == ["synthetic-anthropic-large"]
    r2 = c.post("/v1/chat/completions", json={"model": "anthropic/synthetic-anthropic-small", "messages": BODY["messages"]})
    assert r2.status_code == 200 and r2.json()["model"] == "synthetic-anthropic-small"
    bad = c.post("/v1/chat/completions", json={"model": "gpt-nonexistent", "messages": BODY["messages"]})
    assert bad.status_code == 400 and "auto" in bad.json()["error"]["message"]
    assert not ad["openai"].calls


def test_server_pinned_stream_records_tags(catalog, cfg, store):
    c, _, _ = _client(catalog, cfg, {"openai": "streamed text"}, store)
    with c.stream("POST", "/v1/chat/completions", json={"model": "openai/synthetic-openai-small", "stream": True,
                  "messages": BODY["messages"]}, headers={"X-ModelSpend-Project": "shop", "X-ModelSpend-Agent": "streamer"}) as r:
        body = "".join(r.iter_text())
    assert "[DONE]" in body
    assert tuple(store.db.execute("select project, agent, pinned from requests").fetchone()) == ("shop", "streamer", 1)


def test_usage_report_endpoint(catalog, cfg, store):
    c, _, _ = _client(catalog, cfg, {"openai": "positive"}, store)
    for agent in ("a1", "a1", "a2"):
        c.post("/v1/chat/completions", json={**BODY, "modelspend": {"providers": ["openai"]}},
               headers={"X-ModelSpend-Project": "shop", "X-ModelSpend-Agent": agent})
    j = c.get("/v1/usage/report?by=project,agent").json()
    assert {(r["project"], r["agent"]): r["requests"] for r in j["rows"]} == {("shop", "a1"): 2, ("shop", "a2"): 1}
    assert j["totals"]["requests"] == 3 and j["notes"]
    only = c.get("/v1/usage/report?by=agent&agent=a2").json()
    assert [r["agent"] for r in only["rows"]] == ["a2"]
    for q in ("by=bogus", "by=project&nope=1", "top=0", "since=whenever", "by=a,b,c,d,e"):
        assert c.get(f"/v1/usage/report?{q}").status_code == 400


def test_usage_report_requires_the_token_when_set(catalog, cfg, store):
    c, _, _ = _client(catalog, cfg, store=store, token="sekret")
    assert c.get("/v1/usage/report").status_code == 401
    assert c.get("/v1/usage/report", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert c.get("/v1/usage/report", headers={"Authorization": "Bearer sekret"}).status_code == 200


def test_usage_report_refuses_foreign_host_without_token(catalog, cfg, store):
    # without a token the server is local-only: a DNS-rebinding page carries its own Host and must be refused (reads too)
    c, _, _ = _client(catalog, cfg, store=store)
    assert c.get("/v1/usage/report").status_code == 200
    evil = TestClient(c.app, base_url="http://attacker.test:8787")
    assert evil.get("/v1/usage/report").status_code == 403


# ------------------------------------------------------------------ SDK defaults and CLI
async def test_sdk_defaults_apply_to_every_call(catalog, cfg, store):
    from modelspend import ModelSpend
    eng, _ = make_engine(catalog, cfg, {"openai": "positive"}, store=store)
    ms = ModelSpend(providers=["openai"], config=cfg, catalog=catalog, adapters=eng.adapters, store=store, project="shop", agent="sdk-agent")
    res = await ms.aroute("Classify: great")
    assert (res.project, res.agent) == ("shop", "sdk-agent")
    res2 = await ms.aroute("Classify: great", agent="other")  # per-call overrides the default
    assert res2.agent == "other"


def test_cli_report_reads_the_store(tmp_path, monkeypatch, catalog, cfg):
    from typer.testing import CliRunner

    from modelspend.cli import app
    monkeypatch.setenv("MODELSPEND_HOME", str(tmp_path))
    st = Store(tmp_path / "usage.db")
    monkeypatch.setenv("MODELSPEND_DB", str(tmp_path / "usage.db"))
    _seed(st)
    runner = CliRunner()
    out = runner.invoke(app, ["report", "--by", "project", "--since", "all"])
    assert out.exit_code == 0 and "billing" in out.output and "support" in out.output
    j = runner.invoke(app, ["report", "--by", "agent", "--format", "json", "--since", "all"])
    assert j.exit_code == 0 and '"invoice-bot"' in j.output
    c = runner.invoke(app, ["report", "--by", "project", "--format", "csv", "--since", "all"])
    assert c.exit_code == 0 and c.output.splitlines()[0].startswith("project,requests")
    bad = runner.invoke(app, ["report", "--by", "nonsense"])
    assert bad.exit_code == 2 and "unknown dimension" in bad.output
