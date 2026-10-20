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
        assert c.status == "skip" and "account or capacity reasons" in c.detail and kind in c.detail  # not the adapter's fault, so not a FAIL or WARN
    w = by(run(Fake(unknown="unknown")))["errors"]
    assert w.status == "warn" and "model not found" in w.detail and "HTTP" in w.detail  # enough to diagnose without a debugger


class Broke(Fake):
    """An account that cannot be charged (DeepSeek answers HTTP 402 'Insufficient Balance'), or is rate limited."""
    kind, status = "quota", 402

    async def generate(self, req):  # refuses everything, the unknown-model probe included: the balance is checked before the model is looked at
        raise ProviderError("openai", self.kind, "Insufficient Balance", self.status)

    async def stream(self, req):
        raise ProviderError("openai", self.kind, "Insufficient Balance", self.status)
        yield  # pragma: no cover


def test_an_empty_account_is_blocked_not_failed_so_it_does_not_look_like_an_adapter_fault():
    r = run(Broke(), cache=True)
    for name in ("generate", "stream", "cache"):
        c = by(r)[name]
        assert c.status == "skip" and c.detail.startswith("blocked, not verified") and "out of credit" in c.detail and "not an adapter problem" in c.detail
    assert r.status == "skip" and by(r)["errors"].status == "skip"  # nothing was learned, so it is neither PASS nor FAIL
    t = to_table([r])
    assert "NOT VERIFIED" in t and "FAIL" not in t and "PASS  " in t.split("key")[1].split("\n")[0]  # the key check itself did pass


def test_rate_limits_are_blocked_too_but_a_real_adapter_failure_is_still_a_failure():
    rl = Broke()
    rl.kind, rl.status = "rate_limit", 429
    r = run(rl)
    assert by(r)["generate"].status == "skip" and "rate limited" in by(r)["generate"].detail and r.status == "skip"
    server = Broke()
    server.kind, server.status = "server", 500
    r2 = run(server)
    assert by(r2)["generate"].status == "fail" and r2.status == "fail"  # a 500 is evidence about the provider, not about the account


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
    assert c.status == "skip" and "tried" not in c.detail and "no cache hit was reported for prompts of" in c.detail and "5 s wait" in c.detail
    assert WAITS == [5.0, 5.0]  # one wait per prompt size, never more
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
    with_cache = ((60 * 3 + 3 * 1600 + 3 * 1600 * 4) * 0.1 + (2 + 6) * 512 * 0.4) / 1e6  # six long calls: first, repeat and the repeat after a wait, for a short and a longer prompt
    assert abs(plan_cost(m, cache=True) - with_cache) < 1e-12


# ---- free tiers: a few requests a minute ----
from modelspend.verify import _retry_after  # noqa: E402


class Limited(Fake):
    """Rate limits the first `fail_first` attempts of each distinct call, saying how long to wait (as Gemini's free tier does)."""

    def __init__(self, fail_first=1, always=False, **kw):
        super().__init__(**kw)
        self.fail_first, self.always, self.tries = fail_first, always, {}

    def _gate(self, key):
        self.tries[key] = self.tries.get(key, 0) + 1
        if self.always or self.tries[key] <= self.fail_first:
            raise ProviderError("openai", "rate_limit", "You exceeded your current quota. Please retry in 2.5s.", 429)

    async def list_models(self):
        self._gate("list")
        return await super().list_models()

    async def generate(self, req):
        self._gate("probe" if req.model == "modelspend-verify-no-such-model" else ("long" if len(req.messages[0].content) > 1000 else ("schema" if req.response_schema else "gen")))
        return await super().generate(req)

    async def stream(self, req):
        self._gate("stream")
        async for x in super().stream(req):
            yield x


def test_a_rate_limited_provider_is_waited_for_once_per_call_using_its_own_hint():
    r = run(Limited(fail_first=1), cache=True)
    assert r.status == "ok", to_table([r])
    assert set(WAITS) <= {3.5, 5.0}  # 2.5 s from the provider's message + 1 s of margin; the 5.0 is the separate wait for a late cache
    assert WAITS.count(3.5) == 5  # list, probe, generate, stream and the long call each hit the limit once, and each was waited for once


def test_a_provider_that_stays_rate_limited_is_blocked_not_failed_and_is_only_retried_once():
    r = run(Limited(always=True))
    assert r.status == "skip" and by(r)["key"].status == "skip" and "rate limited" in by(r)["key"].detail and WAITS == [3.5]
    assert [c.name for c in r.checks] == ["key"]  # nothing else can run, and nothing was spammed at a limited key


