from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from typer.testing import CliRunner

from modelspend import reconcile as rc
from modelspend.cli import app
from modelspend.store import Store


def ts(day: str, hour: int = 12) -> float:
    return datetime.fromisoformat(f"{day}T{hour:02d}:00:00").replace(tzinfo=timezone.utc).timestamp()


_n = 0


def add(store, provider, model, day, i=0, o=0, cr=0, cw=0, cost=0.0, hour=12, outcome="ok"):
    """One request with one attempt (the tables the reconciliation reads)."""
    global _n
    _n += 1
    rid = f"r{_n}"
    store.db.execute("insert into requests(id, ts, provider, model, status) values (?,?,?,?, 'ok')", (rid, ts(day, hour), provider, model))
    store.db.execute("insert into attempts(request_id, idx, kind, purpose, provider, model, outcome, input_tokens, cache_read, cache_write, output_tokens, cost) "
                     "values (?,0,'generation','answer',?,?,?,?,?,?,?,?)", (rid, provider, model, outcome, i, cr, cw, o, cost))
    store.db.commit()


def imp(store, tmp_path, text, provider="openai", name="u.csv", **kw):
    p = tmp_path / name
    p.write_text(text)
    return rc.import_file(store, p, provider, **kw)


CSV = "date,model,input_tokens,output_tokens,cost_usd\n2026-10-10,gpt-4.1-nano,1000,500,0.0006\n2026-10-11,gpt-4.1-nano,2000,1000,0.0012\n"


# ---- parsing ----
def test_parse_aliases_dates_and_model_snapshots_merge():
    text = ("Day,Model,Prompt_Tokens,Completion_Tokens,Cached_Tokens,Requests,Amount\n"
            "2026-10-10,gpt-4.1-nano-2025-04-14,\"1,000\",500,100,3,$0.50\n"
            "2026-10-10T23:59:00Z,gpt-4.1-nano,1000,500,0,2,0.25\n"
            "1791590400,gpt-4.1-nano,10,5,0,1,0.01\n")  # epoch for 2026-10-10 00:00 UTC
    rows = rc.parse_usage(text, "openai")
    assert len(rows) == 1
    r = rows[0]
    assert (r["model"], r["day"]) == ("gpt-4.1-nano", "2026-10-10")
    assert (r["input_tokens"], r["output_tokens"], r["cache_read"], r["requests"]) == (2010, 1005, 100, 6)
    assert r["cost"] == pytest.approx(0.76)


def test_parse_json_bucket_shapes_and_jsonl():
    buckets = {"data": [{"start_time": 1791590400, "results": [{"model": "gpt-5-mini", "input_tokens": 7, "output_tokens": 3, "num_model_requests": 2}]}]}
    r = rc.parse_usage(json.dumps(buckets), "openai")[0]
    assert (r["day"], r["input_tokens"], r["output_tokens"], r["requests"]) == ("2026-10-10", 7, 3, 2)
    anth = {"data": [{"starting_at": "2026-10-10T00:00:00Z", "results": [{"model": "claude-sonnet-5-5", "uncached_input_tokens": 5,
            "cache_read_input_tokens": 2, "cache_creation_input_tokens": {"ephemeral_5m": 3, "ephemeral_1h": 4}, "output_tokens": 9}]}]}
    a = rc.parse_usage(json.dumps(anth), "anthropic")[0]
    assert (a["input_tokens"], a["cache_read"], a["cache_write"], a["output_tokens"]) == (5, 2, 7, 9)
    lines = "\n".join(json.dumps(x) for x in [{"day": "2026-10-10", "model": "m", "input_tokens": 1, "output_tokens": 1},
                                              {"day": "2026-10-11", "model": "m", "input_tokens": 2, "output_tokens": 2}])
    assert [x["day"] for x in rc.parse_usage(lines, "xai")] == ["2026-10-10", "2026-10-11"]


