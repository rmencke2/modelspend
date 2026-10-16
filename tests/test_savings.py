"""0.7: the savings projection and the shadow CLI. The projection must be honest: cheaper models only 'clear' on the same bar routing uses,
the saving is scaled by a measured cost ratio, and everything that is missing is said."""
from __future__ import annotations

import json
import time

import pytest
from typer.testing import CliRunner

from modelspend.cli import app
from modelspend.config import Config, ShadowSettings
from modelspend.policy import parse_thresholds
from modelspend.savings import build_savings, to_table
from modelspend.store import Store

LARGE, SMALL, MID = "openai/gpt-4.1", "openai/gpt-4.1-nano", "openai/gpt-5-mini"


def traffic(store, category="classification", ref=LARGE, n=100, cost=1.0, project=None, status="ok", ts=None):
    for i in range(n):
        store.db.execute(
            "insert into requests(id, ts, task, provider, model, status, actual_cost, project) values (?,?,?,?,?,?,?,?)",
            (f"t-{category}-{ref}-{project}-{status}-{i}-{time.monotonic_ns()}", ts or time.time(), category, *ref.split("/"), status,
             None if cost is None else cost / n, project))
    store.db.commit()


def shadow(store, ref=SMALL, category="classification", n=40, passes=40, used_cost=1.0, cand_cost=0.1, unscored=0):
    prov, model = ref.split("/")
    for i in range(n + unscored):
        verdict = "unscored" if i >= n else ("pass" if i < passes else "fail")
        store.record_shadow(request_id=f"r{i}", project=None, agent=None, category=category, used_provider="openai", used_model="gpt-4.1",
                            cand_provider=prov, cand_model=model, method="exact", verdict=verdict, reason="x", used_cost=used_cost / (n + unscored),
                            cand_cost=cand_cost / (n + unscored), judge_cost=0.0, cand_input=10, cand_output=10)


@pytest.fixture
def cfg2(tmp_path):
    return Config(home=tmp_path)


def rep(store, cfg, **kw):
    return build_savings(store, cfg, **kw)


def only(r):
    assert len(r["rows"]) == 1
    return r["rows"][0]


def test_no_usage_says_so(cfg2):
    r = rep(Store(":memory:"), cfg2)
    assert r["rows"] == [] and r["totals"]["cost"] == 0 and "No usage recorded" in to_table(r)


def test_a_cheaper_model_that_clears_the_bar_is_recommended_with_a_measured_saving(cfg2):
    s = Store(":memory:")
    traffic(s, n=100, cost=2.0)
    shadow(s, n=40, passes=40, used_cost=1.0, cand_cost=0.1)  # 90% cheaper on the sampled requests
    row = only(rep(s, cfg2))
    b = row["recommended"]
    assert row["state"] == "ready" and b["model"] == SMALL and b["verdict"] == "clears" and b["lower_bound"] > 0.9
    assert b["cost_ratio"] == pytest.approx(0.1) and b["projected_saving_usd"] == pytest.approx(1.8)  # 2.00 spend x (1 - 0.1)
    assert row["top_model"] == LARGE and row["cost"] == pytest.approx(2.0) and row["requests"] == 100


def test_too_few_samples_is_collecting_and_says_how_many_more(cfg2):
    s = Store(":memory:")
    traffic(s)
    shadow(s, n=12, passes=12)  # balanced bar for classification needs n >= 20
    row = only(rep(s, cfg2))
    assert row["state"] == "collecting" and row["recommended"] is None
    c = row["candidates"][0]
    assert (c["verdict"], c["samples_needed"]) == ("needs_more_cases", 8)


def test_a_cheaper_model_below_the_bar_is_not_recommended(cfg2):
    s = Store(":memory:")
    traffic(s)
    shadow(s, n=40, passes=30)  # lower bound ~0.60 < 0.80
    row = only(rep(s, cfg2))
    assert row["state"] == "no_safe_cheaper_model" and row["candidates"][0]["verdict"] == "below_bar" and row["recommended"] is None


