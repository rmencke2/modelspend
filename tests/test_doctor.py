"""`modelspend doctor`: offline, read-only, and every finding names the thing to run. These tests pin the findings people will actually hit."""
from __future__ import annotations

import json
import os
import socket
import time
from datetime import date, timedelta

import pytest
from typer.testing import CliRunner

from modelspend.catalog import Catalog
from modelspend.cli import app, routability
from modelspend.config import Config
from modelspend.doctor import Finding, diagnose, load_verify, save_verify, summary, to_text
from modelspend.store import Store
from modelspend.verify import Check, ProviderReport

from test_whatif import _m

runner = CliRunner()
TODAY = date(2026, 10, 24)


def run(cfg, catalog, store=None, verify=None, today=TODAY):
    return diagnose(cfg, catalog, store, routability=lambda m: routability(m, cfg), verify_results=verify, today=today)


def levels(fs, area=None):
    return [(f.level, f.area) for f in fs if area is None or f.area == area]


def find(fs, area, level=None):
    return [f for f in fs if f.area == area and (level is None or f.level == level)]


def add_request(store, n=1, *, status="ok", provider="openai", model="gpt-x", cached=0, cost=0.01, ts=None, tag="q"):
    for i in range(n):
        store.db.execute("insert into requests(id, ts, provider, model, status, cache_read_tokens, actual_cost) values (?,?,?,?,?,?,?)",
                         (f"{tag}{status}{provider}{model}{i}", ts or time.time(), provider, model, status, cached, cost))
    store.db.commit()


# ---------------------------------------------------------------- keys and models
def test_a_setup_with_no_key_is_a_problem_that_says_which_command_to_run(tmp_path, catalog):
    fs = run(Config(home=tmp_path, api_keys={}), catalog)
    assert levels(fs) == [("problem", "keys")] and fs[0].fix == "modelspend keys set openai"


def test_a_working_setup_has_no_problem_and_no_warning(cfg, catalog):
    fs = run(cfg, catalog, Store(":memory:"), {p: {"model": "m", "status": "ok", "date": str(TODAY)} for p in cfg.api_keys})
    assert summary(fs)["problem"] == 0 and summary(fs)["warn"] == 0 and find(fs, "keys", "ok") and find(fs, "models", "ok")


def test_a_provider_with_a_key_but_no_models_in_the_catalog_gets_the_exact_sync_command(tmp_path, catalog):
    cfg = Config(home=tmp_path, api_keys={"deepseek": "k"}, key_sources={"deepseek": "environment"})
    fs = run(cfg, catalog)
    f = find(fs, "models", "warn")[0]
    assert "no deepseek models" in f.message and f.fix.startswith("modelspend catalog sync --provider deepseek --add-new --only deepseek --write")
    assert find(fs, "models", "problem")  # and since that was the only provider, nothing at all can be routed


def test_a_provider_whose_models_are_all_unusable_names_the_commonest_reason_and_fix(tmp_path):
    m = _m("openai", "gpt-x", 2.0, 8.0)
    m["availability"] = "unverified"
    cat = Catalog.load(overrides={"add_models": [m]})
    cfg = Config(home=tmp_path, api_keys={"openai": "k"}, key_sources={"openai": "environment"})
    fs = run(cfg, cat)
    f = find(fs, "models", "warn")
    assert f and "availability unverified" in f[0].message and f[0].fix == "modelspend catalog sync --provider openai --write"
    assert find(fs, "models", "problem")[0].fix == "modelspend catalog sync --provider openai --write"


def test_an_enabled_provider_without_a_key_is_a_warning(cfg, catalog):
    cfg.default_providers = ["openai", "deepseek"]
    f = find(run(cfg, catalog), "models", "warn")
    assert len(f) == 1 and "deepseek" in f[0].message and f[0].fix == "modelspend keys set deepseek"


