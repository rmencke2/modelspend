"""`serve --background`, `stop`, `restart`, `status`: real processes, with the safety rules around signalling."""
import json
import os
import shutil
import socket
import subprocess
import sys
import time

import pytest
from typer.testing import CliRunner

from modelspend import Config
from modelspend.cli import app
from modelspend.server import runner

runner_cli = CliRunner()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def port(monkeypatch):
    p = free_port()
    monkeypatch.setenv("MODELSPEND_PORT", str(p))
    yield p
    runner.stop_server(Config.from_env(), timeout=5, force=True)  # never leave a server behind


def invoke(*args):
    return runner_cli.invoke(app, list(args))


def test_background_start_status_double_start_stop_restart(port):
    cfg = Config.from_env()
    assert "server: not running" in invoke("status", "--catalog", "synthetic").output
    r = invoke("serve", "--background", "--catalog", "synthetic")
    assert r.exit_code == 0 and "started ModelSpend server" in r.output and f":{port}" in r.output and "modelspend stop" in r.output
    assert runner.probe("127.0.0.1", port)["ok"] is True
    info = json.loads(runner.pid_path(cfg).read_text())
    assert info["port"] == port and runner.pid_alive(info["pid"])
    assert f"server: running (pid {info['pid']})" in invoke("status", "--catalog", "synthetic").output
    again = invoke("serve", "--background")
    assert again.exit_code == 1 and "already running" in again.output
    fg = invoke("serve")                                         # foreground start refuses too, and returns at once
    assert fg.exit_code == 1 and "already running" in fg.output
    assert (runner.log_path(cfg)).exists()
    # restart gives a NEW process
    re_ = invoke("restart", "--catalog", "synthetic")
    assert re_.exit_code == 0 and "stopped ModelSpend server" in re_.output and "started ModelSpend server" in re_.output
    info2 = json.loads(runner.pid_path(cfg).read_text())
    assert info2["pid"] != info["pid"] and not runner.pid_alive(info["pid"])
    stop = invoke("stop")
    assert stop.exit_code == 0 and "stopped ModelSpend server" in stop.output
    assert runner.probe("127.0.0.1", port) is None and not runner.pid_path(cfg).exists()
    assert not runner.pid_alive(info2["pid"])
    again_stop = invoke("stop")
    assert again_stop.exit_code == 0 and "no ModelSpend server is running" in again_stop.output


def test_stale_pidfile_for_a_dead_process_is_cleaned_up(port):
    cfg = Config.from_env()
    cfg.home.mkdir(parents=True, exist_ok=True)
    runner.pid_path(cfg).write_text(json.dumps({"pid": 2_999_999, "host": "127.0.0.1", "port": port}))
    st = runner.server_state(cfg)
    assert not st.running and not runner.pid_path(cfg).exists()
    runner.pid_path(cfg).write_text(json.dumps({"pid": 2_999_999, "host": "127.0.0.1", "port": port}))
    assert invoke("stop").exit_code == 0 and not runner.pid_path(cfg).exists()


def test_stop_never_signals_an_unrelated_process_just_because_a_pidfile_names_it(port):
    cfg = Config.from_env()
    victim = subprocess.Popen(["sleep", "60"])
    try:
        cfg.home.mkdir(parents=True, exist_ok=True)
        runner.pid_path(cfg).write_text(json.dumps({"pid": victim.pid, "host": "127.0.0.1", "port": port}))  # PID reused by something else
        r = invoke("stop")
        assert r.exit_code == 1 and "Not signalling" in r.output and "reused PID" in r.output
        assert victim.poll() is None, "an unrelated process must not be touched"
        assert invoke("status", "--catalog", "synthetic").output.count("server: not running") == 1
        forced = invoke("stop", "--force")                       # only on explicit request
        assert forced.exit_code == 0
        victim.wait(timeout=5)
    finally:
        if victim.poll() is None:
            victim.kill()


@pytest.mark.skipif(not shutil.which("lsof"), reason="needs lsof to find a server that left no PID file")
def test_stop_finds_a_server_started_by_an_older_version_without_a_pidfile(port):
    code = ("import uvicorn;from modelspend import Catalog,Config;from modelspend.catalog import shipped_catalog_path;"
            "from modelspend.engine import Engine;from modelspend.server.app import create_app;"
            "cfg=Config();e=Engine(Catalog.load(shipped_catalog_path('synthetic_test_catalog.json')),cfg,{});"
            f"uvicorn.run(create_app(e,cfg),host='127.0.0.1',port={port},log_level='warning')")
    old = subprocess.Popen([sys.executable, "-c", code])
    try:
        for _ in range(60):
            if runner.probe("127.0.0.1", port):
                break
            time.sleep(0.2)
        assert runner.probe("127.0.0.1", port) and not runner.pid_path(Config.from_env()).exists()
        assert "server: running" in invoke("status", "--catalog", "synthetic").output
        r = invoke("stop")
        assert r.exit_code == 0 and f"pid {old.pid}" in r.output
        old.wait(timeout=10)
        assert runner.probe("127.0.0.1", port) is None
    finally:
        if old.poll() is None:
            old.kill()


def test_background_start_reports_a_crash_instead_of_hanging(port, monkeypatch):
    monkeypatch.setenv("MODELSPEND_CATALOG", "/nonexistent/catalog.json")
    r = invoke("serve", "--background")
    assert r.exit_code == 1 and ("exited immediately" in r.output or "did not answer" in r.output)