@pytest.mark.parametrize("text,provider,match", [
    ("", "openai", "empty"),
    ("date,input_tokens\n2026-10-10,5\n", "openai", "'model' column"),
    ("model,input_tokens\nm,5\n", "openai", "'day' column"),
    ("date,model\n2026-10-10,m\n", "openai", "no token or cost"),
    ("date,model,input_tokens\n2026-10-10,m,5\n", None, "which provider"),
    ("date,model,input_tokens\n2026-10-10,m,5\n", "mistral", "unknown provider"),
    ("date,model,input_tokens\n2026-10-10,m,abc\n", "openai", "row 1: not a number"),
    ("date,model,input_tokens\n2026-10-10,m,-5\n", "openai", "row 1: invalid number"),
    ("date,model,input_tokens\nsoon,m,5\n", "openai", "row 1: cannot read a date"),
    ("date,model,input_tokens\n2026-10-10,,5\n", "openai", "row 1: missing"),
    ("[1, 2]", "openai", "objects"),
    ("{not json", "openai", "not valid JSON"),
])
def test_parse_errors_are_specific(text, provider, match):
    with pytest.raises(rc.ReconcileError, match=match):
        rc.parse_usage(text, provider)


def test_map_overrides_and_row_cap(monkeypatch):
    rows = rc.parse_usage("when,what,in,out\n2026-10-10,m,4,6\n", "openai", mapping={"day": "when", "model": "what", "input_tokens": "in", "output_tokens": "out"})
    assert (rows[0]["input_tokens"], rows[0]["output_tokens"]) == (4, 6)
    with pytest.raises(rc.ReconcileError, match="no such column"):
        rc.parse_usage("date,model,input_tokens\n2026-10-10,m,1\n", "openai", mapping={"day": "nope"})
    monkeypatch.setattr(rc, "MAX_ROWS", 2)
    with pytest.raises(rc.ReconcileError, match="too many rows"):
        rc.parse_usage("date,model,input_tokens\n" + "2026-10-10,m,1\n" * 3, "openai")


# ---- import ----
def test_import_replaces_same_source_and_warns_on_overlap(store, tmp_path):
    r1 = imp(store, tmp_path, CSV)
    assert (r1["rows"], r1["first_day"], r1["last_day"], r1["has_cost"], r1["overlap"]) == (2, "2026-10-10", "2026-10-11", True, [])
    imp(store, tmp_path, CSV)  # same source again: replaced, not doubled
    assert store.db.execute("select count(*) from provider_usage").fetchone()[0] == 2
    r3 = imp(store, tmp_path, CSV, name="other.csv")
    assert {(o["provider"], o["day"]) for o in r3["overlap"]} == {("openai", "2026-10-10"), ("openai", "2026-10-11")}
    assert rc.remove_source(store, "other.csv") == 2 and rc.remove_source(store, "other.csv") == 0


def test_import_rejects_bad_source_name_missing_and_huge_files(store, tmp_path, monkeypatch):
    with pytest.raises(rc.ReconcileError, match="--source"):
        imp(store, tmp_path, CSV, source="a;drop table")
    with pytest.raises(rc.ReconcileError, match="no such file"):
        rc.import_file(store, tmp_path / "missing.csv", "openai")
    monkeypatch.setattr(rc, "MAX_BYTES", 10)
    with pytest.raises(rc.ReconcileError, match="larger than"):
        imp(store, tmp_path, CSV)


# ---- comparison ----
def test_match_within_tolerance_and_snapshot_names(store, tmp_path):
    imp(store, tmp_path, CSV.replace("gpt-4.1-nano", "gpt-4.1-nano-2025-04-14"))
    add(store, "openai", "gpt-4.1-nano", "2026-10-10", i=1000, o=500, cost=0.0006)
    add(store, "openai", "gpt-4.1-nano", "2026-10-11", i=1990, o=1000, cost=0.0012)  # 10 tokens (0.3%) under
    rep = rc.compare(store)
    assert rep["summary"]["ok"] and rep["summary"]["match"] == 1
    row = rep["rows"][0]
    assert (row["provider_tokens"], row["modelspend_tokens"], row["tokens"]["status"], row["cost"]["status"]) == (4500, 4490, "match", "match")


