"""0.5: your own evidence bars (thresholds), your own eval cases (check/template/dataset), and the evidence verdict table."""
from __future__ import annotations

import json
import random

import pytest
from fastapi.testclient import TestClient

from conftest import make_engine
from modelspend import RouteRequest
from modelspend import evidence as evmod
from modelspend.catalog.models import EvidenceItem, wilson_lower_bound
from modelspend.config import Config
from modelspend.errors import NoEligibleModel
from modelspend.eval.harness import calibrate, lint_tasks, load_tasks, task_from_json
from modelspend.policy import (PROFILES, Threshold, assess, bar_for, judge_evidence, min_samples_for, parse_threshold, parse_thresholds)
from modelspend.server.app import create_app
from modelspend.types import Profile, TaskCategory

CLS, EXT = TaskCategory.classification, TaskCategory.extraction


def ev(passes, n, dataset="my-tickets"):
    return EvidenceItem(n=n, passes=passes, dataset=dataset)


# ============================================================================================ parsing
def test_min_samples_for_is_the_smallest_n_where_a_perfect_record_clears():
    for rate in (0.5, 0.8, 0.9, 0.95, 0.99):
        n = min_samples_for(rate)
        assert wilson_lower_bound(n, n) >= rate and wilson_lower_bound(n - 1, n - 1) < rate
    assert min_samples_for(0.95) == 73 and min_samples_for(0.9) == 35


@pytest.mark.parametrize("cat,raw", [
    ("nonsense", {"min_n": 5}), ("unknown", {"min_n": 5}), ("code", {}), ("code", {"min_n": 0}), ("code", {"min_n": True}),
    ("code", {"min_n": 2.5}), ("code", {"min_n": 10**9}), ("code", {"min_pass_rate": 0}), ("code", {"min_pass_rate": 1.2}),
    ("code", {"min_pass_rate": "high"}), ("code", {"min_pass_rate": 0.9999}), ("code", {"basis": "vibes", "min_pass_rate": 0.9}),
    ("code", {"require_evidence": "yes"}), ("code", {"min_n": 5, "bogus": 1}),
])
def test_parse_threshold_rejects_bad_input(cat, raw):
    with pytest.raises(ValueError):
        parse_threshold(cat, raw)


def test_parse_threshold_accepts_good_input_and_normalizes():
    assert parse_threshold("Extraction", {"min_n": 50, "min_pass_rate": 0.95}) == ("extraction", Threshold(50, 0.95, None, None))
    assert parse_threshold("*", {"require_evidence": True})[1].require_evidence is True
    assert parse_threshold("code", {"min_pass_rate": 1.0, "basis": "observed"})[1].min_pass_rate == 1.0  # a plain 100% bar is fine when observed
    assert parse_thresholds(None) == {} and set(parse_thresholds({"code": {"min_n": 5}, "*": {"min_pass_rate": 0.7}})) == {"code", "*"}
    with pytest.raises(ValueError):
        parse_thresholds({"code": 5})


# ============================================================================================ resolving the bar
def test_bar_falls_back_field_by_field_category_then_star_then_profile():
    bal = PROFILES[Profile.balanced]
    assert bar_for(bal, EXT, None).describe() == "n >= 20 and 95% lower bound >= 0.8" and bar_for(bal, EXT, None).source == "profile"
    th = parse_thresholds({"*": {"min_pass_rate": 0.9, "min_n": 30}, "extraction": {"min_pass_rate": 0.97}})
    b = bar_for(bal, EXT, th)
    assert (b.n, b.rate) == (30, 0.97) and b.source == "settings: extraction, *"  # rate from the category, n from '*'
    b2 = bar_for(bal, CLS, th)
    assert (b2.n, b2.rate) == (30, 0.9) and b2.source == "settings: *"
    assert bar_for(bal, CLS, parse_thresholds({"extraction": {"min_n": 99}})).source == "profile"  # other categories untouched


