"""Check `modelspend caching` against a real provider (spends well under a cent on gpt-4.1-nano).

    modelspend keys set openai
    python examples/caching_live_check.py                           # OpenAI gpt-4.1-nano
    python examples/caching_live_check.py deepseek/deepseek-flash   # any provider/model in your catalog that caches automatically
    python examples/caching_live_check.py anthropic/claude-haiku-5-5   # Claude: cache markers are switched on for this run only

Phase A sends 12 requests that share one long, fixed system prompt, a couple of seconds apart: after the first, OpenAI should serve the prompt from its
cache and the table should say "caching is working". Phase B sends 8 requests whose system prompt starts with the current time, so every call has a
different start: a provider cannot cache that, and the table should name it ("differs between calls only in numbers, dates or ids").
Both use the project name "caching-live-check" (the table below is limited to the requests this script made). Dollar figures need a cached-input price
for the model in your catalog; without one the table says so (set it from the provider's page: `modelspend catalog set openai/gpt-4.1-nano
pricing.cached_input_per_mtok=<usd per million>`). A provider that builds its cache in the background (DeepSeek) may
need a few seconds: pass a longer pause in seconds as the second argument.
"""
from __future__ import annotations

import dataclasses
import sys
import time

from modelspend import ModelSpend
from modelspend.cacheadvice import build_cache_advice, to_table
from modelspend.config import Config
from modelspend.errors import AllAttemptsFailed

PIN = "openai/gpt-4.1-nano"
PROJECT = "caching-live-check"
POLICY = ("Rule {n}: answer politely, cite the policy section you used, never promise a refund before the case is reviewed, and keep every answer under two sentences. " * 1)
FIXED_A = "You are a support agent for a subscription app. " + "".join(POLICY.format(n=n) for n in range(1, 160))  # ~5,000 tokens: well above every provider's minimum
FIXED_B = "You are a billing agent for an invoicing app. " + "".join(POLICY.format(n=n) for n in range(1, 160))
QUESTIONS = ["Where is my invoice?", "Can I change my plan?", "How do I cancel?", "Do you offer a student discount?"]


def _cache_line(u) -> str:
    total = u.input_tokens + u.cache_read_tokens + u.cache_write_tokens
    return f"cached {u.cache_read_tokens:>5}" + (f", wrote {u.cache_write_tokens:>5}" if u.cache_write_tokens else "") + f" of {total:>5} input tokens"


def run(ms: ModelSpend, *, pin: str = PIN, pause_s: float = 2.0, a: int = 12, b: int = 8) -> str:
    started = time.time() - 1
    for i in range(a):
        r = ms.route([{"role": "system", "content": FIXED_A}, {"role": "user", "content": QUESTIONS[i % len(QUESTIONS)]}],
                     pin=pin, project=PROJECT, agent="phase-a", task_type="classification", max_output_tokens=16)
        print(f"A{i + 1:>2}. {_cache_line(r.usage)}")
        time.sleep(pause_s)
    for i in range(b):
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{time.time_ns() % 10**9:09d}Z"  # sub-second digits: two calls never share a stamp
        r = ms.route([{"role": "system", "content": f"Current time: {stamp}\n" + FIXED_B}, {"role": "user", "content": QUESTIONS[i % len(QUESTIONS)]}],
                     pin=pin, project=PROJECT, agent="phase-b", task_type="classification", max_output_tokens=16)
        print(f"B{i + 1:>2}. {_cache_line(r.usage)}")
        time.sleep(pause_s)
    out = to_table(build_cache_advice(ms.store, ms.catalog, ms.config, since=started, project=PROJECT))
    print("\n" + out)
    return out


def main(argv: list[str], ms: ModelSpend | None = None) -> int:
    pin = argv[0] if argv else PIN
    pause = float(argv[1]) if len(argv) > 1 else 2.0
    if ms is None:
        cfg = Config.from_env()
        if pin.startswith("anthropic/") and cfg.anthropic_cache_markers == "off":  # Claude caches only what a request marks
            cfg = dataclasses.replace(cfg, anthropic_cache_markers="repeats")
            print('note: "anthropic_cache_markers" is off in your settings; it is on ("repeats") for this run only\n')
        ms = ModelSpend(providers=[pin.split("/")[0]], config=cfg)
    if not ms.config.track_prefixes:  # (also required by the Claude markers)
        print("Prefix tracking is off. Remove track_prefixes from settings.json (or set MODELSPEND_TRACK_PREFIXES=1) and run again.")
        return 1
    try:
        run(ms, pin=pin, pause_s=pause)
    except AllAttemptsFailed as e:  # most often a model the provider has since retired: say what to do instead of a traceback
        prov = pin.split("/")[0]
        print(f"\n{pin} did not answer: {str(e)[:300]}\n\nIf the provider has retired that model, use another one: `python examples/caching_live_check.py <provider/model>`.\n"
              f"`modelspend verify {prov} --yes` finds a model your key can still use; `modelspend catalog sync --provider {prov}` (a dry run) shows what your key lists now;\n"
              f"`modelspend models disable {pin}` keeps ModelSpend from routing to it.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