def test_lucky_streaks_do_not_qualify(cfg2):
    s = Store(":memory:")
    traffic(s)
    shadow(s, n=20, passes=20)  # perfect, but only 20: lower bound 0.84 clears 0.80 ...
    assert only(rep(s, cfg2))["state"] == "ready"
    s2 = Store(":memory:")
    traffic(s2)
    shadow(s2, n=20, passes=19)  # ... one miss does not: lower bound 0.76
    assert only(rep(s2, cfg2))["state"] == "no_safe_cheaper_model"


def test_a_model_that_costs_more_than_the_one_in_use_saves_nothing(cfg2):
    s = Store(":memory:")
    traffic(s)
    shadow(s, n=40, passes=40, used_cost=1.0, cand_cost=1.5)
    row = only(rep(s, cfg2))
    assert row["recommended"] is None and row["candidates"][0]["projected_saving_usd"] == 0 and row["state"] == "no_safe_cheaper_model"


def test_spend_already_on_the_candidate_is_not_counted_as_movable(cfg2):
    s = Store(":memory:")
    traffic(s, ref=LARGE, n=50, cost=1.0)
    traffic(s, ref=SMALL, n=50, cost=0.05)  # half the requests already use the cheap model
    shadow(s, n=40, passes=40, used_cost=1.0, cand_cost=0.1)
    b = only(rep(s, cfg2))["recommended"]
    assert b["projected_saving_usd"] == pytest.approx(1.0 * 0.9)  # only the 1.00 on the expensive model can move, not the 0.05


def test_the_best_clearing_candidate_wins_by_saving(cfg2):
    s = Store(":memory:")
    traffic(s, n=100, cost=2.0)
    shadow(s, ref=MID, n=40, passes=40, used_cost=1.0, cand_cost=0.4)
    shadow(s, ref=SMALL, n=40, passes=40, used_cost=1.0, cand_cost=0.1)
    row = only(rep(s, cfg2))
    assert row["recommended"]["model"] == SMALL and [c["model"] for c in row["candidates"]] == [SMALL, MID]


def test_unscored_comparisons_are_not_evidence(cfg2):
    s = Store(":memory:")
    traffic(s)
    shadow(s, n=5, passes=5, unscored=200)
    c = only(rep(s, cfg2))["candidates"][0]
    assert c["n"] == 5 and c["unscored"] == 200 and c["verdict"] == "needs_more_cases"


def test_modelspends_own_runs_unknown_tasks_failed_calls_and_old_traffic_are_not_your_traffic(cfg2):
    s = Store(":memory:")
    traffic(s, n=10, cost=1.0)
    traffic(s, n=10, cost=5.0, project="modelspend-eval")
    traffic(s, n=10, cost=5.0, project="modelspend-shadow")
    traffic(s, category="unknown", n=10, cost=5.0)
    traffic(s, n=10, cost=5.0, status="provider_error")
    traffic(s, n=10, cost=5.0, ts=time.time() - 90 * 86400)
    r = rep(s, cfg2, since=time.time() - 30 * 86400)
    assert only(r)["cost"] == pytest.approx(1.0) and only(r)["requests"] == 10
    assert rep(s, cfg2, since=None)["rows"][0]["cost"] == pytest.approx(6.0)  # all time includes the old traffic
    traffic(s, n=5, cost=3.0, project="shop")
    assert only(rep(s, cfg2, project="shop", since=None))["cost"] == pytest.approx(3.0)


def test_unpriced_traffic_is_flagged_as_incomplete(cfg2):
    s = Store(":memory:")
    traffic(s, n=4, cost=None)
    traffic(s, n=4, cost=1.0)
    row = only(rep(s, cfg2))
    assert row["cost_complete"] is False and row["cost"] == pytest.approx(1.0)
    assert " +" in to_table(rep(s, cfg2))


