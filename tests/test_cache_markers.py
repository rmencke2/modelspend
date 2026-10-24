"""Anthropic cache markers: off by default and then byte-for-byte what was sent before; when on, only a prefix that qualifies is marked, and the marker
sits where the cached prefix ends. The write premium a marker causes must show up in what the advisor says caching saved."""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from conftest import make_engine
from modelspend import RouteRequest
from modelspend.cacheadvice import build_cache_advice
from modelspend.cachemarkers import CACHE_TTL_S, choose
from modelspend.catalog import Catalog
from modelspend.config import Config
from modelspend.prefixes import prefix_levels
from modelspend.providers.anthropic_ import AnthropicAdapter
from modelspend.providers.base import ProviderRequest, ProviderResponse
from modelspend.store import Store
from modelspend.types import Message, ToolCall, ToolSpec, Usage

from test_cache_advice import LONG, SALT, advise, cat, seed
from test_whatif import _m

MARK = {"type": "ephemeral"}
A = AnthropicAdapter("k", client=object())


def preq(msgs, **kw):
    return ProviderRequest(model="m", messages=msgs, max_output_tokens=50, **kw)


def sysuser(last="hi", system="Be brief."):
    return [Message(role="system", content=system), Message(role="user", content=last)]


def marks(p):
    """Every cache_control in the request body, as a path: where the cache would end."""
    out = []
    if isinstance(p.get("system"), list):
        out += [("system", i) for i, b in enumerate(p["system"]) if "cache_control" in b]
    for mi, m in enumerate(p["messages"]):
        if isinstance(m["content"], list):
            out += [("messages", mi, bi) for bi, b in enumerate(m["content"]) if "cache_control" in b]
    out += [("tools", i) for i, t in enumerate(p.get("tools", [])) if "cache_control" in t]
    return out


# ---------------------------------------------------------------- the adapter
def test_without_a_cache_prefix_the_request_is_exactly_what_it_was_before():
    p = A.build_params(preq(sysuser()))
    assert p["system"] == "Be brief." and p["messages"] == [{"role": "user", "content": "hi"}] and marks(p) == []
    assert "cache_control" not in json.dumps(p)


def test_a_system_prefix_is_marked_on_the_system_block():
    p = A.build_params(preq(sysuser(), cache_prefix="system"))
    assert p["system"] == [{"type": "text", "text": "Be brief.", "cache_control": MARK}]
    assert p["messages"] == [{"role": "user", "content": "hi"}] and marks(p) == [("system", 0)]


def test_a_context_prefix_is_marked_on_the_last_block_before_the_final_message():
    msgs = [Message(role="system", content="S"), Message(role="user", content="example question"), Message(role="assistant", content="example answer"),
            Message(role="user", content="the real question")]
    p = A.build_params(preq(msgs, cache_prefix="context"))
    assert p["system"] == "S"  # a context marker does not also mark the system prompt
    assert p["messages"][1] == {"role": "assistant", "content": [{"type": "text", "text": "example answer", "cache_control": MARK}]}
    assert p["messages"][2] == {"role": "user", "content": "the real question"} and marks(p) == [("messages", 1, 0)]


def test_a_context_marker_works_without_a_system_prompt_and_with_tool_turns():
    msgs = [Message(role="user", content="q"), Message(role="assistant", content="", tool_calls=[ToolCall(id="t1", name="f", arguments="{}")]),
            Message(role="tool", content="result", tool_call_id="t1"), Message(role="user", content="next")]
    p = A.build_params(preq(msgs, cache_prefix="context"))
    assert marks(p) == [("messages", 2, 0)] and p["messages"][1]["content"][0]["type"] == "tool_use" and p["messages"][2]["content"][0]["type"] == "tool_result"
    assert p["messages"][2]["content"][0]["cache_control"] == MARK and p["messages"][3] == {"role": "user", "content": "next"}


def test_a_tool_result_merged_into_the_previous_turn_keeps_the_marker_on_the_prefix_end():
    msgs = [Message(role="user", content="q"), Message(role="assistant", content="", tool_calls=[ToolCall(id="a", name="f", arguments="{}"), ToolCall(id="b", name="f", arguments="{}")]),
            Message(role="tool", content="r1", tool_call_id="a"), Message(role="tool", content="r2", tool_call_id="b")]
    p = A.build_params(preq(msgs, cache_prefix="context"))
    results = p["messages"][2]["content"]
    assert [b["tool_use_id"] for b in results] == ["a", "b"] and "cache_control" in results[0] and "cache_control" not in results[1]