def test_a_setting_can_only_tighten_evidence_required_never_loosen_it():
    ha = PROFILES[Profile.high_assurance]
    assert bar_for(ha, CLS, None).require_evidence is True
    assert bar_for(ha, CLS, parse_thresholds({"classification": {"require_evidence": False}})).require_evidence is True
    assert bar_for(PROFILES[Profile.balanced], CLS, parse_thresholds({"classification": {"require_evidence": True}})).require_evidence is True


# ============================================================================================ the verdicts
def test_judge_evidence_verdicts():
    bar = bar_for(PROFILES[Profile.balanced], CLS, parse_thresholds({"classification": {"min_n": 20, "min_pass_rate": 0.9}}))
    assert judge_evidence(None, bar)[0] == "none" and judge_evidence(ev(0, 0), bar)[0] == "none"
    assert judge_evidence(ev(9, 10), bar)[0] == "needs_more_cases"
    v, detail, _ = judge_evidence(ev(20, 20), bar)  # perfect at n=20 gives lb 0.84 < 0.9: cannot clear
    assert v == "bar_unreachable" and "needs at least 35 cases" in detail
    assert judge_evidence(ev(35, 35), bar)[0] == "clears"
    assert judge_evidence(ev(40, 50), bar)[0] == "below_bar"
    obs = bar_for(PROFILES[Profile.balanced], CLS, parse_thresholds({"classification": {"min_n": 10, "min_pass_rate": 0.9, "basis": "observed"}}))
    assert judge_evidence(ev(9, 10), obs)[0] == "clears" and judge_evidence(ev(8, 10), obs)[0] == "below_bar"  # plain pass rate


def test_default_behaviour_is_unchanged_without_settings(catalog):
    m = catalog.get("openai/synthetic-openai-small")
    m.evidence = {"classification": ev(19, 20)}  # lb 0.76 < 0.80
    s = assess(m, CLS, Profile.balanced, False)
    assert not s.suitable and s.basis == "measured" and "lower bound 0.76 < 0.8" in s.detail
    m.evidence = {"classification": ev(20, 20)}
    s = assess(m, CLS, Profile.balanced, False)
    assert s.suitable and s.basis == "measured" and "95% lower bound 0.84 >= 0.8" in s.detail and "[bar from" not in s.detail


def test_assess_with_your_own_bars(catalog):
    m = catalog.get("openai/synthetic-openai-small")  # tier 1: fine for classification by its prior
    m.evidence = {"classification": ev(18, 20)}
    strict = parse_thresholds({"classification": {"min_pass_rate": 0.95, "min_n": 10, "require_evidence": True}})
    s = assess(m, CLS, Profile.balanced, False, strict)
    assert not s.suitable and s.basis == "none" and "your settings require measured evidence" in s.detail and "needs at least 73 cases" in s.detail
    lax = parse_thresholds({"classification": {"min_pass_rate": 0.85, "min_n": 10, "basis": "observed"}})
    s = assess(m, CLS, Profile.balanced, False, lax)
    assert s.suitable and s.basis == "measured" and "observed 0.90 >= 0.85" in s.detail and "[bar from settings: classification]" in s.detail
    no_proof_needed = parse_thresholds({"classification": {"min_pass_rate": 0.95, "min_n": 10}})  # unreachable at n=20, priors still allowed
    s = assess(m, CLS, Profile.balanced, False, no_proof_needed)
    assert s.suitable and s.basis == "prior" and "cannot demonstrate" in s.detail


def test_evidence_table_and_router_can_never_disagree(catalog, cfg):
    """Property: the verdict shown to the user is 'clears' exactly when the router treats the model as measured-suitable."""
    rng = random.Random(7)
    m = catalog.get("openai/synthetic-openai-small")
    for _ in range(300):
        n = rng.randint(1, 120)
        m.evidence = {"classification": ev(rng.randint(0, n), n)}
        raw = {k: v for k, v in (("min_n", rng.choice([None, 5, 20, 60])), ("min_pass_rate", rng.choice([None, 0.7, 0.9, 0.95])),
                                 ("basis", rng.choice([None, "observed", "lower_bound"]))) if v is not None}
        cfg.thresholds = parse_thresholds({"classification": raw}) if raw else {}
        prof = rng.choice(list(Profile))
        s = assess(m, CLS, prof, False, cfg.thresholds)
        row = next(r for r in evmod.build(catalog, cfg, prof.value, "classification", "openai/synthetic-openai-small")["rows"])
        assert (row["verdict"] == "clears") == (s.suitable and s.basis == "measured"), (n, m.evidence, raw, prof)


