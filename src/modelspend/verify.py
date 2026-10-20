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


# names that mark a model as something other than a plain text model (speech, images, live audio, ...): never the default pick
NOT_PLAIN_TEXT = ("tts", "image", "audio", "live", "embed", "omni", "imagen", "veo", "transcribe", "native", "robotics", "computer", "guard")
PREVIEWS = ("preview", "exp", "beta")


def _unpriced_order(m: Model) -> tuple:
    """For a provider with no prices yet: plain text models first, then `-latest` aliases (they point at what is served now), stable before preview."""
    i = m.id.lower()
    return (any(w in i for w in NOT_PLAIN_TEXT), not i.endswith("-latest"), any(w in i for w in PREVIEWS), m.ref)


def pick_models(catalog: Catalog, provider: str, visible: set[str] | None = None, explicit: str | None = None) -> list[Model]:
    """Candidates in the order to try them: the cheapest priced text models first, else (nothing priced yet) the most plausible ones the key can see."""
    if explicit:
        try:
            return [catalog.get(explicit)]
        except KeyError:
            return []
    live = [m for m in catalog.models if m.provider == provider and m.availability not in ("deprecated", "unavailable")]
    pool = [m for m in live if m.pricing and m.pricing.known and m.pricing.currency == "USD"]
    pool = [m for m in pool if visible is not None and m.id in visible] or pool
    if pool:
        return sorted(pool, key=lambda m: (m.pricing.input_per_mtok + m.pricing.output_per_mtok, m.ref))
    # nothing is priced yet (a provider you just synced): checking the adapter needs a model, not a price, so use one the key can see
    seen = [m for m in live if visible is not None and m.id in visible]
    return sorted(seen or live, key=_unpriced_order)


def pick_model(catalog: Catalog, provider: str, visible: set[str] | None = None, explicit: str | None = None) -> Model | None:
    found = pick_models(catalog, provider, visible, explicit)
    return found[0] if found else None


def _model_refused(e: BaseException) -> bool:
    """The key was refused THIS model (not found, retired, no longer offered to new accounts): a reason to try another model, not a verdict on the adapter."""
    if getattr(e, "zero_quota", False):
        return True  # quotas are per model: a model whose limit is 0 on this plan says nothing about the others
    if not isinstance(e, ProviderError) or e.kind != "bad_request":
        return False
    text = e.message.lower()
    return e.status_code == 404 or any(k in text for k in ("no longer available", "not found", "does not exist", "is not available", "not supported for"))


def _req(model: Model, text: str, *, max_out: int = MAX_OUT, schema: dict | None = None, timeout_s: float = 60.0) -> ProviderRequest:
    return ProviderRequest(model=model.id, messages=[Message(role="user", content=text)], max_output_tokens=max_out, response_schema=schema, timeout_s=timeout_s,
                           reasoning_in_output=model.pricing.reasoning_in_output if model.pricing else True)


import re

BLOCKING = ("quota", "rate_limit")  # the provider refused for account reasons: nothing is learned about the adapter either way


def _err(e: BaseException) -> str:
    kind = getattr(e, "kind", type(e).__name__)
    hint = {"quota": " (the account is out of credit or at a spending limit: that is for you to fix, not an adapter problem)",
            "rate_limit": " (rate limited: wait a minute and run it again)"}.get(kind, "")
    return f"{kind}: {str(getattr(e, 'message', e))[:220]}{hint}"


def _retry_after(e: BaseException) -> float:
    """How long a rate-limited provider asked us to wait (its own 'retry in 22.5s'), clamped; 20 s when it does not say."""
    secs = getattr(e, "retry_after", None)
    if secs is None:
        m = re.search(r"retry (?:in|after)\s*([0-9]+(?:\.[0-9]+)?)\s*s", str(getattr(e, "message", e)), re.I)
        secs = float(m.group(1)) if m else None
    return 20.0 if secs is None else min(65.0, max(1.0, secs + 1.0))


CALL_TIMEOUT_S = 90.0  # no single call may take longer: a provider that stalls is a finding, never a hang


async def _retrying(op: Any, sleep: Any, say: Any = None, scale: float = 1.0) -> Any:
    """Run `op()` (at most CALL_TIMEOUT_S); if the provider rate-limits it, wait as long as it asks (once) and run it again. Free tiers allow only a handful of requests a minute."""
    async def once() -> Any:
        try:
            return await asyncio.wait_for(op(), CALL_TIMEOUT_S * scale)
        except asyncio.TimeoutError:
            raise ProviderError("provider", "timeout", f"no answer within {CALL_TIMEOUT_S * scale:g} s") from None
    try:
        return await once()
    except ProviderError as e:
        if e.kind != "rate_limit" or getattr(e, "zero_quota", False):
            raise  # a limit of 0 is not a wait
        wait = _retry_after(e)
        if say:
            say(f"rate limited by the provider: waiting {wait:g} s, then trying once more")
        await sleep(wait)
        return await once()


