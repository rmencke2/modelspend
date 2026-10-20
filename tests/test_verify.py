"""The live provider check: each finding it can make is exercised here against fakes, so that when it runs against a real API a FAIL means
something. (The real APIs are checked by running `modelspend verify` on your own key.)"""
from __future__ import annotations

import asyncio
import json

import pytest
from typer.testing import CliRunner

from modelspend.catalog import Catalog
from modelspend.cli import app
from modelspend.errors import ProviderError
from modelspend.providers.base import ProviderAdapter, ProviderResponse, StreamChunk, StreamEnd
from modelspend.providers.mock import ScriptedAdapter
from modelspend.types import Usage
from modelspend.verify import pick_model, plan_cost, to_markdown, to_table, verify_all, verify_provider

from test_whatif import _m


def cat(*extra):
    return Catalog.load(overrides={"add_models": [_m("openai", "big", 10.0, 40.0), _m("openai", "small", 0.1, 0.4), *extra]})


CAT = cat()


WAITS: list[float] = []


async def _fast(seconds):  # never really wait in tests; remember what was asked for
    WAITS.append(seconds)


def run(adapter, **kw):
    WAITS.clear()
    return asyncio.run(verify_provider("openai", adapter, kw.pop("catalog", CAT), sleep=_fast, **kw))


def by(rep):
    return {c.name: c for c in rep.checks}


class Fake(ScriptedAdapter):
    """Well-behaved by default; each test breaks one thing."""

    def __init__(self, *, models=("small", "big"), unknown="bad_request", usage=Usage(input_tokens=12, output_tokens=3), stream_usage="same",
                 reply="pong", finish="stop", leak=False, list_error=None, schema_reply=None, cached=None):
        super().__init__("openai", lambda r, n: "x", models=list(models))
        self.unknown, self.usage, self.stream_usage, self.reply, self.finish, self.leak = unknown, usage, stream_usage, reply, finish, leak
        self.list_error, self.schema_reply, self.cached, self.long = list_error, schema_reply, cached, 0

    async def list_models(self):
        if self.list_error:
            raise self.list_error
        return await super().list_models()

    async def generate(self, req):
        if req.model == "modelspend-verify-no-such-model":
            if self.leak:
                raise RuntimeError("raw SDK exception")
            if self.unknown == "accept":
                return ProviderResponse(text="hi", usage=self.usage)
            raise ProviderError("openai", self.unknown, "model not found")
        if req.response_schema:
            return ProviderResponse(text=self.schema_reply if self.schema_reply is not None else '{"answer": "pong"}', usage=self.usage)
        if self.cached and len(req.messages[0].content) > 1000:  # the long prompt: the second call is served from cache
            self.long += 1
            u = Usage(input_tokens=1500, output_tokens=3) if self.long % 2 else Usage(input_tokens=1500 - self.cached[0], cache_read_tokens=self.cached[1], output_tokens=3)
            return ProviderResponse(text="pong", usage=u)
        return ProviderResponse(text=self.reply, finish_reason=self.finish, usage=self.usage)

    async def stream(self, req):
        r = await self.generate(req)
        yield StreamChunk(text=r.text)
        if self.stream_usage == "missing":
            r = r.model_copy(update={"usage": Usage()})
        elif self.stream_usage == "different":
            r = r.model_copy(update={"usage": Usage(input_tokens=200, output_tokens=3)})
        yield StreamEnd(response=r)


def test_a_well_behaved_provider_passes_every_check():
    r = run(Fake())
    assert r.status == "ok" and r.model == "openai/small"  # cheapest priced model, and the key can see it
    assert [c.name for c in r.checks] == ["key", "errors", "generate", "stream", "structured"]
    assert by(r)["structured"].status == "skip"  # the catalog makes no claim, so nothing to disprove
    assert "$0.0000" in by(r)["generate"].detail


def test_a_failing_key_stops_everything_else():
    r = run(Fake(list_error=ProviderError("openai", "auth", "bad key")))
    assert r.status == "fail" and [c.name for c in r.checks] == ["key"] and "auth" in by(r)["key"].detail


def test_zero_reported_usage_is_a_failure_because_every_cost_would_read_zero():
    assert by(run(Fake(usage=Usage())))["generate"].status == "fail"


def test_streamed_calls_without_usage_are_flagged_and_so_are_mismatches():
    miss = by(run(Fake(stream_usage="missing")))["stream"]
    assert miss.status == "warn" and "report no token usage" in miss.detail
    diff = by(run(Fake(stream_usage="different")))["stream"]
    assert diff.status == "warn" and "differ from the same call not streamed" in diff.detail


