import pytest

from modelspend import RouteRequest
from modelspend.sandbox import SandboxUnavailable, docker_command, run_python_tests
from modelspend.types import ValidatorSpec
from modelspend.validation import run_spec, validate


def spec(**kw):
    return ValidatorSpec(**kw)


def test_deterministic_validators():
    assert run_spec(spec(type="allowed_labels", labels=["Billing", "Bug"]), " bug. ").passed
    assert not run_spec(spec(type="allowed_labels", labels=["a"]), "b").passed
    assert run_spec(spec(type="required_fields", fields=["a", "b"]), '{"a":1,"b":2}').passed
    assert not run_spec(spec(type="required_fields", fields=["a", "b"]), '{"a":1}').passed
    assert run_spec(spec(type="json_schema", schema={"type": "object", "required": ["a"]}), '```json\n{"a": 1}\n```').passed
    assert not run_spec(spec(type="json_schema", schema={"type": "object", "required": ["a"]}), "{}").passed
    assert not run_spec(spec(type="valid_json"), "nope").passed
    assert run_spec(spec(type="max_chars", value=5), "abc").passed and not run_spec(spec(type="max_chars", value=2), "abc").passed
    assert run_spec(spec(type="contains", substrings=["Paris"]), "it is paris").passed
    assert run_spec(spec(type="regex", pattern=r"^\d+$"), "123").passed
    assert run_spec(spec(type="json_equals", expected={"a": 1}), '{"a":1}').passed


def test_misconfigured_and_unknown_validators_fail_closed():
    assert not run_spec(spec(type="contains"), "x").passed
    assert not run_spec(spec(type="bogus"), "x").passed


def test_python_syntax_parses_but_never_executes(tmp_path):
    marker = tmp_path / "pwned"
    code = f"import pathlib\npathlib.Path({str(marker)!r}).write_text('x')\n"
    assert run_spec(spec(type="python_syntax"), code).passed
    assert not marker.exists()  # parsed only
    assert not run_spec(spec(type="python_syntax"), "def (:").passed


def test_python_tests_refuse_without_sandbox(tmp_path):
    marker = tmp_path / "pwned"
    code = f"import pathlib\npathlib.Path({str(marker)!r}).write_text('x')\n"
    r = run_spec(spec(type="python_tests", tests="assert True"), code, sandbox_runner=None)
    assert not r.passed and "never run on the host" in r.detail and not marker.exists()


def test_sandbox_command_is_isolated():
    cmd = docker_command("/w")
    for flag in ("--network", "none", "--read-only", "--cap-drop", "ALL", "--pids-limit", "--memory"):
        assert flag in cmd
    assert "-v" in cmd and cmd[cmd.index("-v") + 1].endswith(":/work:ro")


def test_sandbox_unavailable_without_docker(monkeypatch):
    monkeypatch.setattr("modelspend.sandbox.shutil.which", lambda _: None)
    with pytest.raises(SandboxUnavailable):
        run_python_tests("x=1", {"tests": "assert True"})


async def test_callable_validator_and_statuses():
    r = RouteRequest(providers=["openai"], messages=[{"role": "user", "content": "x"}],
                     callable_validators=[lambda t: (t == "ok", "must be ok")])
    assert (await validate(r, "ok")).status == "passed"
    rep = await validate(r, "no")
    assert rep.status == "failed" and rep.results[0].detail == "must be ok"
    plain = RouteRequest(providers=["openai"], messages=[{"role": "user", "content": "x"}])
    assert (await validate(plain, "z")).status == "not_configured"


async def test_valid_format_note_when_criteria_unverified():
    r = RouteRequest(providers=["openai"], messages=[{"role": "user", "content": "x"}], acceptance_criteria=["be correct"],
                     validators=[{"type": "valid_json"}])
    rep = await validate(r, "{}")
    assert rep.status == "passed" and "not individually verified" in rep.note