def test_a_keys_file_other_users_can_read_is_a_problem_until_it_is_fixed(cfg, catalog):
    kp = cfg.home / ".env"
    from modelspend.config import keys_path
    kp = keys_path(cfg.home)
    kp.write_text("OPENAI_API_KEY=x\n")
    os.chmod(kp, 0o644)
    f = find(run(cfg, catalog), "keys", "problem")
    assert f and f[0].fix.startswith(f"chmod 600 {kp}")
    os.chmod(kp, 0o600)
    os.chmod(cfg.home, 0o700)
    assert not find(run(cfg, catalog), "keys", "problem")


def test_a_private_keys_file_inside_a_folder_others_can_enter_is_still_a_problem(cfg, catalog):
    from modelspend.config import keys_path
    keys_path(cfg.home).write_text("OPENAI_API_KEY=x\n")
    os.chmod(keys_path(cfg.home), 0o600)
    os.chmod(cfg.home, 0o755)
    assert find(run(cfg, catalog), "keys", "problem")


# ---------------------------------------------------------------- prices
def test_old_or_undated_prices_on_routable_models_are_a_warning(cfg):
    old = _m("openai", "gpt-x", 2.0, 8.0)
    old["tier"] = 1
    old["pricing"]["verified_on"] = str(date.today() - timedelta(days=90))
    f = find(run(cfg, Catalog.load(overrides={"add_models": [old]})), "prices", "warn")
    assert f and "openai/gpt-x" in f[0].message and "catalog stale" in f[0].fix


def test_cached_tokens_with_no_cached_price_are_flagged_because_they_are_costed_as_plain_input(cfg):
    store = Store(":memory:")
    add_request(store, 3, cached=1200)
    cat = Catalog.load(overrides={"add_models": [_m("openai", "gpt-x", 2.0, 8.0, cached=None)]})
    f = [x for x in find(run(cfg, cat, store), "prices", "warn") if "cached" in x.message]
    assert f and "3,600 cached tokens" in f[0].message and f[0].fix.startswith("modelspend catalog set openai/gpt-x pricing.cached_input_per_mtok=")
    ok = Catalog.load(overrides={"add_models": [_m("openai", "gpt-x", 2.0, 8.0, cached=0.25)]})
    assert not [x for x in find(run(cfg, ok, store), "prices", "warn") if "cached" in x.message]


# ---------------------------------------------------------------- the last live check
def test_a_provider_never_verified_is_a_note_with_the_command(cfg, catalog):
    f = find(run(cfg, catalog, None, {}), "verify")
    assert [x.level for x in f] == ["info"] and f[0].fix == "modelspend verify --cache" and all(p in f[0].message for p in cfg.api_keys)  # one line for all, not one per provider
    some = {"openai": {"model": "m", "status": "ok", "date": str(TODAY)}}
    g = [x for x in find(run(cfg, catalog, None, some), "verify") if x.level == "info"][0]
    assert "openai" not in g.message.split("yet:")[1] and g.fix != "modelspend verify --cache" and "google" in g.fix and "openai" not in g.fix


@pytest.mark.parametrize("status,level", [("fail", "problem"), ("skip", "warn"), ("warn", "warn"), ("ok", "ok")])
def test_the_last_live_result_decides_the_level(cfg, catalog, status, level):
    r = {"openai": {"model": "openai/gpt-x", "status": status, "date": str(TODAY), "problems": ["cache"], "skipped": ["stream"]}}
    f = find(run(cfg, catalog, None, r), "verify")
    assert [x.level for x in f if x.message.startswith("openai")] == [level]


def test_an_old_pass_is_a_note_and_a_fail_carries_the_report_command(cfg, catalog):
    old = {"openai": {"model": "m", "status": "ok", "date": str(TODAY - timedelta(days=45))}}
    assert [x for x in find(run(cfg, catalog, None, old), "verify") if x.message.startswith("openai")][0].level == "info"
    bad = {"openai": {"model": "m", "status": "fail", "date": str(TODAY), "problems": ["cache"]}}
    f = [x for x in find(run(cfg, catalog, None, bad), "verify") if x.message.startswith("openai")][0]
    assert "cache" in f.message and f.fix == "modelspend verify openai --cache --report check.md"


