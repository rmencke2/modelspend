"""SQLite usage store. Stores metadata and accounting only: no prompts, no responses, no keys."""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
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
create table if not exists budgets(
  id integer primary key autoincrement, project text, agent text, period text, limit_usd real, limit_tokens integer,
  mode text, created_ts real);
create unique index if not exists budgets_scope on budgets(coalesce(project, ''), coalesce(agent, ''), period);
create table if not exists conversations(key text primary key, model text, category text, ts real);
create table if not exists provider_usage(
  source text, provider text, model text, day text, input_tokens int, output_tokens int, cache_read int, cache_write int,
  requests int, cost real, imported_ts real, primary key(source, provider, model, day));
create table if not exists shadow_results(
  id integer primary key autoincrement, ts real, request_id text, project text, agent text, category text,
  used_provider text, used_model text, cand_provider text, cand_model text,
  method text, verdict text, reason text,
  used_cost real, cand_cost real, judge_cost real, cand_input int, cand_output int);
create index if not exists shadow_cat on shadow_results(category, cand_provider, cand_model);
create table if not exists prefix_stats(
  request_id text, ts real, provider text, model text, level text, h text, tokens int, hn text);
create index if not exists prefix_seen on prefix_stats(provider, model, h, ts);
create table if not exists budget_holds(hold_id text primary key, project text, agent text, usd real, tokens integer, ts real);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            from .config import private_mkdir
            private_mkdir(Path(self.path).parent)
        self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=10.0)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()  # budget reservations and admin writes serialize on this (and on SQLite's write lock across processes)
        self.db.execute("pragma busy_timeout = 10000")
        if self.path != ":memory:":
            self.db.execute("pragma journal_mode = wal")  # readers (reports, dashboard) never block the writers
        self.db.executescript(SCHEMA)
        self._migrate()
        if self.path != ":memory:":
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass

    def _migrate(self) -> None:
        have = {r[1] for r in self.db.execute("pragma table_info(requests)")}
        for col in ("cache_read_tokens", "reasoning_tokens", "total_tokens"):
            if col not in have:
                self.db.execute(f"alter table requests add column {col} int default 0")
        # 0.2: attribution tags (nullable: rows written before 0.2 simply have no project/agent)
        for col, decl in (("project", "text"), ("agent", "text"), ("run_id", "text"), ("labels", "text"), ("pinned", "int default 0")):
            if col not in have:
                self.db.execute(f"alter table requests add column {col} {decl}")
        # 0.8.0b3: the shape hash that recognises a prefix drifting in numbers or ids (rows from 0.8.0b1/b2 simply have none)
        if "hn" not in {r[1] for r in self.db.execute("pragma table_info(prefix_stats)")}:
            self.db.execute("alter table prefix_stats add column hn text")
        self.db.execute("create index if not exists requests_project_agent on requests(project, agent, ts)")
        self.db.execute("create index if not exists attempts_request on attempts(request_id)")
        self.db.commit()

    # ---- classification cache (keys are salted HMACs, never prompt text) ---------------
    def salt(self) -> bytes:
        r = self.db.execute("select v from meta where k='salt'").fetchone()
        if r:
            return bytes(r[0])
        s = secrets.token_bytes(32)
        # another process sharing this database may have created the salt since the select above: keep whichever row won
        self.db.execute("insert or ignore into meta values('salt', ?)", (s,))
        self.db.commit()
        return bytes(self.db.execute("select v from meta where k='salt'").fetchone()[0])

    def get_class(self, key: str) -> str | None:
        r = self.db.execute("select category from class_cache where key=?", (key,)).fetchone()
        return r[0] if r else None

    def put_class(self, key: str, category: str) -> None:
        self.db.execute("insert or replace into class_cache values(?,?,?)", (key, category, time.time()))
        self.db.commit()

    # ---- conversation stickiness (salted-hash key -> model ref; no content) ---------------------------
    STICKY_TTL_S = 24 * 3600.0
    STICKY_MAX_ROWS = 20_000

    def get_conversation(self, key: str) -> tuple[str, str | None] | None:
        """(model ref, task category of the conversation so far) or None."""
        r = self.db.execute("select model, category from conversations where key=? and ts >= ?", (key, time.time() - self.STICKY_TTL_S)).fetchone()
        return (r[0], r[1]) if r else None

    def put_conversation(self, key: str, model_ref: str, category: str | None = None) -> None:
        """Remember the model (and task category) a conversation is using. An 'unknown' category never overwrites a known one."""
        now = time.time()
        with self.lock:
            self.db.execute(
                "insert into conversations(key, model, category, ts) values(?,?,?,?) on conflict(key) do update set model=excluded.model, ts=excluded.ts, "
                "category=coalesce(nullif(excluded.category, 'unknown'), conversations.category, excluded.category)", (key, model_ref, category, now))
            self.db.execute("delete from conversations where ts < ?", (now - self.STICKY_TTL_S,))
            n = self.db.execute("select count(*) from conversations").fetchone()[0]
            if n > self.STICKY_MAX_ROWS:
                self.db.execute("delete from conversations where key in (select key from conversations order by ts limit ?)", (n - self.STICKY_MAX_ROWS,))
            self.db.commit()

    # ---- recording --------------------------------------------------------------------
    def record(self, r: RouteResult) -> None:
        self.record_row(
            r.request_id, r.profile.value, r.task.category.value, r.task.basis, r.provider, r.model, r.status,
            r.validation.status, r.retries, r.failovers, r.escalations, r.estimated_cost_usd, r.actual_cost_usd,
            r.uncertain_cost_usd, r.budget_usd, r.usage.input_tokens + r.usage.cache_read_tokens + r.usage.cache_write_tokens,
            r.usage.output_tokens, r.latency_ms, r.catalog_version, r.policy_version, r.attempts,
            cache_read=r.usage.cache_read_tokens, reasoning=r.usage.reasoning_tokens, total=r.total_tokens,
            tags={"project": r.project, "agent": r.agent, "run_id": r.run_id, "labels": r.labels, "pinned": r.pinned})

    def record_failure(self, request_id: str, profile: str, task: str, status: str, attempts: list, est: float,
                       cost: float, uncertain: float, budget: float | None, catalog_v: str, policy_v: str, latency_ms: int,
                       tags: dict | None = None) -> None:
        last = attempts[-1] if attempts else None
        self.record_row(request_id, profile, task, "n/a", last.provider if last else "-", last.model if last else "-",
                        status, "n/a", 0, 0, 0, est, cost, uncertain, budget, 0, 0, latency_ms, catalog_v, policy_v, attempts,
                        tags=tags)

    def record_row(self, rid, profile, task, basis, provider, model, status, validation, retries, failovers,
                   escalations, est, cost, uncertain, budget, in_tok, out_tok, latency, cv, pv, attempts,
                   cache_read=0, reasoning=0, total=None, tags: dict | None = None) -> None:
        total = (in_tok or 0) + (out_tok or 0) if total is None else total
        t = tags or {}
        self.db.execute(
            "insert or replace into requests(id,ts,profile,task,task_basis,provider,model,status,validation,retries,failovers,"
            "escalations,estimated_cost,actual_cost,uncertain_cost,budget,input_tokens,output_tokens,latency_ms,"
            "catalog_version,policy_version,cache_read_tokens,reasoning_tokens,total_tokens,project,agent,run_id,labels,pinned) "
            "values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rid, time.time(), profile, task, basis, provider, model, status, validation, retries, failovers,
             escalations, est, cost, uncertain, budget, in_tok, out_tok, latency, cv, pv, cache_read, reasoning, total,
             t.get("project"), t.get("agent"), t.get("run_id"), json.dumps(t["labels"], sort_keys=True) if t.get("labels") else None,
             1 if t.get("pinned") else 0))
        self.db.execute("delete from attempts where request_id=?", (rid,))
        for a in attempts:
            u = a.usage
            self.db.execute(
                "insert into attempts values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rid, a.index, a.kind, a.purpose, a.provider, a.model, a.outcome, a.error_kind, u.input_tokens,
                 u.cache_read_tokens, u.cache_write_tokens, u.output_tokens, u.reasoning_tokens,
                 a.estimated_cost_usd, a.cost_usd, a.cost_basis, a.latency_ms))
        self.db.commit()

    def record_shadow(self, **r: Any) -> None:
        """One shadow comparison. Only a verdict, a fixed reason code, costs and token counts: never prompts or answers."""
        cols = ("request_id", "project", "agent", "category", "used_provider", "used_model", "cand_provider", "cand_model", "method", "verdict",
                "reason", "used_cost", "cand_cost", "judge_cost", "cand_input", "cand_output")
        with self.lock:
            self.db.execute(f"insert into shadow_results(ts, {', '.join(cols)}) values (?{', ?' * len(cols)})", (time.time(), *(r.get(c) for c in cols)))
            self.db.commit()

    def prune(self, older_than_ts: float, dry_run: bool = False) -> dict[str, int]:
        """Delete requests (and their attempts) and shadow comparisons recorded before the given time. Budgets and the classifier cache are untouched."""
        with self.lock:
            n = self.db.execute("select count(*) from requests where ts < ?", (older_than_ts,)).fetchone()[0]
            a = self.db.execute("select count(*) from attempts where request_id in (select id from requests where ts < ?)", (older_than_ts,)).fetchone()[0]
            sh = self.db.execute("select count(*) from shadow_results where ts < ?", (older_than_ts,)).fetchone()[0]
            if not dry_run and (n or sh):
                self.db.execute("delete from attempts where request_id in (select id from requests where ts < ?)", (older_than_ts,))
                self.db.execute("delete from requests where ts < ?", (older_than_ts,))
                self.db.execute("delete from shadow_results where ts < ?", (older_than_ts,))
                self.db.execute("delete from prefix_stats where ts < ?", (older_than_ts,))
                self.db.commit()
        return {"requests": n, "attempts": a, "shadow": sh}

    def record_prefixes(self, request_id: str, provider: str, model: str, levels: list[dict[str, Any]]) -> None:
        """Keyed hashes and token counts of a request's reusable prompt prefixes. Never the text."""
        if not levels:
            return
        now = time.time()
        with self.lock:
            self.db.executemany("insert into prefix_stats(request_id, ts, provider, model, level, h, tokens, hn) values (?,?,?,?,?,?,?,?)",
                                [(request_id, now, provider, model, lv["level"], lv["h"], lv["tokens"], lv.get("hn")) for lv in levels])
            self.db.commit()

    def forget_prefixes(self) -> int:
        with self.lock:
            n = self.db.execute("select count(*) from prefix_stats").fetchone()[0]
            self.db.execute("delete from prefix_stats")
            self.db.commit()
        return n

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
                      "coalesce(sum(retries),0) ret, coalesce(sum(failovers),0) fo, "
                      "coalesce(sum(input_tokens),0) input_tokens, coalesce(sum(cache_read_tokens),0) cache_read_tokens, "
                      "coalesce(sum(output_tokens),0) output_tokens, coalesce(sum(reasoning_tokens),0) reasoning_tokens, "
                      "coalesce(sum(input_tokens + output_tokens),0) total_tokens, "
                      "sum(case when actual_cost is null then 1 else 0 end) unpriced_requests from requests")[0]
        tot["unpriced_requests"] = tot["unpriced_requests"] or 0
        return {
            "totals": tot,
            "escalation_rate": (sum(1 for _ in self._q("select 1 from requests where escalations>0")) / tot["n"]) if tot["n"] else 0.0,
            "by_model": self._q("select provider, model, count(*) n, coalesce(sum(input_tokens),0) input_tokens, coalesce(sum(output_tokens),0) output_tokens, coalesce(sum(input_tokens+output_tokens),0) tokens, sum(actual_cost) cost, sum(estimated_cost) est from requests group by 1,2 order by tokens desc"),
            "by_task": self._q("select task, count(*) n, coalesce(sum(input_tokens),0) input_tokens, coalesce(sum(output_tokens),0) output_tokens, coalesce(sum(input_tokens+output_tokens),0) tokens, sum(actual_cost) cost from requests group by 1 order by tokens desc"),
            "by_provider": self._q("select provider, count(*) n, coalesce(sum(input_tokens),0) input_tokens, coalesce(sum(output_tokens),0) output_tokens, coalesce(sum(input_tokens+output_tokens),0) tokens, sum(actual_cost) cost from requests group by 1 order by tokens desc"),
            "validation": self._q("select validation, count(*) n from requests group by 1"),
            "recent": self._q("select id, ts, profile, task, provider, model, status, validation, escalations, retries, "
                              "estimated_cost, actual_cost, input_tokens, cache_read_tokens, output_tokens, reasoning_tokens, latency_ms "
                              "from requests order by ts desc limit ?", limit),
        }