# ============================================================================================ routing uses them
def R(**kw):
    kw.setdefault("providers", ["openai"])
    return RouteRequest(messages=[{"role": "user", "content": "Classify: great"}], task_type="classification", **kw)


async def test_router_honours_require_evidence_then_picks_a_model_that_has_it(catalog, cfg, store):
    cfg.thresholds = parse_thresholds({"classification": {"min_n": 10, "min_pass_rate": 0.8, "require_evidence": True}})
    eng, ad = make_engine(catalog, cfg, {"openai": "positive"}, store=store)
    with pytest.raises(NoEligibleModel) as ei:  # priors alone no longer qualify anything
        await eng.run(R())
    assert "your settings require measured evidence" in str(ei.value) and not ad["openai"].calls
    catalog.get("openai/synthetic-openai-mid").evidence = {"classification": ev(30, 30)}
    res = await eng.run(R())
    assert res.model == "synthetic-openai-mid"  # the only model with proof, even though cheaper ones exist
    assert "measured" in res.explanation and "[bar from settings: classification]" in res.explanation


async def test_a_stricter_bar_demotes_a_model_the_default_bar_accepts(catalog, cfg, store):
    small = catalog.get("openai/synthetic-openai-small")
    small.evidence = {"classification": ev(40, 50)}  # 80% observed, lb 0.67: below even the default bar of 0.80
    eng, _ = make_engine(catalog, cfg, {"openai": "positive"}, store=store)
    plan = await eng.plan(R())
    assert all(c.model != "synthetic-openai-small" for c in plan.candidates)  # evidence says no, despite a tier-1 prior
    small.evidence = {"classification": ev(50, 50)}  # lb 0.93
    assert any(c.model == "synthetic-openai-small" for c in (await eng.plan(R())).candidates)
    cfg.thresholds = parse_thresholds({"classification": {"min_n": 30, "min_pass_rate": 0.95}})  # n=50 cannot show 0.95; falls back to prior
    plan2 = await eng.plan(R())
    small_c = next(c for c in plan2.candidates if c.model == "synthetic-openai-small")
    assert small_c.suitability_basis == "prior" and "cannot demonstrate" in small_c.suitability_detail


# ============================================================================================ config
def test_settings_load_validate_and_fail_loudly(tmp_path, monkeypatch):
    monkeypatch.setenv("MODELSPEND_HOME", str(tmp_path))
    (tmp_path / "settings.json").write_text(json.dumps({"thresholds": {"extraction": {"min_n": 50, "min_pass_rate": 0.95}}}))
    th = Config.from_env().thresholds
    assert th["extraction"].min_n == 50 and th["extraction"].min_pass_rate == 0.95
    (tmp_path / "settings.json").write_text(json.dumps({"thresholds": {"extraction": {"min_pass_rate": 95}}}))  # 95 instead of 0.95: must not be ignored
    with pytest.raises(ValueError) as ei:
        Config.from_env()
    assert "settings.json" in str(ei.value) and "min_pass_rate" in str(ei.value)


# ============================================================================================ your own eval cases
CASE = {"id": "t1", "category": "classification", "messages": [{"role": "user", "content": "Classify: refund me. billing|bug|other"}],
        "validators": [{"type": "allowed_labels", "labels": ["billing", "bug", "other"]}], "reference": "billing", "wrong": "I think it is a billing issue, maybe."}