def test_modelspend_higher_is_always_flagged_provider_higher_only_when_exclusive(store, tmp_path):
    imp(store, tmp_path, CSV)
    add(store, "openai", "gpt-4.1-nano", "2026-10-10", i=1000, o=500, cost=0.0006)
    add(store, "openai", "gpt-4.1-nano", "2026-10-11", i=2000, o=1000, cost=0.0012)
    add(store, "openai", "gpt-4.1-nano", "2026-10-11", i=5000, o=500, cost=0.002)  # ModelSpend saw 6k more tokens than were billed
    rep = rc.compare(store)
    r = rep["rows"][0]
    assert r["tokens"]["status"] == "modelspend_higher" and r["attention"] and not rep["summary"]["ok"]
    assert r["tokens"]["diff"] == 5500 and r["cost"]["status"] == "modelspend_higher"

    other = Store(":memory:")
    imp(other, tmp_path, CSV, name="o.csv")
    add(other, "openai", "gpt-4.1-nano", "2026-10-10", i=100, o=50, cost=0.0001)
    soft = rc.compare(other)
    assert soft["rows"][0]["tokens"]["status"] == "provider_higher" and not soft["rows"][0]["attention"] and soft["summary"]["ok"]
    hard = rc.compare(other, exclusive=True)
    assert hard["rows"][0]["attention"] and not hard["summary"]["ok"]


def test_one_sided_rows_and_coverage_window(store, tmp_path):
    imp(store, tmp_path, CSV)
    add(store, "openai", "gpt-4.1-nano", "2026-10-10", i=1000, o=500, cost=0.0006)
    add(store, "openai", "gpt-4.1-nano", "2026-10-11", i=2000, o=1000, cost=0.0012)
    add(store, "openai", "gpt-4.1-nano", "2026-10-20", i=999999, o=1)  # outside the export's days: must not count
    add(store, "openai", "gpt-5-mini", "2026-10-10", i=300, o=300)  # ModelSpend only
    add(store, "openai", "gpt-5-mini", "2026-10-09", i=300, o=300)  # before the export: ignored
    add(store, "anthropic", "claude-sonnet-5-5", "2026-10-10", i=50, o=50)  # provider not imported: ignored
    rep = rc.compare(store)
    by = {r["model"]: r for r in rep["rows"]}
    assert set(by) == {"gpt-4.1-nano", "gpt-5-mini"}
    assert by["gpt-4.1-nano"]["tokens"]["status"] == "match"
    assert by["gpt-5-mini"]["tokens"]["status"] == "modelspend_only" and by["gpt-5-mini"]["modelspend_tokens"] == 600 and by["gpt-5-mini"]["attention"]

    store.db.execute("delete from provider_usage")
    imp(store, tmp_path, "date,model,input_tokens,output_tokens\n2026-10-10,gpt-4o,40,60\n", name="x.csv")
    only = {r["model"]: r for r in rc.compare(store)["rows"]}["gpt-4o"]
    assert only["tokens"]["status"] == "provider_only" and not only["attention"]  # other tools on the key are normal
    assert {r["model"]: r for r in rc.compare(store, exclusive=True)["rows"]}["gpt-4o"]["attention"]


def test_cached_tokens_follow_each_providers_semantics(store, tmp_path):
    # OpenAI-style: input column already includes cached; Anthropic-style: input excludes cache read/write.
    add(store, "openai", "gpt-4.1-nano", "2026-10-10", i=600, cr=400, o=100)
    add(store, "anthropic", "claude-sonnet-5-5", "2026-10-10", i=600, cr=300, cw=100, o=100)
    imp(store, tmp_path, "date,model,input_tokens,cached_tokens,output_tokens\n2026-10-10,gpt-4.1-nano,1000,400,100\n", "openai", name="o.csv")
    imp(store, tmp_path, "date,model,input_tokens,cache_read_tokens,cache_write_tokens,output_tokens\n2026-10-10,claude-sonnet-5-5,600,300,100,100\n",
        "anthropic", name="a.csv")
    rep = rc.compare(store)
    assert rep["summary"]["ok"], rep["rows"]
    assert {r["provider"]: r["provider_tokens"] for r in rep["rows"]} == {"openai": 1100, "anthropic": 1100}
    flipped = rc.compare(store, cached_included={"anthropic": True})  # wrong assumption: provider side shrinks to 700, ModelSpend's 1100 is flagged
    assert not flipped["summary"]["ok"]
    assert [r["tokens"]["status"] for r in flipped["rows"] if r["provider"] == "anthropic"] == ["modelspend_higher"]