def test_verify_results_are_remembered_per_provider_without_keys_or_text(tmp_path):
    reports = [ProviderReport("openai", "openai/gpt-x", [Check("generate", "ok", "fine"), Check("cache", "fail", "secret detail sk-abc")]),
               ProviderReport("google", None, [Check("key", "ok", "x")])]
    save_verify(tmp_path, reports, TODAY)
    d = load_verify(tmp_path)
    assert d["openai"] == {"model": "openai/gpt-x", "status": "fail", "date": "2026-10-24", "problems": ["cache"], "skipped": [], "refused": []}
    assert "sk-abc" not in (tmp_path / "verify-results.json").read_text() and d["google"]["status"] == "skip"
    save_verify(tmp_path, [ProviderReport("openai", "openai/gpt-x", [Check("generate", "ok", "")])], TODAY)
    assert load_verify(tmp_path)["openai"]["status"] == "ok" and "google" in load_verify(tmp_path)  # one provider's rerun leaves the others
    (tmp_path / "verify-results.json").write_text("{not json")
    assert load_verify(tmp_path) == {}  # a damaged file is ignored, never a crash


# ---------------------------------------------------------------- the ledger
def test_an_empty_ledger_is_a_note_not_a_problem(cfg, catalog):
    f = find(run(cfg, catalog, Store(":memory:")), "ledger")
    assert [x.level for x in f] == ["info"] and "serve --background" in f[0].fix


def test_a_high_failure_rate_is_a_warning_with_the_commonest_error(cfg, catalog):
    s = Store(":memory:")
    add_request(s, 6)
    add_request(s, 6, status="failed", tag="f")
    for i in range(6):
        s.db.execute("insert into attempts(request_id, idx, kind, provider, model, outcome, error_kind) values (?,?,?,?,?,?,?)", (f"ffailedopenaigpt-x{i}", 0, "generation", "openai", "gpt-x", "error", "rate_limit"))
    s.db.commit()
    f = find(run(cfg, catalog, s), "ledger", "warn")
    assert f and "6 of 12 requests" in f[0].message and "rate_limit x6" in f[0].message


def test_a_few_failures_in_a_busy_ledger_are_fine(cfg, catalog):
    s = Store(":memory:")
    add_request(s, 20)
    add_request(s, 1, status="failed", tag="f")
    assert not find(run(cfg, catalog, s), "ledger", "warn") and find(run(cfg, catalog, s), "ledger", "ok")


def test_calls_with_no_price_are_a_warning_naming_the_model(cfg, catalog):
    s = Store(":memory:")
    add_request(s, 4, provider="deepseek", model="deepseek-flash", cost=None)
    f = [x for x in find(run(cfg, catalog, s), "ledger", "warn") if "no price" in x.message]
    assert f and "deepseek/deepseek-flash x4" in f[0].message and f[0].fix.startswith("modelspend catalog price deepseek/deepseek-flash --input")


def test_old_traffic_does_not_count(cfg, catalog):
    s = Store(":memory:")
    add_request(s, 15, status="failed", ts=time.time() - 40 * 86400)
    assert [x.level for x in find(run(cfg, catalog, s), "ledger")] == ["info"]


# ---------------------------------------------------------------- caching, security, setup
def test_claude_traffic_with_markers_off_gets_a_note_and_markers_on_or_no_traffic_does_not(cfg, catalog):
    s = Store(":memory:")
    assert not find(run(cfg, catalog, s), "caching")  # no Claude traffic: nothing to say
    add_request(s, 2, provider="anthropic", model="claude-x")
    f = find(run(cfg, catalog, s), "caching", "info")
    assert f and "anthropic_cache_markers" in f[0].fix
    cfg.anthropic_cache_markers = "repeats"
    assert not find(run(cfg, catalog, s), "caching")