def test_empty_text_is_never_marked_and_tools_and_schema_are_untouched():
    p = A.build_params(preq([Message(role="user", content=""), Message(role="user", content="x")], cache_prefix="context", tools=[ToolSpec(name="f")]))
    assert marks(p) == [] and p["messages"][0] == {"role": "user", "content": ""} and "cache_control" not in json.dumps(p["tools"])
    q = A.build_params(preq(sysuser(), cache_prefix="system", response_schema={"type": "object"}))
    assert marks(q) == [("system", 0)] and "cache_control" not in json.dumps(q["tools"])


# ---------------------------------------------------------------- the decision
def req(system=LONG, last="hi", **kw):
    return RouteRequest(messages=[{"role": "system", "content": system}, {"role": "user", "content": last}], providers=["anthropic"], **kw)


def remember(store, r, provider="anthropic", model="m", ts=None):
    store.record_prefixes("rid", provider, model, prefix_levels(r.messages, r.response_schema, r.tools, store.salt()))
    if ts is not None:
        store.db.execute("update prefix_stats set ts = ?", (ts,))


def test_off_and_other_providers_never_mark():
    s = Store(":memory:")
    remember(s, req())
    assert choose(s, "off", req(), "anthropic", "m") is None
    assert choose(s, "always", req(), "openai", "m") is None and choose(s, "always", req(), "google", "m") is None
    assert choose(None, "always", req(), "anthropic", "m") is None


def test_repeats_marks_only_a_prefix_that_was_sent_before_within_the_cache_lifetime():
    s = Store(":memory:")
    assert choose(s, "repeats", req(), "anthropic", "m") is None  # the first request is never marked: no write premium for a prompt that may never repeat
    remember(s, req())
    assert choose(s, "repeats", req(last="a different question"), "anthropic", "m") == "system"
    assert choose(s, "repeats", req(system=LONG + " changed"), "anthropic", "m") is None  # a different prefix is a different cache
    assert choose(s, "repeats", req(), "anthropic", "other-model") is None  # caches are per model
    s.db.execute("update prefix_stats set ts = ?", (time.time() - CACHE_TTL_S - 5,))
    assert choose(s, "repeats", req(), "anthropic", "m") is None  # the cache would have expired


def test_always_marks_the_first_request_too_but_never_a_short_prompt():
    s = Store(":memory:")
    assert choose(s, "always", req(), "anthropic", "m") == "system"
    assert choose(s, "always", req(system="Be brief."), "anthropic", "m") is None


def test_the_longest_qualifying_prefix_is_the_one_marked():
    s = Store(":memory:")
    long_ctx = RouteRequest(messages=[{"role": "system", "content": LONG}, {"role": "user", "content": "Example. " * 600}, {"role": "user", "content": "q1"}], providers=["anthropic"])
    again = RouteRequest(messages=[{"role": "system", "content": LONG}, {"role": "user", "content": "Example. " * 600}, {"role": "user", "content": "q2"}], providers=["anthropic"])
    remember(s, long_ctx)
    assert choose(s, "repeats", again, "anthropic", "m") == "context"
    only_system = RouteRequest(messages=[{"role": "system", "content": LONG}, {"role": "user", "content": "other example " * 600}, {"role": "user", "content": "q"}], providers=["anthropic"])
    assert choose(s, "repeats", only_system, "anthropic", "m") == "system"  # the system prompt repeats, the context does not


def test_a_failure_choosing_a_marker_never_reaches_the_request(monkeypatch):
    s = Store(":memory:")
    monkeypatch.setattr(s, "salt", lambda: (_ for _ in ()).throw(RuntimeError("disk full")))
    assert choose(s, "always", req(), "anthropic", "m") is None


# ---------------------------------------------------------------- configuration
def test_the_setting_is_off_by_default_validated_and_overridable_by_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("MODELSPEND_HOME", str(tmp_path))
    monkeypatch.delenv("MODELSPEND_ANTHROPIC_CACHE_MARKERS", raising=False)
    monkeypatch.delenv("MODELSPEND_TRACK_PREFIXES", raising=False)
    assert Config.from_env().anthropic_cache_markers == "off"
    (tmp_path / "settings.json").write_text('{"anthropic_cache_markers": "repeats"}')
    assert Config.from_env().anthropic_cache_markers == "repeats"
    monkeypatch.setenv("MODELSPEND_ANTHROPIC_CACHE_MARKERS", "always")
    assert Config.from_env().anthropic_cache_markers == "always"
    monkeypatch.setenv("MODELSPEND_ANTHROPIC_CACHE_MARKERS", "sometimes")
    with pytest.raises(ValueError, match="off, repeats or always"):
        Config.from_env()