def _fail(e: BaseException) -> tuple[str, str]:
    """(status, detail) for a call that raised. An account refusal is 'skip' (blocked), not 'fail': FAIL is reserved for 'a number would be wrong'."""
    if _model_refused(e):  # checked first: a limit of 0 is also a rate limit, but the useful advice is about the model
        return "skip", "blocked, not verified: this key is refused the model (retired, not offered to new accounts, or no quota on your plan); name another with --model. " + _err(e)
    if getattr(e, "kind", None) in BLOCKING:
        return "skip", "blocked, not verified: " + _err(e)
    return "fail", _err(e)


def _out(u: Usage) -> str:
    """Output tokens, with the thinking part named: a reasoning model's cost depends on it being counted once, inside the output."""
    return f"{u.output_tokens} out" + (f" (of which {u.reasoning_tokens} reasoning)" if u.reasoning_tokens else "")


def _total(u: Usage) -> int:
    return u.input_tokens + u.cache_read_tokens + u.cache_write_tokens


def plan_cost(model: Model | None, cache: bool, structured: bool = False) -> float:
    """An upper bound on what the checks can cost: every call charged its full output allowance."""
    if not model or not model.pricing or not model.pricing.known:
        return 0.0
    out_calls = 2 + (1 if (model.capabilities.structured_output or structured) else 0) + (3 if cache else 0)  # the cache check may repeat once after a wait
    inp = 60 * 3 + (3 * 1600 if cache else 0)
    return (inp * model.pricing.input_per_mtok + out_calls * MAX_OUT * model.pricing.output_per_mtok) / 1_000_000


CACHE_RETRY_WAIT_S = 5.0  # some providers build their cache a few seconds after the first call (DeepSeek documents this)