def test_prefix_tracking_off_is_a_note(cfg, catalog):
    cfg.track_prefixes = False
    assert [x.level for x in find(run(cfg, catalog), "caching")] == ["info"]


def test_listening_beyond_this_machine_without_a_token_is_a_problem(cfg, catalog):
    cfg.host = "0.0.0.0"
    f = find(run(cfg, catalog), "security", "problem")
    assert f and "MODELSPEND_API_TOKEN" in f[0].fix
    cfg.api_token = "x" * 32
    assert not find(run(cfg, catalog), "security", "problem")


def test_a_ledger_database_other_users_can_read_is_a_warning(cfg, catalog):
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.db_path.write_text("")
    os.chmod(cfg.db_path, 0o644)
    f = find(run(cfg, catalog), "security", "warn")
    assert f and f[0].fix == f"chmod 600 {cfg.db_path}"


# ---------------------------------------------------------------- the report
def test_the_text_groups_by_area_puts_problems_first_and_prints_every_fix():
    fs = [Finding("ok", "keys", "keys fine"), Finding("info", "verify", "never checked", "modelspend verify openai"), Finding("problem", "keys", "x unreadable", "chmod 600 f")]
    t = to_text(fs)
    assert t.index("PROBLEM") < t.index("keys fine") and "-> chmod 600 f" in t and "-> modelspend verify openai" in t
    assert "1 problem(s), 0 warning(s), 1 note(s), 1 check(s) fine. Fix the problems first." in t
    assert t.count("\nkeys\n") == 1 and "offline" in t


# ---------------------------------------------------------------- the command
def env(monkeypatch, home, **extra):
    monkeypatch.setenv("MODELSPEND_HOME", str(home))
    for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY", "XAI_API_KEY", "DEEPSEEK_API_KEY", "MODELSPEND_ANTHROPIC_CACHE_MARKERS", "MODELSPEND_TRACK_PREFIXES"):
        monkeypatch.delenv(k, raising=False)
    for k, v in extra.items():
        monkeypatch.setenv(k, v)


def test_the_command_exits_1_on_a_problem_0_otherwise_and_prints_json_on_request(tmp_path, monkeypatch):
    env(monkeypatch, tmp_path)
    r = runner.invoke(app, ["doctor"])
    assert r.exit_code == 1 and "modelspend keys set openai" in r.output
    j = runner.invoke(app, ["doctor", "--format", "json"])
    d = json.loads(j.output)
    assert d["summary"]["problem"] == 1 and d["findings"][0]["area"] == "keys" and j.exit_code == 1
    bad = runner.invoke(app, ["doctor", "--format", "yaml"])
    assert bad.exit_code != 0


def test_a_broken_settings_file_is_reported_not_a_traceback(tmp_path, monkeypatch):
    env(monkeypatch, tmp_path)
    (tmp_path / "settings.json").write_text("{oops")
    r = runner.invoke(app, ["doctor"])
    assert r.exit_code == 1 and "settings.json is not valid JSON" in r.output and "Traceback" not in r.output


def test_doctor_touches_no_network_and_writes_nothing(tmp_path, monkeypatch):
    env(monkeypatch, tmp_path, OPENAI_API_KEY="sk-test")
    monkeypatch.setattr(socket.socket, "connect", lambda *a, **k: (_ for _ in ()).throw(AssertionError("doctor used the network")))
    before = sorted(p.name for p in tmp_path.iterdir())
    r = runner.invoke(app, ["doctor"])
    assert "Traceback" not in r.output and "keys for openai (environment)" in r.output
    assert sorted(p.name for p in tmp_path.iterdir()) == before  # no database, no results file created by looking


# ---------------------------------------------------------------- what a real setup showed (2026-10-24)
def two_providers(tmp_path, enabled):
    cat = Catalog.load(overrides={"add_models": [dict(_m("openai", "gpt-x", 2.0, 8.0), tier=1), dict(_m("google", "gem-x", 1.0, 4.0), tier=1)]})
    cfg = Config(home=tmp_path, api_keys={"openai": "k", "google": "k"}, key_sources={"openai": "environment", "google": "keys file"}, default_providers=enabled)
    return cfg, cat