def test_repeats_without_prefix_tracking_is_refused_because_it_could_never_know_what_repeats(tmp_path, monkeypatch):
    monkeypatch.setenv("MODELSPEND_HOME", str(tmp_path))
    monkeypatch.delenv("MODELSPEND_ANTHROPIC_CACHE_MARKERS", raising=False)
    (tmp_path / "settings.json").write_text('{"anthropic_cache_markers": "repeats", "track_prefixes": false}')
    with pytest.raises(ValueError, match="needs prefix tracking"):
        Config.from_env()
    (tmp_path / "settings.json").write_text('{"anthropic_cache_markers": "always", "track_prefixes": false}')
    assert Config.from_env().anthropic_cache_markers == "always"


# ---------------------------------------------------------------- through the engine
def run3(catalog, cfg, store, mode, provider="anthropic", n=3, system=LONG):
    cfg.anthropic_cache_markers = mode
    eng, ads = make_engine(catalog, cfg, {provider: "ok"}, store=store)
    for i in range(n):
        asyncio.run(eng.run(RouteRequest(messages=[{"role": "system", "content": system}, {"role": "user", "content": f"q{i}"}], providers=[provider])))
    return [c.cache_prefix for c in ads[provider].calls]


def test_the_engine_marks_from_the_second_request_in_repeats_mode(catalog, cfg, store):
    assert run3(catalog, cfg, store, "repeats") == [None, "system", "system"]


def test_the_engine_marks_every_request_in_always_mode_and_none_when_off_or_for_other_providers(catalog, cfg):
    assert run3(catalog, cfg, Store(":memory:"), "always") == ["system"] * 3
    assert run3(catalog, cfg, Store(":memory:"), "off") == [None] * 3
    assert run3(catalog, cfg, Store(":memory:"), "always", provider="openai") == [None] * 3


def test_a_prefix_that_changes_every_call_is_never_marked_in_repeats_mode(catalog, cfg, store):
    cfg.anthropic_cache_markers = "repeats"
    eng, ads = make_engine(catalog, cfg, {"anthropic": "ok"}, store=store)
    for i in range(4):
        asyncio.run(eng.run(RouteRequest(messages=[{"role": "system", "content": f"Current time: 10:0{i}\n" + LONG}, {"role": "user", "content": "hi"}], providers=["anthropic"])))
    assert [c.cache_prefix for c in ads["anthropic"].calls] == [None] * 4  # no write premium is paid for a prompt that cannot be cached


# ---------------------------------------------------------------- what the advisor says
def with_markers(mode):
    return Config(home="/tmp/x", anthropic_cache_markers=mode)


def seed_anthropic(store, writes, reads, n=5, tokens=2000):
    seed(store, n, 60, provider="anthropic", model="gpt-x", cached=reads)
    for i, w in enumerate(writes):
        store.db.execute("insert into attempts(request_id, idx, kind, provider, model, outcome, cache_write) values (?,?,?,?,?,?,?)", (f"r{i}-anthropic", 0, "generation", "anthropic", "gpt-x", "ok", w))
    store.db.commit()


def adv(store, mode, **kw):
    return build_cache_advice(store, cat("anthropic", cached=0.3, inp=3.0, write=3.75), with_markers(mode), **kw)["rows"][0]


def test_with_markers_off_the_advice_says_how_to_turn_them_on():
    s = Store(":memory:")
    seed_anthropic(s, [0] * 5, 0)
    a = adv(s, "off")["advice"]
    assert 'anthropic_cache_markers' in a and "repeats" in a


def test_a_cache_write_is_not_a_miss_and_its_premium_comes_off_the_realized_saving():
    s = Store(":memory:")
    seed_anthropic(s, [0, 2000, 0, 0, 0], [0, 0, 2000, 2000, 2000])  # request 2 wrote the cache, 3 to 5 read it
    r = adv(s, "repeats")
    assert (r["repeats"], r["cached_tokens"], r["missed_tokens"]) == (4, 6000, 0) and r["advice"] == "caching is working"
    assert abs(r["realized_saving_usd"] - (6000 * (3.0 - 0.3) - 2000 * (3.75 - 3.0)) / 1e6) < 1e-12