def test_a_bad_key_is_a_failure_but_an_empty_or_limited_key_listing_is_only_blocked():
    class BadKey(Fake):
        async def list_models(self):
            raise ProviderError("openai", "auth", "API key not valid", 400)
    r = run(BadKey())
    assert r.status == "fail" and by(r)["key"].status == "fail"
    class Empty(Fake):
        async def list_models(self):
            raise ProviderError("openai", "quota", "Insufficient Balance", 402)
    assert run(Empty()).status == "skip"


def test_retry_after_reads_the_providers_hint_and_clamps_it():
    assert _retry_after(ProviderError("g", "rate_limit", "Please retry in 22.5s.")) == 23.5
    assert _retry_after(ProviderError("g", "rate_limit", "retry after 3s")) == 4.0
    assert _retry_after(ProviderError("g", "rate_limit", "Please retry in 900s")) == 65.0  # never an unbounded wait
    assert _retry_after(ProviderError("g", "rate_limit", "slow down")) == 20.0 and _retry_after(ProviderError("g", "rate_limit", "retry in 0s")) == 1.0


def test_the_wait_prefers_the_classifiers_retry_after_over_scanning_the_message():
    e = ProviderError("g", "rate_limit", "something unrelated")
    e.retry_after = 7.0
    assert _retry_after(e) == 8.0


def test_reasoning_tokens_are_shown_inside_the_output_count_so_a_thinking_model_can_be_checked():
    think = Fake(usage=Usage(input_tokens=12, output_tokens=40, reasoning_tokens=35))
    g = by(run(think))["generate"]
    assert g.status == "ok" and "40 out (of which 35 reasoning)" in g.detail
    assert "of which" not in by(run(Fake()))["generate"].detail  # nothing to show for a model that does not think


# ---- a listed model the key is refused, and testing structured output on demand ----
from modelspend.verify import pick_models  # noqa: E402


def bare(*ids, provider="openai"):
    return Catalog.load(overrides={"add_models": [{"provider": provider, "id": i, "availability": "available", "verified_on": "2026-10-20", "capabilities": {"text": True}} for i in ids]})


GEM = ("gemini-2.5-flash", "gemini-2.5-flash-image", "gemini-2.5-flash-preview-tts", "gemini-3-flash-preview", "gemini-flash-latest", "gemini-omni-flash-preview", "gemini-3.5-flash")


def test_with_nothing_priced_plain_text_models_come_first_latest_aliases_before_dated_ones_and_previews_after_stable():
    order = [m.id for m in pick_models(bare(*GEM, provider="google"), "google", set(GEM))]
    assert order[0] == "gemini-flash-latest"  # an alias points at what is served now
    assert order[1:3] == ["gemini-2.5-flash", "gemini-3.5-flash"] and order[3] == "gemini-3-flash-preview"  # stable before preview
    assert set(order[4:]) == {"gemini-2.5-flash-image", "gemini-2.5-flash-preview-tts", "gemini-omni-flash-preview"}  # image, speech and omni variants last
    priced = cat()  # with prices the order is simply cheapest first
    assert [m.id for m in pick_models(priced, "openai", {"small", "big"})] == ["small", "big"]
    assert [m.id for m in pick_models(priced, "openai", None, "openai/big")] == ["big"] and pick_models(priced, "openai", None, "openai/none") == []


class Refusing(Fake):
    """Lists many models but refuses some of them the way Google does for a model that is no longer offered to new accounts."""

    def __init__(self, refuse, status=404, kind="bad_request", message="This model models/x is no longer available to new users.", **kw):
        super().__init__(**kw)
        self.refuse, self.status, self.kind, self.message, self.asked = set(refuse), status, kind, message, []

    async def generate(self, req):
        if req.model != "modelspend-verify-no-such-model":
            self.asked.append(req.model)
            if req.model in self.refuse:
                raise ProviderError("openai", self.kind, self.message, self.status)
        return await super().generate(req)


def test_a_model_the_key_is_refused_is_skipped_and_the_next_candidate_is_used():
    cat3 = bare("a-old", "b-latest", "c-new")
    ad = Refusing(refuse={"b-latest", "a-old"}, models=("a-old", "b-latest", "c-new"))
    r = run(ad, catalog=cat3)
    assert r.model == "openai/c-new" and r.status == "ok" and ad.asked[:3] == ["b-latest", "a-old", "c-new"]
    skips = [c.detail for c in r.checks if c.name == "model" and c.status == "skip"]
    assert len(skips) == 2 and "b-latest was refused by this key" in skips[0] and "trying openai/a-old" in skips[0] and "no longer available" in skips[0]


