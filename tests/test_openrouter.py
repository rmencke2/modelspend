"""0.8: the OpenRouter importer. The response is untrusted network data shaped like OpenRouter's documented models list (not yet checked against
the live API): the parser must skip what it cannot trust, convert per-token prices exactly, and never invent a tier."""
from __future__ import annotations

import json
from types import SimpleNamespace as NS

import pytest
from typer.testing import CliRunner

from modelspend import Catalog, Config, RouteRequest, openrouter as orr, registry
from modelspend.cli import app
from modelspend.engine import Engine
from modelspend.errors import NoEligibleModel
from modelspend.providers import make_compat_adapter
from modelspend.store import Store
from modelspend.types import normalize_provider
from test_adapters import FakeOpenAI, oa_response

runner = CliRunner()


def model(mid, prompt="0.000003", completion="0.000015", ctx=200000, max_out=8192, params=("tools", "structured_outputs", "temperature", "top_p"),
          modality=None, **extra):
    m = {"id": mid, "name": f"Vendor: {mid}", "context_length": ctx, "architecture": modality or {"input_modalities": ["text", "image"], "output_modalities": ["text"]},
         "pricing": {"prompt": prompt, "completion": completion, "request": "0", "image": "0"},
         "top_provider": {"context_length": ctx, "max_completion_tokens": max_out, "is_moderated": False}, "supported_parameters": list(params)}
    m.update(extra)
    return m


PAYLOAD = {"data": [
    model("anthropic/claude-sonnet-x"),
    model("openai/gpt-x", prompt="0.0000004", completion="0.0000016", ctx=1000000, max_out=32768),
    model("meta/llama-x:free", prompt="0", completion="0"),
    model("openrouter/auto", prompt="-1", completion="-1"),
    model("vendor/image-maker", modality={"input_modalities": ["text"], "output_modalities": ["image"]}),
    model("vendor/no-limit", max_out=None),
    model("vendor/cached", **{}),
]}
PAYLOAD["data"][-1]["pricing"].update({"input_cache_read": "0.0000003", "input_cache_write": "0.00000375"})


# ---- parsing: the response is untrusted ----
def test_per_token_prices_become_exact_per_million_prices_and_the_right_models_are_kept():
    entries, skips = orr.parse(PAYLOAD)
    by = {e.id: e for e in entries}
    s = by["anthropic/claude-sonnet-x"]
    assert (s.input_per_mtok, s.output_per_mtok, s.context, s.max_output) == (3.0, 15.0, 200000, 8192)
    assert by["openai/gpt-x"].input_per_mtok == 0.4 and by["openai/gpt-x"].output_per_mtok == 1.6  # not 0.39999999999999997
    assert (by["vendor/cached"].cached_per_mtok, by["vendor/cached"].cache_write_per_mtok) == (0.3, 3.75)
    assert by["vendor/no-limit"].max_output is None
    assert by["meta/llama-x:free"].free and not s.free
    assert {x.id: x.reason.split(":")[0].split(" (")[0] for x in skips} == {"openrouter/auto": "no fixed price", "vendor/image-maker": "not a text-in, text-out model"}


def test_capabilities_are_taken_from_the_advertised_parameters_and_nothing_else_is_assumed():
    e = {x.id: x for x in orr.parse({"data": [model("a/b", params=("tools", "temperature")), model("a/c", params=("structured_outputs",)), model("a/d", params=())]})[0]}
    assert e["a/b"].capabilities == {"text": True, "tool_calling": True, "temperature": True}
    assert e["a/c"].capabilities == {"text": True, "structured_output": True}
    assert e["a/d"].capabilities == {"text": True}  # nothing advertised, nothing claimed: streaming etc. are never assumed


@pytest.mark.parametrize("bad", [
    {"id": None}, {"id": ""}, {"id": "../../etc/passwd"}, {"id": "a b"}, {"id": "x" * 200}, {"id": "ok/model", "architecture": "text"},
    {"id": "ok/model", "architecture": {"modality": "text->image"}},
    "not even an object", 5, None, [],
])
def test_malformed_entries_are_skipped_not_trusted(bad):
    entries, skips = orr.parse({"data": [bad]})
    assert entries == [] and len(skips) == 1


@pytest.mark.parametrize("price", ["abc", None, "", True, "nan", "inf", "-0.5", [], {}])
def test_unusable_prices_skip_the_model(price):
    e, s = orr.parse({"data": [model("a/b", prompt=price)]})
    assert e == [] and "no fixed price" in s[0].reason


