"""Live conformance check for a provider, small enough to run on your own key (a fraction of a cent) and report what it finds.

ModelSpend's accounting is only as good as its adapters: if a provider reports no token usage on a stream, or counts cached tokens differently
than the adapter assumes, every cost number for that provider is quietly wrong. Fakes can't find that; only the real API can. This module sends a
handful of tiny requests and checks, for each provider:

  key         the key lists models                                      (read-only, no tokens)
  errors      an unknown model fails with a normalized error            (no tokens generated)
  generate    one tiny completion reports non-zero token usage and finishes
  stream      the same call, streamed, reports usage that matches
  structured  a JSON-schema reply parses (only if the catalog claims the capability)
  cache       (opt in) the same long prompt twice: normalized input + cached tokens add up the same both times

Nothing here is stored: the calls go straight to the adapters, not through the ledger, so a provider usage export will show them as a few
requests ModelSpend did not record. The report holds model ids, counts and error kinds, never keys or prompts.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

from .catalog import Catalog
from .catalog.models import Model
from .errors import ProviderError
from .pricing import cost_for
from .providers.base import ProviderAdapter, ProviderRequest, StreamEnd
from .types import Message, Usage

PROMPT = "Reply with exactly one word: pong"
SCHEMA = {"type": "object", "properties": {"answer": {"type": "string", "enum": ["pong"]}}, "required": ["answer"], "additionalProperties": False}
FILLER = "The committee reviewed the quarterly figures, compared them with last year, and recorded every difference in the shared ledger. " * 60  # ~1,500 tokens
MAX_OUT = 512  # room for a reasoning model to think before it says one word
STATUS_ORDER = {"fail": 3, "warn": 2, "ok": 1, "skip": 0}


@dataclass
class Check:
    name: str
    status: str  # ok | warn | fail | skip
    detail: str


@dataclass
class ProviderReport:
    provider: str
    model: str | None
    checks: list[Check] = field(default_factory=list)
    est_cost: float = 0.0

    @property
    def status(self) -> str:
        worst = max((c.status for c in self.checks), key=lambda s: STATUS_ORDER[s], default="skip")
        blocked = self.model is None or any(c.name in ("generate", "stream") and c.status == "skip" for c in self.checks)
        return "skip" if worst == "ok" and blocked else worst  # only the key was checked (or the account refused the calls): nothing about this provider was verified


def pick_model(catalog: Catalog, provider: str, visible: set[str] | None = None, explicit: str | None = None) -> Model | None:
    """The cheapest usable text model for this provider (priced, not retired), preferring ones the key can actually see."""
    if explicit:
        try:
            return catalog.get(explicit)
        except KeyError:
            return None
    live = [m for m in catalog.models if m.provider == provider and m.availability not in ("deprecated", "unavailable")]
    pool = [m for m in live if m.pricing and m.pricing.known and m.pricing.currency == "USD"]
    pool = [m for m in pool if visible is not None and m.id in visible] or pool
    if pool:
        return min(pool, key=lambda m: (m.pricing.input_per_mtok + m.pricing.output_per_mtok, m.ref))
    # nothing is priced yet (a provider you just synced): checking the adapter needs a model, not a price, so use one the key can see
    seen = [m for m in live if visible is not None and m.id in visible]
    return min(seen or live, key=lambda m: m.ref, default=None)


def _req(model: Model, text: str, *, max_out: int = MAX_OUT, schema: dict | None = None) -> ProviderRequest:
    return ProviderRequest(model=model.id, messages=[Message(role="user", content=text)], max_output_tokens=max_out, response_schema=schema, timeout_s=60.0,
                           reasoning_in_output=model.pricing.reasoning_in_output if model.pricing else True)


import re

BLOCKING = ("quota", "rate_limit")  # the provider refused for account reasons: nothing is learned about the adapter either way


def _err(e: BaseException) -> str:
    kind = getattr(e, "kind", type(e).__name__)
    hint = {"quota": " (the account is out of credit or at a spending limit: that is for you to fix, not an adapter problem)",
            "rate_limit": " (rate limited: wait a minute and run it again)"}.get(kind, "")
    return f"{kind}: {str(getattr(e, 'message', e))[:100]}{hint}"


def _retry_after(e: BaseException) -> float:
    """How long a rate-limited provider asked us to wait (its own 'retry in 22.5s'), clamped; 20 s when it does not say."""
    secs = getattr(e, "retry_after", None)
    if secs is None:
        m = re.search(r"retry (?:in|after)\s*([0-9]+(?:\.[0-9]+)?)\s*s", str(getattr(e, "message", e)), re.I)
        secs = float(m.group(1)) if m else None
    return 20.0 if secs is None else min(65.0, max(1.0, secs + 1.0))


async def _retrying(op: Any, sleep: Any) -> Any:
    """Run `op()`; if the provider rate-limits it, wait as long as it asks (once) and run it again. Free tiers allow only a handful of requests a minute."""
    try:
        return await op()
    except ProviderError as e:
        if e.kind != "rate_limit":
            raise
        await sleep(_retry_after(e))
        return await op()


def _fail(e: BaseException) -> tuple[str, str]:
    """(status, detail) for a call that raised. An account refusal is 'skip' (blocked), not 'fail': FAIL is reserved for 'a number would be wrong'."""
    if getattr(e, "kind", None) in BLOCKING:
        return "skip", "blocked, not verified: " + _err(e)
    return "fail", _err(e)


def _total(u: Usage) -> int:
    return u.input_tokens + u.cache_read_tokens + u.cache_write_tokens


def plan_cost(model: Model | None, cache: bool) -> float:
    """An upper bound on what the checks can cost: every call charged its full output allowance."""
    if not model or not model.pricing or not model.pricing.known:
        return 0.0
    out_calls = 2 + (1 if model.capabilities.structured_output else 0) + (3 if cache else 0)  # the cache check may repeat once after a wait
    inp = 60 * 3 + (3 * 1600 if cache else 0)
    return (inp * model.pricing.input_per_mtok + out_calls * MAX_OUT * model.pricing.output_per_mtok) / 1_000_000


CACHE_RETRY_WAIT_S = 5.0  # some providers build their cache a few seconds after the first call (DeepSeek documents this)


async def verify_provider(name: str, adapter: ProviderAdapter, catalog: Catalog, *, model: str | None = None, cache: bool = False,
                          sleep: Any = asyncio.sleep) -> ProviderReport:
    rep = ProviderReport(name, None)
    add = lambda n, s, d: rep.checks.append(Check(n, s, d))  # noqa: E731
    visible: set[str] | None = None
    try:
        visible = set(await _retrying(adapter.list_models, sleep))
        add("key", "ok", f"{len(visible)} models visible to this key")
    except Exception as e:  # noqa: BLE001
        add("key", *_fail(e))
        return rep  # nothing else can work without a key
    m = pick_model(catalog, name, visible, model)
    if m is None:
        add("model", "skip", f"no priced model in your catalog for {name}: run `modelspend catalog sync`, then `modelspend catalog price <ref> --input .. --output ..`")
        return rep
    rep.model = m.ref
    rep.est_cost = plan_cost(m, cache)
    if not (m.pricing and m.pricing.known):
        add("model", "ok", f"using {m.ref}: no price in your catalog, so no cost is shown (the checks do not need one)")
    if m.id not in visible and visible:
        add("model", "warn", f"{m.ref} is in your catalog but this key's model list does not include it")
    # 2. an unknown model must fail in a way the router understands
    try:
        probe = ProviderRequest(model="modelspend-verify-no-such-model", messages=[Message(role="user", content="hi")], max_output_tokens=8, timeout_s=30.0)
        await _retrying(lambda: adapter.generate(probe), sleep)
        add("errors", "fail", "an unknown model name was accepted: the provider (or its adapter) may be routing it somewhere else")
    except ProviderError as e:
        if e.kind in ("quota", "auth", "rate_limit"):  # refused before the model was even looked at: this says nothing about how model errors are classified
            add("errors", "skip", f"inconclusive: the provider refused for account reasons ({e.kind}) before looking at the model; fix that and run again")
        elif e.kind == "unknown":
            add("errors", "warn", f"unknown model -> unknown (not classified: failover may treat it wrongly). The provider said: HTTP {e.status_code}, {e.message[:100]}")
        else:
            add("errors", "ok", f"unknown model -> {e.kind}")
    except Exception as e:  # noqa: BLE001
        add("errors", "fail", f"an SDK error leaked past the adapter unclassified ({type(e).__name__}): the router cannot react to it")
    # 3. generate
    gen_usage: Usage | None = None
    try:
        t0 = time.monotonic()
        r = await _retrying(lambda: adapter.generate(_req(m, PROMPT)), sleep)
        ms = int((time.monotonic() - t0) * 1000)
        u = r.usage
        gen_usage = u
        cost = ""
        try:
            cost = f", ${cost_for(m, u):.6f} at catalog prices"
        except ValueError:
            pass
        facts = f"{u.input_tokens} in + {u.cache_read_tokens} cached, {u.output_tokens} out, finish={r.finish_reason}, {ms} ms{cost}"
        if u.output_tokens <= 0 or _total(u) <= 0:
            add("generate", "fail", f"the provider reported no token usage ({facts}): every cost for this provider would read zero")
        elif r.finish_reason == "length":
            add("generate", "warn", f"cut off at {MAX_OUT} tokens ({facts}): a reasoning model needs more room")
        elif not r.text.strip():
            add("generate", "warn", f"empty reply ({facts})")
        elif not 3 <= _total(u) <= 400:
            add("generate", "warn", f"input token count looks off for a one-line prompt ({facts})")
        else:
            add("generate", "ok", facts)
    except Exception as e:  # noqa: BLE001
        add("generate", *_fail(e))
    # 4. stream
    try:
        async def consume() -> tuple[str, Any]:
            got, last = "", None
            async for ch in adapter.stream(_req(m, PROMPT)):
                if isinstance(ch, StreamEnd):
                    last = ch.response
                else:
                    got += ch.text
            return got, last
        text, end = await _retrying(consume, sleep)
        if end is None:
            add("stream", "fail", "the stream ended without a final response")
        elif end.usage.output_tokens <= 0 and _total(end.usage) <= 0:
            add("stream", "warn", "streamed calls report no token usage: their cost would read zero and budgets would not see them")
        elif gen_usage and gen_usage.input_tokens and abs(_total(end.usage) - _total(gen_usage)) > max(5, 0.2 * _total(gen_usage)):
            add("stream", "warn", f"streamed input tokens ({_total(end.usage)}) differ from the same call not streamed ({_total(gen_usage)})")
        else:
            add("stream", "ok", f"{end.usage.input_tokens} in + {end.usage.cache_read_tokens} cached, {end.usage.output_tokens} out, text {'received' if text.strip() else 'EMPTY'}")
    except Exception as e:  # noqa: BLE001
        add("stream", *_fail(e))
    # 5. structured output, only where the catalog claims it
    if m.capabilities.structured_output:
        try:
            r = await _retrying(lambda: adapter.generate(_req(m, PROMPT + ". Put it in the 'answer' field.", schema=SCHEMA)), sleep)
            try:
                ok = json.loads(r.text).get("answer") == "pong"
            except (ValueError, AttributeError):
                ok = False
            add("structured", "ok" if ok else "fail", "reply parsed and matched the schema" if ok else f"the catalog says {m.ref} supports structured output, but the reply was not the schema's JSON")
        except Exception as e:  # noqa: BLE001
            add("structured", *_fail(e))
    else:
        add("structured", "skip", "the catalog does not claim structured output for this model")
    # 6. cached-token accounting (opt in: ~3,000 input tokens)
    if cache:
        try:
            long_call = lambda: adapter.generate(_req(m, FILLER + "\n" + PROMPT, max_out=MAX_OUT))  # noqa: E731
            a = (await _retrying(long_call, sleep)).usage
            b = (await _retrying(long_call, sleep)).usage
            waited = False
            if b.cache_read_tokens <= 0:
                await sleep(CACHE_RETRY_WAIT_S)  # a cache that is built asynchronously is not there for an immediate repeat
                b = (await _retrying(long_call, sleep)).usage
                waited = True
            if b.cache_read_tokens <= 0:
                add("cache", "skip", f"inconclusive: no cache hit was reported on the repeat, nor after a {CACHE_RETRY_WAIT_S:g} s wait ({_total(b)} tokens). Not every provider caches automatically")
            elif abs(_total(a) - _total(b)) > 0.05 * max(_total(a), 1):
                add("cache", "fail", f"cached tokens are not counted once: first call {_total(a)} total, second {_total(b)} (= {b.input_tokens} + {b.cache_read_tokens} cached). Reconciliation and costs would be off")
            else:
                add("cache", "ok", f"repeat: {b.input_tokens} uncached + {b.cache_read_tokens} cached = {_total(b)}, first call {_total(a)}: counted once"
                   + (f" (the cache was only there after a {CACHE_RETRY_WAIT_S:g} s wait)" if waited else ""))
        except Exception as e:  # noqa: BLE001
            add("cache", *_fail(e))
    return rep


async def verify_all(adapters: dict[str, ProviderAdapter], catalog: Catalog, targets: list[str], *, model: str | None = None, cache: bool = False) -> list[ProviderReport]:
    out = []
    for p in targets:
        if p not in adapters:
            out.append(ProviderReport(p, None, [Check("key", "skip", "no key configured")]))
            continue
        out.append(await verify_provider(p, adapters[p], catalog, model=model if model and model.startswith(p + "/") else None, cache=cache))
    return out


MARK = {"ok": "PASS", "warn": "WARN", "fail": "FAIL", "skip": "skip"}


def to_table(reports: list[ProviderReport]) -> str:
    lines = []
    for r in reports:
        lines.append(f"{r.provider}  {r.model or '-'}  =>  {MARK[r.status] if r.status != 'skip' else 'NOT VERIFIED'}")
        lines += [f"   {c.name:<11}{MARK[c.status]:<5} {c.detail}" for c in r.checks]
        lines.append("")
    return "\n".join(lines).rstrip()


def to_markdown(reports: list[ProviderReport], version: str, when: str) -> str:
    out = [f"### ModelSpend {version} live provider check ({when})", "", "Numbers and error kinds only: no keys, no prompts.", "",
           "| provider | model | check | result | detail |", "|---|---|---|---|---|"]
    for r in reports:
        for c in r.checks:
            out.append(f"| {r.provider} | {r.model or '-'} | {c.name} | {MARK[c.status]} | {c.detail.replace('|', '/')} |")
    return "\n".join(out) + "\n"