def test_an_unknown_model_error_must_be_normalized():
    assert by(run(Fake(unknown="not_found_kind_x")))["errors"].status == "ok"  # any classified kind is fine
    assert by(run(Fake(unknown="unknown")))["errors"].status == "warn"
    assert by(run(Fake(leak=True)))["errors"].status == "fail"  # a raw SDK exception reaching the router
    assert by(run(Fake(unknown="accept")))["errors"].status == "fail"


def test_an_account_problem_makes_the_error_check_inconclusive_and_an_unclassified_error_shows_what_the_provider_said():
    for kind in ("quota", "auth", "rate_limit"):
        c = by(run(Fake(unknown=kind)))["errors"]
        assert c.status == "skip" and "account reasons" in c.detail and kind in c.detail  # not the adapter's fault, so not a FAIL or WARN
    w = by(run(Fake(unknown="unknown")))["errors"]
    assert w.status == "warn" and "model not found" in w.detail and "HTTP" in w.detail  # enough to diagnose without a debugger


def test_a_failed_call_on_an_empty_account_says_it_is_the_account():
    class Broke(Fake):
        async def generate(self, req):
            if req.model == "modelspend-verify-no-such-model":
                return await super().generate(req)
            raise ProviderError("openai", "quota", "Insufficient Balance", 402)
    r = run(Broke())
    assert by(r)["generate"].status == "fail" and "out of credit" in by(r)["generate"].detail and "not an adapter problem" in by(r)["generate"].detail


def test_a_cut_off_reply_is_a_warning_not_a_failure():
    r = by(run(Fake(finish="length")))["generate"]
    assert r.status == "warn" and "more room" in r.detail


def test_structured_output_is_checked_only_when_the_catalog_claims_it():
    claimed = Catalog.load(overrides={"add_models": [dict(_m("openai", "small", 0.1, 0.4), capabilities={"text": True, "structured_output": True})]})
    assert by(run(Fake(), catalog=claimed))["structured"].status == "ok"
    assert by(run(Fake(schema_reply="pong"), catalog=claimed))["structured"].status == "fail"  # claimed but the reply is not JSON


def test_a_model_the_key_cannot_see_is_noted_and_a_catalog_without_prices_skips():
    r = run(Fake(models=("something-else",)))
    assert by(r)["model"].status == "warn"
    empty = Catalog(models=[], base_version="t", content_hash="0" * 8)
    r2 = run(Fake(), catalog=empty)
    assert r2.status == "skip" and "catalog sync" in by(r2)["model"].detail


def test_cache_check_passes_when_cached_tokens_are_counted_once_and_fails_when_they_are_not():
    ok = run(Fake(cached=(1024, 1024)), cache=True)  # 476 uncached + 1024 cached = 1500, same as the first call
    assert by(ok)["cache"].status == "ok"
    bad = run(Fake(cached=(0, 1024)), cache=True)  # 1500 uncached + 1024 cached: cached counted twice
    assert by(bad)["cache"].status == "fail" and "counted once" in by(bad)["cache"].detail
    none = run(Fake(), cache=True)
    assert by(none)["cache"].status == "skip" and "inconclusive" in by(none)["cache"].detail
    assert "cache" not in by(run(Fake()))  # opt in only


def test_pick_model_prefers_visible_priced_cheapest_and_honours_an_explicit_choice():
    c = cat(_m("openai", "gone", 0.0001, 0.0001))
    assert pick_model(c, "openai", {"small", "big"}).id == "small"
    assert pick_model(c, "openai", {"big"}).id == "big"  # the cheapest one the key can see
    assert pick_model(c, "openai", None, "openai/big").id == "big" and pick_model(c, "openai", None, "openai/nope") is None
    assert plan_cost(CAT.get("openai/small"), cache=True) > plan_cost(CAT.get("openai/small"), cache=False) > 0 and plan_cost(None, False) == 0


def test_missing_providers_are_skipped_and_reports_carry_no_secrets():
    reps = asyncio.run(verify_all({"openai": Fake()}, CAT, ["openai", "deepseek"]))
    assert reps[1].status == "skip" and "no key" in reps[1].checks[0].detail
    t, md = to_table(reps), to_markdown(reps, "0.8", "2026-10-20")
    assert "PASS" in t and "| openai | openai/small | generate |" in md and "|" not in by(reps[0])["generate"].detail


