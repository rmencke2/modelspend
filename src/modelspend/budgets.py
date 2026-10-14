"""Persistent per-project / per-agent spending limits (daily or monthly, in USD and/or tokens), hard or soft.

A budget is scoped by (project, agent) where either may be empty meaning "any": a (shop, *) budget covers every agent in the
shop project, an (*, invoice-bot) budget covers that agent in every project, and (*, *) covers everything. A call is subject to every
budget whose scope matches its tags.

Enforcement (hard budgets) happens before EVERY provider call, in one SQLite write transaction (`BEGIN IMMEDIATE`), so several
threads or processes sharing the usage database cannot jointly overshoot a limit:

    committed spend in the window  (calls already recorded)
  + in-flight holds of other requests (their spend so far + worst case of the call they are making)
  + this request's spend so far + the worst case of the call about to be made
  must be <= limit

The hold of a request is replaced after each call by what it actually spent, and removed when the request ends. A hold left behind by
a crash expires after HOLD_TTL_S. Soft budgets never refuse a call: they add a warning to the result.

Windows are UTC calendar days / months. USD limits count only calls that have a price (calls on unpriced models are counted in tokens
only); a hard USD limit therefore refuses to run an unpriced model rather than guess. Token limits count everything.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .errors import ModelSpendError
from .store import Store
from .types import clean_tag

PERIODS = ("daily", "monthly")
MODES = ("hard", "soft")
HOLD_TTL_S = 900.0  # a crashed request's reservation stops counting after this long
MAX_BUDGETS = 200
WARN_AT = 0.8
EPS = 1e-9


class PeriodBudgetExceeded(ModelSpendError):
    code = "period_budget_exceeded"
    http_status = 429


def window(period: str, now: float | None = None) -> tuple[float, float]:
    """(start, next reset) of the current UTC day or month, as epoch seconds."""
    t = datetime.fromtimestamp(time.time() if now is None else now, tz=timezone.utc)
    if period == "daily":
        start = t.replace(hour=0, minute=0, second=0, microsecond=0)
        return start.timestamp(), (start + timedelta(days=1)).timestamp()
    if period == "monthly":
        start = t.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        nxt = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
        return start.timestamp(), nxt.timestamp()
    raise ValueError(f"period must be one of {', '.join(PERIODS)}")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def scope_label(project: str | None, agent: str | None) -> str:
    return f"project {project or '*'} / agent {agent or '*'}"


@dataclass
class Budget:
    id: int
    project: str | None
    agent: str | None
    period: str
    limit_usd: float | None
    limit_tokens: int | None
    mode: str

    def matches(self, project: str | None, agent: str | None) -> bool:
        return (self.project is None or self.project == project) and (self.agent is None or self.agent == agent)

    @property
    def label(self) -> str:
        return scope_label(self.project, self.agent)


def _row(r: Any) -> Budget:
    return Budget(r["id"], r["project"], r["agent"], r["period"], r["limit_usd"], r["limit_tokens"], r["mode"])


# ---------------------------------------------------------------------------------------------------------------- admin
def set_budget(store: Store, *, project: str | None = None, agent: str | None = None, period: str = "monthly",
               limit_usd: float | None = None, limit_tokens: int | None = None, mode: str = "hard") -> Budget:
    """Create or replace the budget for (project, agent, period). Raises ValueError on invalid input."""
    project, agent = clean_tag(project, "project"), clean_tag(agent, "agent")
    if period not in PERIODS:
        raise ValueError(f"period must be one of {', '.join(PERIODS)}")
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    if limit_usd is None and limit_tokens is None:
        raise ValueError("set at least one limit: limit_usd and/or limit_tokens")
    if limit_usd is not None and not (isinstance(limit_usd, (int, float)) and not isinstance(limit_usd, bool) and 0 < limit_usd < 1e9):
        raise ValueError("limit_usd must be a positive number")
    if limit_tokens is not None and not (isinstance(limit_tokens, int) and not isinstance(limit_tokens, bool) and 0 < limit_tokens < 10**15):
        raise ValueError("limit_tokens must be a positive whole number")
    with store.lock:
        cur = store.db.execute("select id from budgets where coalesce(project,'')=coalesce(?,'') and coalesce(agent,'')=coalesce(?,'') and period=?",
                               (project, agent, period)).fetchone()
        if cur:
            store.db.execute("update budgets set limit_usd=?, limit_tokens=?, mode=? where id=?", (limit_usd, limit_tokens, mode, cur["id"]))
            bid = cur["id"]
        else:
            if store.db.execute("select count(*) from budgets").fetchone()[0] >= MAX_BUDGETS:
                raise ValueError(f"at most {MAX_BUDGETS} budgets")
            bid = store.db.execute("insert into budgets(project, agent, period, limit_usd, limit_tokens, mode, created_ts) values(?,?,?,?,?,?,?)",
                                   (project, agent, period, limit_usd, limit_tokens, mode, time.time())).lastrowid
        store.db.commit()
    return Budget(bid, project, agent, period, limit_usd, limit_tokens, mode)


def remove_budget(store: Store, budget_id: int) -> bool:
    with store.lock:
        n = store.db.execute("delete from budgets where id=?", (budget_id,)).rowcount
        store.db.commit()
    return n > 0


def list_budgets(store: Store) -> list[Budget]:
    with store.lock:
        return [_row(r) for r in store.db.execute("select * from budgets order by id")]


# ------------------------------------------------------------------------------------------------------------- accounting
def _committed(store: Store, b: Budget, start: float) -> tuple[float, int, int]:
    r = store.db.execute(
        "select coalesce(sum(a.cost),0) usd, coalesce(sum(a.input_tokens + a.cache_read + a.cache_write + a.output_tokens),0) tok, "
        "sum(case when a.cost is null then 1 else 0 end) unpriced "
        "from attempts a join requests r on r.id = a.request_id "
        "where a.outcome != 'skipped_budget' and r.ts >= ? and (? is null or r.project = ?) and (? is null or r.agent = ?)",
        (start, b.project, b.project, b.agent, b.agent)).fetchone()
    return float(r["usd"]), int(r["tok"]), int(r["unpriced"] or 0)


def _held(store: Store, b: Budget, now: float, exclude: str | None = None) -> tuple[float, int]:
    r = store.db.execute(
        "select coalesce(sum(usd),0) usd, coalesce(sum(tokens),0) tok from budget_holds "
        "where hold_id != ? and ts >= ? and (? is null or project = ?) and (? is null or agent = ?)",
        (exclude or "", now - HOLD_TTL_S, b.project, b.project, b.agent, b.agent)).fetchone()
    return float(r["usd"]), int(r["tok"])


def status(store: Store, now: float | None = None) -> list[dict[str, Any]]:
    """Every budget with what has been spent (and is in flight) in its current window."""
    now = time.time() if now is None else now
    out = []
    with store.lock:
        for b in list_budgets(store):
            start, resets = window(b.period, now)
            usd, tok, unpriced = _committed(store, b, start)
            h_usd, h_tok = _held(store, b, now)
            u_usd, u_tok = usd + h_usd, tok + h_tok
            pct_usd = (u_usd / b.limit_usd) if b.limit_usd else None
            pct_tok = (u_tok / b.limit_tokens) if b.limit_tokens else None
            worst = max([p for p in (pct_usd, pct_tok) if p is not None], default=0.0)
            out.append({
                "id": b.id, "project": b.project, "agent": b.agent, "scope": b.label, "period": b.period, "mode": b.mode,
                "limit_usd": b.limit_usd, "limit_tokens": b.limit_tokens,
                "spent_usd": usd, "spent_tokens": tok, "in_flight_usd": h_usd, "in_flight_tokens": h_tok,
                "pct_usd": pct_usd, "pct_tokens": pct_tok,
                "state": "over" if worst >= 1.0 - EPS else "warning" if worst >= WARN_AT else "ok",
                "window_start": start, "resets_at": resets, "resets_at_text": _iso(resets),
                "usd_complete": unpriced == 0, "unpriced_calls": unpriced,
            })
    return out


# ------------------------------------------------------------------------------------------------------------- the guard
class Violation:
    def __init__(self, budget: Budget, message: str, details: dict[str, Any]):
        self.budget, self.message, self.details = budget, message, details

    def error(self) -> PeriodBudgetExceeded:
        return PeriodBudgetExceeded(self.message, **self.details)


class BudgetGuard:
    """Per-request handle on the persistent budgets that apply to it. Created by `for_request`; None when nothing applies."""

    def __init__(self, store: Store, budgets: list[Budget], project: str | None, agent: str | None):
        self.store, self.budgets, self.project, self.agent = store, budgets, project, agent
        self.hold_id = uuid.uuid4().hex
        self.warnings: list[str] = []

    @classmethod
    def for_request(cls, store: Store | None, req: Any) -> "BudgetGuard | None":
        if store is None:
            return None
        applicable = [b for b in list_budgets(store) if b.matches(req.project, req.agent)]
        return cls(store, applicable, req.project, req.agent) if applicable else None

    def reserve(self, worst_usd: float | None, worst_tokens: int, base_usd: float, base_tokens: int) -> Violation | None:
        """Atomically check every applicable budget for one more call and, if all hard budgets allow it, hold the worst case.

        base_* = what this request has already spent. Returns a Violation (and holds nothing) if a hard budget would be exceeded."""
        now = time.time()
        proposed_usd = base_usd + (worst_usd or 0.0)
        proposed_tok = base_tokens + worst_tokens
        with self.store.lock:
            db = self.store.db
            if db.in_transaction:
                db.commit()
            db.execute("begin immediate")  # one writer at a time across threads and processes
            try:
                db.execute("delete from budget_holds where ts < ?", (now - HOLD_TTL_S,))
                for b in self.budgets:
                    start, resets = window(b.period, now)
                    usd, tok, _ = _committed(self.store, b, start)
                    h_usd, h_tok = _held(self.store, b, now, exclude=self.hold_id)
                    over = self._exceeds(b, usd + h_usd + proposed_usd, tok + h_tok + proposed_tok, worst_usd)
                    if over is None:
                        if b.mode == "soft":
                            self._note_soft(b, usd + h_usd + base_usd, tok + h_tok + base_tokens)
                        continue
                    msg = (f"{b.period} {b.mode} budget for {b.label} {over[0]} (resets {_iso(resets)}). "
                           f"{over[1]} Change it with `modelspend budget set`, or wait for the reset.")
                    if b.mode == "soft":
                        self._warn(f"{b.period} soft budget for {b.label} {over[0]}")
                        continue
                    db.rollback()
                    return Violation(b, msg, {"budget_id": b.id, "scope": b.label, "period": b.period, "mode": b.mode,
                                              "limit_usd": b.limit_usd, "limit_tokens": b.limit_tokens, "spent_usd": usd,
                                              "spent_tokens": tok, "in_flight_usd": h_usd, "in_flight_tokens": h_tok,
                                              "resets_at": _iso(resets)})
                db.execute("insert or replace into budget_holds(hold_id, project, agent, usd, tokens, ts) values(?,?,?,?,?,?)",
                           (self.hold_id, self.project, self.agent, proposed_usd, proposed_tok, now))
                db.commit()
            except BaseException:
                if db.in_transaction:
                    db.rollback()
                raise
        return None

    @staticmethod
    def _exceeds(b: Budget, usd: float, tok: int, worst_usd: float | None) -> tuple[str, str] | None:
        if b.limit_usd is not None:
            if worst_usd is None:
                return ("has a USD limit but this call's model has no price",
                        "Add a token limit instead, or give the model a price (`modelspend catalog price`).")
            if usd > b.limit_usd + EPS:
                return ("would be exceeded", f"Spent plus in-flight plus this call's worst case is ${usd:.6f} against a ${b.limit_usd:.6f} limit.")
        if b.limit_tokens is not None and tok > b.limit_tokens:
            return ("would be exceeded", f"Spent plus in-flight plus this call's worst case is {tok:,} tokens against a {b.limit_tokens:,} limit.")
        return None

    def _note_soft(self, b: Budget, usd: float, tok: int) -> None:
        pct = max([p for p in ((usd / b.limit_usd) if b.limit_usd else None, (tok / b.limit_tokens) if b.limit_tokens else None) if p is not None],
                  default=0.0)
        if pct >= WARN_AT:
            self._warn(f"{b.period} soft budget for {b.label} is at {pct:.0%}")

    def _warn(self, text: str) -> None:
        if text not in self.warnings:
            self.warnings.append(text)

    def settle(self, spent_usd: float, spent_tokens: int) -> None:
        """After a call: the hold shrinks from 'worst case' to what the request has really spent so far."""
        with self.store.lock:
            self.store.db.execute("update budget_holds set usd=?, tokens=?, ts=? where hold_id=?", (spent_usd, spent_tokens, time.time(), self.hold_id))
            self.store.db.commit()

    def close(self) -> None:
        """The request ended (its calls are recorded in the store): drop the hold."""
        with self.store.lock:
            try:
                self.store.db.execute("delete from budget_holds where hold_id=?", (self.hold_id,))
                self.store.db.commit()
            except Exception:  # noqa: BLE001 - a stale hold simply expires; never fail a finished request over it
                pass
