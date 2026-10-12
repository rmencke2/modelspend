"""Isolated execution of generated code. NEVER runs on the host.

Requires Docker. The container has no network, a read-only root, dropped capabilities, a pids limit, memory/CPU caps and a hard
wall-clock kill. If Docker is unavailable, `SandboxUnavailable` is raised and callers must treat the check as not performed.

Limits (best effort, not adversary-proof): the candidate code and the tests run in the SAME process, so hostile code can try to
fake a pass. The harness makes that much harder than printing a fixed string: the pass marker is a random per-run nonce held only
by the trusted runner (removed from the environment before the candidate runs) and printed only after the tests complete. A
determined adversary inspecting interpreter frames could still find it; treat `python_tests` as a quality signal for ordinary
model output, not as a security boundary against malicious output.
"""
from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from .errors import SandboxUnavailable

IMAGE = "python:3.12-slim"

# The trusted runner. The candidate and the tests are executed by it, in that order; the nonce is printed only if both complete.
RUNNER = """\
import os, sys

def _main():
    _nonce = os.environ.pop("MS_NONCE")          # gone from the environment before candidate code can read it
    _g = {"__name__": "__candidate__"}
    exec(compile(open("/work/candidate.py").read(), "candidate.py", "exec"), _g)
    exec(compile(open("/work/tests.py").read(), "tests.py", "exec"), _g)
    sys.stdout.write("\\n" + _nonce + "\\n")
    sys.stdout.flush()

_main()
"""


def docker_command(workdir: str, name: str, nonce: str, timeout_s: int = 10, image: str = IMAGE) -> list[str]:
    return [
        "docker", "run", "--rm", "--name", name, "--network", "none", "--read-only", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--pids-limit", "64", "--memory", "256m", "--cpus", "0.5",
        "--tmpfs", "/tmp:rw,size=16m", "-v", f"{workdir}:/work:ro", "-w", "/work", "-u", "65534:65534",
        "-e", f"MS_NONCE={nonce}",
        image, "timeout", "-s", "KILL", str(timeout_s), "python", "/work/run.py",  # SIGKILL: code that ignores SIGTERM must still die
    ]


def docker_available() -> bool:
    return shutil.which("docker") is not None


def run_python_tests(code: str, spec: dict, timeout_s: int = 10) -> tuple[bool, str]:
    """`spec['tests']` is Python source (asserts) run after the candidate code, in the same process."""
    if not docker_available():
        raise SandboxUnavailable("docker is not installed; generated code is never executed on the host")
    nonce, name = secrets.token_hex(16), f"modelspend-sandbox-{secrets.token_hex(6)}"
    with tempfile.TemporaryDirectory() as d:
        os.chmod(d, 0o755)  # the container runs as an unprivileged uid that must be able to read the mount
        for fname, body in (("run.py", RUNNER), ("candidate.py", code), ("tests.py", spec["tests"])):
            Path(d, fname).write_text(body)
            os.chmod(Path(d, fname), 0o644)
        try:
            p = subprocess.run(docker_command(d, name, nonce, timeout_s), capture_output=True, text=True, timeout=timeout_s + 20)
        except subprocess.TimeoutExpired:
            subprocess.run(["docker", "kill", name], capture_output=True, timeout=15)  # the client was killed, not the container
            return False, "timeout"
    ok = p.returncode == 0 and nonce in p.stdout
    return ok, "" if ok else (p.stderr.strip().splitlines() or ["failed"])[-1][:200]


def make_runner() -> Callable[[str, dict], tuple[bool, str]] | None:
    """Runner for the validator layer, or None when Docker is missing."""
    return run_python_tests if docker_available() else None