def write(tmp_path, *cases, name="cases.jsonl"):
    p = tmp_path / name
    p.write_text("\n".join(c if isinstance(c, str) else json.dumps(c) for c in cases) + "\n")
    return p


def test_minimal_cases_load_from_a_file_a_folder_or_the_split_layout(tmp_path):
    minimal = {k: CASE[k] for k in ("id", "category", "messages", "validators")}
    f = write(tmp_path, minimal)
    t = load_tasks("calibration", f)[0]
    assert t.difficulty == 1 and t.reference == "" and t.wrong == "" and t.id == "t1"  # optional fields default
    assert len(load_tasks("heldout", tmp_path)) == 1  # a folder of .jsonl files: used for whichever split is asked
    (tmp_path / "lay" / "calibration").mkdir(parents=True)
    (tmp_path / "lay" / "heldout").mkdir()
    write(tmp_path / "lay" / "calibration", minimal, name="a.jsonl")
    write(tmp_path / "lay" / "heldout", {**minimal, "id": "h1"}, name="a.jsonl")
    assert [t.id for t in load_tasks("calibration", tmp_path / "lay")] == ["t1"] and [t.id for t in load_tasks("heldout", tmp_path / "lay")] == ["h1"]
    with pytest.raises(ValueError):
        load_tasks("calibration", tmp_path / "nothing-here")


def test_loader_errors_name_the_file_and_line(tmp_path):
    for body, needle in ((['{"id": "x"'], "a.jsonl:1: not valid JSON"), ([json.dumps({"id": "x"})], "missing category, messages, validators"),
                         ([json.dumps({**CASE, "difficulty_label": 2})], "unknown field(s) difficulty_label"), (["[1, 2]"], "must be a JSON object")):
        p = write(tmp_path, *body, name="a.jsonl")
        with pytest.raises(ValueError) as ei:
            load_tasks("calibration", p)
        assert needle in str(ei.value), (body, str(ei.value))
    assert task_from_json(CASE, "calibration", "x").id == "t1"


def test_lint_catches_cases_that_would_measure_nothing(tmp_path):
    def issues(*cases):
        return [(sev, msg) for sev, _id, msg in lint_tasks([task_from_json(c, "calibration", "x") for c in cases])]
    has = lambda sev, needle, found: any(s == sev and needle in m for s, m in found)  # noqa: E731
    assert issues(CASE) == []
    assert has("error", "duplicate id", issues(CASE, CASE))
    assert has("error", "unknown category 'poetry'", issues({**CASE, "category": "poetry"}))
    assert has("error", "no validators", issues({**CASE, "validators": []}))
    assert has("error", "unknown validator type", issues({**CASE, "validators": [{"type": "vibes"}]}))
    assert has("error", "validator misconfigured", issues({**CASE, "validators": [{"type": "allowed_labels"}]}))  # missing 'labels'
    assert has("error", "at least one user message", issues({**CASE, "messages": [{"role": "system", "content": "x"}]}))
    assert has("error", "reject this case's own `reference`", issues({**CASE, "reference": "I would say: billing"}))
    assert has("warning", "accept this case's `wrong` answer", issues({**CASE, "wrong": "bug"}))  # too lax: 'bug' is an allowed label
    assert has("error", "bad validator", issues({**CASE, "validators": [{"name": "no type"}]}))


async def test_calibrate_can_report_which_cases_failed_without_storing_anything(catalog, tmp_path):
    from modelspend.eval.sim import build_sim_engine
    tasks = [t for t in load_tasks("calibration") if t.difficulty >= 2][:4] + load_tasks("calibration")[:2]
    eng = build_sim_engine(catalog, tasks, providers=["openai"])
    failures: list = []
    evd, _s, _ = await calibrate(eng, tasks, ["openai"], None, False, "x", failures)
    assert failures and all({"model", "case", "category", "reason", "output"} <= set(f) for f in failures)
    small = "openai/synthetic-openai-small"  # tier 1: fails the tasks labelled harder than it
    assert any(f["model"] == small for f in failures)
    total_fail = sum(i.n - i.passes for cats in evd.values() for i in cats.values())
    assert len(failures) == total_fail  # every failure is listed, none invented
    assert all(len(f["output"]) <= 160 for f in failures)
    # nothing about the failures reaches the evidence overlay: only counts
    assert not any("output" in i.model_dump() for cats in evd.values() for i in cats.values())


