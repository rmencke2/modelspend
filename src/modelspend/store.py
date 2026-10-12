"""SQLite usage store. Stores metadata and accounting only: no prompts, no responses, no keys."""
from __future__ import annotations

import os
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any

from .types import RouteResult

SCHEMA = """
create table if not exists requests(
  id text primary key, ts real, profile text, task text, task_basis text,
  provider text, model text, status text, validation text,
  retries int, failovers int, escalations int,
  estimated_cost real, actual_cost real, uncertain_cost real, budget real,
  input_tokens int, output_tokens int, latency_ms int, catalog_version text, policy_version text);
create table if not exists attempts(
  request_id text, idx int, kind text, purpose text, provider text, model text, outcome text, error_kind text,
  input_tokens int, cache_read int, cache_write int, output_tokens int, reasoning_tokens int,
  estimated_cost real, cost real, cost_basis text, latency_ms int);
create table if not exists class_cache(key text primary key, category text, ts real);
create table if not exists meta(k text primary key, v blob);
create index if not exists attempts_model on attempts(provider, model);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        if self.path != ":memory:":
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass

    # ---- classification cache (keys are salted HMACs, never prompt text) ---------------
    def salt(self) -> bytes:
        r = self.db.execute("select v from meta where k='salt'").fetchone()
        if r:
            return bytes(r[0])
        s = secrets.token_bytes(32)
        self.db.execute("insert into meta values('salt', ?)", (s,))
        self.db.commit()
        return s

    def get_class(self, key: str) -> str | None:
        r = self.db.execute("select category from class_cache where key=?", (key,)).fetchone()
        return r[0] if r else None

    def put_class(self, key: str, category: str) -> None:
        self.db.execute("insert or replace into class_cache values(?,?,?)", (key, category, time.time()))
        self.db.commit()

    # ---- recording --------------------------------------------------------------------
    def record(self, r: RouteResult) -> None:
        self.record_row(
            r.request_id, r.profile.value, r.task.category.value, r.task.basis, r.provider, r.model, r.status,
            r.validation.status, r.retries, r.failovers, r.escalations, r.estimated_cost_usd, r.actual_cost_usd,
            r.uncertain_cost_usd, r.budget_usd, r.usage.input_tokens + r.usage.cache_read_tokens + r.usage.cache_write_tokens,
            r.usage.output_tokens, r.latency_ms, r.catalog_version, r.policy_version, r.attempts)

    def record_failure(self, request_id: str, profile: str, task: str, status: str, attempts: list, est: float,
                       cost: float, uncertain: float, budget: float | None, catalog_v: str, policy_v: str, latency_ms: int) -> None:
        last = attempts[-1] if attempts else None
        self.record_row(request_id, profile, task, "n/a", last.provider if last else "-", last.model if last else "-",
                        status, "n/a", 0, 0, 0, est, cost, uncertain, budget, 0, 0, latency_ms, catalog_v, policy_v, attempts)

    def record_row(self, rid, profile, task, basis, provider, model, status, validation, retries, failovers,
                   escalations, est, cost, uncertain, budget, in_tok, out_tok, latency, cv, pv, attempts) -> None:
        self.db.execute(
            "insert or replace into requests values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rid, time.time(), profile, task, basis, provider, model, status, validation, retries, failovers,
             escalations, est, cost, uncertain, budget, in_tok, out_tok, latency, cv, pv))
        self.db.execute("delete from attempts where request_id=?", (rid,))
        for a in attempts:
            u = a.usage
            self.db.execute(
                "insert into attempts values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rid, a.index, a.kind, a.purpose, a.provider, a.model, a.outcome, a.error_kind, u.input_tokens,
                 u.cache_read_tokens, u.cache_write_tokens, u.output_tokens, u.reasoning_tokens,
                 a.estimated_cost_usd, a.cost_usd, a.cost_basis, a.latency_ms))
        self.db.commit()

    # ---- queries ----------------------------------------------------------------------
    def latency_p50(self, provider: str, model: str) -> tuple[float, int] | None:
        rows = [r[0] for r in self.db.execute(
            "select latency_ms from attempts where provider=? and model=? and outcome='ok' and kind='generation' order by latency_ms",
            (provider, model))]
        if not rows:
            return None
        return float(rows[len(rows) // 2]), len(rows)

    def _q(self, sql: str, *a: Any) -> list[dict]:
        return [dict(r) for r in self.db.execute(sql, a)]

    def summary(self, limit: int = 50) -> dict[str, Any]:
        tot = self._q("select count(*) n, coalesce(sum(estimated_cost),0) est, coalesce(sum(actual_cost),0) act, "
                      "coalesce(sum(uncertain_cost),0) unc, coalesce(sum(escalations),0) esc, "
                      "coalesce(sum(retries),0) ret, coalesce(sum(failovers),0) fo from requests")[0]
        return {
            "totals": tot,
            "escalation_rate": (sum(1 for _ in self._q("select 1 from requests where escalations>0")) / tot["n"]) if tot["n"] else 0.0,
            "by_model": self._q("select provider, model, count(*) n, sum(actual_cost) cost, sum(estimated_cost) est from requests group by 1,2 order by n desc"),
            "by_task": self._q("select task, count(*) n, sum(actual_cost) cost from requests group by 1 order by cost desc"),
            "by_provider": self._q("select provider, count(*) n, sum(actual_cost) cost from requests group by 1 order by cost desc"),
            "validation": self._q("select validation, count(*) n from requests group by 1"),
            "recent": self._q("select id, ts, profile, task, provider, model, status, validation, escalations, retries, "
                              "estimated_cost, actual_cost, latency_ms from requests order by ts desc limit ?", limit),
        }
