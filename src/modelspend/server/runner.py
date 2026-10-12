"""Starting and stopping the local server safely: a PID file, plus an identity check before any signal is sent.

`stop` never signals a process just because a PID file names it (PIDs get reused): the process must also answer
GET /healthz like a ModelSpend server on the recorded host:port. A server started by an older version (no PID file)
can still be stopped: the listener on the port is looked up with `lsof` and verified the same way.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

from ..config import Config


def pid_path(cfg: Config) -> Path:
    return cfg.home / "server.pid"


def log_path(cfg: Config) -> Path:
    return cfg.home / "server.log"


def probe_host(host: str) -> str:
    return "127.0.0.1" if host in ("0.0.0.0", "::", "") else host


def probe(host: str, port: int, timeout: float = 1.5) -> dict | None:
    """The /healthz payload if a ModelSpend server answers there, else None."""
    try:
        r = httpx.get(f"http://{probe_host(host)}:{port}/healthz", timeout=timeout)
        j = r.json()
        return j if r.status_code == 200 and "policy_version" in j and "catalog_version" in j else None
    except Exception:  # noqa: BLE001
        return None


def pid_alive(pid: int) -> bool:
    """True if the process exists and has not exited. A child of this process that has exited but not yet been reaped
    (a zombie) still answers kill(pid, 0), so reap it first."""
    if hasattr(os, "waitpid"):
        try:
            done, _ = os.waitpid(pid, os.WNOHANG)
            if done:
                return False
        except ChildProcessError:
            pass  # not our child: fall through to the signal-0 check
        except OSError:
            pass
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def write_pidfile(cfg: Config, host: str, port: int) -> None:
    p = pid_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"pid": os.getpid(), "host": host, "port": port,
                             "started": datetime.now(timezone.utc).isoformat(timespec="seconds")}))


def read_pidfile(cfg: Config) -> dict | None:
    p = pid_path(cfg)
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def remove_pidfile(cfg: Config, only_if_pid: int | None = None) -> None:
    info = read_pidfile(cfg)
    if only_if_pid is not None and info and info.get("pid") != only_if_pid:
        return  # a newer server owns the file
    try:
        pid_path(cfg).unlink()
    except OSError:
        pass


@dataclass
class ServerState:
    running: bool
    pid: int | None = None
    host: str | None = None
    port: int | None = None
    started: str | None = None
    source: str = ""  # pidfile | port-scan
    detail: str = ""


def listener_pid(port: int) -> int | None:
    """PID listening on a TCP port (macOS/Linux, needs lsof). Used only for servers that left no PID file."""
    if not shutil.which("lsof"):
        return None
    try:
        out = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"], capture_output=True, text=True, timeout=5).stdout.split()
        return int(out[0]) if out else None
    except Exception:  # noqa: BLE001
        return None


def server_state(cfg: Config) -> ServerState:
    info = read_pidfile(cfg)
    if info:
        alive = pid_alive(info["pid"])
        up = probe(info["host"], info["port"])
        if alive and up:
            return ServerState(True, info["pid"], info["host"], info["port"], info.get("started"), "pidfile")
        if not alive:
            remove_pidfile(cfg)
        if alive and not up:
            return ServerState(False, info["pid"], info["host"], info["port"], info.get("started"), "pidfile",
                               "a PID file names a live process that does not answer like a ModelSpend server (stale file or reused PID)")
    up = probe(cfg.host, cfg.port)
    if up:  # started by an older version, or by hand: no PID file
        return ServerState(True, listener_pid(cfg.port), cfg.host, cfg.port, None, "port-scan")
    return ServerState(False, detail="no ModelSpend server found")


def stop_server(cfg: Config, timeout: float = 10.0, force: bool = False) -> tuple[bool, str]:
    """(ok, message). Sends SIGTERM (graceful), waits, and only with force=True escalates to SIGKILL."""
    st = server_state(cfg)
    if not st.running:
        if st.pid and st.source == "pidfile" and st.detail:
            if not force:
                return False, f"{st.detail}. Not signalling pid {st.pid}; remove {pid_path(cfg)} if it is stale, or use --force to signal it anyway."
            return _terminate(cfg, st.pid, timeout, True)
        remove_pidfile(cfg)
        return True, "no ModelSpend server is running"
    if st.pid is None:
        return False, (f"a ModelSpend server answers on port {st.port} but its PID could not be found (lsof not available). "
                       f"Stop it from the terminal it runs in (Ctrl-C), or: kill $(lsof -ti tcp:{st.port})")
    return _terminate(cfg, st.pid, timeout, force)


def _terminate(cfg: Config, pid: int, timeout: float, force: bool) -> tuple[bool, str]:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        remove_pidfile(cfg)
        return True, "server was already gone"
    except PermissionError:
        return False, f"not allowed to signal pid {pid} (started by another user?)"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not pid_alive(pid):
            remove_pidfile(cfg)
            return True, f"stopped ModelSpend server (pid {pid})"
        time.sleep(0.1)
    if force:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        remove_pidfile(cfg)
        return True, f"server (pid {pid}) did not exit within {timeout:g}s and was killed"
    return False, f"server (pid {pid}) is still shutting down after {timeout:g}s; run `modelspend stop --force` to kill it"


def start_background(cfg: Config, host: str | None, port: int | None, catalog: str | None, wait: float = 15.0) -> tuple[bool, str]:
    st = server_state(cfg)
    if st.running:
        return False, f"already running (pid {st.pid}) at http://{probe_host(st.host or cfg.host)}:{st.port}. Use `modelspend restart` to reload it."
    cmd = [sys.executable, "-m", "modelspend.cli", "serve"]
    if host:
        cmd += ["--host", host]
    if port:
        cmd += ["--port", str(port)]
    if catalog:
        cmd += ["--catalog", catalog]
    from ..config import private_mkdir
    private_mkdir(cfg.home)
    log = os.fdopen(os.open(log_path(cfg), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "ab")
    kw: dict = {"start_new_session": True} if os.name != "nt" else {"creationflags": 0x00000008 | 0x00000200}
    proc = subprocess.Popen(cmd, stdout=log, stderr=log, stdin=subprocess.DEVNULL, env=os.environ.copy(), **kw)
    h, p = host or cfg.host, port or cfg.port
    deadline = time.time() + wait
    while time.time() < deadline:
        if probe(h, p):
            return True, f"started ModelSpend server (pid {proc.pid}) at http://{probe_host(h)}:{p}  |  log: {log_path(cfg)}  |  stop it with: modelspend stop"
        if proc.poll() is not None:
            tail = log_path(cfg).read_text(errors="replace").strip().splitlines()[-3:]
            return False, "the server exited immediately: " + " / ".join(tail)
        time.sleep(0.15)
    return False, f"the server did not answer within {wait:g}s; see {log_path(cfg)}"