def test_cli_show_failures_flag_is_accepted_in_mock_mode(home):
    r = cli("eval", "calibrate", "--mode", "mock", "--providers", "openai", "--show-failures")
    assert r.exit_code == 0 and "failed cases" in r.output and "->" in r.output


async def test_calibrate_stores_your_dataset_name(catalog):
    from modelspend.eval.sim import build_sim_engine
    tasks = load_tasks("calibration")[:6]
    eng = build_sim_engine(catalog, tasks, providers=["openai"])
    evd, _spent, _ = await calibrate(eng, tasks, ["openai"], None, False, "my-tickets-v2")
    items = [i for cats in evd.values() for i in cats.values()]
    assert items and {i.dataset for i in items} == {"my-tickets-v2"}


# ============================================================================================ CLI
@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("MODELSPEND_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


def cli(*args):
    from typer.testing import CliRunner

    from modelspend.cli import app
    return CliRunner().invoke(app, list(args))


def test_cli_thresholds_set_merge_list_remove(home):
    r = cli("thresholds", "set", "extraction", "--min-pass-rate", "0.95")
    assert r.exit_code == 0 and "at least 73 cases" in r.output
    assert cli("thresholds", "set", "Extraction", "--min-n", "80").exit_code == 0  # merges into the same category
    assert json.loads((home / "settings.json").read_text())["thresholds"] == {"extraction": {"min_n": 80, "min_pass_rate": 0.95}}
    lst = cli("thresholds", "list").output
    assert "extraction     n >= 80 and 95% lower bound >= 0.95   [settings: extraction]" in lst and "classification n >= 20" in lst
    assert "evidence required" in cli("thresholds", "list", "--profile", "high_assurance").output
    cli("thresholds", "set", "code", "--min-pass-rate", "0.9", "--basis", "observed", "--require-evidence")
    assert "observed pass rate >= 0.9 (evidence required" in cli("thresholds", "list").output
    for bad in (["nonsense", "--min-n", "5"], ["analysis"], ["code", "--min-pass-rate", "95"], ["code", "--basis", "vibes", "--min-pass-rate", "0.9"]):
        assert cli("thresholds", "set", *bad).exit_code == 2, bad
    assert cli("thresholds", "remove", "extraction").exit_code == 0 and cli("thresholds", "remove", "extraction").exit_code == 2
    assert "extraction" not in json.loads((home / "settings.json").read_text())["thresholds"]


def test_cli_evidence_table(home, tmp_path):
    (home).mkdir(parents=True, exist_ok=True)
    (home / "evidence.json").write_text(json.dumps({"evidence": {
        "anthropic/claude-haiku-5-5": {"classification": {"n": 40, "passes": 40, "dataset": "my-tickets", "measured_on": "2026-10-15"},
                                       "extraction": {"n": 12, "passes": 12, "dataset": "my-tickets"}},
        "anthropic/claude-sonnet-5-5": {"classification": {"n": 40, "passes": 40, "dataset": "my-tickets"}},
        "anthropic/claude-opus-5-5": {"classification": {"n": 40, "passes": 36, "dataset": "my-tickets"}}}}))
    cli("thresholds", "set", "classification", "--min-pass-rate", "0.9")
    out = cli("evidence").output
    assert "classification" in out and "claude-haiku-5-5" in out and "40/40" in out and "clears the bar" in out and "below the bar" in out  # opus 36/40
    assert "needs more cases" in out  # extraction n=12 < 20
    assert "Cheapest model that clears the bar" in out and "claude-haiku-5-5" in out.split("Cheapest model that clears the bar")[1]
    js = json.loads(cli("evidence", "--json", "--category", "classification").output)
    assert {r["model"] for r in js["rows"]} == {"anthropic/claude-haiku-5-5", "anthropic/claude-sonnet-5-5", "anthropic/claude-opus-5-5"}
    assert js["bars"]["classification"]["source"] == "settings: classification"
    assert js["cheapest_clearing"]["classification"]["model"] == "anthropic/claude-haiku-5-5"  # cheapest of the two that clear it
    assert js["cheapest_clearing"]["classification"]["clearing_models"] == ["anthropic/claude-haiku-5-5", "anthropic/claude-sonnet-5-5"]
    assert cli("evidence", "--category", "poetry").exit_code == 2 and cli("evidence", "--profile", "x").exit_code != 0
    assert "No measured evidence yet" in cli("evidence", "--model", "anthropic/claude-fable-5-1").output


def test_cli_eval_check_and_template(home, tmp_path):
    good = write(tmp_path, CASE)
    r = cli("eval", "check", "--fixtures", str(good))
    assert r.exit_code == 0 and "1 case(s)" in r.output and "too few to clear the bar (needs 20)" in r.output and r.output.strip().endswith("ok")
    bad = write(tmp_path, CASE, {**CASE, "validators": [{"type": "vibes"}]}, name="bad.jsonl")
    r = cli("eval", "check", "--fixtures", str(bad))
    assert r.exit_code == 1 and "ERROR" in r.output and "duplicate id" in r.output and "unknown validator type" in r.output
    assert cli("eval", "check", "--fixtures", str(tmp_path / "missing")).exit_code == 2
    lay = tmp_path / "lay"
    (lay / "calibration").mkdir(parents=True)
    (lay / "heldout").mkdir()
    write(lay / "calibration", CASE, name="a.jsonl")
    write(lay / "heldout", CASE, name="a.jsonl")
    r = cli("eval", "check", "--fixtures", str(lay))
    assert "calibration:" in r.output and "held-out:" in r.output and "share case ids" in r.output  # leaking cases between the sets
    t = cli("eval", "template", "extraction")
    case = json.loads(t.stdout.splitlines()[0]) if t.stdout else {}
    assert t.exit_code == 0 and case["category"] == "extraction" and case["validators"] and "difficulty" not in case
    assert cli("eval", "template", "poetry").exit_code == 2


def test_the_template_output_passes_its_own_check(home, tmp_path):
    """The example a user copies must be a valid, self-consistent case."""
    t = cli("eval", "template", "classification")
    f = tmp_path / "mine.jsonl"
    f.write_text(t.stdout.splitlines()[0] + "\n")
    assert cli("eval", "check", "--fixtures", str(f)).exit_code == 0


# ============================================================================================ server
def test_server_exposes_thresholds_and_evidence(catalog, cfg, store):
    cfg.api_token = None
    cfg.default_providers = ["openai"]
    cfg.thresholds = parse_thresholds({"classification": {"min_n": 10, "min_pass_rate": 0.9}})
    catalog.get("openai/synthetic-openai-small").evidence = {"classification": ev(40, 40)}
    eng, _ = make_engine(catalog, cfg, {}, store=store)
    c = TestClient(create_app(eng, cfg), base_url="http://127.0.0.1:8787")
    assert c.get("/v1/policy").json()["thresholds"] == {"classification": {"min_n": 10, "min_pass_rate": 0.9}}
    j = c.get("/v1/evidence?category=classification").json()
    assert j["rows"][0]["verdict"] == "clears" and j["bars"]["classification"]["source"] == "settings: classification"
    assert c.get("/v1/evidence?category=poetry").status_code == 400 and c.get("/v1/evidence?nope=1").status_code == 400
    assert TestClient(c.app, base_url="http://attacker.test:8787").get("/v1/evidence").status_code == 403
    cfg.api_token = "sekret"
    assert c.get("/v1/evidence").status_code == 401 and c.get("/v1/evidence", headers={"Authorization": "Bearer sekret"}).status_code == 200