def test_an_absurd_price_is_treated_as_a_unit_mistake():
    e, s = orr.parse({"data": [model("a/b", prompt="1.5")]})  # $1,500,000 per million tokens
    assert e == [] and "unit mistake" in s[0].reason


def test_names_are_cleaned_limits_must_be_real_integers_and_duplicates_collapse():
    m = model("a/b", name="Evil \x00<script>alert(1)</script>`x`" + "y" * 300, ctx="200000", max_out=-5)
    m["top_provider"]["context_length"] = True
    e, _ = orr.parse({"data": [m, model("a/b")]})
    assert len(e) == 1 and "<" not in e[0].name and "\x00" not in e[0].name and len(e[0].name) <= 100
    assert e[0].context is None and e[0].max_output is None  # strings, booleans and negatives are not limits


def test_the_old_modality_shape_and_separately_priced_reasoning_are_handled():
    old = model("a/old", modality={"modality": "text+image->text"})
    thinker = model("a/think")
    thinker["pricing"]["internal_reasoning"] = "0.00004"
    e = {x.id: x for x in orr.parse({"data": [old, thinker]})[0]}
    assert set(e) == {"a/old", "a/think"} and "priced separately" in e["a/think"].warnings[0] and e["a/old"].warnings == []


def test_payload_loading_rejects_the_wrong_shape_and_oversize_input(monkeypatch):
    for raw in (b"not json", b"[]", b'{"data": 5}', b'{"nodata": []}', b"\xff\xfe"):
        with pytest.raises(orr.OpenRouterError):
            orr.load_payload(raw)
    monkeypatch.setattr(orr, "MAX_BYTES", 10)
    with pytest.raises(orr.OpenRouterError, match="larger than"):
        orr.load_payload(b"x" * 11)


# ---- selection ----
def test_select_needs_names_skips_free_models_unless_asked_and_filters_by_context():
    entries, _ = orr.parse(PAYLOAD)
    got, left = orr.select(entries, ["llama", "gpt-x"])
    assert [e.id for e in got] == ["openai/gpt-x"] and "free-tier" in left[0].reason
    assert [e.id for e in orr.select(entries, ["llama"], include_free=True)[0]] == ["meta/llama-x:free"]
    assert [e.id for e in orr.select(entries, ["openai/gpt-x"], exact=True)[0]] == ["openai/gpt-x"] and orr.select(entries, ["gpt"], exact=True)[0] == []
    big, small = orr.select(entries, ["vendor/", "gpt-x", "claude"], min_context=500000)
    assert [e.id for e in big] == ["openai/gpt-x"] and len(small) >= 1


# ---- the catalog entry ----
def test_a_catalog_entry_has_prices_limits_capabilities_and_no_tier():
    e = {x.id: x for x in orr.parse(PAYLOAD)[0]}
    c = orr.catalog_entry(e["vendor/cached"], "2026-10-20")
    assert (c["provider"], c["id"], c["availability"]) == ("openrouter", "vendor/cached", "available") and "tier" not in c
    assert c["pricing"] == {"currency": "USD", "input_per_mtok": 3.0, "output_per_mtok": 15.0, "effective_date": "2026-10-20", "verified_on": "2026-10-20",
                            "source_url": "https://openrouter.ai/api/v1/models", "reasoning_in_output": True, "cached_input_per_mtok": 0.3, "cache_write_per_mtok": 3.75}
    assert c["limits"] == {"context_tokens": 200000, "max_output_tokens": 8192}
    nolimit = orr.catalog_entry(e["vendor/no-limit"], "2026-10-20")
    nolimit2 = orr.catalog_entry(e["vendor/no-limit"], "2026-10-20", default_max_output=4096)
    assert "max_output_tokens" not in nolimit["limits"] and nolimit2["limits"]["max_output_tokens"] == 4096


def test_refreshing_keeps_what_you_set_yourself_and_takes_new_prices():
    old = {"provider": "openrouter", "id": "a/b", "tier": 2, "tier_note": "mine", "capabilities": {"text": True, "streaming": True, "tool_calling": False},
           "limits": {"context_tokens": 1000, "max_output_tokens": 77}, "pricing": {"input_per_mtok": 1.0}}
    fresh = orr.catalog_entry(orr.parse({"data": [model("a/b")]})[0][0], "2026-10-20")
    m = orr.merge(old, fresh)
    assert m["tier"] == 2 and m["tier_note"] == "mine"  # your prior survives
    assert m["capabilities"]["streaming"] is True and m["capabilities"]["tool_calling"] is False  # your marks beat the advertised ones
    assert m["pricing"]["input_per_mtok"] == 3.0 and m["limits"] == {"context_tokens": 200000, "max_output_tokens": 8192}  # prices and limits are refreshed
    assert orr.merge(None, fresh) == fresh