def test_cost_partial_when_unpriced_and_missing_cost_column(store, tmp_path):
    imp(store, tmp_path, CSV)
    add(store, "openai", "gpt-4.1-nano", "2026-10-10", i=1000, o=500, cost=0.0006)
    add(store, "openai", "gpt-4.1-nano", "2026-10-11", i=2000, o=1000, cost=None)
    row = rc.compare(store)["rows"][0]
    assert row["tokens"]["status"] == "match" and row["cost"]["status"] == "n/a" and "partial" in row["cost"]["note"] and row["modelspend_cost"] is None

    no_cost = Store(":memory:")
    imp(no_cost, tmp_path, "date,model,input_tokens,output_tokens\n2026-10-10,m,10,10\n", name="n.csv")
    add(no_cost, "openai", "m", "2026-10-10", i=10, o=10, cost=1.0)
    r = rc.compare(no_cost)["rows"][0]
    assert r["cost"]["status"] == "n/a" and r["provider_cost"] is None and r["tokens"]["status"] == "match"


def test_by_day_and_tolerance_validation_and_floor(store, tmp_path):
    imp(store, tmp_path, CSV)
    add(store, "openai", "gpt-4.1-nano", "2026-10-10", i=1000, o=500, cost=0.0006)
    add(store, "openai", "gpt-4.1-nano", "2026-10-11", i=1000, o=500, cost=0.0006)  # day 11 is 1.5k short
    rep = rc.compare(store, by_day=True)
    assert [(r["day"], r["tokens"]["status"]) for r in rep["rows"]] == [("2026-10-10", "match"), ("2026-10-11", "provider_higher")]
    assert rc.compare(store)["rows"][0]["tokens"]["status"] == "provider_higher"  # 3000 of 4500 -> far outside 2%
    with pytest.raises(rc.ReconcileError, match="tolerance"):
        rc.compare(store, tolerance=2)
    # tiny usage: the absolute floor (100 tokens) stops 1-2 token rounding from being flagged
    assert rc.judge(1098, 1000, 0.02, 100, False)["status"] == "match"
    assert rc.judge(1121, 1000, 0.02, 100, False)["status"] == "modelspend_higher"
    assert rc.judge(879, 1000, 0.02, 100, True)["attention"]


def test_compare_needs_an_import(store):
    with pytest.raises(rc.ReconcileError, match="no imported provider usage"):
        rc.compare(store)


def test_formatting_and_csv_injection_guard(store, tmp_path):
    imp(store, tmp_path, "date,model,input_tokens,output_tokens\n2026-10-10,=cmd|' /c calc'!A0,10,10\n", name="e.csv")
    add(store, "openai", "gpt-4.1-nano", "2026-10-10", i=10, o=10)
    rep = rc.compare(store)
    assert "'=cmd" in rc.to_csv(rep).replace('"', "") and rep["rows"][0]["model"].startswith("=")
    table = rc.to_table(rep)
    assert "Compared days (UTC): openai: 2026-10-10..2026-10-10" in table and "invoice is authoritative" in table and "<-- check" in table


