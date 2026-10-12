import asyncio
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from modelspend import Catalog
from modelspend.catalog import shipped_catalog_path
from modelspend.cli import app
from modelspend.eval import calibrate, evaluate, load_tasks, render_markdown
from modelspend.eval.harness import grade, wilson_interval
from modelspend.eval.sim import build_sim_engine
from modelspend.types import TaskCategory

runner = CliRunner()
CATS = {c.value for c in TaskCategory if c != TaskCategory.unknown}


@pytest.fixture
def synth():
    return Catalog.load(shipped_catalog_path("synthetic_test_catalog.json"))


def test_fixtures_cover_every_category_in_both_splits_and_are_disjoint():
    cal, held = load_tasks("calibration"), load_tasks("heldout")
    assert {t.category for t in cal} == CATS == {t.category for t in held}
    assert not ({t.id for t in cal} & {t.id for t in held})
    assert not ({t.prompt for t in cal} & {t.prompt for t in held})  # no leakage between splits
    for t in cal + held:
        assert t.validators and t.reference and t.difficulty in (1, 2, 3)
        assert grade(t, t.reference)[0], f"reference answer must pass its own validators: {t.id}"
        assert not grade(t, t.wrong)[0], f"wrong answer must fail: {t.id}"


def test_sandbox_validators_only_run_when_requested_and_never_on_host():
    t = next(t for t in load_tasks("heldout") if t.sandbox_validators)
    ok, fails = grade(t, t.reference, sandbox_runner=None, use_sandbox=True)
    assert not ok and any("never run on the host" in f for f in fails)
    assert grade(t, t.reference, use_sandbox=False)[0]


async def test_mock_eval_compares_three_arms(synth):
    tasks = load_tasks("heldout")
    rep = await evaluate(build_sim_engine(synth, tasks), tasks, ["openai", "anthropic", "google", "xai"])
    a = rep["arms"]
    assert rep["simulated"] and set(a) == {"premium", "cheapest_eligible", "modelspend"}
    assert all(x["tasks"] == len(tasks) for x in a.values())
    assert a["premium"]["pass_rate"] == 1.0
    assert a["cheapest_eligible"]["pass_rate"] < a["modelspend"]["pass_rate"]  # quality routing + escalation help
    assert a["modelspend"]["total_spend_usd"] < a["premium"]["total_spend_usd"]
    assert a["modelspend"]["escalation_rate"] > 0
    md = render_markdown(rep)
    assert "SIMULATED DATA" in md and "not** evidence" in md and "Cost / success" in md


async def test_eval_budget_is_a_hard_ceiling(synth):
    tasks = load_tasks("heldout")
    rep = await evaluate(build_sim_engine(synth, tasks), tasks, ["openai", "anthropic"], budget_usd=0.002)
    assert rep["total_spend_usd"] <= 0.002 and rep["stopped_early"] and rep["tasks_completed"] < len(tasks)


async def test_calibration_uses_only_calibration_split_and_counts(synth):
    tasks = load_tasks("calibration")
    ev, spent, _ = await calibrate(build_sim_engine(synth, tasks), tasks, ["openai"])
    small = ev["openai/synthetic-openai-small"]
    assert small["classification"].n == 2 and small["classification"].passes == 1  # difficulty-2 task fails on tier 1
    assert small["code"].passes == 0 and ev["openai/synthetic-openai-large"]["analysis"].passes == 2 and spent > 0


def test_wilson_interval_sane():
    lo, hi = wilson_interval(16, 16)
    assert 0.75 < lo < 0.85 and hi == pytest.approx(1.0)


def test_cli_preview_with_synthetic_catalog_json():
    r = runner.invoke(app, ["preview", "Translate this to French: hello", "--providers", "openai,google", "--catalog", "synthetic",
                            "--assume-keys", "--json"])
    j = json.loads(r.stdout[r.stdout.index("{"):])
    assert r.exit_code == 0 and j["task"]["category"] == "translation" and j["candidates"]


def test_cli_catalog_commands(tmp_path):
    assert runner.invoke(app, ["catalog", "validate", "--catalog", "synthetic"]).exit_code == 0
    r = runner.invoke(app, ["catalog", "list", "--catalog", "synthetic", "--provider", "xai"])
    assert "synthetic-xai-small" in r.stdout
    assert "claude-haiku-5-5" in runner.invoke(app, ["catalog", "list"]).stdout  # shipped Anthropic pricing snapshot
    t = runner.invoke(app, ["catalog", "template", "openai/some-model"])
    assert json.loads(t.stdout)["add_models"][0]["pricing"]["input_per_mtok"] is None
    stale = runner.invoke(app, ["catalog", "stale", "--catalog", "synthetic", "--max-age-days", "0"])
    assert "stale" in stale.stdout


def test_cli_eval_mock_and_live_guards(tmp_path):
    out = tmp_path / "r.md"
    r = runner.invoke(app, ["eval", "run", "--out", str(out)])
    assert r.exit_code == 0 and "SIMULATED" in out.read_text() and json.loads(out.with_suffix(".json").read_text())["arms"]
    r = runner.invoke(app, ["eval", "run", "--mode", "live"])
    assert r.exit_code == 2 and "--budget" in r.output
    r = runner.invoke(app, ["eval", "run", "--mode", "live", "--budget", "1"], env={"OPENAI_API_KEY": ""})
    assert r.exit_code == 2


def test_cli_route_without_keys_fails_cleanly(tmp_path):
    r = runner.invoke(app, ["route", "hi", "--providers", "openai", "--catalog", "synthetic"], env={"MODELSPEND_HOME": str(tmp_path),
                      "OPENAI_API_KEY": "", "ANTHROPIC_API_KEY": "", "GEMINI_API_KEY": "", "XAI_API_KEY": ""})
    assert r.exit_code == 2 and "no_eligible_model" in r.output