def test_when_every_candidate_is_refused_it_is_not_verified_not_failed_and_only_three_are_tried():
    ad = Refusing(refuse={"m1", "m2", "m3", "m4"}, models=("m1", "m2", "m3", "m4"))
    r = run(ad, catalog=bare("m1", "m2", "m3", "m4"))
    assert r.status == "skip" and by(r)["generate"].status == "skip" and "refused the model" in by(r)["generate"].detail and "--model" in by(r)["generate"].detail
    assert len(ad.asked) == 3  # not the whole catalog


def test_a_named_model_is_never_swapped_and_a_server_error_does_not_trigger_the_fallback():
    ad = Refusing(refuse={"m1"}, models=("m1", "m2"))
    r = run(ad, catalog=bare("m1", "m2"), model="openai/m1")
    assert ad.asked == ["m1"] and r.status == "skip"
    srv = Refusing(refuse={"m1"}, status=500, kind="server", message="boom", models=("m1", "m2"))
    r2 = run(srv, catalog=bare("m1", "m2"))
    assert set(srv.asked) == {"m1"} and by(r2)["generate"].status == "fail"  # never switched to m2; a 500 says something about the provider, so it is not hidden by trying another model


def test_structured_output_can_be_tested_on_demand_and_a_pass_prints_the_command_that_records_it():
    plain = by(run(Fake()))["structured"]
    assert plain.status == "skip" and "--structured" in plain.detail
    ok = by(run(Fake(), structured=True))["structured"]
    assert ok.status == "ok" and "modelspend catalog set openai/small capabilities.structured_output=true" in ok.detail
    bad = by(run(Fake(schema_reply="pong"), structured=True))["structured"]
    assert bad.status == "warn" and "do not mark it" in bad.detail  # not claimed, so not a failure of the catalog's word
    claimed = Catalog.load(overrides={"add_models": [dict(_m("openai", "small", 0.1, 0.4), capabilities={"text": True, "structured_output": True})]})
    assert by(run(Fake(schema_reply="pong"), catalog=claimed))["structured"].status == "fail"  # claimed and false is still a failure
    assert plan_cost(CAT.get("openai/small"), False, True) > plan_cost(CAT.get("openai/small"), False, False)


def test_error_details_keep_enough_of_the_message_to_read_the_reason():
    long = "This model models/gemini-2.5-flash is no longer available to new users. Please update your code to use a newer model for the latest features and improvements."
    class Gone(Fake):
        async def generate(self, req):
            raise ProviderError("openai", "server", long, 500)
    d = by(run(Gone()))["generate"].detail
    assert "latest features and improvements" in d  # used to be cut at 100 characters, mid-sentence


def test_only_a_refusal_of_the_model_triggers_the_fallback_not_any_bad_request():
    gone = Refusing(refuse={"m1"}, message="Resource unavailable", models=("m1", "m2"))  # only the 404 status says it is the model
    r = run(gone, catalog=bare("m1", "m2"))
    assert r.model == "openai/m2" and r.status == "ok"
    bad = Refusing(refuse={"m1", "m2"}, status=400, message="Unsupported value: temperature", models=("m1", "m2"))
    r2 = run(bad, catalog=bare("m1", "m2"))
    assert set(bad.asked) == {"m1"} and by(r2)["generate"].status == "fail"  # a real bad request about OUR request is not hidden by trying another model


# ---- a slow or limited provider must never look like a hang ----
class Slow(Fake):
    async def generate(self, req):
        if req.model != "modelspend-verify-no-such-model":
            await asyncio.sleep(3)
        return await super().generate(req)


def test_a_call_that_never_answers_is_a_finding_not_a_hang(monkeypatch):
    import modelspend.verify as vmod
    monkeypatch.setattr(vmod, "CALL_TIMEOUT_S", 0.05)
    r = run(Slow())
    g = by(r)["generate"]
    assert g.status == "fail" and "no answer within" in g.detail and r.status == "fail"  # a stalled provider says something about the provider


class AlwaysLimited(Fake):
    """The key lists models fine, but every generation is refused (a free-tier quota of zero, or no credit)."""

    def __init__(self, kind="rate_limit", status=429, **kw):
        super().__init__(**kw)
        self.kind, self.status, self.attempts = kind, status, 0

    async def generate(self, req):
        self.attempts += 1
        raise ProviderError("openai", self.kind, "You exceeded your current quota. Please retry in 2.5s.", self.status)


