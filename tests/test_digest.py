"""Spend digest: the arithmetic against the period before, what is flagged, and the one place usage metadata leaves the machine (the webhook)."""
from __future__ import annotations

import json
import socket
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from modelspend import budgets
from modelspend.cli import app
from modelspend.config import Config
from modelspend.digest import DigestError, build_digest, payload, post, to_text, validate_webhook
from modelspend.shadow import SHADOW_PROJECT
from modelspend.store import Store

runner = CliRunner()
NOW = 1_800_000_000.0
H = 3600.0
_n = [0]


def call(store, hours_ago, cost, *, project="web", model="gpt-x", provider="openai", status="ok", tokens=1000, purpose="answer", cached=0, idx=0, basis="reported_usage"):
    _n[0] += 1
    rid = f"r{_n[0]}"
    ts = NOW - hours_ago * H
    store.db.execute("insert into requests(id, ts, provider, model, status, project, task, profile, cache_read_tokens) values (?,?,?,?,?,?,?,?,?)", (rid, ts, provider, model, status, project, "other", "balanced", cached))
    store.db.execute("insert into attempts(request_id, idx, kind, purpose, provider, model, outcome, input_tokens, cache_read, cache_write, output_tokens, reasoning_tokens, cost, cost_basis) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (rid, idx, "generation", purpose, provider, model, "ok" if status == "ok" else "error", tokens, cached, 0, 10, 0, cost, basis if cost is not None else "unpriced"))
    store.db.commit()


@pytest.fixture
def store():
    return Store(":memory:")


def digest(store, **kw):
    return build_digest(store, now=NOW, **kw)


# ---------------------------------------------------------------- the numbers
def test_the_last_day_is_compared_with_the_day_before(store):
    call(store, 2, 3.0)
    call(store, 20, 1.0)
    call(store, 30, 1.5)  # the previous 24 h
    call(store, 47, 0.5)  # previous too
    call(store, 60, 9.0)  # older: ignored
    d = digest(store)
    assert (d["cost"], d["previous_cost"], d["requests"]) == (4.0, 2.0, 2) and abs(d["change_pct"] - 1.0) < 1e-9
    assert digest(store, period="week")["cost"] == 4.0 + 2.0 + 9.0 and digest(store, period="week")["previous_cost"] == 0.0


def test_a_period_with_no_history_has_no_percentage_and_says_so(store):
    call(store, 1, 2.0)
    d = digest(store)
    assert d["change_pct"] is None and "nothing recorded in the previous 24 h" in to_text(d)


def test_an_empty_ledger_says_nothing_went_through(store):
    d = digest(store)
    assert d["cost"] == 0 and d["requests"] == 0 and "Nothing went through ModelSpend in this period." in to_text(d)


def test_projects_are_ranked_by_the_size_of_their_change_and_new_ones_are_marked(store):
    call(store, 2, 5.0, project="web")
    call(store, 30, 1.0, project="web")  # +4.00
    call(store, 3, 2.0, project="batch")
    call(store, 31, 3.0, project="batch")  # -1.00
    call(store, 4, 0.5, project="fresh")  # new
    call(store, 5, 1.0, project="same")
    call(store, 33, 1.0, project="same")  # no change: not listed
    m = digest(store)["projects"]
    assert [(x["name"], round(x["delta"], 2), x["new"]) for x in m] == [("web", 4.0, False), ("batch", -1.0, False), ("fresh", 0.5, True)]
    t = to_text(digest(store))
    assert "web" in t and "+$4.00" in t and "-$1.00" in t and "new" in t and "same" not in t


def test_models_show_their_share_of_the_spend(store):
    call(store, 2, 3.0, model="big")
    call(store, 3, 1.0, model="small")
    ms = digest(store)["models"]
    assert [(m["name"], round(m["share"], 2)) for m in ms] == [("openai/big", 0.75), ("openai/small", 0.25)]


def test_the_shadow_runs_own_spend_is_shown_apart_and_never_listed_as_a_project_mover(store):
    call(store, 2, 1.0)
    call(store, 2, 0.4, project=SHADOW_PROJECT)
    d = digest(store)
    assert abs(d["tooling_cost"] - 0.4) < 1e-9 and d["cost"] == 1.4 and all(p["name"] != SHADOW_PROJECT for p in d["projects"])
    assert "ModelSpend's own shadow and eval runs: $0.40" in to_text(d)


# ---------------------------------------------------------------- what is flagged
def flags(store, **kw):
    return digest(store, **kw)["flags"]


def test_a_spike_is_flagged_only_when_it_is_both_large_and_not_trivial(store):
    call(store, 2, 3.0)
    call(store, 30, 1.0)
    assert any("3.0x the previous 24 h" in f for f in flags(store))
    s2 = Store(":memory:")
    call(s2, 2, 0.30)
    call(s2, 30, 0.10)  # three times, but 20 cents
    assert not any("x the previous" in f for f in flags(s2))
    s3 = Store(":memory:")
    call(s3, 2, 1.0)
    call(s3, 30, 0.9)
    assert not flags(s3)


def test_spend_after_a_silent_period_is_flagged(store):
    call(store, 2, 2.0)
    assert any("no spend in the previous 24 h, then $2.00" in f for f in flags(store))


def test_unpriced_calls_make_the_total_partial_and_say_how_to_fix_it(store):
    call(store, 2, 1.0)
    call(store, 3, None, model="mystery")
    d = digest(store)
    assert not d["complete"] and any("no price" in f and "catalog price" in f for f in d["flags"]) and "partial" in to_text(d)


def test_a_high_failure_rate_is_flagged_but_a_few_failures_are_not(store):
    for _ in range(6):
        call(store, 2, 0.1)
    for _ in range(6):
        call(store, 3, 0.0, status="failed")
    assert any("6 of 12 requests did not produce an answer" in f for f in flags(store))
    s2 = Store(":memory:")
    for _ in range(20):
        call(s2, 2, 0.1)
    call(s2, 3, 0.0, status="failed")
    assert not any("did not produce" in f for f in flags(s2))


def test_heavy_overhead_is_flagged(store):
    call(store, 2, 1.0, tokens=1000)  # one request whose first two attempts were superseded by a third
    rid = f"r{_n[0]}"
    for idx in (1, 2):
        store.db.execute("insert into attempts(request_id, idx, kind, purpose, provider, model, outcome, input_tokens, cache_read, cache_write, output_tokens, reasoning_tokens, cost, cost_basis) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (rid, idx, "generation", "retry", "openai", "gpt-x", "ok", 1000, 0, 0, 10, 0, 1.0, "reported_usage"))
    store.db.commit()
    assert any("67% of tokens went on retries, failovers" in f for f in flags(store))
    clean = Store(":memory:")
    for _ in range(3):
        call(clean, 2, 1.0)
    assert not any("retries" in f for f in flags(clean))


def test_budgets_near_or_over_their_limit_are_listed_and_healthy_ones_are_not(store, monkeypatch):
    monkeypatch.setattr(budgets, "status", lambda st, now=None: [
        {"scope": "web", "period": "monthly", "state": "warning", "pct_usd": 0.82, "pct_tokens": None, "spent_usd": 41.0, "limit_usd": 50.0, "resets_at_text": "2026-11-01"},
        {"scope": "batch", "period": "daily", "state": "ok", "pct_usd": 0.1, "pct_tokens": None, "spent_usd": 1.0, "limit_usd": 10.0, "resets_at_text": "x"}])
    d = digest(store)
    assert [b["scope"] for b in d["budgets"]] == ["web"]
    assert "web (monthly): WARNING at 82%, $41.00 of $50.00, resets 2026-11-01" in to_text(d)


# ---------------------------------------------------------------- the webhook
def test_a_webhook_must_be_https_or_local_and_carry_no_credentials_or_query():
    for bad in ("http://hooks.example.com/x", "ftp://x/y", "https://user:pw@hooks.example.com/x", "https://hooks.example.com/x?token=1", "hooks.example.com/x", ""):
        with pytest.raises(ValueError):
            validate_webhook(bad)
    assert validate_webhook("https://hooks.slack.com/services/T0/B0/xyz") == "https://hooks.slack.com/services/T0/B0/xyz"
    assert validate_webhook("http://127.0.0.1:9000/hook") == "http://127.0.0.1:9000/hook"


def test_each_format_has_the_shape_its_service_expects(store):
    call(store, 2, 1.0)
    d = digest(store)
    t = to_text(d)
    assert payload("slack", t, d) == {"text": "```\n" + t + "\n```"}
    assert set(payload("discord", t, d)) == {"content"} and len(payload("discord", "x" * 5000, d)["content"]) <= 2000
    j = payload("json", t, d)
    assert j["text"] == t and j["digest"]["cost"] == 1.0


def fake_post(status=200, record=None):
    def f(url, json=None, timeout=None):
        if record is not None:
            record.append((url, json, timeout))
        return SimpleNamespace(status_code=status)
    return f


def test_a_post_sends_one_request_with_a_timeout_and_nothing_else(store):
    call(store, 2, 1.0)
    d = digest(store)
    seen: list = []
    post("https://hooks.slack.com/services/T/B/X", "slack", to_text(d), d, post_fn=fake_post(200, seen))
    assert len(seen) == 1 and seen[0][0] == "https://hooks.slack.com/services/T/B/X" and seen[0][2] == 10.0 and "text" in seen[0][1]


def test_errors_never_contain_the_webhook_address(store):
    d = digest(store)
    secret = "https://hooks.slack.com/services/T0SECRET/B0SECRET/xyzSECRET"
    for status in (400, 404, 500, 302):
        with pytest.raises(DigestError) as e:
            post(secret, "slack", "t", d, post_fn=fake_post(status))
        assert "SECRET" not in str(e.value) and str(status) in str(e.value)

    def boom(url, json=None, timeout=None):
        raise ConnectionError(f"cannot reach {url}")
    with pytest.raises(DigestError) as e:
        post(secret, "slack", "t", d, post_fn=boom)
    assert "SECRET" not in str(e.value) and "ConnectionError" in str(e.value)
    with pytest.raises(DigestError, match="format"):
        post(secret, "teams", "t", d, post_fn=fake_post())


def test_redirects_are_not_followed(store, monkeypatch):
    import httpx
    seen = {}
    real = httpx.post

    def spy(url, **kw):
        seen.update(kw)
        raise httpx.ConnectError("no network in tests")
    monkeypatch.setattr(httpx, "post", spy)
    with pytest.raises(DigestError):
        post("https://hooks.slack.com/services/T/B/X", "slack", "t", digest(store), timeout=3.0)
    assert seen["follow_redirects"] is False and seen["timeout"] == 3.0
    assert real is not spy


# ---------------------------------------------------------------- configuration and the command
def env(monkeypatch, home, **extra):
    monkeypatch.setenv("MODELSPEND_HOME", str(home))
    for k in ("MODELSPEND_DIGEST_WEBHOOK", "MODELSPEND_DIGEST_FORMAT"):
        monkeypatch.delenv(k, raising=False)
    for k, v in extra.items():
        monkeypatch.setenv(k, v)


def test_the_webhook_comes_from_settings_or_the_environment_and_is_validated(tmp_path, monkeypatch):
    env(monkeypatch, tmp_path)
    assert Config.from_env().digest_webhook is None and Config.from_env().digest_format == "slack"
    (tmp_path / "settings.json").write_text(json.dumps({"digest": {"webhook": "https://hooks.slack.com/services/A/B/C", "format": "discord"}}))
    c = Config.from_env()
    assert c.digest_webhook.endswith("/A/B/C") and c.digest_format == "discord" and "hooks.slack.com" not in repr(c)  # never shown in a repr or a log line
    monkeypatch.setenv("MODELSPEND_DIGEST_WEBHOOK", "https://discord.com/api/webhooks/1/tok")
    assert Config.from_env().digest_webhook.startswith("https://discord.com")
    (tmp_path / "settings.json").write_text(json.dumps({"digest": {"webhook": "http://insecure.example.com/x"}}))
    monkeypatch.delenv("MODELSPEND_DIGEST_WEBHOOK")
    with pytest.raises(ValueError, match="https"):
        Config.from_env()
    (tmp_path / "settings.json").write_text(json.dumps({"digest": {"format": "teams"}}))
    with pytest.raises(ValueError, match="slack, discord, json"):
        Config.from_env()
    (tmp_path / "settings.json").write_text(json.dumps({"digest": "https://x"}))
    with pytest.raises(ValueError, match="object"):
        Config.from_env()


def test_the_command_prints_locally_and_makes_no_network_call_without_post(tmp_path, monkeypatch):
    env(monkeypatch, tmp_path, MODELSPEND_DIGEST_WEBHOOK="https://hooks.slack.com/services/A/B/C")
    monkeypatch.setattr(socket.socket, "connect", lambda *a, **k: (_ for _ in ()).throw(AssertionError("the digest used the network")))
    r = runner.invoke(app, ["digest"])
    assert r.exit_code == 0 and "ModelSpend digest" in r.output and "Traceback" not in r.output
    j = runner.invoke(app, ["digest", "--period", "week", "--format", "json"])
    assert json.loads(j.output)["period"] == "week"
    assert runner.invoke(app, ["digest", "--period", "year"]).exit_code == 2


def test_post_without_a_webhook_is_refused_with_the_setting_to_add(tmp_path, monkeypatch):
    env(monkeypatch, tmp_path)
    r = runner.invoke(app, ["digest", "--post"])
    assert r.exit_code == 2 and "no webhook configured" in r.output


def test_posting_leaves_project_names_out_unless_asked(tmp_path, monkeypatch):
    env(monkeypatch, tmp_path, MODELSPEND_DIGEST_WEBHOOK="https://hooks.slack.com/services/A/B/C")
    st = Store(tmp_path / "modelspend.db")
    import time
    now = time.time()
    st.db.execute("insert into requests(id, ts, provider, model, status, project, task, profile) values ('p1', ?, 'openai', 'm', 'ok', 'secret-client-x', 'other', 'balanced')", (now - 60,))
    st.db.execute("insert into attempts(request_id, idx, kind, purpose, provider, model, outcome, input_tokens, cache_read, cache_write, output_tokens, reasoning_tokens, cost, cost_basis) values ('p1',0,'generation','answer','openai','m','ok',100,0,0,10,0,2.5,'reported_usage')")
    st.db.commit()
    st.db.close()
    sent: list = []
    monkeypatch.setattr("httpx.post", lambda url, **kw: (sent.append(kw["json"]), SimpleNamespace(status_code=200))[1])
    r = runner.invoke(app, ["digest", "--post"])
    assert r.exit_code == 0 and "secret-client-x" in r.output.split("posted")[0]  # printed on your own screen
    assert "secret-client-x" not in json.dumps(sent[0]) and "Project names left out" in sent[0]["text"]
    sent.clear()
    r2 = runner.invoke(app, ["digest", "--post", "--include-projects"])
    assert "secret-client-x" in json.dumps(sent[0]) and r2.exit_code == 0
    assert "hooks.slack.com" not in r.output and "hooks.slack.com" not in r2.output


def test_a_failed_post_exits_1_and_does_not_leak_the_address(tmp_path, monkeypatch):
    env(monkeypatch, tmp_path, MODELSPEND_DIGEST_WEBHOOK="https://hooks.slack.com/services/TSECRET/B/C")
    monkeypatch.setattr("httpx.post", lambda url, **kw: SimpleNamespace(status_code=404))
    r = runner.invoke(app, ["digest", "--post"])
    assert r.exit_code == 1 and "HTTP 404" in r.output and "SECRET" not in r.output


def test_the_json_format_carries_no_project_names_either_unless_asked(tmp_path, monkeypatch):
    env(monkeypatch, tmp_path, MODELSPEND_DIGEST_WEBHOOK="http://127.0.0.1:9/hook", MODELSPEND_DIGEST_FORMAT="json")
    st = Store(tmp_path / "modelspend.db")
    import time
    st.db.execute("insert into requests(id, ts, provider, model, status, project, task, profile) values ('p1', ?, 'openai', 'm', 'ok', 'secret-client-x', 'other', 'balanced')", (time.time() - 60,))
    st.db.execute("insert into attempts(request_id, idx, kind, purpose, provider, model, outcome, input_tokens, cache_read, cache_write, output_tokens, reasoning_tokens, cost, cost_basis) values ('p1',0,'generation','answer','openai','m','ok',100,0,0,10,0,2.5,'reported_usage')")
    st.db.commit()
    st.db.close()
    sent: list = []
    monkeypatch.setattr("httpx.post", lambda url, **kw: (sent.append(kw["json"]), SimpleNamespace(status_code=200))[1])
    runner.invoke(app, ["digest", "--post"])
    assert sent[0]["digest"]["projects"] == [] and "secret-client-x" not in json.dumps(sent[0])
    runner.invoke(app, ["digest", "--post", "--include-projects"])
    assert [p["name"] for p in sent[1]["digest"]["projects"]] == ["secret-client-x"]


def test_calls_from_before_a_model_was_priced_do_not_say_to_price_it_again(store):
    from modelspend.catalog import Catalog
    from test_whatif import _m
    call(store, 2, 1.0)
    call(store, 3, None, model="early")
    call(store, 4, None, model="never")
    cat = Catalog.load(overrides={"add_models": [dict(_m("openai", "early", 1.0, 2.0), tier=1)]})
    d = build_digest(store, now=NOW, catalog=cat)
    assert any("1 call(s) used a model with no price" in f and "catalog price" in f for f in d["flags"])
    assert any("1 earlier call(s) ran before their model was priced" in f and "new calls are costed" in f for f in d["flags"])
    d2 = build_digest(store, now=NOW, catalog=Catalog.load(overrides={"add_models": [dict(_m("openai", "early", 1.0, 2.0), tier=1), dict(_m("openai", "never", 1.0, 2.0), tier=1)]}))
    assert not any("catalog price" in f for f in d2["flags"]) and sum("earlier call(s)" in f for f in d2["flags"]) == 1 and "2 earlier call(s)" in "".join(d2["flags"])