def test_markers_on_but_nothing_ever_read_says_the_prefix_may_be_too_short():
    s = Store(":memory:")
    seed_anthropic(s, [0] * 5, 0)
    a = adv(s, "repeats")["advice"]
    assert "markers are on but no cached tokens were reported" in a and "modelspend verify anthropic --cache" in a


def test_a_write_in_a_request_that_was_not_a_repeat_does_not_hide_a_real_miss():
    s = Store(":memory:")
    seed_anthropic(s, [2000, 0, 0, 0, 0], [0, 0, 0, 0, 0])  # always-mode: the first request wrote, nobody read
    r = adv(s, "always")
    assert r["repeat_tokens"] == 8000 and r["missed_tokens"] == 8000 and r["cached_tokens"] == 0


# ---------------------------------------------------------------- the live-check script on Claude, against a fake that caches only what is marked
def test_the_live_check_script_on_claude_writes_once_reads_after_and_never_pays_for_the_drifting_prompt(monkeypatch, cfg, capsys):
    import importlib.util
    import pathlib
    from modelspend import ModelSpend
    from modelspend.providers.mock import ScriptedAdapter

    spec = importlib.util.spec_from_file_location("caching_live_check", pathlib.Path(__file__).parent.parent / "examples" / "caching_live_check.py")
    ex = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ex)
    monkeypatch.setattr(ex.time, "sleep", lambda s: None)
    warm: set[str] = set()

    def script(r, n):  # Anthropic: only a marked prefix is written (billed as a write the first time) and read (every time after)
        sysmsg = r.messages[0].content
        total = sum(len(m.content or "") for m in r.messages) // 4
        if r.cache_prefix != "system":
            return ProviderResponse(text="ok", usage=Usage(input_tokens=total, output_tokens=1))
        pre = len(sysmsg) // 4
        if sysmsg in warm:
            return ProviderResponse(text="ok", usage=Usage(input_tokens=total - pre, cache_read_tokens=pre, output_tokens=1))
        warm.add(sysmsg)
        return ProviderResponse(text="ok", usage=Usage(input_tokens=total - pre, cache_write_tokens=pre, output_tokens=1))

    m = _m("anthropic", "claude-test", 3.0, 15.0, cached=0.3)
    m["pricing"]["cache_write_per_mtok"] = 3.75
    catalog = Catalog.load(overrides={"add_models": [m]})
    cfg.anthropic_cache_markers = "repeats"
    cfg.track_prefixes = True
    ad = ScriptedAdapter("anthropic", script)
    ms = ModelSpend(providers=["anthropic"], config=cfg, catalog=catalog, adapters={"anthropic": ad}, store=Store(":memory:"))
    out = ex.run(ms, pin="anthropic/claude-test", pause_s=0)
    assert [c.cache_prefix for c in ad.calls] == [None] + ["system"] * 11 + [None] * 8  # drifting prompts are never marked
    lines = capsys.readouterr().out.splitlines()
    assert [ln[:3] for ln in lines if "wrote" in ln] == ["A 2"]  # exactly one cache write in the whole run
    assert "caching is working" in out and "only in numbers, dates or ids" in out
    rows = {r["drifting"]: r for r in build_cache_advice(ms.store, catalog, cfg, project=ex.PROJECT)["rows"]}
    ok = rows[False]
    reads = ok["cached_tokens"]
    pre = reads // 10
    assert reads == 10 * pre and ok["missed_tokens"] <= 0.1 * ok["repeat_tokens"]  # request 1 unmarked, 2 wrote, 3 to 12 read
    assert abs(ok["realized_saving_usd"] - (reads * 2.7 - pre * 0.75) / 1e6) < 1e-12  # the one write's premium comes off the saving


def test_a_provider_that_counts_more_cached_tokens_than_our_estimate_is_believed_up_to_the_request_total():
    s = Store(":memory:")
    seed_anthropic(s, [0, 7489, 0, 0, 0], [0, 0, 7489, 7489, 7489], tokens=6193)  # estimate 6,193; Claude's own count was 7,489
    s.db.execute("update prefix_stats set tokens = 6193")
    s.db.execute("update requests set input_tokens = 7503")
    r = adv(s, "repeats")
    assert r["cached_tokens"] == 3 * 7489 and r["missed_tokens"] == 0 and r["prefix_tokens"] == 7489 and r["advice"] == "caching is working"
    assert abs(r["realized_saving_usd"] - (3 * 7489 * 2.7 - 7489 * 0.75) / 1e6) < 1e-12
    s.db.execute("update requests set input_tokens = 7000")  # but never more than the request itself held
    assert adv(s, "repeats")["cached_tokens"] == 3 * 7000
