"""The what-if price table: recorded tokens replayed against catalog prices, with the same arithmetic as real accounting."""
from __future__ import annotations

import time

from typer.testing import CliRunner

from modelspend.catalog import Catalog
from modelspend.cli import app
from modelspend.config import Config
from modelspend.pricing import cost_for
from modelspend.store import Store
from modelspend.types import Usage
from modelspend.whatif import build_whatif, to_table

def _m(prov, mid, inp, out, cached=None, ctx=200_000, cap=8_000, tier=None):
    pr = {"input_per_mtok": inp, "output_per_mtok": out, "effective_date": "2026-10-01", "verified_on": "2026-10-01", "source_url": "https://example.com/p"}
    if cached is not None:
        pr["cached_input_per_mtok"] = cached
    m = {"provider": prov, "id": mid, "availability": "available", "verified_on": "2026-10-01", "capabilities": {"text": True}, "pricing": pr,
         "limits": {"context_tokens": ctx, "max_output_tokens": cap}}
    if tier:
        m["tier"] = tier
    return m


CAT = Catalog.load(overrides={"add_models": [_m("openai", "big", 10.0, 40.0, cached=1.0, tier=3), _m("openai", "small", 0.1, 0.4, tier=1),
                                              _m("openrouter", "vendor/slash-1", 0.5, 2.0)]})


def attempt(s, rid, ref, i=1000, o=200, cr=0, cw=0, task="classification", project=None, status="ok", cost=0.01):
    prov, model = ref.split("/", 1)
    s.db.execute("insert into requests(id, ts, task, provider, model, status, actual_cost, project) values (?,?,?,?,?,?,?,?)",
                 (rid, time.time(), task, prov, model, status, cost, project))
    s.db.execute("insert into attempts(request_id, idx, provider, model, outcome, input_tokens, cache_read, cache_write, output_tokens, cost) values (?,?,?,?,?,?,?,?,?,?)",
                 (rid, 0, prov, model, "ok", i, cr, cw, o, cost))
    s.db.commit()


def priced(n=1):
    return [CAT.get(r) for r in ("openai/big", "openai/small", "openrouter/vendor/slash-1")][:n]


def test_a_models_cost_is_the_catalog_price_of_the_recorded_tokens():
    m = priced()[0]
    s = Store(":memory:")
    attempt(s, "a", m.ref, i=1000, o=200, cr=300, cw=50)
    attempt(s, "b", m.ref, i=5000, o=10)
    r = build_whatif(s, CAT, Config(home="/tmp/x"))
    row = next(x for x in r["rows"] if x["model"] == m.ref)
    want = cost_for(m, Usage(input_tokens=1000, output_tokens=200, cache_read_tokens=300, cache_write_tokens=50)) + cost_for(m, Usage(input_tokens=5000, output_tokens=10))
    assert abs(row["cost"] - want) < 1e-12 and row["used"] and r["calls"] == 2
    assert r["tokens"] == {"input": 6000, "cache_read": 300, "cache_write": 50, "output": 210}


def test_rows_are_cheapest_first_and_compare_with_actual_spend():
    s = Store(":memory:")
    attempt(s, "a", priced()[0].ref, cost=0.5)
    r = build_whatif(s, CAT, Config(home="/tmp/x"))
    costs = [x["cost"] for x in r["rows"]]
    assert costs == sorted(costs) and len(costs) == 3
    assert abs(r["actual_cost"] - 0.5) < 1e-12
    assert all(abs(x["ratio"] - x["cost"] / 0.5) < 1e-9 for x in r["rows"])


def test_tooling_projects_and_failed_calls_are_not_your_traffic():
    s = Store(":memory:")
    ref = priced()[0].ref
    attempt(s, "a", ref)
    attempt(s, "b", ref, project="modelspend-shadow")
    attempt(s, "c", ref, project="modelspend-eval")
    attempt(s, "d", ref, status="failed")
    assert build_whatif(s, CAT, Config(home="/tmp/x"))["calls"] == 1