# ---- CLI ----
def test_cli_import_run_check_and_remove(tmp_path, monkeypatch):
    monkeypatch.setenv("MODELSPEND_DB", str(tmp_path / "u.db"))
    f = tmp_path / "export.csv"
    f.write_text(CSV)
    runner = CliRunner()
    r = runner.invoke(app, ["reconcile", "import", str(f), "--provider", "openai"])
    assert r.exit_code == 0 and "imported 2 rows" in r.output
    s = Store(str(tmp_path / "u.db"))
    add(s, "openai", "gpt-4.1-nano", "2026-10-10", i=1000, o=500, cost=0.0006)
    add(s, "openai", "gpt-4.1-nano", "2026-10-11", i=2000, o=1000, cost=0.0012)
    ok = runner.invoke(app, ["reconcile", "run", "--check"])
    assert ok.exit_code == 0 and "1 of 1 rows match" in ok.output
    assert json.loads(runner.invoke(app, ["reconcile", "run", "--format", "json"]).output)["summary"]["ok"] is True
    add(s, "openai", "gpt-4.1-nano", "2026-10-11", i=9000, o=1)
    bad = runner.invoke(app, ["reconcile", "run", "--check"])
    assert bad.exit_code == 1 and "need a look" in bad.output
    assert "export.csv" in runner.invoke(app, ["reconcile", "list"]).output
    assert runner.invoke(app, ["reconcile", "import", str(f)]).exit_code == 2  # provider missing
    assert runner.invoke(app, ["reconcile", "remove", "nope"]).exit_code == 2
    assert runner.invoke(app, ["reconcile", "remove", "export.csv"]).exit_code == 0
    assert runner.invoke(app, ["reconcile", "run"]).exit_code == 2


# ---- real OpenAI completions-usage export layout (2026-10-16, IDs anonymized): blank buckets for quiet days, float-formatted numbers ----
OPENAI_EXPORT = (
    "start_time,end_time,start_time_iso,end_time_iso,project_id,num_model_requests,user_id,api_key_id,model,batch,service_tier,input_tokens,"
    "output_tokens,input_cached_tokens,input_cache_write_tokens,input_cache_write_12h_tokens,input_uncached_tokens,input_text_tokens,"
    "output_text_tokens,input_cached_text_tokens,input_audio_tokens,input_cached_audio_tokens,output_audio_tokens,input_image_tokens,"
    "input_cached_image_tokens,output_image_tokens,api_source\n"
    "1791763200,1791849600,2026-10-12T00:00:00+00:00,2026-10-13T00:00:00+00:00,proj_x,1.0,user-x,key_x,gpt-5-mini-2025-08-07,False,default,37.0,164.0,0.0,0.0,0.0,37.0,37.0,164.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,\n"
    "1791763200,1791849600,2026-10-12T00:00:00+00:00,2026-10-13T00:00:00+00:00,proj_x,3.0,user-x,key_x,gpt-4.1-nano-2025-04-14,False,default,66.0,45.0,0.0,0.0,0.0,66.0,66.0,45.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,\n"
    "1791849600,1791936000,2026-10-13T00:00:00+00:00,2026-10-14T00:00:00+00:00,,,,,,,,,,,,,,,,,,,,,,,\n"
    "1791936000,1792022400,2026-10-14T00:00:00+00:00,2026-10-15T00:00:00+00:00,proj_x,6.0,user-x,key_x,gpt-4.1-nano-2025-04-14,False,default,96.0,85.0,0.0,0.0,0.0,96.0,96.0,85.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,\n"
    "1792108800,1792195200,2026-10-16T00:00:00+00:00,2026-10-17T00:00:00+00:00,,,,,,,,,,,,,,,,,,,,,,,\n")


def test_real_openai_export_layout_imports_with_empty_days_kept(store, tmp_path):
    r = imp(store, tmp_path, OPENAI_EXPORT)
    assert (r["rows"], r["models"], r["first_day"], r["last_day"], r["has_cost"]) == (3, 2, "2026-10-12", "2026-10-16", False)
    assert r["empty_days"] == ["2026-10-13", "2026-10-16"]
    got = {(x["day"], x["model"]): (x["input_tokens"], x["output_tokens"], x["requests"]) for x in rc.parse_usage(OPENAI_EXPORT, "openai")}
    assert got[("2026-10-12", "gpt-5-mini")] == (37, 164, 1) and got[("2026-10-14", "gpt-4.1-nano")] == (96, 85, 6)