# ---- slash ids through the whole pipeline ----
def test_openrouter_is_a_preset_and_its_ids_keep_their_slash_everywhere(tmp_path):
    s = registry.spec("openrouter")
    assert (s.kind, s.base_url, s.key_env, s.max_tokens_param) == ("preset", "https://openrouter.ai/api/v1", ("OPENROUTER_API_KEY",), "max_tokens")
    assert normalize_provider("OpenRouter") == "openrouter"
    req = RouteRequest(messages=[{"role": "user", "content": "x"}], providers=["openrouter"], pin="openrouter/openai/gpt-x")
    assert req.pin == "openrouter/openai/gpt-x" and req.providers == ["openrouter"]
    from modelspend.aliases import clean_ref
    assert clean_ref("openrouter/anthropic/claude-sonnet-x") == "openrouter/anthropic/claude-sonnet-x"


# ---- CLI, end to end with a saved response ----
@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("MODELSPEND_HOME", str(home))
    monkeypatch.setenv("MODELSPEND_DB", str(tmp_path / "u.db"))
    f = tmp_path / "models.json"
    f.write_text(json.dumps(PAYLOAD))
    return home, f


def cli(*a):
    return runner.invoke(app, list(a))


def overrides(home):
    return json.loads((home / "overrides.json").read_text())


def test_the_cli_needs_names_defaults_to_a_dry_run_and_writes_only_what_was_asked(env):
    home, f = env
    r = cli("catalog", "import-openrouter", "--file", str(f))
    assert r.exit_code == 2 and "name the models you want" in r.output
    dry = cli("catalog", "import-openrouter", "--file", str(f), "--only", "claude,gpt-x,llama,auto")
    assert dry.exit_code == 0 and "openrouter/anthropic/claude-sonnet-x" in dry.output and "openrouter/openai/gpt-x" in dry.output
    assert "dry run" in dry.output and "free-tier" in dry.output and not (home / "overrides.json").exists()
    ok = cli("catalog", "import-openrouter", "--file", str(f), "--only", "claude,gpt-x", "--write")
    assert ok.exit_code == 0 and "wrote 2 model(s)" in ok.output and "Next: let shadow evaluation measure them" in ok.output
    added = {m["id"]: m for m in overrides(home)["add_models"]}
    assert set(added) == {"anthropic/claude-sonnet-x", "openai/gpt-x"} and all("tier" not in m for m in added.values())


def test_imported_models_load_are_priced_and_untiered_models_are_not_picked_on_a_prior(env):
    home, f = env
    cli("catalog", "import-openrouter", "--file", str(f), "--only", "claude", "--write")
    cat = Catalog.from_config(Config.from_env())
    m = cat.get("openrouter/anthropic/claude-sonnet-x")
    assert (m.provider, m.id, m.tier, m.pricing.input_per_mtok, m.pricing.output_per_mtok) == ("openrouter", "anthropic/claude-sonnet-x", None, 3.0, 15.0)
    assert (m.limits.context_tokens, m.limits.max_output_tokens, m.capabilities.tool_calling) == (200000, 8192, True)
    r = cli("preview", "Classify this ticket: charged twice", "--providers", "openrouter", "--task-type", "classification", "--assume-keys")
    assert "No suitable candidate" in r.output and "no tier prior and evidence insufficient (n=0, need 20)" in r.output  # no tier: never chosen on a prior
    assert cli("catalog", "set", "openrouter/anthropic/claude-sonnet-x", "tier=2").exit_code == 0
    ok = cli("preview", "Classify this ticket: charged twice", "--providers", "openrouter", "--task-type", "classification", "--assume-keys")
    assert "Selected openrouter/anthropic/claude-sonnet-x" in ok.output  # once you give it a tier


