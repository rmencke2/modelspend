"""`catalog remove`: take back models you added, without ever touching shipped ones and without leaving a catalog that fails to load."""
from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from modelspend.catalog import Catalog
from modelspend.cli import app
from modelspend.config import Config
from modelspend.overrides import remove_added_models

from test_whatif import _m

runner = CliRunner()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("MODELSPEND_HOME", str(tmp_path))
    adds = [_m("openrouter", "openai/gpt-4.1", 2.0, 8.0), _m("openrouter", "openai/gpt-4.1:batch", 1.0, 4.0), _m("openrouter", "anthropic/claude:batch", 1.5, 7.5),
            _m("openai", "mine", 0.5, 1.0)]
    ov = {"add_models": adds, "models": {"openrouter/openai/gpt-4.1:batch": {"tier": 1}, "openrouter/openai/gpt-4.1": {"tier": 2}}}
    (tmp_path / "overrides.json").write_text(json.dumps(ov))
    return tmp_path


def refs(home):
    return sorted(m.ref for m in Catalog.from_config(Config.from_env()).models if m.provider in ("openrouter", "openai"))


def test_a_dry_run_changes_nothing_and_says_what_it_would_do(home):
    before = (home / "overrides.json").read_text()
    r = runner.invoke(app, ["catalog", "remove", "openrouter/*:batch"])
    assert r.exit_code == 0 and "would remove 2 model(s)" in r.output and "dry run" in r.output
    assert (home / "overrides.json").read_text() == before and not (home / "overrides.json.bak").exists()


def test_removing_by_pattern_takes_only_the_matches_and_their_patches_and_the_catalog_still_loads(home):
    r = runner.invoke(app, ["catalog", "remove", "openrouter/*:batch", "--yes"])
    assert r.exit_code == 0 and "removed 2 model(s)" in r.output
    assert "openrouter/openai/gpt-4.1" in refs(home) and "openrouter/openai/gpt-4.1:batch" not in refs(home) and "openai/mine" in refs(home)
    ov = json.loads((home / "overrides.json").read_text())
    assert list(ov["models"]) == ["openrouter/openai/gpt-4.1"]  # the batch patch went too: a patch for a vanished model would break loading
    assert (home / "overrides.json.bak").exists() and len(json.loads((home / "overrides.json.bak").read_text())["add_models"]) == 4


def test_shipped_models_are_never_removed_and_a_miss_is_an_error(home):
    shipped = next(m.ref for m in Catalog.load().models)
    r = runner.invoke(app, ["catalog", "remove", shipped, "--yes"])
    assert r.exit_code == 1 and "shipped model" in r.output and "models disable" in r.output
    assert shipped in {m.ref for m in Catalog.from_config(Config.from_env()).models}
    miss = runner.invoke(app, ["catalog", "remove", "nothing/here", "--yes"])
    assert miss.exit_code == 1 and "no model you added matches" in miss.output


def test_a_removal_that_would_break_the_catalog_is_undone(home, monkeypatch):
    import modelspend.overrides as ovm
    real = ovm.Catalog.from_config
    calls = {"n": 0}

    def flaky(cfg):
        calls["n"] += 1
        if calls["n"] > 1:  # the first call lists shipped models; the second is the post-removal validation
            raise ValueError("boom")
        return real(cfg)
    monkeypatch.setattr(ovm.Catalog, "from_config", staticmethod(flaky))
    before = (home / "overrides.json").read_text()
    with pytest.raises(ValueError, match="nothing was changed"):
        remove_added_models(Config.from_env(), ["openai/mine"], apply=True)
    assert json.loads((home / "overrides.json").read_text()) == json.loads(before)


def test_aliases_pointing_at_a_removed_model_are_reported_and_empty_input_is_refused(home):
    (home / "settings.json").write_text(json.dumps({"aliases": {"cheap": "openai/mine"}}))
    r = runner.invoke(app, ["catalog", "remove", "openai/mine", "--yes"])
    assert r.exit_code == 0 and "aliases point at a removed model" in r.output and "cheap" in r.output
    assert runner.invoke(app, ["catalog", "remove", " "]).exit_code == 2
