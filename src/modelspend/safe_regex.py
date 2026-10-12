"""Regular expressions and JSON-Schema `pattern`s with a HARD timeout.

Python's `re` can backtrack catastrophically (`(a+)+$` on `aaaa...b`) and holds the GIL while it does, so no thread or asyncio
trick can interrupt it. The only reliable defence is to run risky matching in a separate worker process and kill the worker if it
takes too long. The pattern and the text come from request data (validators, response schemas), so they are untrusted.

One persistent spawned worker is reused (start-up cost is paid once). On timeout the worker is killed and a new one starts on
the next call. Patterns are capped at 500 characters and subject text at 100,000.
"""
from __future__ import annotations

import multiprocessing as mp
import threading
from typing import Any

MAX_PATTERN = 500
MAX_TEXT = 100_000
TIMEOUT_S = 2.0
STARTUP_S = 30.0

_lock = threading.Lock()
_proc: Any = None
_conn: Any = None


class RegexTimeout(Exception):
    """The pattern did not finish in time (possible catastrophic backtracking)."""


def _worker(conn) -> None:  # runs in the spawned child process
    import re

    from modelspend.validation import validate_schema_local

    conn.send(("ready", None))
    while True:
        try:
            op, a, b = conn.recv()
        except (EOFError, OSError):
            return
        try:
            if op == "search":
                conn.send(("ok", re.search(a, b, re.S) is not None))
            elif op == "schema":
                validate_schema_local(a, b)
                conn.send(("ok", True))
        except Exception as e:  # noqa: BLE001
            conn.send(("error", f"{type(e).__name__}: {getattr(e, 'message', str(e))}"))


def _spawn() -> None:
    global _proc, _conn
    ctx = mp.get_context("spawn")
    parent, child = ctx.Pipe()
    _proc = ctx.Process(target=_worker, args=(child,), daemon=True)
    _proc.start()
    child.close()
    _conn = parent
    if not _conn.poll(STARTUP_S) or _conn.recv()[0] != "ready":
        _kill()
        raise RuntimeError("regex worker failed to start")


def _kill() -> None:
    global _proc, _conn
    try:
        if _proc is not None and _proc.is_alive():
            _proc.kill()
            _proc.join(2)
    except Exception:  # noqa: BLE001
        pass
    _proc = _conn = None


def _call(op: str, a: Any, b: Any, timeout: float) -> Any:
    with _lock:
        if _proc is None or not _proc.is_alive():
            _kill()
            _spawn()
        _conn.send((op, a, b))
        if not _conn.poll(timeout):
            _kill()
            raise RegexTimeout(f"timed out after {timeout:g}s (possible catastrophic backtracking)")
        kind, val = _conn.recv()
    if kind == "error":
        raise ValueError(val)
    return val


def search(pattern: str, text: str, timeout: float = TIMEOUT_S) -> bool:
    if len(pattern) > MAX_PATTERN:
        raise ValueError(f"pattern is longer than {MAX_PATTERN} characters")
    return _call("search", pattern, text[:MAX_TEXT], timeout)


def validate_schema(instance: Any, schema: dict, timeout: float = TIMEOUT_S) -> None:
    """jsonschema validation (with `pattern` keywords) in the worker. Raises ValueError with the validation message on failure."""
    _call("schema", instance, schema, timeout)