def test_once_a_provider_is_persistently_refused_the_remaining_checks_are_not_attempted_or_waited_for():
    ad = AlwaysLimited()
    r = run(ad, cache=True)
    assert ad.attempts == 2 and WAITS == [3.5]  # one try and one retry in total, not one pair per check
    assert r.status == "skip" and all(by(r)[n].status == "skip" for n in ("errors", "generate", "stream", "cache"))
    assert all("rate limited" in by(r)[n].detail for n in ("generate", "stream", "cache"))
    nocredit = AlwaysLimited(kind="quota", status=402)
    r2 = run(nocredit, cache=True)
    assert nocredit.attempts == 1 and WAITS == [] and r2.status == "skip"  # no credit is not retried, and not repeated for every check


def test_progress_is_reported_as_each_check_finishes_and_waits_are_announced():
    lines: list[str] = []
    run(Limited(fail_first=1), progress=lines.append)
    text = "\n".join(lines)
    assert "openai: checking openai/small" in text
    assert "openai: rate limited by the provider: waiting 3.5 s, then trying once more" in text
    assert [ln for ln in lines if ln.startswith("openai: generate")] and [ln for ln in lines if ln.startswith("openai: stream")]
    assert "PASS" in text


class ZeroQuota(Fake):
    """Per-model quotas, as on Gemini's free tier: some models have a limit of 0 on this plan, others work."""

    def __init__(self, zero, **kw):
        super().__init__(**kw)
        self.zero, self.asked = set(zero), []

    async def generate(self, req):
        if req.model != "modelspend-verify-no-such-model":
            self.asked.append(req.model)
            if req.model in self.zero:
                e = ProviderError("openai", "rate_limit", "You exceeded your current quota. Please retry in 4.2s.", 429)
                e.zero_quota = True
                raise e
        return await super().generate(req)


def test_a_model_with_a_quota_of_zero_is_skipped_without_waiting_and_the_next_one_is_tried():
    ad = ZeroQuota(zero={"a-latest"}, models=("a-latest", "b", "c"))
    r = run(ad, catalog=bare("a-latest", "b", "c"))
    assert r.model == "openai/b" and r.status == "ok" and WAITS == []  # no point waiting out a limit of zero
    assert ad.asked[0] == "a-latest" and "b" in ad.asked
    assert any("a-latest was refused by this key" in c.detail and "trying openai/b" in c.detail for c in r.checks if c.name == "model")
    assert by(r)["stream"].status == "ok"  # the earlier block did not carry over to the next model


def test_when_every_candidate_has_a_quota_of_zero_it_is_not_verified_after_three_tries():
    ad = ZeroQuota(zero={"a", "b", "c", "d"}, models=("a", "b", "c", "d"))
    r = run(ad, catalog=bare("a", "b", "c", "d"))
    assert r.status == "skip" and len(ad.asked) == 3 and WAITS == [] and "no quota on your plan" in by(r)["generate"].detail


# ---- slow (thinking) models ----
class Timed(Fake):
    """Records the timeout each call was given; `delay` is how long a real answer takes."""

    def __init__(self, delay=0.0, **kw):
        super().__init__(**kw)
        self.delay, self.timeouts = delay, []

    async def generate(self, req):
        if req.model != "modelspend-verify-no-such-model":
            self.timeouts.append(req.timeout_s)
            await asyncio.sleep(self.delay)
        return await super().generate(req)


def test_a_fast_model_keeps_the_default_allowance_and_a_slow_one_gets_room_in_proportion():
    fast = Timed(delay=0.0)
    run(fast, cache=True)
    assert set(fast.timeouts) == {60.0}
    slow = Timed(delay=0.02)
    lines: list[str] = []
    r = run(slow, cache=True, timeout_s=0.03, progress=lines.append)
    assert slow.timeouts[0] == 0.03 and all(x > 0.03 for x in slow.timeouts[1:])  # raised after the first answer showed how slow it is
    assert any("slow model" in ln and "allowing" in ln for ln in lines) and "(slow)" in by(r)["generate"].detail


def test_the_allowance_is_raised_but_never_beyond_five_minutes():
    ticks = iter([0.0, 200.0])  # the first answer "took" 200 s, which would ask for 600 s
    g = Timed()
    run(g, cache=True, clock=lambda: next(ticks))
    assert g.timeouts[0] == 60.0 and all(x == 300.0 for x in g.timeouts[1:])  # capped at five minutes


def test_the_hard_cap_on_a_single_call_grows_with_the_allowance_so_the_two_never_disagree(monkeypatch):
    import modelspend.verify as vmod
    monkeypatch.setattr(vmod, "CALL_TIMEOUT_S", 0.05)
    r = run(Timed(delay=0.08), timeout_s=60.0)  # cap 0.05 s: an 0.08 s answer is cut off
    assert by(r)["generate"].status == "fail" and "no answer within" in by(r)["generate"].detail
    r2 = run(Timed(delay=0.08), timeout_s=120.0)  # allowance doubled, so the cap is 0.1 s and the same answer is accepted
    assert by(r2)["generate"].status in ("ok", "warn")


