"""Prompt-caching advice. Two promises to pin down: it never keeps prompt text (only keyed hashes and counts) and can never hurt a request, and its
arithmetic about repeats, cached tokens and savings is right on constructed timelines."""
from __future__ import annotations

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from conftest import make_engine
from modelspend import RouteRequest
from modelspend.cacheadvice import build_cache_advice, parse_window, to_table
from modelspend.catalog import Catalog
from modelspend.cli import app
from modelspend.config import Config
from modelspend.prefixes import MIN_PREFIX_TOKENS, prefix_levels
from modelspend.server.app import create_app
from modelspend.store import Store
from modelspend.types import Message, ToolSpec

from test_whatif import _m

SALT = b"s" * 32
SECRET = "SECRET-POLICY-TEXT-9931"
LONG = (f"{SECRET} You are a careful support agent. Always answer politely and cite the policy section you used. " * 40)  # ~4,000 chars, >1,024 tokens


def msgs(system=LONG, last="Where is my order?", mid=None):
    out = [Message(role="system", content=system)] if system is not None else []
    out += [Message(role="user", content=m) for m in mid or []]
    return out + [Message(role="user", content=last)]


# ---------------------------------------------------------------- prefix tracking
def test_long_prefixes_are_hashed_and_the_last_message_is_not_part_of_them():
    a = prefix_levels(msgs(last="Where is my order?"), None, None, SALT)
    b = prefix_levels(msgs(last="A completely different question"), None, None, SALT)
    assert [x["level"] for x in a] == ["system"] and a[0]["tokens"] >= MIN_PREFIX_TOKENS
    assert a == b  # only the fixed start repeats; the question changes every time
    assert len(a[0]["h"]) == 16 and SECRET not in json.dumps(a)


def test_one_changed_character_early_in_the_prefix_changes_the_hash_and_the_salt_changes_it_too():
    base = prefix_levels(msgs(), None, None, SALT)[0]["h"]
    assert prefix_levels(msgs(system="X" + LONG[1:]), None, None, SALT)[0]["h"] != base
    assert prefix_levels(msgs(), None, None, b"t" * 32)[0]["h"] != base  # a hash from another database matches nothing here


def test_tools_and_schema_are_part_of_what_is_cached_so_they_are_part_of_the_prefix():
    base = prefix_levels(msgs(), None, None, SALT)[0]["h"]
    tool = ToolSpec(name="lookup", description="find an order", parameters={"type": "object"})
    assert prefix_levels(msgs(), None, [tool], SALT)[0]["h"] != base
    assert prefix_levels(msgs(), {"type": "object"}, None, SALT)[0]["h"] != base


def test_context_beyond_the_system_prompt_is_a_second_prefix_and_short_prompts_cost_nothing(monkeypatch):
    levels = prefix_levels(msgs(mid=["Example one. " * 300]), None, None, SALT)
    assert [x["level"] for x in levels] == ["system", "context"] and levels[1]["tokens"] > levels[0]["tokens"]
    import modelspend.prefixes as px
    monkeypatch.setattr(px, "estimate_input_tokens", lambda *a, **k: (_ for _ in ()).throw(AssertionError("tokenized a short prompt")))
    assert prefix_levels(msgs(system="Be brief."), None, None, SALT) == []  # below the character pre-filter: nothing is tokenized


def test_a_long_prefix_under_the_token_minimum_is_not_tracked():
    assert prefix_levels(msgs(system="word " * 650), None, None, SALT) == []  # 3,250 chars but ~650 tokens: no provider caches that


# ---------------------------------------------------------------- engine integration
def run_requests(catalog, cfg, store, n=3, system=LONG):
    eng, _ = make_engine(catalog, cfg, {"openai": "ok"}, store=store)
    for i in range(n):
        asyncio.run(eng.run(RouteRequest(messages=[{"role": "system", "content": system}, {"role": "user", "content": f"question {i}"}], providers=["openai"])))
    return eng