def test_cli_asks_first_runs_and_exits_nonzero_on_a_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("MODELSPEND_HOME", str(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    out = CliRunner().invoke(app, ["verify", "openai"], input="n\n")
    assert out.exit_code == 1 and "Go ahead?" in out.output and "Most it can cost" in out.output and "sk-test-not-real" not in out.output
    assert CliRunner().invoke(app, ["verify", "nonsense", "--yes"]).exit_code != 0


def test_the_stream_line_shows_cached_tokens_so_a_cache_split_is_not_mistaken_for_missing_input():
    class Cached(Fake):
        async def stream(self, req):
            r = await self.generate(req)
            yield StreamChunk(text=r.text)
            yield StreamEnd(response=r.model_copy(update={"usage": Usage(input_tokens=1, cache_read_tokens=11, output_tokens=3)}))
    c = by(run(Cached()))["stream"]
    assert c.status == "ok" and "1 in + 11 cached" in c.detail  # 12 in total, the same as the non-streamed call


class Delayed(Fake):
    """A provider whose cache is built a few seconds after the first sight of a prompt (DeepSeek documents this): hits start on the third long call."""

    async def generate(self, req):
        if req.model != "modelspend-verify-no-such-model" and not req.response_schema and len(req.messages[0].content) > 1000:
            self.long += 1
            u = Usage(input_tokens=1500, output_tokens=3) if self.long < 3 else Usage(input_tokens=476, cache_read_tokens=1024, output_tokens=3)
            return ProviderResponse(text="pong", usage=u)
        return await super().generate(req)


def test_a_cache_that_is_built_a_few_seconds_late_is_found_after_one_wait():
    r = run(Delayed(), cache=True)
    c = by(r)["cache"]
    assert c.status == "ok" and "only there after a 5 s wait" in c.detail and WAITS == [5.0]


def test_no_hit_even_after_the_wait_is_inconclusive_and_waits_only_once_and_an_immediate_hit_never_waits():
    c = by(run(Fake(), cache=True))["cache"]
    assert c.status == "skip" and "nor after a 5 s wait" in c.detail and WAITS == [5.0]
    run(Fake(cached=(1024, 1024)), cache=True)
    assert WAITS == []


def test_a_provider_with_nothing_priced_still_gets_every_check_on_a_model_the_key_can_see():
    unpriced = Catalog.load(overrides={"add_models": [{"provider": "openai", "id": "zeta", "availability": "available", "verified_on": "2026-10-01", "capabilities": {"text": True}},
                                                       {"provider": "openai", "id": "alpha", "availability": "available", "verified_on": "2026-10-01", "capabilities": {"text": True}}]})
    assert pick_model(unpriced, "openai", {"zeta"}).id == "zeta"  # one the key can see beats alphabetical
    assert pick_model(unpriced, "openai", None).id == "alpha" and pick_model(unpriced, "nothing", None) is None
    r = run(Fake(models=("alpha",)), catalog=unpriced)
    assert r.model == "openai/alpha" and r.status == "ok" and "no price in your catalog" in by(r)["model"].detail
    assert [c.name for c in r.checks if c.status == "ok"] == ["key", "model", "errors", "generate", "stream"]
    assert "$" not in by(r)["generate"].detail  # no invented cost
    dep = Catalog.load(overrides={"add_models": [dict(_m("openai", "old", 1.0, 2.0), availability="deprecated")]})
    assert pick_model(dep, "openai", None) is None  # retired models are never picked, priced or not
    mixed = cat(_m("openai", "unpriced-twin", 0.0, 0.0))
    assert pick_model(Catalog.load(overrides={"add_models": [_m("openai", "priced", 5.0, 5.0), {"provider": "openai", "id": "aaa", "availability": "available",
                                                         "verified_on": "2026-10-01", "capabilities": {"text": True}}]}), "openai", None).id == "priced" and mixed


def test_the_plan_line_says_so_when_a_model_has_no_price(tmp_path, monkeypatch):
    monkeypatch.setenv("MODELSPEND_HOME", str(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    (tmp_path / "overrides.json").write_text(json.dumps({"add_models": [{"provider": "openai", "id": "only-one", "availability": "available", "verified_on": "2026-10-01",
                                                                       "capabilities": {"text": True}}]}))
    out = CliRunner().invoke(app, ["verify", "openai", "--model", "openai/only-one"], input="n\n")
    assert "not counting openai/only-one" in out.output and "no price in your catalog" in out.output


def test_the_cost_bound_covers_every_call_the_checks_can_make_including_the_cache_retry():
    m = CAT.get("openai/small")  # $0.10 in, $0.40 out per million
    plain = (60 * 3 * 0.1 + 2 * 512 * 0.4) / 1e6  # key and errors cost nothing; generate and stream each at the full allowance
    assert abs(plan_cost(m, cache=False) - plain) < 1e-12
    with_cache = ((60 * 3 + 3 * 1600) * 0.1 + (2 + 3) * 512 * 0.4) / 1e6  # three long calls: first, repeat, and the repeat after a wait
    assert abs(plan_cost(m, cache=True) - with_cache) < 1e-12