def test_your_own_thresholds_move_the_bar(cfg2):
    s = Store(":memory:")
    traffic(s)
    shadow(s, n=40, passes=40)
    assert only(rep(s, cfg2))["state"] == "ready"
    cfg2.thresholds = parse_thresholds({"classification": {"min_n": 80}})
    row = only(rep(s, cfg2))
    assert row["state"] == "collecting" and row["candidates"][0]["samples_needed"] == 40 and "n >= 80" in row["bar"]
    cfg2.thresholds = parse_thresholds({"classification": {"min_pass_rate": 0.99}})
    row = only(rep(s, cfg2))
    assert row["candidates"][0]["verdict"] == "bar_unreachable" and row["state"] == "collecting" and row["candidates"][0]["samples_needed"] > 0
    assert only(rep(s, cfg2, profile="economy"))["bar"] != row["bar"]


def test_the_table_is_honest_and_the_json_serializable(cfg2):
    s = Store(":memory:")
    traffic(s, n=100, cost=2.0)
    shadow(s, n=40, passes=40)
    r = rep(s, cfg2)
    t = to_table(r)
    for needle in ("READY", "gpt-4.1-nano", "40/40", "projection, not a measurement", "does not prove the answer is correct", "Shadow evaluation is OFF"):
        assert needle in t, needle
    assert json.loads(json.dumps(r))["totals"]["categories_ready"] == 1
    cfg2.shadow = ShadowSettings(enabled=True)
    assert "Shadow spend this month" in to_table(rep(s, cfg2))


# ---- CLI ----
@pytest.fixture
def run(tmp_path, monkeypatch):
    monkeypatch.setenv("MODELSPEND_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MODELSPEND_DB", str(tmp_path / "u.db"))
    runner = CliRunner()
    return lambda *a: runner.invoke(app, list(a))


def test_cli_shadow_settings_roundtrip_and_validation(run, tmp_path):
    r = run("shadow", "status")
    assert r.exit_code == 0 and "OFF" in r.output and "no comparisons yet" in r.output
    assert run("shadow", "on").exit_code == 0
    r = run("shadow", "set", "--rate", "0.2", "--budget", "7.5", "--max-candidates", "3", "--no-judge")
    assert "ON" in r.output and "20% of requests" in r.output and "$7.5/month" in r.output and "judge off" in r.output
    saved = json.loads((tmp_path / "home" / "settings.json").read_text())["shadow"]
    assert saved == {"enabled": True, "sample_rate": 0.2, "monthly_budget_usd": 7.5, "max_candidates": 3, "judge": False}
    bad = run("shadow", "set", "--rate", "5")
    assert bad.exit_code == 2 and "between 0 and 1" in bad.output
    assert json.loads((tmp_path / "home" / "settings.json").read_text())["shadow"]["sample_rate"] == 0.2  # an invalid change writes nothing
    assert run("shadow", "set").exit_code != 0  # nothing to change
    assert "OFF" in run("shadow", "off").output


def test_cli_status_lists_what_was_measured(run, tmp_path):
    s = Store(str(tmp_path / "u.db"))
    shadow(s, n=7, passes=5, unscored=2)
    out = run("shadow", "status").output
    assert "gpt-4.1-nano" in out and "classification" in out and "7" in out and "Passed = the cheaper model gave an answer as good" in out


def test_cli_apply_shows_first_and_only_changes_with_yes(run, tmp_path):
    s = Store(str(tmp_path / "u.db"))
    traffic(s, n=100, cost=2.0)
    r = run("shadow", "apply")
    assert "would not change any routing decision" in r.output
    shadow(s, n=40, passes=40)
    r = run("shadow", "apply")
    assert "classification" in r.output and "gpt-4.1-nano cleared the bar" in r.output and "Nothing changed" in r.output
    assert not (tmp_path / "home" / "settings.json").exists() or "use_as_evidence" not in (tmp_path / "home" / "settings.json").read_text()
    r = run("shadow", "apply", "--yes")
    assert "Applied" in r.output and json.loads((tmp_path / "home" / "settings.json").read_text())["shadow"]["use_as_evidence"] is True
    assert "Already applied" in run("shadow", "apply").output
    assert "no longer uses shadow evidence" in run("shadow", "revert").output
    assert json.loads((tmp_path / "home" / "settings.json").read_text())["shadow"]["use_as_evidence"] is False


