import json
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
    cmd = docker_command("/w", "modelspend-sandbox-abc", "N0NCE")
    for flag in ("--network", "none", "--read-only", "--cap-drop", "ALL", "--pids-limit", "--memory", "--name", "modelspend-sandbox-abc"):
        assert flag in cmd
    assert "-v" in cmd and cmd[cmd.index("-v") + 1].endswith(":/work:ro")
    assert cmd[cmd.index("timeout") + 1] == "-s" and cmd[cmd.index("timeout") + 2] == "KILL"  # SIGKILL, not a catchable SIGTERM


def _run_runner_locally(tmp_path, candidate, tests, nonce="NONCE123"):
    """Executes the sandbox RUNNER with fixed test constants (NOT model output) to check its pass/fail semantics without Docker.
    The runner reads /work/...: point those paths at a temp dir for this check."""
    import subprocess, sys
    from modelspend.sandbox import RUNNER
    (tmp_path / "candidate.py").write_text(candidate)
    (tmp_path / "tests.py").write_text(tests)
    src = RUNNER.replace("/work/", str(tmp_path) + "/")
    p = subprocess.run([sys.executable, "-I", "-c", src], capture_output=True, text=True, timeout=20, env={"MS_NONCE": nonce})
    return p.returncode == 0 and nonce in p.stdout


def test_runner_passes_only_when_candidate_and_tests_both_complete(tmp_path):
    assert _run_runner_locally(tmp_path, "def f(x): return x + 1", "assert f(1) == 2")
    assert not _run_runner_locally(tmp_path, "def f(x): return x + 2", "assert f(1) == 2")          # failing test
    assert not _run_runner_locally(tmp_path, "def f(:", "assert True")                               # syntax error


def test_hostile_output_cannot_fake_a_pass_with_a_printed_marker(tmp_path):
    """Reviewer repro: output that prints the old fixed marker and exits 0 before the tests ran."""
    hostile = "print('SANDBOX_OK', flush=True)\nimport os\nos._exit(0)\n"
    assert not _run_runner_locally(tmp_path, hostile, "assert False")
    assert not _run_runner_locally(tmp_path, "import sys\nsys.exit(0)\n", "assert False")
    assert not _run_runner_locally(tmp_path, "import os\nprint(os.environ.get('MS_NONCE'))\nos._exit(0)\n", "assert False")  # nonce not in env


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


def test_request_supplied_schema_cannot_make_the_server_fetch_urls():
    """Regression: jsonschema resolves remote $refs over the network by default (blind SSRF)."""
    import socket, threading
    hits = []
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    port = srv.getsockname()[1]
    def serve():
        while True:
            try:
                c, _ = srv.accept()
                hits.append(c.recv(100))
                c.close()
            except OSError:
                break
    threading.Thread(target=serve, daemon=True).start()
    try:
        for ref in (f"http://127.0.0.1:{port}/x.json", f"https://127.0.0.1:{port}/x.json", f"file:///etc/passwd"):
            r = run_spec(spec(type="json_schema", schema={"$ref": ref}), '{"a": 1}')
            assert not r.passed and "only local" in r.detail
        nested = {"type": "object", "properties": {"a": {"items": [{"$ref": f"http://127.0.0.1:{port}/y"}]}}}
        assert not run_spec(spec(type="json_schema", schema=nested), '{"a": 1}').passed
        assert hits == [], "the server must never open a connection because of a schema"
    finally:
        srv.close()


def test_local_refs_still_work():
    schema = {"$defs": {"n": {"type": "integer"}}, "type": "object", "properties": {"a": {"$ref": "#/$defs/n"}}, "required": ["a"]}
    assert run_spec(spec(type="json_schema", schema=schema), '{"a": 3}').passed
    assert not run_spec(spec(type="json_schema", schema=schema), '{"a": "x"}').passed


# ---------------------------------------------------------------- reviewer finding: ReDoS must not block the server
import time

EVIL = ("a" * 40) + "b"


def test_catastrophic_regex_validator_times_out_and_the_worker_recovers():
    t0 = time.monotonic()
    r = run_spec(spec(type="regex", pattern=r"(a+)+$"), EVIL)
    assert not r.passed and "timed out" in r.detail and time.monotonic() - t0 < 6
    assert run_spec(spec(type="regex", pattern=r"^\d+$"), "123").passed        # a fresh worker serves the next call
    assert not run_spec(spec(type="regex", pattern=r"^\d+$"), "12x").passed


def test_catastrophic_schema_pattern_times_out_and_normal_patterns_work():
    evil = {"type": "string", "pattern": r"(a+)+$"}
    r = run_spec(spec(type="json_schema", schema=evil), '"' + EVIL + '"')
    assert not r.passed and "timed out" in r.detail
    ok = {"type": "object", "properties": {"id": {"type": "string", "pattern": r"^[A-Z]{3}-\d+$"}}, "required": ["id"]}
    assert run_spec(spec(type="json_schema", schema=ok), '{"id": "ABC-42"}').passed
    assert not run_spec(spec(type="json_schema", schema=ok), '{"id": "nope"}').passed
    nested = {"patternProperties": {r"(a+)+$": {"type": "integer"}}}
    assert not run_spec(spec(type="json_schema", schema=nested), json.dumps({EVIL: 1})).passed


def test_oversized_patterns_are_refused_and_huge_text_is_capped():
    assert "longer than" in run_spec(spec(type="regex", pattern="a" * 501), "a").detail
    assert run_spec(spec(type="regex", pattern="^a"), "a" * 5_000_000).passed       # only the first 100,000 characters are examined


async def test_a_slow_validator_does_not_stall_the_event_loop():
    import asyncio
    from modelspend import RouteRequest
    ticks = []
    async def ticker():
        for _ in range(8):
            await asyncio.sleep(0.2)
            ticks.append(time.monotonic())
    req = RouteRequest(providers=["openai"], messages=[{"role": "user", "content": "x"}], validators=[{"type": "regex", "pattern": r"(a+)+$"}])
    t0 = time.monotonic()
    rep, _ = await asyncio.gather(validate(req, EVIL), ticker())
    assert rep.status == "failed" and "timed out" in rep.results[0].detail
    assert len(ticks) == 8 and ticks[1] - t0 < 1.0, "the loop kept running while the regex was being evaluated"