def test_verify_all_passes_the_timeout_through():
    t = Timed()
    asyncio.run(verify_all({"openai": t}, CAT, ["openai"], timeout_s=123.0))
    assert set(t.timeouts) == {123.0}


# ---- a busy provider (Gemini answers HTTP 503 'high demand' at times) ----
class Busy(Fake):
    """Answers 503 'high demand' to the first `fail_first` attempts of each distinct call."""

    def __init__(self, fail_first=1, always=False, status=503, message="This model is currently experiencing high demand. Spikes in demand are usually temporary.", **kw):
        super().__init__(**kw)
        self.fail_first, self.always, self.status, self.message, self.tries = fail_first, always, status, message, {}

    def _gate(self, key):
        self.tries[key] = self.tries.get(key, 0) + 1
        if self.always or self.tries[key] <= self.fail_first:
            raise ProviderError("openai", "server", self.message, self.status)

    async def stream(self, req):
        self._gate("stream")
        async for x in super().stream(req):
            yield x

    async def generate(self, req):
        if req.model != "modelspend-verify-no-such-model":
            self._gate("gen" if not req.response_schema and len(req.messages[0].content) < 1000 else "other")
        return await super().generate(req)


def test_a_busy_provider_is_waited_for_once_and_if_it_stays_busy_the_result_is_not_verified_not_failed():
    lines: list[str] = []
    r = run(Busy(fail_first=1), progress=lines.append)
    assert r.status == "ok" and WAITS.count(8.0) >= 1 and any("overloaded (HTTP 503)" in ln and "waiting 8 s" in ln for ln in lines)
    r2 = run(Busy(always=True))
    assert r2.status == "skip" and by(r2)["generate"].status == "skip" and "overloaded right now" in by(r2)["generate"].detail
    assert by(r2)["stream"].status == "skip" and WAITS == [8.0]  # waited once, then the rest were not attempted


def test_a_plain_server_error_is_still_a_failure_and_is_not_retried():
    r = run(Busy(always=True, status=500, message="internal error"))
    assert by(r)["generate"].status == "fail" and r.status == "fail" and WAITS == []


# ---- caches that only work above a minimum prompt size ----
class BigOnly(Fake):
    """Caches only prompts above ~20,000 characters (the longer prompt); the first sight of each prompt is never a hit."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.seen, self.long_calls = set(), 0

    async def generate(self, req):
        text = req.messages[0].content or ""
        if len(text) > 1000 and not req.response_schema and req.model != "modelspend-verify-no-such-model":
            self.long_calls += 1
            first = text not in self.seen
            self.seen.add(text)
            total = max(1, len(text) // 4)
            u = Usage(input_tokens=total, output_tokens=3) if (first or len(text) < 20000) else Usage(input_tokens=total - 1024, cache_read_tokens=1024, output_tokens=3)
            return ProviderResponse(text="pong", usage=u)
        return await super().generate(req)


def test_a_provider_that_only_caches_long_prompts_is_found_with_the_longer_prompt():
    ad = BigOnly()
    c = by(run(ad, cache=True))["cache"]
    assert c.status == "ok" and "only on the longer prompt" in c.detail and "counted once" in c.detail
    assert ad.long_calls == 5  # three tries for the short prompt (first, repeat, repeat after the wait), then two for the long one: it hit on the repeat
    assert WAITS == [5.0]  # one wait, for the short prompt; the long one hit at once


def test_a_short_prompt_that_already_hits_never_triggers_the_longer_one():
    ad = Fake(cached=(1024, 1024))
    c = by(run(ad, cache=True))["cache"]
    assert c.status == "ok" and "only on the longer prompt" not in c.detail and ad.long == 2 and WAITS == []


def test_overload_is_recognised_by_status_or_by_wording_but_only_for_server_errors():
    from modelspend.verify import _overloaded
    assert _overloaded(ProviderError("g", "server", "backend error", 503))  # the status alone is enough
    assert _overloaded(ProviderError("g", "server", "The model is overloaded. Please try again later.", 500))  # and so is the wording alone
    assert not _overloaded(ProviderError("g", "server", "internal error", 500))
    assert not _overloaded(ProviderError("g", "bad_request", "please try again later", 400))  # a bad request about our call is not the provider being busy
    assert not _overloaded(ProviderError("g", "auth", "service unavailable", 503)) and not _overloaded(RuntimeError("503"))