async def verify_provider(name: str, adapter: ProviderAdapter, catalog: Catalog, *, model: str | None = None, cache: bool = False,
                          sleep: Any = asyncio.sleep, structured: bool = False, progress: Any = None, timeout_s: float = 60.0, clock: Any = time.monotonic) -> ProviderReport:
    rep = ProviderReport(name, None)

    def say(text: str) -> None:
        if progress:
            progress(f"{name}: {text}")

    def add(n: str, s: str, d: str) -> None:
        rep.checks.append(Check(n, s, d))
        say(f"{n:<11}{MARK[s]:<5} {d[:200]}")  # shown as each check finishes, so a slow provider never looks like a hang

    state: dict[str, Any] = {"blocked": None, "timeout": timeout_s}

    async def call(op: Any) -> Any:
        """Every provider call goes through here. Once the provider has refused for account reasons even after the one retry (no credit, a quota of zero),
        the rest are not attempted: waiting out the same refusal for every remaining check would only look like a hang."""
        if state["blocked"] is not None:
            raise state["blocked"]
        try:
            return await _retrying(op, sleep, say, state["timeout"] / 60.0)  # the hard cap grows with the allowance, so the two never disagree
        except ProviderError as e:
            if e.kind in BLOCKING:
                state["blocked"] = e
            raise
    visible: set[str] | None = None
    try:
        visible = set(await call(adapter.list_models))
        add("key", "ok", f"{len(visible)} models visible to this key")
    except Exception as e:  # noqa: BLE001
        add("key", *_fail(e))
        return rep  # nothing else can work without a key
    cands = pick_models(catalog, name, visible, model)
    m = cands[0] if cands else None
    fallbacks = cands[1:3]  # (a model you named is the only candidate, so it is never swapped for another)
    if m is None:
        add("model", "skip", f"no priced model in your catalog for {name}: run `modelspend catalog sync`, then `modelspend catalog price <ref> --input .. --output ..`")
        return rep
    rep.model = m.ref
    say(f"checking {m.ref}")
    rep.est_cost = plan_cost(m, cache, structured)
    if not (m.pricing and m.pricing.known):
        add("model", "ok", f"using {m.ref}: no price in your catalog, so no cost is shown (the checks do not need one)")
    if m.id not in visible and visible:
        add("model", "warn", f"{m.ref} is in your catalog but this key's model list does not include it")
    # 2. an unknown model must fail in a way the router understands
    try:
        probe = ProviderRequest(model="modelspend-verify-no-such-model", messages=[Message(role="user", content="hi")], max_output_tokens=8, timeout_s=30.0)
        await call(lambda: adapter.generate(probe))
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
    r: Any = None
    gen_err: BaseException | None = None
    while r is None and gen_err is None:
        try:
            t0 = clock()
            r = await call(lambda: adapter.generate(_req(m, PROMPT, timeout_s=state["timeout"])))
            ms = int((clock() - t0) * 1000)
        except Exception as e:  # noqa: BLE001
            if _model_refused(e) and fallbacks:
                nxt = fallbacks.pop(0)
                add("model", "skip", f"{m.ref} was refused by this key (HTTP {getattr(e, 'status_code', '?')}: {e.message[:160]}); trying {nxt.ref}")  # type: ignore[attr-defined]
                m, rep.model = nxt, nxt.ref
                state["blocked"] = None  # the refusal was about that model, so the next one gets a fair try
                rep.est_cost = plan_cost(m, cache, structured)
                if not (m.pricing and m.pricing.known):
                    add("model", "ok", f"using {m.ref}: no price in your catalog, so no cost is shown")
                continue
            gen_err = e
    try:
        if gen_err is not None:
            raise gen_err
        u = r.usage
        gen_usage = u
        cost = ""
        try:
            cost = f", ${cost_for(m, u):.6f} at catalog prices"
        except ValueError:
            pass
        facts = f"{u.input_tokens} in + {u.cache_read_tokens} cached, {_out(u)}, finish={r.finish_reason}, {ms} ms{cost}"
        if ms / 1000 * 3 > state["timeout"]:  # a thinking model that needs 40 s for one word would be cut off on the next call: give it room in proportion
            state["timeout"] = min(300.0, ms / 1000 * 3)
            say(f"slow model ({ms / 1000:.0f} s for a one-word answer): allowing {state['timeout']:.0f} s per call from here")
            facts += " (slow)"
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
        if _model_refused(e):
            return rep  # every later step would ask the same refused model again
    # 4. stream
    try:
        async def consume() -> tuple[str, Any]:
            got, last = "", None
            async for ch in adapter.stream(_req(m, PROMPT, timeout_s=state["timeout"])):
                if isinstance(ch, StreamEnd):
                    last = ch.response
                else:
                    got += ch.text
            return got, last
        text, end = await call(consume)
        if end is None:
            add("stream", "fail", "the stream ended without a final response")
        elif end.usage.output_tokens <= 0 and _total(end.usage) <= 0:
            add("stream", "warn", "streamed calls report no token usage: their cost would read zero and budgets would not see them")
        elif gen_usage and gen_usage.input_tokens and abs(_total(end.usage) - _total(gen_usage)) > max(5, 0.2 * _total(gen_usage)):
            add("stream", "warn", f"streamed input tokens ({_total(end.usage)}) differ from the same call not streamed ({_total(gen_usage)})")
        else:
            add("stream", "ok", f"{end.usage.input_tokens} in + {end.usage.cache_read_tokens} cached, {_out(end.usage)}, text {'received' if text.strip() else 'EMPTY'}")
    except Exception as e:  # noqa: BLE001
        add("stream", *_fail(e))
    # 5. structured output, only where the catalog claims it
    claimed = bool(m.capabilities.structured_output)
    if claimed or structured:
        try:
            r = await call(lambda: adapter.generate(_req(m, PROMPT + ". Put it in the 'answer' field.", schema=SCHEMA, timeout_s=state["timeout"])))
            try:
                ok = json.loads(r.text).get("answer") == "pong"
            except (ValueError, AttributeError):
                ok = False
            if ok and not claimed:  # asked for, and it works: say how to record that, which is the step ModelSpend requires before it will route a schema request here
                add("structured", "ok", f"works on this model. To let ModelSpend use it for structured output: modelspend catalog set {m.ref} capabilities.structured_output=true")
            elif ok:
                add("structured", "ok", "reply parsed and matched the schema")
            elif claimed:
                add("structured", "fail", f"the catalog says {m.ref} supports structured output, but the reply was not the schema's JSON")
            else:
                add("structured", "warn", f"{m.ref} did not return the schema's JSON: do not mark it as supporting structured output")
        except Exception as e:  # noqa: BLE001
            add("structured", *_fail(e))
    else:
        add("structured", "skip", "the catalog does not claim structured output for this model (test it with --structured)")
    # 6. cached-token accounting (opt in: ~3,000 input tokens)
    if cache:
        try:
            long_call = lambda: adapter.generate(_req(m, FILLER + "\n" + PROMPT, max_out=MAX_OUT, timeout_s=state["timeout"]))  # noqa: E731
            a = (await call(long_call)).usage
            b = (await call(long_call)).usage
            waited = False
            if b.cache_read_tokens <= 0:
                await sleep(CACHE_RETRY_WAIT_S)  # a cache that is built asynchronously is not there for an immediate repeat
                b = (await call(long_call)).usage
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


async def verify_all(adapters: dict[str, ProviderAdapter], catalog: Catalog, targets: list[str], *, model: str | None = None, cache: bool = False,
                     structured: bool = False, progress: Any = None, timeout_s: float = 60.0) -> list[ProviderReport]:
    out = []
    for p in targets:
        if p not in adapters:
            out.append(ProviderReport(p, None, [Check("key", "skip", "no key configured")]))
            continue
        out.append(await verify_provider(p, adapters[p], catalog, model=model if model and model.startswith(p + "/") else None, cache=cache, structured=structured, progress=progress, timeout_s=timeout_s))
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