def test_cli_savings_table_and_json(run, tmp_path):
    s = Store(str(tmp_path / "u.db"))
    traffic(s, n=100, cost=2.0)
    shadow(s, n=40, passes=40)
    assert "READY" in run("savings").output
    j = json.loads(run("savings", "--format", "json").output)
    assert j["totals"]["categories_ready"] == 1 and j["rows"][0]["recommended"]["model"] == SMALL
    assert run("savings", "--format", "xml").exit_code != 0 and run("savings", "--profile", "ultra").exit_code != 0
    assert run("savings", "--since", "soon").exit_code != 0


# ---- server ----
def _client(catalog, cfg, store):
    from conftest import make_engine
    from fastapi.testclient import TestClient
    from modelspend.server.app import create_app
    cfg.api_token = None
    cfg.default_providers = ["openai"]
    eng, _ = make_engine(catalog, cfg, {}, store=store)
    return TestClient(create_app(eng, cfg), base_url="http://127.0.0.1:8787"), eng


def test_server_savings_and_shadow_endpoints(catalog, cfg):
    s = Store(":memory:")
    traffic(s, n=100, cost=2.0)
    shadow(s, n=40, passes=40)
    c, eng = _client(catalog, cfg, s)
    j = c.get("/v1/savings").json()
    assert j["totals"]["categories_ready"] == 1 and j["rows"][0]["recommended"]["model"] == SMALL
    assert c.get("/v1/savings?profile=economy&since=7d&project=nope").json()["rows"] == []
    for bad in ("/v1/savings?profile=ultra", "/v1/savings?since=soon", "/v1/savings?nope=1"):
        assert c.get(bad).status_code == 400, bad
    st = c.get("/v1/shadow").json()
    assert st["settings"]["enabled"] is False and st["active"] is False and st["comparisons"][0]["n"] == 40 and st["spent_this_month"] == 0


def test_server_can_switch_shadow_on_and_off_without_a_restart_and_saves_it(catalog, cfg):
    s = Store(":memory:")
    c, eng = _client(catalog, cfg, s)
    assert eng.shadow is None
    hdr = {"Content-Type": "application/json"}
    r = c.put("/v1/shadow", json={"enabled": True, "sample_rate": 0.5, "monthly_budget_usd": 2}, headers=hdr)
    assert r.status_code == 200 and r.json()["active"] is True and r.json()["settings"]["sample_rate"] == 0.5
    assert eng.shadow is not None and eng.cfg.shadow.enabled
    saved = json.loads((cfg.home / "settings.json").read_text())["shadow"]
    assert saved == {"enabled": True, "sample_rate": 0.5, "monthly_budget_usd": 2}
    r = c.put("/v1/shadow", json={"enabled": False}, headers=hdr)
    assert r.json()["active"] is False and eng.shadow is not None and not eng.cfg.shadow.enabled  # sampling stopped

    for body in ({"sample_rate": 9}, {"nope": 1}, {"home": "/tmp/x"}, {"enabled": "yes"}):
        bad = c.put("/v1/shadow", json=body, headers=hdr)
        assert bad.status_code == 400, body
    assert json.loads((cfg.home / "settings.json").read_text())["shadow"]["sample_rate"] == 0.5  # an invalid change changes nothing
    assert c.put("/v1/shadow", content="not json", headers=hdr).status_code == 400
    assert c.put("/v1/shadow", content='{"enabled": true}', headers={"Content-Type": "text/plain"}).status_code in (400, 415)  # writes need JSON


def test_server_shadow_endpoints_are_local_and_token_protected(catalog, cfg):
    from fastapi.testclient import TestClient
    c, eng = _client(catalog, cfg, Store(":memory:"))
    assert TestClient(c.app, base_url="http://attacker.test:8787").get("/v1/shadow").status_code == 403
    assert TestClient(c.app, base_url="http://attacker.test:8787").get("/v1/savings").status_code == 403
    cfg.api_token = "sekret"
    assert c.get("/v1/shadow").status_code == 401 and c.get("/v1/savings").status_code == 401
    ok = {"Authorization": "Bearer sekret", "Content-Type": "application/json"}
    assert c.get("/v1/savings", headers=ok).status_code == 200 and c.put("/v1/shadow", json={"enabled": True}, headers=ok).status_code == 200


