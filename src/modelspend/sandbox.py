"""Isolated execution of generated code. NEVER runs on the host.

Requires Docker. The container has no network, a read-only root, dropped capabilities, a pids
limit, memory/CPU caps and a wall-clock timeout. If Docker is unavailable, `SandboxUnavailable`
is raised and callers must treat the check as not performed.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from .errors import SandboxUnavailable

IMAGE = "python:3.12-slim"


def docker_command(workdir: str, timeout_s: int = 10, image: str = IMAGE) -> list[str]:
    return [
        "docker", "run", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--pids-limit", "64", "--memory", "256m", "--cpus", "0.5",
        "--tmpfs", "/tmp:rw,size=16m", "-v", f"{workdir}:/work:ro", "-w", "/work", "-u", "65534:65534",
        image, "timeout", str(timeout_s), "python", "/work/run.py",
    ]


def docker_available() -> bool:
    return shutil.which("docker") is not None


def run_python_tests(code: str, spec: dict, timeout_s: int = 10) -> tuple[bool, str]:
    """`spec['tests']` is Python source (asserts) appended after the candidate code."""
    if not docker_available():
        raise SandboxUnavailable("docker is not installed; generated code is never executed on the host")
    with tempfile.TemporaryDirectory() as d:
        Path(d, "run.py").write_text(code + "\n\n" + spec["tests"] + "\nprint('SANDBOX_OK')\n")
        try:
            p = subprocess.run(docker_command(d, timeout_s), capture_output=True, text=True, timeout=timeout_s + 20)
        except subprocess.TimeoutExpired:
            return False, "timeout"
    ok = p.returncode == 0 and "SANDBOX_OK" in p.stdout
    return ok, "" if ok else (p.stderr.strip().splitlines() or ["failed"])[-1][:200]


def make_runner() -> Callable[[str, dict], tuple[bool, str]] | None:
    """Runner for the validator layer, or None when Docker is missing."""
    return run_python_tests if docker_available() else None