def test_a_modelspend_call_on_a_day_the_provider_reports_empty_is_flagged(store, tmp_path):
    imp(store, tmp_path, OPENAI_EXPORT)
    add(store, "openai", "gpt-4.1-nano", "2026-10-12", i=66, o=45)
    add(store, "openai", "gpt-5-mini", "2026-10-12", i=37, o=164)
    add(store, "openai", "gpt-4.1-nano", "2026-10-14", i=96, o=85)
    ok = rc.compare(store)
    assert ok["summary"]["ok"] and all(r["model"] != rc.NO_USAGE for r in ok["rows"])
    assert {r["model"]: r["provider_tokens"] for r in ok["rows"]} == {"gpt-4.1-nano": 292, "gpt-5-mini": 201}
    add(store, "openai", "gpt-4.1-nano", "2026-10-13", i=500, o=100)  # provider says nothing happened that day
    add(store, "openai", "gpt-4.1-nano", "2026-10-16", i=500, o=100)  # the last (empty) day is covered too
    bad = rc.compare(store)
    assert not bad["summary"]["ok"] and bad["rows"][0]["tokens"]["status"] == "modelspend_higher"
    days = {r["day"]: r["tokens"]["status"] for r in rc.compare(store, by_day=True)["rows"] if r["model"] == "gpt-4.1-nano"}
    assert days["2026-10-13"] == days["2026-10-16"] == "modelspend_only"


def test_a_row_with_numbers_but_no_model_is_still_an_error():
    with pytest.raises(rc.ReconcileError, match="row 1: missing or oversized model"):
        rc.parse_usage("date,model,input_tokens\n2026-10-10,,5\n", "openai")
    with pytest.raises(rc.ReconcileError, match="row 1: cannot read a date"):
        rc.parse_usage("date,model,input_tokens\n,,\n", "openai")  # a blank row without even a day is not a bucket


def test_zero_token_modelspend_rows_are_not_flagged_and_the_summary_explains_provider_higher(store, tmp_path):
    imp(store, tmp_path, CSV)
    add(store, "openai", "gpt-4.1-nano", "2026-10-10", i=1000, o=500, cost=0.0006)
    add(store, "openai", "gpt-4.1-nano", "2026-10-11", i=100, o=50)  # provider saw far more (other tools)
    add(store, "openai", "gpt-4.1", "2026-10-10", i=0, o=0, outcome="error")  # a call that failed before anything was billed
    rep = rc.compare(store)
    by = {r["model"]: r for r in rep["rows"]}
    assert by["gpt-4.1"]["tokens"]["status"] == "n/a" and not by["gpt-4.1"]["attention"]
    assert rep["summary"]["ok"] and rep["summary"]["other_usage_rows"] == 1
    text = rc.to_table(rep)
    assert "nothing needs a look: ModelSpend is never above the provider" in text and "all rows within tolerance" not in text
    assert "--exclusive" in text
    clean = Store(":memory:")
    imp(clean, tmp_path, CSV, name="c.csv")
    add(clean, "openai", "gpt-4.1-nano", "2026-10-10", i=1000, o=500, cost=0.0006)
    add(clean, "openai", "gpt-4.1-nano", "2026-10-11", i=2000, o=1000, cost=0.0012)
    assert "Result: 1 of 1 rows match within 2%; nothing needs a look.\n" in rc.to_table(rc.compare(clean))


def test_request_counts_are_shown_so_missing_calls_can_be_counted(store, tmp_path):
    imp(store, tmp_path, "date,model,input_tokens,output_tokens,num_model_requests\n2026-10-10,gpt-4.1-nano,3000,1000,5\n")
    for _ in range(3):
        add(store, "openai", "gpt-4.1-nano", "2026-10-10", i=500, o=100)
    rep = rc.compare(store)
    r = rep["rows"][0]
    assert (r["provider_requests"], r["modelspend_calls"]) == (5, 3)  # two calls were made outside ModelSpend
    assert "calls prov/MS" in rc.to_table(rep) and "5/3" in rc.to_table(rep)
    assert rc.to_csv(rep).splitlines()[0].endswith("provider_requests,modelspend_calls,attention") and ",5,3,True" in rc.to_csv(rep)