def test_an_engine_built_for_evals_never_shadows_even_when_settings_say_so(catalog, cfg):
    from conftest import make_engine
    from modelspend.engine import Engine
    cfg.shadow = ShadowSettings(enabled=True)
    base, _ = make_engine(catalog, cfg, {}, store=Store(":memory:"))
    eng = Engine(catalog, cfg, base.adapters, Store(":memory:"), shadow=False)
    eng.set_shadow(ShadowSettings(enabled=True))
    assert eng.shadow is None


def test_the_dashboard_has_a_savings_tab_wired_to_the_new_endpoints(catalog, cfg):
    c, _ = _client(catalog, cfg, Store(":memory:"))
    html = c.get("/dashboard").text
    for needle in ('id="nav-savings"', 'id="savings"', "/v1/savings", "/v1/shadow", "projection, not a measurement", "modelspend shadow apply", "loadSavings"):
        assert needle in html, needle


def test_the_live_check_example_is_valid_python_and_refuses_politely_when_shadow_is_off():
    import ast
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "examples" / "shadow_live_check.py").read_text()
    ast.parse(src)
    assert "modelspend shadow set --rate 1 --budget 0.50" in src and "wait_for_shadow" in src and "SystemExit" in src


def test_cut_offs_at_the_output_cap_are_called_out_and_not_counted_against_the_model(cfg2):
    s = Store(":memory:")
    traffic(s)
    for i in range(30):  # a reasoning model with a tight max_output_tokens: empty answers, cut off
        s.record_shadow(request_id=f"t{i}", project=None, agent=None, category="classification", used_provider="openai", used_model="gpt-4.1",
                        cand_provider="openai", cand_model="gpt-5-mini", method="none", verdict="unscored" if i < 27 else "pass", reason="truncated" if i < 27 else "exact_match",
                        used_cost=0.01, cand_cost=0.002, judge_cost=0.0, cand_input=5, cand_output=64)
    c = only(rep(s, cfg2))["candidates"][0]
    assert (c["model"], c["n"], c["passes"], c["truncated"], c["unscored"]) == (MID, 3, 3, 27, 27) and c["verdict"] == "needs_more_cases"  # not 'below the bar'
    t = to_table(rep(s, cfg2))
    assert "27 comparison(s) on classification were cut off at your max_output_tokens and are not counted" in t and "give them more room" in t


def test_cli_reset_previews_then_deletes(run, tmp_path):
    s = Store(str(tmp_path / "u.db"))
    shadow(s, n=5, passes=5)
    shadow(s, ref=MID, n=3, passes=3)
    assert "would delete 5" in run("shadow", "reset", "--model", SMALL).output and "add --yes" in run("shadow", "reset", "--model", SMALL).output
    assert "deleted 5" in run("shadow", "reset", "--model", SMALL, "--yes").output
    assert "would delete 3" in run("shadow", "reset").output
    assert "deleted 3" in run("shadow", "reset", "--yes").output and "no comparisons yet" in run("shadow", "status").output


def test_cli_status_explains_why_comparisons_passed_failed_or_went_unscored(run, tmp_path):
    s = Store(str(tmp_path / "u.db"))
    for reason, verdict, n in (("exact_match", "pass", 5), ("truncated", "unscored", 3), ("judge_unparseable", "unscored", 2), ("provider_error_rate_limit", "unscored", 1)):
        for i in range(n):
            s.record_shadow(request_id=f"{reason}{i}", project=None, agent=None, category="classification", used_provider="openai", used_model="gpt-4.1",
                            cand_provider="openai", cand_model="gpt-5-mini", method="x", verdict=verdict, reason=reason, used_cost=0.01, cand_cost=0.001,
                            judge_cost=0.0, cand_input=1, cand_output=1)
    out = run("shadow", "status").output
    assert "why:" in out and "same answer as the model you used" in out and "cut off at your max_output_tokens" in out and "judge reply could not be read" in out
    assert "rate limited" in out