def test_project_and_only_filters():
    s = Store(":memory:")
    a, b = priced(2)
    attempt(s, "a", a.ref, project="web")
    attempt(s, "b", a.ref, project="batch")
    r = build_whatif(s, CAT, Config(home="/tmp/x"), project="web", only=[b.ref.lower()])
    assert r["calls"] == 1 and [x["model"] for x in r["rows"]] == [b.ref]


def test_models_without_a_price_are_listed_apart_never_guessed():
    s = Store(":memory:")
    attempt(s, "a", priced()[0].ref)
    cat = Catalog.load(overrides={"add_models": [{"provider": "openai", "id": "mystery-1", "availability": "available", "verified_on": "2026-10-01",
                                                   "capabilities": {"text": True}}]})
    r = build_whatif(s, cat, Config(home="/tmp/x"))
    assert "openai/mystery-1" in r["unpriced_models"] and all(x["model"] != "openai/mystery-1" for x in r["rows"])


def test_calls_that_exceed_a_models_limits_are_flagged():
    s = Store(":memory:")
    attempt(s, "a", priced()[0].ref, i=10_000_000, o=10)
    r = build_whatif(s, CAT, Config(home="/tmp/x"))
    assert any(x["too_large"] == 1 for x in r["rows"])
    attempt(s, "b", priced()[0].ref, i=10, o=9_000_000)
    assert any(x["over_output_cap"] for x in build_whatif(s, CAT, Config(home="/tmp/x"))["rows"])


def test_evidence_clears_only_on_the_bar_and_unmeasured_stays_unmeasured():
    s = Store(":memory:")
    a, b = priced(2)
    attempt(s, "a", a.ref, task="classification")
    prov, model = b.ref.split("/", 1)
    for i in range(60):  # 60/60 passes clears every built-in bar
        s.record_shadow(request_id=f"r{i}", project=None, agent=None, category="classification", used_provider="x", used_model="y", cand_provider=prov,
                        cand_model=model, method="exact", verdict="pass", reason="x", used_cost=1.0, cand_cost=0.1, judge_cost=0.0, cand_input=1, cand_output=1)
    c = priced(3)[2]
    cp, cm = c.ref.split("/", 1)
    for i in range(60):  # 20/60 is far below every bar
        s.record_shadow(request_id=f"w{i}", project=None, agent=None, category="classification", used_provider="x", used_model="y", cand_provider=cp,
                        cand_model=cm, method="exact", verdict="pass" if i < 20 else "fail", reason="x", used_cost=1.0, cand_cost=0.1, judge_cost=0.0, cand_input=1, cand_output=1)
    r = build_whatif(s, CAT, Config(home="/tmp/x"))
    rc = next(x for x in r["rows"] if x["model"] == c.ref)
    assert rc["clears"] == [] and rc["not_cleared"] == ["classification"] and rc["evidence_n"] == 60
    rb = next(x for x in r["rows"] if x["model"] == b.ref)
    assert rb["clears"] == ["classification"] and rb["evidence_n"] == 60
    assert all(x["clears"] == [] and x["evidence_n"] == 0 for x in r["rows"] if x["model"] not in (b.ref, c.ref))
    t = to_table(r, top=10_000)
    assert "unmeasured" in t and "60/60" in t and "Same tokens, other prices" in t


def test_empty_window_says_so():
    assert "No usage recorded" in to_table(build_whatif(Store(":memory:"), CAT, Config(home="/tmp/x")))


def test_cli_prints_json_and_table(tmp_path, monkeypatch):
    monkeypatch.setenv("MODELSPEND_HOME", str(tmp_path))
    s = Store(tmp_path / "modelspend.db")
    attempt(s, "a", priced()[0].ref)
    s.db.commit()
    s.db.close() if hasattr(s.db, "close") else None
    out = CliRunner().invoke(app, ["whatif", "--format", "json"])
    assert out.exit_code == 0, out.output
    assert '"rows"' in out.output
    assert CliRunner().invoke(app, ["whatif", "--all"]).exit_code == 0
    assert CliRunner().invoke(app, ["whatif", "--provider", "nonsense"]).exit_code == 2