def test_a_model_without_an_output_limit_stays_unroutable_and_the_command_says_how_to_fix_it(env):
    home, f = env
    r = cli("catalog", "import-openrouter", "--file", str(f), "--only", "no-limit", "--write")
    assert "No output limit is listed for: openrouter/vendor/no-limit" in r.output and "--default-max-output" in r.output
    fixed = cli("catalog", "import-openrouter", "--file", str(f), "--only", "no-limit", "--default-max-output", "4096", "--write")
    assert "No output limit" not in fixed.output
    assert Catalog.from_config(Config.from_env()).get("openrouter/vendor/no-limit").limits.max_output_tokens == 4096


def test_refresh_updates_prices_reports_the_change_and_replaces_a_hand_set_price(env, tmp_path):
    home, f = env
    cli("catalog", "import-openrouter", "--file", str(f), "--only", "gpt-x", "--write")
    cli("catalog", "set", "openrouter/openai/gpt-x", "tier=1")
    assert cli("catalog", "price", "openrouter/openai/gpt-x", "--input", "9", "--output", "9").exit_code == 0  # a hand-set price, now stale
    cheaper = json.loads(json.dumps(PAYLOAD))
    next(m for m in cheaper["data"] if m["id"] == "openai/gpt-x")["pricing"]["prompt"] = "0.0000002"
    f2 = tmp_path / "new.json"
    f2.write_text(json.dumps(cheaper))
    r = cli("catalog", "import-openrouter", "--file", str(f2), "--refresh", "--write")
    assert r.exit_code == 0 and "update" in r.output and "replaced the price you set by hand" in r.output
    m = Catalog.from_config(Config.from_env()).get("openrouter/openai/gpt-x")
    assert (m.pricing.input_per_mtok, m.tier) == (0.2, 1)  # OpenRouter's current price wins; your tier is kept


def test_refresh_flags_a_price_change_and_notes_models_that_vanished(env, tmp_path):
    home, f = env
    cli("catalog", "import-openrouter", "--file", str(f), "--only", "gpt-x,claude", "--write")
    changed = {"data": [m for m in PAYLOAD["data"] if m["id"] != "anthropic/claude-sonnet-x"]}
    next(m for m in changed["data"] if m["id"] == "openai/gpt-x")["pricing"]["completion"] = "0.000002"
    f2 = tmp_path / "new.json"
    f2.write_text(json.dumps(changed))
    r = cli("catalog", "import-openrouter", "--file", str(f2), "--refresh")  # a dry run
    assert "PRICE CHANGED: in $0.4 -> $0.4, out $1.6 -> $2" in r.output
    assert "anthropic/claude-sonnet-x is no longer in OpenRouter's usable list" in r.output and "dry run" in r.output


def test_bad_input_files_and_no_matches_fail_politely(env, tmp_path):
    home, f = env
    bad = tmp_path / "bad.json"
    bad.write_text("nope")
    r = cli("catalog", "import-openrouter", "--file", str(bad), "--only", "x")
    assert r.exit_code == 1 and r.output.startswith("error: that is not valid JSON")
    assert cli("catalog", "import-openrouter", "--file", str(tmp_path / "missing.json"), "--only", "x").exit_code == 1
    none = cli("catalog", "import-openrouter", "--file", str(f), "--only", "zzz")
    assert none.exit_code == 0 and "no matching models" in none.output and "usable models in the list" in none.output


# ---- fetching ----
def test_fetch_sends_the_key_only_to_openrouter_caps_the_body_and_reports_failures(monkeypatch):
    import httpx
    seen = {}

    def handler(request):
        seen["url"], seen["auth"] = str(request.url), request.headers.get("authorization")
        return httpx.Response(200, json=PAYLOAD)

    out = orr.fetch("https://openrouter.ai/api/v1/", "sk-or-secret", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert seen == {"url": "https://openrouter.ai/api/v1/models", "auth": "Bearer sk-or-secret"} and len(out["data"]) == len(PAYLOAD["data"])
    orr.fetch("https://openrouter.ai/api/v1", None, client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert seen["auth"] is None
    with pytest.raises(orr.OpenRouterError, match="HTTP 503"):
        orr.fetch("https://x.test/api/v1", None, client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503))))
    monkeypatch.setattr(orr, "MAX_BYTES", 50)
    with pytest.raises(orr.OpenRouterError, match="larger than"):
        orr.fetch("https://x.test/api/v1", None, client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"{" + b" " * 200 + b"}"))))

    def boom(request):
        raise httpx.ConnectError("down", request=request)

    with pytest.raises(orr.OpenRouterError, match="could not reach OpenRouter"):
        orr.fetch("https://x.test/api/v1", "sk-or-secret", client=httpx.Client(transport=httpx.MockTransport(boom)))