def test_the_engine_stores_hashes_not_text_and_dumping_the_database_shows_no_prompt(catalog, cfg, store):
    run_requests(catalog, cfg, store)
    rows = [dict(r) for r in store.db.execute("select * from prefix_stats")]
    assert len(rows) == 3 and len({r["h"] for r in rows}) == 1 and rows[0]["level"] == "system"
    dump = json.dumps([dict(r) for t in ("prefix_stats", "requests", "attempts", "meta") for r in store.db.execute(f"select * from {t}") if t != "meta"], default=str)
    assert SECRET not in dump and "question 1" not in dump


def test_tracking_can_be_turned_off(catalog, cfg, store):
    cfg.track_prefixes = False
    run_requests(catalog, cfg, store)
    assert store.db.execute("select count(*) from prefix_stats").fetchone()[0] == 0


def test_a_tracking_failure_never_fails_the_request(catalog, cfg, store, monkeypatch):
    monkeypatch.setattr(store, "record_prefixes", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full")))
    eng = run_requests(catalog, cfg, store, n=1)
    assert store.db.execute("select count(*) from requests").fetchone()[0] == 1 and eng is not None


def test_streaming_requests_are_tracked_too(catalog, cfg, store):
    eng, _ = make_engine(catalog, cfg, {"openai": "streamed answer"}, store=store)

    async def go():
        async for _ in eng.stream(RouteRequest(messages=[{"role": "system", "content": LONG}, {"role": "user", "content": "hi"}], providers=["openai"])):
            pass
    asyncio.run(go())
    assert store.db.execute("select count(*) from prefix_stats").fetchone()[0] == 1


def test_forgetting_and_pruning_remove_prefix_rows(store):
    store.record_prefixes("r1", "openai", "m", [{"level": "system", "h": "a" * 16, "tokens": 2000}])
    store.db.execute("update prefix_stats set ts = 1")
    store.db.execute("insert into requests(id, ts) values ('r1', 1)")
    assert store.prune(100)["requests"] == 1 and store.db.execute("select count(*) from prefix_stats").fetchone()[0] == 0
    store.record_prefixes("r2", "openai", "m", [{"level": "system", "h": "b" * 16, "tokens": 2000}])
    assert store.forget_prefixes() == 1 and store.forget_prefixes() == 0


def test_settings_track_prefixes_must_be_a_real_boolean(tmp_path, monkeypatch):
    monkeypatch.setenv("MODELSPEND_HOME", str(tmp_path))
    (tmp_path / "settings.json").write_text('{"track_prefixes": false}')
    assert Config.from_env().track_prefixes is False
    (tmp_path / "settings.json").write_text('{"track_prefixes": "false"}')
    with pytest.raises(ValueError, match="track_prefixes"):
        Config.from_env()
    (tmp_path / "settings.json").write_text("{}")
    assert Config.from_env().track_prefixes is True
    monkeypatch.setenv("MODELSPEND_TRACK_PREFIXES", "0")
    assert Config.from_env().track_prefixes is False


# ---------------------------------------------------------------- the advice itself
def cat(provider="openai", cached=0.25, write=None, inp=2.0):
    m = _m(provider, "gpt-x", inp, 8.0, cached=cached)
    if write is not None:
        m["pricing"]["cache_write_per_mtok"] = write
    return Catalog.load(overrides={"add_models": [m]})


def seed(store, n, gap, *, provider="openai", model="gpt-x", cached=0, system=("S", 2000), context=None, project=None, start=None, tag="r"):
    t0 = start if start is not None else time.time() - n * gap - 10
    for i in range(n):
        rid = f"{tag}{i}-{provider}"
        store.db.execute("insert into requests(id, ts, provider, model, status, project, cache_read_tokens) values (?,?,?,?,?,?,?)",
                         (rid, t0 + i * gap, provider, model, "ok", project, cached if isinstance(cached, int) else cached[i]))
        lv = [("system", system[0] * 16, system[1])]
        if context:
            lv.append(("context", (context[0] if isinstance(context[0], str) else context[0][i]) * 16, context[1]))
        for level, h, tok in lv:
            store.db.execute("insert into prefix_stats(request_id, ts, provider, model, level, h, tokens) values (?,?,?,?,?,?,?)", (rid, t0 + i * gap, provider, model, level, h, tok))
    store.db.commit()


def advise(store, catalog=None, **kw):
    return build_cache_advice(store, catalog or cat(), Config(home="/tmp/x"), **kw)


def test_repeats_inside_the_window_that_were_never_cached_are_counted_and_priced():
    s = Store(":memory:")
    seed(s, 5, 60)  # five calls a minute apart, 2,000-token prefix, provider reported 0 cached tokens
    r = advise(s)["rows"][0]
    assert (r["requests"], r["repeats"], r["repeat_tokens"], r["cached_tokens"], r["missed_tokens"]) == (5, 4, 8000, 0, 8000)
    assert abs(r["potential_saving_usd"] - 8000 * (2.0 - 0.25) / 1e6) < 1e-12 and r["realized_saving_usd"] == 0
    assert "no cached tokens were reported" in r["advice"] and r["kind"] == "automatic" and not r["drifting"]
    assert "changes between calls" not in r["advice"]  # the text was identical: blaming the prompt would be wrong


def test_cached_tokens_the_provider_reported_are_realized_savings_and_close_the_gap():
    s = Store(":memory:")
    seed(s, 5, 60, cached=[0, 1900, 1900, 1900, 1900])
    r = advise(s)["rows"][0]
    assert r["cached_tokens"] == 7600 and r["missed_tokens"] == 400
    assert abs(r["realized_saving_usd"] - 7600 * 1.75 / 1e6) < 1e-12 and r["advice"] == "caching is working"
    seed(s, 5, 60, cached=[0, 700, 0, 700, 0], system=("T", 2000), tag="u")
    part = next(x for x in advise(s)["rows"] if x["prefix"] == "T" * 8)
    assert "partly cached" in part["advice"]


def test_repeats_further_apart_than_the_window_do_not_count_and_a_longer_window_changes_that():
    s = Store(":memory:")
    seed(s, 4, 900)  # 15 minutes apart
    r = advise(s)["rows"][0]
    assert r["repeats"] == 0 and r["potential_saving_usd"] == 0 and "more than 5 min apart" in r["advice"]
    assert advise(s, window_s=3600)["rows"][0]["repeats"] == 3


def test_a_prefix_seen_once_is_not_a_finding():
    s = Store(":memory:")
    seed(s, 1, 60)
    r = advise(s)
    assert r["rows"] == [] and r["seen_once"] == 1 and "No repeated long prompt prefix" in to_table(r)


def test_nested_prefixes_are_not_double_counted_and_the_longest_repeating_one_gets_the_credit():
    s = Store(":memory:")
    seed(s, 4, 60, context=(["a", "b", "c", "d"], 3500))  # same system prompt, a different context every time
    rows = advise(s)["rows"]
    assert len(rows) == 1 and rows[0]["level"] == "system" and rows[0]["requests"] == 4 and rows[0]["repeats"] == 3 and rows[0]["repeat_tokens"] == 6000
    s2 = Store(":memory:")
    seed(s2, 4, 60, context=("x", 3500))  # the context repeats too: it is the longer prefix a cache holds
    rows2 = advise(s2)["rows"]
    top = max(rows2, key=lambda r: r["prefix_tokens"])
    assert top["level"] == "context" and top["repeats"] == 3 and top["repeat_tokens"] == 10500
    assert next(r for r in rows2 if r["level"] == "system")["repeats"] == 0  # nothing is counted twice


def test_anthropic_needs_markers_and_the_write_premium_comes_off_the_projection():
    s = Store(":memory:")
    seed(s, 5, 60, provider="anthropic", model="gpt-x")
    plain = advise(s, cat("anthropic", cached=0.3, inp=3.0))["rows"][0]
    withw = advise(s, cat("anthropic", cached=0.3, inp=3.0, write=3.75))["rows"][0]
    assert "sends no markers unless you turn them on" in plain["advice"] and plain["kind"] == "explicit"
    assert withw["potential_saving_usd"] < plain["potential_saving_usd"]
    assert abs(plain["potential_saving_usd"] - withw["potential_saving_usd"] - 1 * 2000 * 0.75 / 1e6) < 1e-12  # one cache write in the window


def test_unknown_providers_are_not_assumed_to_cache_and_unpriced_models_get_no_dollar_figure():
    s = Store(":memory:")
    seed(s, 4, 60, provider="xai", model="gpt-x")
    r = advise(s, cat("xai"))["rows"][0]
    assert r["kind"] == "unknown" and "modelspend verify xai --cache" in r["advice"]
    s2 = Store(":memory:")
    seed(s2, 4, 60)
    nop = advise(s2, cat(cached=None))["rows"][0]
    assert nop["potential_saving_usd"] is None and "no cached-input price" in nop["advice"]
    gone = advise(s2, Catalog.load(overrides={"add_models": []}))["rows"][0]
    assert gone["potential_saving_usd"] is None and "no price in your catalog" in gone["advice"]


def test_tooling_projects_and_other_projects_are_excluded_or_filtered():
    s = Store(":memory:")
    seed(s, 3, 60, project="modelspend-shadow", tag="a")
    assert advise(s)["rows"] == []
    seed(s, 3, 60, project="web", tag="b", system=("W", 2000))
    seed(s, 3, 60, project="batch", tag="c", system=("B", 2000))
    assert {r["prefix"] for r in advise(s)["rows"]} == {"W" * 8, "B" * 8}
    assert [r["prefix"] for r in advise(s, project="web")["rows"]] == ["W" * 8]


def test_window_parsing_and_the_table_and_totals():
    assert parse_window("90s") == 90 and parse_window("5m") == 300 and parse_window("1h") == 3600
    for bad in ("", "5", "0m", "5 days", "-1m", "1e3s"):
        with pytest.raises(ValueError):
            parse_window(bad)
    s = Store(":memory:")
    seed(s, 5, 60)
    r = advise(s)
    t = to_table(r)
    assert "Still on the table:" in t and "(system)" in t and r["totals"]["potential_saving_usd"] > 0 and "keyed hashes" in t
    off = build_cache_advice(Store(":memory:"), cat(), Config(home="/tmp/x", track_prefixes=False))
    assert "tracking is off" in to_table(off)


# ---------------------------------------------------------------- CLI and endpoint
def test_cli_prints_validates_and_forgets(tmp_path, monkeypatch):
    monkeypatch.setenv("MODELSPEND_HOME", str(tmp_path))
    st = Store(tmp_path / "modelspend.db")
    seed(st, 4, 60)
    st.db.commit()
    o = CliRunner().invoke(app, ["caching", "--since", "all"]).output
    assert "what to do" in o and "No dollar figure" in o  # the CLI loads your catalog, which has no price for the test model
    out = CliRunner().invoke(app, ["caching", "--since", "all", "--format", "json"])
    assert out.exit_code == 0 and '"rows"' in out.output
    assert CliRunner().invoke(app, ["caching", "--window", "soon"]).exit_code == 2
    assert CliRunner().invoke(app, ["caching", "--format", "xml"]).exit_code == 2
    f = CliRunner().invoke(app, ["caching", "--forget"])
    assert f.exit_code == 0 and "erased 4" in f.output
    assert Store(tmp_path / "modelspend.db").db.execute("select count(*) from prefix_stats").fetchone()[0] == 0


def _client(tmp_path, token=None):
    cfg = Config(home=tmp_path, api_keys={"openai": "k"}, retry_backoff_s=0.0)
    cfg.api_token = token
    s = Store(":memory:")
    seed(s, 4, 60)
    eng, _ = make_engine(cat(), cfg, {}, store=s)
    return TestClient(create_app(eng, cfg), base_url="http://127.0.0.1")


def test_endpoint_returns_advice_validates_and_honours_the_token(tmp_path):
    c = _client(tmp_path)
    r = c.get("/v1/cache-advice?since=all&window=10m")
    assert r.status_code == 200 and r.json()["rows"][0]["repeats"] == 3 and r.json()["window_s"] == 600
    assert c.get("/v1/cache-advice?window=soon").status_code == 400 and c.get("/v1/cache-advice?bogus=1").status_code == 400
    c2 = _client(tmp_path, token="tok-123456")
    assert c2.get("/v1/cache-advice").status_code == 401 and c2.get("/v1/cache-advice", headers={"Authorization": "Bearer tok-123456"}).status_code == 200


def test_when_nothing_is_priced_the_table_says_no_dollar_figure_instead_of_zero():
    s = Store(":memory:")
    seed(s, 4, 60)
    rep = advise(s, cat(cached=None))
    t = to_table(rep)
    assert rep["totals"]["priced_rows"] == 0 and "No dollar figure" in t and "Saved so far" not in t
    assert "Saved so far" in to_table(advise(s))


# ---------------------------------------------------------------- a prefix that drifts in numbers or ids
def seed_drift(store, n, gap, *, hn="n" * 16, cached=0, level="system", tokens=2000, with_hn=True, start=None, tag="d", provider="openai"):
    t0 = start if start is not None else time.time() - n * gap - 10
    for i in range(n):
        rid = f"{tag}{i}"
        store.db.execute("insert into requests(id, ts, provider, model, status, cache_read_tokens) values (?,?,?,?,?,?)",
                         (rid, t0 + i * gap, provider, "gpt-x", "ok", cached if isinstance(cached, int) else cached[i]))
        store.db.execute("insert into prefix_stats(request_id, ts, provider, model, level, h, tokens, hn) values (?,?,?,?,?,?,?,?)",
                         (rid, t0 + i * gap, provider, "gpt-x", level, f"{i:016x}", tokens, hn if with_hn else None))
    store.db.commit()


def test_a_timestamp_near_the_top_changes_the_hash_but_not_the_shape():
    a = prefix_levels(msgs(system="Current time: 2026-10-21T10:01:00Z\n" + LONG), None, None, SALT)[0]
    b = prefix_levels(msgs(system="Current time: 2026-10-21T10:02:00Z\n" + LONG), None, None, SALT)[0]
    assert a["h"] != b["h"] and a["hn"] == b["hn"]
    for volatile in ("request 7f3a9c1e22b4", "id 123e4567-e89b-12d3-a456-426614174000", "Tuesday, 3 March", "counter=42"):
        x = prefix_levels(msgs(system=volatile + " " + LONG), None, None, SALT)[0]
        y = prefix_levels(msgs(system=volatile.replace("7f3a9c1e22b4", "0123456789ab").replace("42", "43").replace("Tuesday", "Friday").replace("3 March", "9 July")
                               .replace("123e4567", "223e4567") + " " + LONG), None, None, SALT)[0]
        assert x["h"] != y["h"] and x["hn"] == y["hn"], volatile


def test_a_change_in_the_words_changes_the_shape_too():
    a = prefix_levels(msgs(system="Be formal. " + LONG), None, None, SALT)[0]
    b = prefix_levels(msgs(system="Be casual. " + LONG), None, None, SALT)[0]
    assert a["hn"] != b["hn"]
    assert prefix_levels(msgs(), None, None, b"t" * 32)[0]["hn"] != prefix_levels(msgs(), None, None, SALT)[0]["hn"]  # keyed like the exact hash


def test_a_drifting_prefix_is_reported_and_priced():
    s = Store(":memory:")
    seed_drift(s, 5, 60)  # every call has a different exact prefix, all with one shape, a minute apart
    rep = advise(s)
    assert rep["rows"] and len(rep["rows"]) == 1
    r = rep["rows"][0]
    assert r["drifting"] and (r["requests"], r["repeats"], r["repeat_tokens"], r["cached_tokens"], r["missed_tokens"]) == (5, 4, 8000, 0, 8000)
    assert abs(r["potential_saving_usd"] - 8000 * (2.0 - 0.25) / 1e6) < 1e-12 and r["realized_saving_usd"] == 0
    assert "only in numbers, dates or ids" in r["advice"] and "after the fixed text" in r["advice"]
    assert "numbers, dates or ids" in to_table(rep)


def test_drift_is_not_reported_when_calls_are_further_apart_than_the_window():
    s = Store(":memory:")
    seed_drift(s, 4, 900)
    assert advise(s)["rows"] == []  # a cache would have expired between calls anyway
    assert advise(s, window_s=3600.0)["rows"][0]["drifting"]


def test_rows_recorded_before_the_shape_hash_existed_report_no_drift():
    s = Store(":memory:")
    seed_drift(s, 5, 60, with_hn=False)
    assert advise(s)["rows"] == []


def test_an_exact_repeat_is_not_also_called_drift():
    s = Store(":memory:")
    seed(s, 5, 60)
    s.db.execute("update prefix_stats set hn = 'z'")
    rows = advise(s)["rows"]
    assert len(rows) == 1 and not rows[0]["drifting"]


def test_drift_counts_the_cached_tokens_the_provider_did_report():
    s = Store(":memory:")
    seed_drift(s, 4, 60, cached=[0, 500, 500, 500])
    r = advise(s)["rows"][0]
    assert r["cached_tokens"] == 1500 and r["missed_tokens"] == 4500


def test_the_longest_drifting_level_gets_the_credit_not_both():
    s = Store(":memory:")
    seed_drift(s, 4, 60, level="system", tokens=1500, tag="a")
    for i in range(4):  # same four requests also carry a longer context prefix with its own shape
        s.db.execute("insert into prefix_stats(request_id, ts, provider, model, level, h, tokens, hn) select id, ts, provider, 'gpt-x', 'context', ?, 3000, ? from requests where id = ?",
                     (f"{i + 100:016x}", "c" * 16, f"a{i}"))
    s.db.commit()
    rows = advise(s)["rows"]
    assert [(r["level"], r["repeat_tokens"]) for r in rows if r["drifting"]] == [("context", 9000)]


def test_an_old_database_gets_the_shape_column(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("create table prefix_stats(request_id text, ts real, provider text, model text, level text, h text, tokens int)")
    db.execute("insert into prefix_stats values ('r', 1.0, 'openai', 'm', 'system', 'abc', 2000)")
    db.commit()
    db.close()
    st = Store(path)
    assert "hn" in {r[1] for r in st.db.execute("pragma table_info(prefix_stats)")}
    assert st.db.execute("select hn from prefix_stats").fetchone()[0] is None
    Store(path)  # opening again must not fail on the existing column


def test_a_timestamp_first_system_prompt_is_found_end_to_end(catalog, cfg, store):
    eng, _ = make_engine(catalog, cfg, {"openai": "ok"}, store=store)
    for i in range(5):
        asyncio.run(eng.run(RouteRequest(messages=[{"role": "system", "content": f"Current time: 2026-10-21T10:0{i}:00Z\n" + LONG}, {"role": "user", "content": "hi"}], providers=["openai"])))
    assert len({r[0] for r in store.db.execute("select h from prefix_stats")}) == 5 and len({r[0] for r in store.db.execute("select hn from prefix_stats")}) == 1
    rep = advise(store, catalog)
    assert [(r["drifting"], r["requests"]) for r in rep["rows"]] == [(True, 5)]
    assert "Current time" not in json.dumps([dict(r) for r in store.db.execute("select * from prefix_stats")])


# ---------------------------------------------------------------- the live-check script, against a fake provider that caches like OpenAI
def test_the_live_check_script_shows_working_then_drifting(monkeypatch, cfg):
    import importlib.util
    import pathlib
    from modelspend import ModelSpend
    from modelspend.providers.mock import ScriptedAdapter
    from modelspend.providers.base import ProviderResponse
    from modelspend.types import Usage

    spec = importlib.util.spec_from_file_location("caching_live_check", pathlib.Path(__file__).parent.parent / "examples" / "caching_live_check.py")
    ex = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ex)
    monkeypatch.setattr(ex.time, "sleep", lambda s: None)
    last: list[str] = []

    def script(req, n):  # like OpenAI: the system prompt is served from cache when this call starts with exactly what the previous call started with
        sysmsg = req.messages[0].content
        total = sum(len(m.content or "") for m in req.messages) // 4
        cached = len(sysmsg) // 4 if last and last[-1] == sysmsg and len(sysmsg) // 4 >= 1024 else 0
        last.append(sysmsg)
        return ProviderResponse(text="ok", usage=Usage(input_tokens=total - cached, cache_read_tokens=cached, output_tokens=1))

    catalog = Catalog.load(overrides={"add_models": [_m("openai", "gpt-4.1-nano", 0.10, 0.40, cached=0.025)]})
    cfg.track_prefixes = True
    ms = ModelSpend(providers=["openai"], config=cfg, catalog=catalog, adapters={"openai": ScriptedAdapter("openai", script)}, store=Store(":memory:"))
    out = ex.run(ms, pause_s=0)
    assert "caching is working" in out and "only in numbers, dates or ids" in out
    rows = build_cache_advice(ms.store, catalog, cfg, project=ex.PROJECT)["rows"]
    assert sorted((r["drifting"], r["requests"], r["cached_tokens"] > 0) for r in rows) == [(False, 12, True), (True, 8, False)]


def test_a_token_estimate_above_what_the_provider_counted_is_capped(catalog=None):
    s = Store(":memory:")
    seed(s, 12, 2, system=("S", 6200), cached=[0] + [4992] * 11)  # estimate 6,200; the provider counted 5,115 input tokens in all
    s.db.execute("update requests set input_tokens = 5115")  # requests.input_tokens is the whole input, cached part included
    r = advise(s)["rows"][0]
    assert r["prefix_tokens"] == 5115 and r["repeat_tokens"] == 11 * 5115 and r["missed_tokens"] == 11 * 5115 - 11 * 4992
    assert r["advice"].startswith("caching is working")  # 2% missed, not the 24% the estimate alone would show


def test_the_live_check_script_explains_a_retired_model_instead_of_a_traceback(monkeypatch, cfg, capsys):
    import importlib.util
    import pathlib
    from modelspend import ModelSpend
    from modelspend.errors import ProviderError
    from modelspend.providers.mock import ScriptedAdapter

    spec = importlib.util.spec_from_file_location("caching_live_check", pathlib.Path(__file__).parent.parent / "examples" / "caching_live_check.py")
    ex = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ex)
    cfg.track_prefixes = True
    gone = ScriptedAdapter("openai", lambda r, n: ProviderError("openai", "bad_request", "The model `gpt-4.1-nano` has been deprecated"))
    ms = ModelSpend(providers=["openai"], config=cfg, catalog=Catalog.load(overrides={"add_models": [_m("openai", "gpt-4.1-nano", 0.1, 0.4, cached=0.025)]}),
                    adapters={"openai": gone}, store=Store(":memory:"))
    assert ex.main([], ms) == 1
    out = capsys.readouterr().out
    assert "openai/gpt-4.1-nano did not answer" in out and "deprecated" in out and "modelspend verify openai --yes" in out and "modelspend models disable openai/gpt-4.1-nano" in out
    assert "Traceback" not in out