def test_a_provider_you_chose_not_to_enable_is_a_note_with_the_enable_command_not_a_problem(tmp_path):
    cfg, cat = two_providers(tmp_path, ["openai"])
    fs = run(cfg, cat)
    f = [x for x in find(fs, "models") if x.message.startswith("google")]
    assert [x.level for x in f] == ["info"] and f[0].fix == "modelspend providers enable google"
    assert summary(fs)["problem"] == 0 and find(fs, "models", "ok")


def test_one_unusable_provider_among_working_ones_is_a_warning_not_a_problem(tmp_path):
    cfg, cat = two_providers(tmp_path, [])
    cfg.api_keys["xai"] = "k"
    cfg.key_sources["xai"] = "keys file"
    fs = run(cfg, cat)
    assert [x.level for x in find(fs, "models") if x.message.startswith("xai")] == ["warn"] and summary(fs)["problem"] == 0


def test_calls_from_before_a_model_was_priced_are_a_note_and_calls_to_a_still_unpriced_model_a_warning(cfg):
    s = Store(":memory:")
    add_request(s, 20, provider="deepseek", model="deepseek-flash", cost=None)
    priced = Catalog.load(overrides={"add_models": [dict(_m("deepseek", "deepseek-flash", 0.3, 1.2), tier=1)]})
    fs = run(cfg, priced, s)
    assert not [x for x in find(fs, "ledger", "warn") if "no price" in x.message]
    n = [x for x in find(fs, "ledger", "info") if "deepseek/deepseek-flash x20" in x.message]
    assert n and n[0].fix is None and "new calls are costed" in n[0].message
    assert [x for x in run(cfg, Catalog.load(), s)  if x.level == "warn" and "no price" in x.message]


# ---------------------------------------------------------------- a model the provider refused (OpenAI retired gpt-4.1-nano, 2026-10)
def refused_result(*refs):
    return {"openai": {"model": "openai/gpt-5-mini", "status": "ok", "date": str(TODAY), "refused": list(refs)}}


def test_a_model_refused_during_verify_but_still_listed_as_available_is_a_warning_with_the_disable_command(cfg):
    cat = Catalog.load(overrides={"add_models": [dict(_m("openai", "gpt-gone", 0.1, 0.4), tier=1)]})
    f = [x for x in find(run(cfg, cat, None, refused_result("openai/gpt-gone")), "models", "warn") if "gpt-gone" in x.message]
    assert f and "refused" in f[0].message and f[0].fix == "modelspend models disable openai/gpt-gone"


def test_a_refused_model_you_already_turned_off_or_that_is_not_in_the_catalog_is_not_reported_again(cfg):
    cat = Catalog.load(overrides={"add_models": [dict(_m("openai", "gpt-gone", 0.1, 0.4), tier=1)]})
    cfg.deny_models = ["openai/gpt-gone"]
    assert not [x for x in run(cfg, cat, None, refused_result("openai/gpt-gone")) if "gpt-gone" in x.message]
    cfg.deny_models = []
    assert not [x for x in run(cfg, cat, None, refused_result("openai/never-listed")) if "never-listed" in x.message]


def test_verify_remembers_which_model_was_refused_and_a_later_clean_run_clears_it(tmp_path):
    refused = ProviderReport("openai", "openai/gpt-5-mini", [Check("model", "skip", "openai/gpt-4.1-nano was refused by this key (HTTP 404: deprecated); trying openai/gpt-5-mini"), Check("generate", "ok", "")])
    save_verify(tmp_path, [refused], TODAY)
    assert load_verify(tmp_path)["openai"]["refused"] == ["openai/gpt-4.1-nano"]
    save_verify(tmp_path, [ProviderReport("openai", "openai/gpt-5-mini", [Check("generate", "ok", "")])], TODAY)
    assert load_verify(tmp_path)["openai"]["refused"] == []