# ---- an imported model is a real routed model ----
async def test_a_routed_call_to_an_imported_openrouter_model_is_priced_from_the_imported_price(env, tmp_path):
    home, f = env
    cli("catalog", "import-openrouter", "--file", str(f), "--only", "gpt-x", "--write")
    cli("catalog", "set", "openrouter/openai/gpt-x", "tier=1", "capabilities.streaming=true")
    cat = Catalog.from_config(Config.from_env())
    fake = FakeOpenAI(response=oa_response("billing"))
    cfg = Config(home=home, api_keys={"openrouter": "k"}, retry_backoff_s=0.0)
    eng = Engine(cat, cfg, {"openrouter": make_compat_adapter(registry.spec("openrouter"), "k", client=fake)}, store=Store(":memory:"), sandbox_runner=None)
    res = await eng.run(RouteRequest(providers=["openrouter"], messages=[{"role": "user", "content": "Classify: charged twice"}], task_type="classification"))
    assert (res.provider, res.model, res.status) == ("openrouter", "openai/gpt-x", "ok")
    assert fake.calls[0]["model"] == "openai/gpt-x" and fake.calls[0]["max_tokens"] > 0  # the slash id goes to the API as written
    price = cat.get("openrouter/openai/gpt-x").pricing
    u = res.usage
    want = (u.input_tokens * price.input_per_mtok + u.output_tokens * price.output_per_mtok + u.cache_read_tokens * (price.cached_input_per_mtok or price.input_per_mtok)) / 1e6
    assert res.actual_cost_usd == pytest.approx(want) and want > 0


async def test_untiered_imported_models_can_still_be_measured_by_shadow_evaluation(env):
    """The point of importing without a tier: routing will not pick the model on a prior, but shadow evaluation can test it against what you use."""
    from modelspend.config import ShadowSettings
    home, f = env
    cli("catalog", "import-openrouter", "--file", str(f), "--only", "gpt-x,claude", "--write")
    cli("catalog", "set", "openrouter/anthropic/claude-sonnet-x", "tier=3")  # the model you use today (the premium one)
    cat = Catalog.from_config(Config.from_env())
    cfg = Config(home=home, api_keys={"openrouter": "k"}, retry_backoff_s=0.0, shadow=ShadowSettings(enabled=True, sample_rate=1.0))
    fake = FakeOpenAI(response=oa_response("billing"))
    ad = make_compat_adapter(registry.spec("openrouter"), "k", client=fake)
    store = Store(":memory:")
    eng = Engine(cat, cfg, {"openrouter": ad}, store=store, sandbox_runner=None)
    await eng.run(RouteRequest(providers=["openrouter"], messages=[{"role": "user", "content": "Classify: charged twice"}], task_type="classification",
                               pin="openrouter/anthropic/claude-sonnet-x"))
    await eng.shadow.drain()
    rows = store.db.execute("select cand_model, verdict from shadow_results").fetchall()
    assert [(r["cand_model"], r["verdict"]) for r in rows] == [("openai/gpt-x", "pass")]  # the untiered, cheaper model was measured


@pytest.mark.parametrize("bad_id", ["../../etc/passwd", ".hidden/x", "a b", "a\nb", "x" * 200, "vendor/model;rm -rf", "-flag/x", "ünï/code", "a/b`c`", ""])
def test_ids_are_checked_on_their_own_so_an_otherwise_valid_model_with_a_bad_id_is_skipped(bad_id):
    entries, skips = orr.parse({"data": [model(bad_id)]})
    assert entries == [] and skips[0].reason == "unusable id"
    assert [e.id for e in orr.parse({"data": [model("meta-llama/llama-3.3-70b-instruct:free"), model("vendor/model_v2.1+x")]})[0]] == [
        "meta-llama/llama-3.3-70b-instruct:free", "vendor/model_v2.1+x"]  # real-looking ids, colons, dots and slashes are fine


def test_an_oversized_response_stops_being_read_instead_of_filling_memory(monkeypatch):
    import httpx
    read = []

    def chunks():
        for i in range(1000):
            read.append(i)
            yield b" " * 40

    monkeypatch.setattr(orr, "MAX_BYTES", 100)
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=chunks())))
    with pytest.raises(orr.OpenRouterError, match="refusing to read it"):
        orr.fetch("https://x.test/api/v1", None, client=client)
    assert len(read) < 10  # it gave up after a few chunks, not after all 1000
