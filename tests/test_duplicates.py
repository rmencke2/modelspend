"""Duplicate-request finder: it must never keep prompt text, never affect a request, and its arithmetic about repeats and their cost must be right."""
from __future__ import annotations

import asyncio
import json
import time

import pytest
from typer.testing import CliRunner

from conftest import make_engine
from modelspend import RouteRequest
from modelspend.cli import app
from modelspend.config import Config
from modelspend.duplicates import BURST_S, MIN_CHARS, build_duplicates, request_hash, to_table
from modelspend.shadow import SHADOW_PROJECT
from modelspend.store import Store
from modelspend.types import Message, ToolSpec

runner = CliRunner()
SALT = b"s" * 32
SECRET = "SECRET-CLIENT-RECORD-7741"
BODY = f"{SECRET} Please summarise this support conversation and list the follow-up actions for the account owner. " * 3  # > 200 characters
_n = [0]


def msgs(text=BODY):
    return [Message(role="user", content=text)]


def req(text=BODY, **kw):
    return RouteRequest(messages=[{"role": "user", "content": text}], providers=["openai"], **kw)


# ---------------------------------------------------------------- the hash
def test_identical_requests_hash_alike_and_any_difference_changes_it():
    base = request_hash(req(), SALT)
    assert base and base == request_hash(req(), SALT) and len(base) == 16
    assert request_hash(req(BODY + "!"), SALT) != base
    assert request_hash(req(temperature=0.7), SALT) != base and request_hash(req(temperature=0.7), SALT) == request_hash(req(temperature=0.7), SALT)
    assert request_hash(req(tools=[ToolSpec(name="f")]), SALT) != base
    assert request_hash(req(response_schema={"type": "object"}), SALT) != base
    assert request_hash(req(), b"t" * 32) != base  # a hash from another database matches nothing here


def test_short_requests_are_not_tracked_at_all():
    assert request_hash(req("Classify: refund request"), SALT) is None
    assert request_hash(req("x" * (MIN_CHARS - 30)), SALT) is None and request_hash(req("x" * (MIN_CHARS + 30)), SALT)


# ---------------------------------------------------------------- through the engine
def run_n(catalog, cfg, store, texts):
    eng, _ = make_engine(catalog, cfg, {"openai": "ok"}, store=store)
    for t in texts:
        asyncio.run(eng.run(req(t)))


def test_the_engine_keeps_hashes_not_text_and_a_repeat_shares_its_hash(catalog, cfg, store):
    run_n(catalog, cfg, store, [BODY, BODY, BODY + " different", "short"])
    rows = [dict(r) for r in store.db.execute("select * from dup_stats")]
    assert len(rows) == 3 and len({r["h"] for r in rows}) == 2  # two identical, one different, the short one untracked
    dump = json.dumps([dict(r) for t in ("dup_stats", "requests", "attempts") for r in store.db.execute(f"select * from {t}")], default=str)
    assert SECRET not in dump


def test_streaming_requests_are_tracked_too(catalog, cfg, store):
    eng, _ = make_engine(catalog, cfg, {"openai": "streamed answer"}, store=store)

    async def go():
        for _ in range(2):
            async for _ev in eng.stream(req()):
                pass
    asyncio.run(go())
    assert len({r[0] for r in store.db.execute("select h from dup_stats")}) == 1 and store.db.execute("select count(*) from dup_stats").fetchone()[0] == 2


def test_tracking_can_be_turned_off(catalog, cfg, store):
    cfg.track_duplicates = False
    run_n(catalog, cfg, store, [BODY, BODY])
    assert store.db.execute("select count(*) from dup_stats").fetchone()[0] == 0


