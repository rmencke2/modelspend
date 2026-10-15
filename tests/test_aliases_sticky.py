"""0.4: model aliases (choose the model by name) and conversation stickiness (keep the model chosen for earlier turns)."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from conftest import make_engine
from modelspend import ModelSpend, RouteRequest
from modelspend.aliases import clean_name, clean_ref, describe, remove_alias, resolve, set_alias
from modelspend.config import Config, load_settings
from modelspend.providers.base import ProviderResponse
from modelspend.server.app import create_app
from modelspend.tasks import conversation_key, is_continuation
from modelspend.types import Message, Usage

ALL = ["openai", "anthropic", "google", "xai"]


def resp(text="positive", i=100, o=10):
    return ProviderResponse(text=text, usage=Usage(input_tokens=i, output_tokens=o))


def convo(*turns, **kw):
    """turns alternate user, assistant, user, ... ; returns a RouteRequest carrying the history."""
    roles = ["user", "assistant"]
    msgs = [{"role": roles[i % 2], "content": t} for i, t in enumerate(turns)]
    kw.setdefault("providers", ALL)
    return RouteRequest(messages=msgs, **kw)


FIRST = "Classify this support ticket: I was charged twice."


# =============================================================================================== aliases
def test_alias_names_and_refs_are_validated():
    assert clean_name(" Coding ") == "coding" and clean_name("fast_1-x") == "fast_1-x"
    for bad in ("auto", "AUTO", "modelspend", "", "has space", "a/b", "x" * 33, "-lead", "emoji😀"):
        with pytest.raises(ValueError):
            clean_name(bad)
    assert clean_ref("Claude/some-model") == "anthropic/some-model" and clean_ref("gemini/x") == "google/x"
    for bad in ("no-slash", "anthropic/", "nobody/x"):
        with pytest.raises(ValueError):
            clean_ref(bad)


def test_resolve_leaves_non_aliases_alone():
    al = {"coding": "anthropic/claude-sonnet-5-5"}
    assert resolve("coding", al) == "anthropic/claude-sonnet-5-5" and resolve("CODING", al) == "anthropic/claude-sonnet-5-5"
    assert resolve("openai/gpt-4.1", al) == "openai/gpt-4.1" and resolve("unknown", al) == "unknown" and resolve(None, al) is None


def test_set_alias_persists_validates_against_the_catalog_and_reloads(tmp_path, catalog):
    cfg = Config(home=tmp_path, api_keys={"openai": "k"})
    assert set_alias(cfg, "Classify", "openai/synthetic-openai-small", catalog) == ("classify", "openai/synthetic-openai-small")
    assert load_settings(tmp_path)["aliases"] == {"classify": "openai/synthetic-openai-small"} and cfg.aliases["classify"]
    with pytest.raises(ValueError) as ei:
        set_alias(cfg, "coding", "openai/not-in-catalog", catalog)
    assert "catalog sync" in str(ei.value) and "coding" not in load_settings(tmp_path)["aliases"]
    set_alias(cfg, "coding", "openai/not-in-catalog", None)  # --force path: no catalog check
    from modelspend.config import Config as C
    import os
    os.environ["MODELSPEND_HOME"] = str(tmp_path)
    try:
        assert C.from_env().aliases == {"classify": "openai/synthetic-openai-small", "coding": "openai/not-in-catalog"}
    finally:
        del os.environ["MODELSPEND_HOME"]
    assert remove_alias(cfg, "coding") and not remove_alias(cfg, "coding") and "coding" not in load_settings(tmp_path)["aliases"]


def test_describe_flags_problems(tmp_path, catalog):
    catalog.get("openai/synthetic-openai-mid").pricing = None
    cfg = Config(home=tmp_path, api_keys={"openai": "k"}, aliases={"a": "openai/synthetic-openai-small", "b": "openai/synthetic-openai-mid",
                                                                    "c": "openai/gone", "d": "anthropic/synthetic-anthropic-small"})
    d = {x["alias"]: x for x in describe(cfg, catalog)}
    assert d["a"]["in_catalog"] and d["a"]["priced"] and d["a"]["has_key"]
    assert not d["b"]["priced"] and not d["c"]["in_catalog"] and not d["d"]["has_key"]


async def test_sdk_pin_accepts_an_alias(catalog, cfg, store):
    cfg.aliases = {"coding": "anthropic/synthetic-anthropic-large"}
    eng, ad = make_engine(catalog, cfg, {"anthropic": "hello"}, store=store)
    ms = ModelSpend(providers=["anthropic"], config=cfg, catalog=catalog, adapters=eng.adapters, store=store)
    res = await ms.aroute("write a function", pin="coding", agent="dev")
    assert res.model == "synthetic-anthropic-large" and res.pinned and [c.model for c in ad["anthropic"].calls] == ["synthetic-anthropic-large"]
    ms2 = ModelSpend(config=cfg, catalog=catalog, adapters=eng.adapters, store=store, pin="coding")  # as a default for every call
    assert (await ms2.aroute("again")).model == "synthetic-anthropic-large"


def _client(catalog, cfg, store, aliases=None, scripts=None, token=None):
    cfg.api_token = token
    cfg.default_providers = ["openai", "anthropic"]
    cfg.aliases = dict(aliases or {})
    eng, ad = make_engine(catalog, cfg, scripts or {"openai": "positive", "anthropic": "hello"}, store=store)
    return TestClient(create_app(eng, cfg), base_url="http://127.0.0.1:8787"), ad


MSG = [{"role": "user", "content": "Classify: great"}]


def test_server_model_can_be_an_alias(catalog, cfg, store):
    c, ad = _client(catalog, cfg, store, {"coding": "anthropic/synthetic-anthropic-large", "cheap": "openai/synthetic-openai-small"})
    r = c.post("/v1/chat/completions", json={"model": "coding", "messages": MSG}, headers={"X-ModelSpend-Agent": "dev"})
    j = r.json()
    assert r.status_code == 200 and j["model"] == "synthetic-anthropic-large" and j["modelspend"]["pinned"] and j["modelspend"]["agent"] == "dev"
    assert c.post("/v1/chat/completions", json={"model": "Cheap", "messages": MSG}).json()["model"] == "synthetic-openai-small"  # case-insensitive
    bad = c.post("/v1/chat/completions", json={"model": "nonexistent", "messages": MSG})
    assert bad.status_code == 400 and "alias" in bad.json()["error"]["message"]
    assert [x.model for x in ad["openai"].calls] == ["synthetic-openai-small"]


def test_alias_beats_a_bare_catalog_id_and_auto_is_never_an_alias(catalog, cfg, store):
    c, _ = _client(catalog, cfg, store, {"synthetic-openai-small": "openai/synthetic-openai-large"})
    assert c.post("/v1/chat/completions", json={"model": "synthetic-openai-small", "messages": MSG}).json()["model"] == "synthetic-openai-large"
    assert c.put("/v1/aliases/auto", json={"model": "openai/synthetic-openai-small"}).status_code == 400


def test_alias_endpoints(catalog, cfg, store, tmp_path):
    cfg.home = tmp_path
    c, _ = _client(catalog, cfg, store)
    r = c.put("/v1/aliases/Coding", json={"model": "anthropic/synthetic-anthropic-large"})
    assert r.status_code == 200 and r.json()["alias"] == {"alias": "coding", "model": "anthropic/synthetic-anthropic-large",
                                                         "in_catalog": True, "priced": True, "has_key": True}
    assert [a["alias"] for a in c.get("/v1/aliases").json()["aliases"]] == ["coding"] and cfg.aliases == {"coding": "anthropic/synthetic-anthropic-large"}
    # it works immediately, without a restart
    assert c.post("/v1/chat/completions", json={"model": "coding", "messages": MSG}).json()["model"] == "synthetic-anthropic-large"
    for bad in ({"model": "openai/not-in-catalog"}, {"model": "nope"}, {}, {"model": "openai/synthetic-openai-small", "x": 1}, {"model": 5}):
        assert c.put("/v1/aliases/bad", json=bad).status_code == 400, bad
    assert c.put("/v1/aliases/has%20space", json={"model": "openai/synthetic-openai-small"}).status_code == 400
    h = {"Content-Type": "application/json"}
    assert c.delete("/v1/aliases/coding").status_code == 415  # writes need a JSON content type, like every other write
    assert c.delete("/v1/aliases/coding", headers=h).status_code == 200 and c.delete("/v1/aliases/coding", headers=h).status_code == 400
    assert load_settings(tmp_path).get("aliases") == {}


def test_alias_endpoints_are_protected(catalog, cfg, store, tmp_path):
    cfg.home = tmp_path
    c, _ = _client(catalog, cfg, store, token="sekret")
    assert c.get("/v1/aliases").status_code == 401 and c.put("/v1/aliases/x", json={"model": "openai/synthetic-openai-small"}).status_code == 401
    ok = {"Authorization": "Bearer sekret"}
    assert c.put("/v1/aliases/x", json={"model": "openai/synthetic-openai-small"}, headers=ok).status_code == 200
    c2, _ = _client(catalog, cfg, Store_mem())
    assert TestClient(c2.app, base_url="http://attacker.test:8787").get("/v1/aliases").status_code == 403
    assert c2.put("/v1/aliases/y", json={"model": "openai/synthetic-openai-small"}, headers={"Origin": "https://evil.example"}).status_code == 403


def Store_mem():
    from modelspend.store import Store
    return Store(":memory:")


def test_cli_alias_roundtrip_and_use(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from modelspend.cli import app
    monkeypatch.setenv("MODELSPEND_HOME", str(tmp_path))
    run = CliRunner()
    assert "no aliases" in run.invoke(app, ["alias", "list"]).output
    bad = run.invoke(app, ["alias", "set", "coding", "openai/not-in-catalog"])  # the shipped catalog only has Anthropic models
    assert bad.exit_code == 2 and "catalog sync" in bad.output
    assert run.invoke(app, ["alias", "set", "auto", "anthropic/claude-haiku-5-5"]).exit_code == 2
    ok = run.invoke(app, ["alias", "set", "Coding", "anthropic/claude-sonnet-5-5"])
    assert ok.exit_code == 0 and "alias 'coding' -> anthropic/claude-sonnet-5-5" in ok.output
    lst = run.invoke(app, ["alias", "list"]).output
    assert "coding" in lst and "claude-sonnet-5-5" in lst and "no API key" in lst  # no key in this hermetic environment
    forced = run.invoke(app, ["alias", "set", "syn", "openai/synthetic-openai-large", "--force"])
    assert forced.exit_code == 0
    # an alias works wherever --model does
    pv = run.invoke(app, ["preview", "write a function", "--catalog", "synthetic", "--assume-keys", "--model", "syn", "--json"])
    assert pv.exit_code == 0 and '"synthetic-openai-large"' in pv.output
    assert run.invoke(app, ["alias", "remove", "coding"]).exit_code == 0 and run.invoke(app, ["alias", "remove", "coding"]).exit_code == 2


# ====================================================================================== stickiness: keys
def test_conversation_key_is_stable_private_and_scoped():
    salt = b"s" * 32
    t1 = [Message(role="user", content=FIRST)]
    t3 = t1 + [Message(role="assistant", content="billing"), Message(role="user", content="and the refund?")]
    k = conversation_key(salt, t1, "shop", "bot")
    assert k == conversation_key(salt, t3, "shop", "bot") and len(k) == 64 and FIRST.lower()[:10] not in k  # same chat, any turn; a hash only
    assert k != conversation_key(salt, t1, "shop", "other-bot") != conversation_key(salt, t1, "blog", "bot")
    assert k != conversation_key(b"x" * 32, t1, "shop", "bot")  # salted per installation
    assert conversation_key(salt, t1, "shop", "bot", run_id="r1") == conversation_key(salt, [Message(role="user", content="something else")], "shop", "bot", run_id="r1")
    assert conversation_key(salt, t1, "shop", "bot", run_id="r1") != conversation_key(salt, t1, "shop", "bot", run_id="r2")
    sys1 = [Message(role="system", content="You are terse"), *t1]
    assert conversation_key(salt, sys1) != conversation_key(salt, t1)


def test_is_continuation():
    u = Message(role="user", content="hi")
    assert not is_continuation([u]) and not is_continuation([Message(role="system", content="s"), u])
    assert is_continuation([u, Message(role="assistant", content="x"), u]) and is_continuation([u, Message(role="tool", content="x", tool_call_id="1")])


# ================================================================================== stickiness: routing
async def test_a_continuation_keeps_the_model_chosen_for_the_conversation(engine_factory, store):
    eng, ad = engine_factory()
    first = await eng.run(convo(FIRST, allow_models=["xai/synthetic-xai-small"]))  # a model that is NOT the cheapest overall
    assert first.model == "synthetic-xai-small" and first.sticky is None
    # without stickiness the cheapest model would win; with it, the conversation stays where it was
    fresh = await eng.run(convo(FIRST))
    assert fresh.model != "synthetic-xai-small"  # a single-turn request is routed freshly...
    await eng.run(convo(FIRST, allow_models=["xai/synthetic-xai-small"]))  # ...and re-seeds the conversation's model
    follow = convo(FIRST, "billing", "and what about the refund?")
    res = await eng.run(follow)
    assert res.model == "synthetic-xai-small" and res.sticky == "held"
    assert "Conversation stickiness: continuing on xai/synthetic-xai-small" in res.explanation and "cheapest suitable model" in res.explanation


async def test_held_plan_puts_the_conversations_model_first_with_renumbered_ranks(engine_factory, store):
    eng, _ = engine_factory()
    await eng.run(convo(FIRST, allow_models=["xai/synthetic-xai-small"]))
    plan = await eng.plan(convo(FIRST, "billing", "next question"))
    assert plan.sticky == "held" and f"{plan.candidates[0].provider}/{plan.candidates[0].model}" == "xai/synthetic-xai-small"
    assert [c.rank for c in plan.candidates] == list(range(1, len(plan.candidates) + 1))
    assert len(plan.candidates) > 1  # the others stay available for failover and escalation


async def test_stickiness_can_be_turned_off_per_request_and_globally(engine_factory, store, cfg):
    eng, _ = engine_factory()
    await eng.run(convo(FIRST, allow_models=["xai/synthetic-xai-small"]))
    follow = ("billing", "and the refund?")
    off = await eng.run(convo(FIRST, *follow, sticky=False))
    assert off.model != "synthetic-xai-small" and off.sticky is None
    cfg.sticky = False
    await eng.run(convo(FIRST, allow_models=["xai/synthetic-xai-small"]))
    assert (await eng.run(convo(FIRST, *follow))).sticky is None  # global default off; nothing was remembered either
    assert store.db.execute("select count(*) from conversations").fetchone()[0] >= 1  # (from the earlier, sticky-on calls)


async def test_single_turn_requests_are_never_held(engine_factory, store):
    eng, _ = engine_factory()
    await eng.run(convo(FIRST, allow_models=["xai/synthetic-xai-small"]))
    res = await eng.run(convo(FIRST))  # same text, no earlier turns: this is a new task, routed on its own merits
    assert res.sticky is None and res.model != "synthetic-xai-small"


async def test_released_when_the_model_is_no_longer_suitable_for_the_turn(engine_factory, store):
    eng, _ = engine_factory()
    await eng.run(convo(FIRST, allow_models=["xai/synthetic-xai-small"]))  # tier 1, fine for classification
    res = await eng.run(convo(FIRST, "billing", "Now write a Python function that parses these tickets.", task_type="code"))
    assert res.sticky and res.sticky.startswith("released: no longer suitable") and res.model != "synthetic-xai-small"
    assert "Conversation stickiness released" in res.explanation
    # the conversation now continues on the model that was actually used
    follow = await eng.run(convo(FIRST, "billing", "Now write a Python function that parses these tickets.", "thanks", task_type="code"))
    assert follow.sticky == "held" and follow.model == res.model


async def test_released_when_the_model_is_denied_or_unavailable(engine_factory, store):
    eng, _ = engine_factory()
    await eng.run(convo(FIRST, allow_models=["xai/synthetic-xai-small"]))
    res = await eng.run(convo(FIRST, "billing", "more?", deny_models=["xai/synthetic-xai-small"]))
    assert res.sticky and res.sticky.startswith("released: policy") and res.model != "synthetic-xai-small"
    res2 = await eng.run(convo(FIRST, "billing", "more?", providers=["openai"]))  # the provider is not in this request's allowlist
    assert res2.sticky and res2.sticky.startswith("released:") and res2.provider == "openai"


async def test_the_model_after_an_escalation_becomes_the_conversations_model(engine_factory, store):
    eng, _ = engine_factory()
    first = await eng.run(convo(FIRST, allow_models=["xai/synthetic-xai-small", "xai/synthetic-xai-large"],
                                validators=[{"type": "allowed_labels", "labels": ["impossible"]}]))
    assert first.escalations == 1 and first.model == "synthetic-xai-large"
    follow = await eng.run(convo(FIRST, "billing", "next?", allow_models=["xai/synthetic-xai-small", "xai/synthetic-xai-large"]))
    assert follow.sticky == "held" and follow.model == "synthetic-xai-large"


async def test_run_id_keeps_the_conversation_together_even_if_the_first_message_changes(engine_factory, store):
    eng, _ = engine_factory()
    await eng.run(convo(FIRST, allow_models=["xai/synthetic-xai-small"], run_id="chat-1"))
    compacted = convo("[summary of the conversation so far]", "ok", "next question", run_id="chat-1")  # a client compacted its history
    assert (await eng.run(compacted)).sticky == "held"
    no_run_id = convo("[summary of the conversation so far]", "ok", "next question")
    assert (await eng.run(no_run_id)).sticky is None  # without a run_id the first message is the identity


async def test_different_agents_and_projects_never_share_a_conversation(engine_factory, store):
    eng, _ = engine_factory()
    await eng.run(convo(FIRST, allow_models=["xai/synthetic-xai-small"], project="shop", agent="a"))
    follow = ("billing", "next?")
    assert (await eng.run(convo(FIRST, *follow, project="shop", agent="a"))).sticky == "held"
    assert (await eng.run(convo(FIRST, *follow, project="shop", agent="b"))).sticky is None
    assert (await eng.run(convo(FIRST, *follow, project="blog", agent="a"))).sticky is None


async def test_pinned_requests_neither_hold_nor_remember(engine_factory, store):
    eng, _ = engine_factory()
    await eng.run(convo(FIRST, pin="xai/synthetic-xai-large", providers=[]))
    assert store.db.execute("select count(*) from conversations").fetchone()[0] == 0
    res = await eng.run(convo(FIRST, "billing", "next?", pin="openai/synthetic-openai-small", providers=[]))
    assert res.sticky is None and res.model == "synthetic-openai-small"


async def test_failed_requests_do_not_change_the_conversations_model(engine_factory, store):
    from modelspend.errors import ProviderError
    eng, _ = engine_factory({"openai": lambda r, n: ProviderError("openai", "bad_request", "no", 400)})
    with pytest.raises(Exception):
        await eng.run(convo(FIRST, providers=["openai"]))
    assert store.db.execute("select count(*) from conversations").fetchone()[0] == 0


async def test_streams_hold_and_remember_too(engine_factory, store):
    eng, _ = engine_factory({"xai": "streamed text"})
    async for _ in eng.stream(convo(FIRST, allow_models=["xai/synthetic-xai-small"])):
        pass
    plan_event = None
    async for ev in eng.stream(convo(FIRST, "billing", "next?")):
        if ev.type == "route":
            plan_event = ev
    assert plan_event.plan.sticky == "held" and plan_event.plan.candidates[0].model == "synthetic-xai-small"


# ================================================================================ stickiness: the store
async def test_only_a_salted_hash_a_model_and_a_time_are_stored(engine_factory, store):
    eng, _ = engine_factory()
    await eng.run(convo("My card number is 4111 1111 1111 1111, why was it charged twice?", allow_models=["xai/synthetic-xai-small"],
                       task_type="classification"))
    rows = [tuple(r) for r in store.db.execute("select key, model, ts from conversations")]
    assert len(rows) == 1 and len(rows[0][0]) == 64 and rows[0][1] == "xai/synthetic-xai-small"
    dump = json.dumps(rows) + json.dumps([tuple(r) for r in store.db.execute("select * from requests")], default=str)
    assert "4111" not in dump and "card number" not in dump


def test_entries_expire_and_the_table_is_bounded(store, monkeypatch):
    import time
    store.put_conversation("old", "openai/m")
    store.db.execute("update conversations set ts=? where key='old'", (time.time() - store.STICKY_TTL_S - 5,))
    store.db.commit()
    assert store.get_conversation("old") is None
    store.put_conversation("fresh", "openai/m")
    assert store.get_conversation("fresh") == ("openai/m", None)
    assert store.db.execute("select count(*) from conversations where key='old'").fetchone()[0] == 0  # swept on the next write
    monkeypatch.setattr(type(store), "STICKY_MAX_ROWS", 3)
    for i in range(6):
        store.put_conversation(f"k{i}", "openai/m")
        store.db.execute("update conversations set ts=? where key=?", (time.time() - 1000 + i, f"k{i}"))
    store.db.commit()
    store.put_conversation("last", "openai/m")
    keys = {r[0] for r in store.db.execute("select key from conversations")}
    assert len(keys) <= 3 and "last" in keys


# ======================================================================================== through the server
def test_server_conversation_is_held_across_turns_and_reports_it(catalog, cfg, store):
    c, ad = _client(catalog, cfg, store, scripts={"openai": "ok", "anthropic": "ok", "google": "ok", "xai": "ok"})
    cfg.default_providers = ["openai", "anthropic", "google", "xai"]
    h = {"X-ModelSpend-Project": "shop", "X-ModelSpend-Agent": "chat-bot"}
    t1 = [{"role": "user", "content": FIRST}]
    r1 = c.post("/v1/chat/completions", json={"model": "auto", "messages": t1, "modelspend": {"allow_models": ["xai/synthetic-xai-small"]}}, headers=h).json()
    assert r1["model"] == "synthetic-xai-small" and r1["modelspend"]["sticky"] is None
    t2 = t1 + [{"role": "assistant", "content": "billing"}, {"role": "user", "content": "and the refund?"}]
    r2 = c.post("/v1/chat/completions", json={"model": "auto", "messages": t2}, headers=h).json()
    assert r2["model"] == "synthetic-xai-small" and r2["modelspend"]["sticky"] == "held"
    r3 = c.post("/v1/chat/completions", json={"model": "auto", "messages": t2, "modelspend": {"sticky": False}}, headers=h).json()
    assert r3["modelspend"]["sticky"] is None and r3["model"] != "synthetic-xai-small"
    bad = c.post("/v1/chat/completions", json={"model": "auto", "messages": t2, "modelspend": {"sticky": "maybe"}}, headers=h)
    assert bad.status_code == 400


def test_env_switch_turns_stickiness_off(monkeypatch, tmp_path):
    monkeypatch.setenv("MODELSPEND_HOME", str(tmp_path))
    assert Config.from_env().sticky is True
    monkeypatch.setenv("MODELSPEND_STICKY", "false")
    assert Config.from_env().sticky is False