def test_a_failure_in_tracking_never_reaches_the_request(catalog, cfg, store, monkeypatch):
    monkeypatch.setattr(store, "record_duplicate", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full")))
    eng, _ = make_engine(catalog, cfg, {"openai": "ok"}, store=store)
    assert asyncio.run(eng.run(req())).text


def test_the_setting_defaults_on_and_follows_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("MODELSPEND_HOME", str(tmp_path))
    monkeypatch.delenv("MODELSPEND_TRACK_DUPLICATES", raising=False)
    assert Config.from_env().track_duplicates is True
    (tmp_path / "settings.json").write_text('{"track_duplicates": "no"}')
    with pytest.raises(ValueError, match="track_duplicates"):
        Config.from_env()
    (tmp_path / "settings.json").write_text('{"track_duplicates": false}')
    assert Config.from_env().track_duplicates is False
    monkeypatch.setenv("MODELSPEND_TRACK_DUPLICATES", "1")
    assert Config.from_env().track_duplicates is True


def test_prune_and_forget_remove_the_hashes(catalog, cfg, store):
    run_n(catalog, cfg, store, [BODY, BODY])
    assert store.prune(time.time() + 10)["requests"] == 2 and store.db.execute("select count(*) from dup_stats").fetchone()[0] == 0
    run_n(catalog, cfg, store, [BODY, BODY])
    assert store.forget_duplicates() == 2 and store.db.execute("select count(*) from dup_stats").fetchone()[0] == 0


# ---------------------------------------------------------------- the arithmetic
def seed(store, h, gaps, cost=0.10, *, project="web", agent="bot", status="ok", model="gpt-x", start=None, costs=None, tokens=500):
    """One group: the first request at `start`, then each next one `gap` seconds after the previous."""
    t = start if start is not None else time.time() - 3600 - sum(gaps)
    times = [t]
    for g in gaps:
        times.append(times[-1] + g)
    for i, ts in enumerate(times):
        _n[0] += 1
        rid = f"d{_n[0]}"
        c = costs[i] if costs else cost
        store.db.execute("insert into requests(id, ts, provider, model, status, project, agent, actual_cost, input_tokens, output_tokens) values (?,?,?,?,?,?,?,?,?,?)",
                         (rid, ts, "openai", model, status, project, agent, c, tokens, 0))
        store.db.execute("insert into dup_stats(request_id, ts, h) values (?,?,?)", (rid, ts, h))
    store.db.commit()


def test_repeats_exclude_the_first_send_and_cost_what_the_repeats_cost():
    s = Store(":memory:")
    seed(s, "a" * 16, [10, 10, 10], costs=[0.50, 0.20, 0.30, 0.40])  # four sends, three repeats
    r = build_duplicates(s)["rows"][0]
    assert (r["requests"], r["repeats"]) == (4, 3) and abs(r["repeat_cost_usd"] - 0.90) < 1e-12 and r["repeat_tokens"] == 1500 and r["cost_complete"]
    t = build_duplicates(s)["totals"]
    assert t["repeat_requests"] == 3 and abs(t["share_of_cost"] - 0.90 / 1.40) < 1e-12 and abs(t["cost_considered_usd"] - 1.40) < 1e-12


def test_a_request_sent_once_is_not_a_duplicate_and_groups_are_ranked_by_repeat_cost():
    s = Store(":memory:")
    seed(s, "1" * 16, [])
    seed(s, "2" * 16, [30], cost=0.05)
    seed(s, "3" * 16, [30, 30], cost=0.20)
    rows = build_duplicates(s)["rows"]
    assert [r["request"] for r in rows] == ["33333333", "22222222"]
    assert [r["request"] for r in build_duplicates(s, min_requests=3)["rows"]] == ["33333333"]


def test_patterns_are_burst_later_or_mixed_from_the_gaps_between_sends():
    s = Store(":memory:")
    seed(s, "b" * 16, [5, 20, BURST_S])
    seed(s, "c" * 16, [3600, 7200])
    seed(s, "d" * 16, [5, 3600])
    p = {r["request"]: r for r in build_duplicates(s)["rows"]}
    assert (p["bbbbbbbb"]["pattern"], p["cccccccc"]["pattern"], p["dddddddd"]["pattern"]) == ("burst", "later", "mixed")
    assert "retry loop" in p["bbbbbbbb"]["advice"] and "keep the first answer" in p["cccccccc"]["advice"] and "quick succession" in p["dddddddd"]["advice"]
    assert p["cccccccc"]["median_gap_s"] == 5400 and "1.5 h later" in p["cccccccc"]["advice"]


def test_unknown_costs_are_flagged_as_partial_not_counted_as_zero():
    s = Store(":memory:")
    seed(s, "e" * 16, [10, 10], costs=[0.10, None, 0.30])
    r = build_duplicates(s)["rows"][0]
    assert abs(r["repeat_cost_usd"] - 0.30) < 1e-12 and not r["cost_complete"] and not build_duplicates(s)["totals"]["complete"]
    assert "+" in to_table(build_duplicates(s)) and "partial" in to_table(build_duplicates(s))
    s2 = Store(":memory:")
    seed(s2, "f" * 16, [10], costs=[None, None])
    r2 = build_duplicates(s2)["rows"][0]
    assert r2["repeat_cost_usd"] is None and "unknown" in to_table(build_duplicates(s2))


def test_failed_requests_and_the_tools_own_runs_do_not_count():
    s = Store(":memory:")
    seed(s, "a" * 16, [10, 10], status="failed")
    seed(s, "b" * 16, [10, 10], project=SHADOW_PROJECT)
    assert build_duplicates(s)["rows"] == []
    seed(s, "c" * 16, [10, 10])
    assert [r["request"] for r in build_duplicates(s)["rows"]] == ["cccccccc"]


def test_the_window_and_the_project_filter_apply():
    s = Store(":memory:")
    now = time.time()
    seed(s, "a" * 16, [10], project="web", start=now - 40 * 86400)
    seed(s, "b" * 16, [10], project="batch", start=now - 3600)
    assert [r["request"] for r in build_duplicates(s, since=now - 30 * 86400)["rows"]] == ["bbbbbbbb"]
    assert len(build_duplicates(s)["rows"]) == 2
    assert [r["request"] for r in build_duplicates(s, project="web")["rows"]] == ["aaaaaaaa"]


def test_repeats_split_by_a_request_between_them_still_belong_to_one_group_in_time_order():
    s = Store(":memory:")
    t = time.time() - 1000
    seed(s, "a" * 16, [100], start=t)
    seed(s, "a" * 16, [100], start=t + 50)  # interleaved in time with the first pair
    r = build_duplicates(s)["rows"][0]
    assert r["requests"] == 4 and r["repeats"] == 3


# ---------------------------------------------------------------- the text and the command
def test_the_table_names_the_project_the_model_and_what_to_do():
    s = Store(":memory:")
    seed(s, "a" * 16, [5, 5], cost=0.30, project="web", agent="support-bot")
    t = to_table(build_duplicates(s))
    assert "web / support-bot" in t and "openai/gpt-x" in t and "retry loop" in t and "burst" in t
    assert "2 repeated request(s) across 1 distinct request(s) cost $0.60" in t and "on purpose" in t


def test_empty_and_off_have_their_own_messages():
    s = Store(":memory:")
    assert "No request was sent more than once" in to_table(build_duplicates(s))
    assert "tracking is off" in to_table(build_duplicates(s), tracking=False)


def test_the_command_prints_json_forgets_and_rejects_bad_input(tmp_path, monkeypatch):
    monkeypatch.setenv("MODELSPEND_HOME", str(tmp_path))
    st = Store(tmp_path / "modelspend.db")
    seed(st, "a" * 16, [5, 5])
    st.db.close()
    j = runner.invoke(app, ["duplicates", "--format", "json"])
    assert json.loads(j.output)["rows"][0]["repeats"] == 2
    assert "request" in runner.invoke(app, ["duplicates"]).output
    assert runner.invoke(app, ["duplicates", "--format", "xml"]).exit_code != 0
    assert runner.invoke(app, ["duplicates", "--since", "nonsense"]).exit_code == 2
    assert "erased 3 stored request hash(es)" in runner.invoke(app, ["duplicates", "--forget"]).output
    assert "No request was sent more than once" in runner.invoke(app, ["duplicates"]).output
