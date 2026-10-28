# ModelSpend 0.8.0b4 (beta)

**Claude gets prompt caching (opt-in), and three new commands for running ModelSpend day to day: `doctor`, `digest` and `duplicates`. All of it was run on the maintainer's own accounts before release, and this page says what each run did and did not show.**

ModelSpend is a small open-source tool (Apache-2.0) that sits between your code and the LLM providers you already use (OpenAI, Anthropic, Google, xAI, DeepSeek and anything OpenAI-compatible, with **your own keys**). It shows where your tokens and money go per project and agent, enforces budgets *before* the spend, and can route each task to the lowest-cost model expected to do the job. It runs locally, stores no prompts, and is built for **one developer or a small team (1 to 25 people)**.

This release builds on [0.8.0b3](https://github.com/rmencke2/modelspend/blob/main/RELEASE_NOTES_0.8.0b3.md), which fixed the bugs that real accounts found in `verify`, the caching advisor and price refresh. Everything in those notes still applies.

## Try it

```bash
git pull && pip install -e .
modelspend doctor                     # offline: what is wrong with your setup, and the command that fixes it
modelspend digest                     # the last day against the day before
modelspend duplicates                 # requests sent more than once (needs traffic since this version)
python examples/caching_live_check.py <provider/model>   # a fixed prompt, then one that starts with the time (well under a cent)
```

## New: ModelSpend can ask Claude to cache your repeated prompts (off by default)

OpenAI and DeepSeek cache a repeated prompt start by themselves. Anthropic's Claude does not: it caches only what a request marks with `cache_control`, so a long system prompt sent over and over was billed in full every time, and `modelspend caching` could only say so. Now:

```json
{ "anthropic_cache_markers": "repeats" }
```

in `~/.modelspend/settings.json` (or `MODELSPEND_ANTHROPIC_CACHE_MARKERS`). `repeats` marks a prompt start only after the same one was sent to the same model in the last five minutes, so the first request is never marked and a prompt that never repeats never pays the cache-write premium. `always` marks every long prefix, including the first. `off` (the default) leaves requests exactly as they were. The write premium comes off "saved so far" in `modelspend caching`, so the number is net.

**Confirmed live on `claude-haiku-5-5` (2026-10-24):** the first request unmarked, the second wrote 7,489 tokens, the next ten each read 7,489 of about 7,503 input tokens; a prompt whose first line was the current time was never marked, paid no premium and was named as drifting. 74,890 cached tokens, nothing missed, $0.0066 saved, matching the catalog prices by hand. **Not yet seen live:** `always` mode, a marker on a long few-shot or document context (only the system-prompt marker ran), and a prefix with tools or a response schema.

## New: three commands for running it day to day

- **`modelspend doctor`** checks your setup offline (nothing is sent, nothing spent, no file created) and prints the command that fixes each finding: no key; a provider with no usable model; a failed last `verify`; stale or missing prices; cached tokens costed as plain input because no cached price is set; calls with no price; a high failure rate; a model your key was refused but the catalog still lists; a keys file others can read; a server set to listen beyond this machine with no token. Exit status 1 only for real problems. Run on a real setup (2026-10-24) it first raised four false or noisy findings (a provider you chose not to enable, one unusable provider among working ones, calls from before a model was priced, six identical "never checked" lines); all fixed.
- **`modelspend digest`**: the last day (or `--period week`) against the one before: spend and the change, the three projects that moved most, the models behind it, budgets near their limit, and flags only when true (spend doubling by at least $0.50, calls with no price, a failure rate above 20%, more than 30% of tokens on retries and judge calls). `--post` sends it to one webhook you configure (Slack, Discord or JSON): off until set, project names left out unless `--include-projects`, https only, redirects not followed, and the address never printed or logged. Run on a real ledger it read sensibly with one false alarm (calls from before a model was priced), fixed. **Posting to a real channel has not been tried.** Cron example in `docs/digest.md`.
- **`modelspend duplicates`** lists requests sent more than once, word for word, with the cost of the repeats, a pattern (burst: within a minute, usually a retry loop or a client resending after a timeout; later: the same input hours or days on) and what to do. It keeps one keyed hash per request of 200+ characters (never the text; shorter requests are not tracked); on by default, `"track_duplicates": false` turns it off, `modelspend duplicates --forget` erases it. Confirmed live only as plumbing (a script that sends the same four requests three times each: it found four groups of three, repeat cost $0.0007 of $0.0136). **It has not been run on real traffic**, so whether it finds waste in a real app is unknown.

## Found by running it, now fixed

- **The caching advisor's token estimate was wrong in both directions** (*live*). 0.8.0b3 capped it where it overshot (OpenAI). On Claude it undershoots (6,193 against 7,489 tokens), which clipped the cached tokens and the saving the table reported. The provider's own count of what it cached or wrote is now believed, up to the request's total input.
- **A retired model stays in the provider's own model list** (*live*, OpenAI). `gpt-4.1-nano` answered HTTP 404 "has been deprecated" while `modelspend catalog sync` still saw it and kept it available, so a sync cannot find retirements. `verify` fell through to `gpt-5-mini` and passed; it now remembers which model it was refused, and `doctor` warns about it with `modelspend models disable <model>`. `examples/caching_live_check.py` explains a refused model instead of printing a traceback.

## Confirmed live since 0.8.0b3 (2026-10-24 to 2026-10-28)

| What | Result |
|---|---|
| **Caching advisor, Anthropic** | `claude-haiku-5-5` with the cache markers on: one write, then reads of 7,489 tokens on every call; a time-first prompt never marked and named; $0.0066 saved, checked by hand. |
| **OpenAI reasoning accounting** | `gpt-5-mini` (after `gpt-4.1-nano` was refused as retired): one word cost 74 completion tokens of which 64 were reasoning, reported inside the completion count as the catalog says, counted once. |
| **`doctor`, `digest`, `duplicates`** | Each run once on the maintainer's own setup (594 requests of test traffic); `duplicates` only on a script that repeats requests on purpose. See above for what each found. |

## Not yet verified

- **xAI** generation, streaming, cached tokens and reasoning-token accounting: they need a key with credit.
- **Reasoning-token accounting on a provider that reports it separately.** Confirmed live for OpenAI (inside the completion count); the check has only been rehearsed against a fake for xAI's documented behaviour, and DeepSeek's is unconfirmed.
- **`modelspend digest --post` on a real Slack or Discord channel, and `modelspend duplicates` on real traffic.**
- **The caching advisor on Gemini**, `always` mode and a context-level marker on Claude, and any prefix with tools or a response schema.
- **Gemini's thinking model `gemini-flash-latest`** took 30 to 45 seconds for one word and once answered HTTP 503 "high demand". Structured output and tool calls are unchecked on Gemini and DeepSeek.
- **A call routed through OpenRouter or Ollama.** **Automatic price refresh on a page whose table is readable as rows**: the only real page tried was DeepSeek's.
- **Savings from `shadow`, `savings` and `whatif` remain projections.**
- **One intermittent test failure** was seen once in about twelve full runs and has not been identified (eleven consecutive runs passed afterwards).
- Everything listed as not yet verified in [0.8.0b3](https://github.com/rmencke2/modelspend/blob/main/RELEASE_NOTES_0.8.0b3.md) and [0.8.0b1](https://github.com/rmencke2/modelspend/blob/main/RELEASE_NOTES_0.8.0b1.md) that is not mentioned above still applies (the Docker sandbox, Windows, a cost comparison against a provider's invoice).

## Upgrade

```bash
git pull && pip install -e .
```

Behaviour changes to know about:
- **A new setting, `anthropic_cache_markers`, defaults to off.** Nothing changes for Claude calls until you turn it on. `repeats` refuses to start with `track_prefixes` off.
- **New settings, all with safe defaults:** `track_duplicates` (on; hashes only), and a `digest` block (a webhook, off until set). `verify` now writes `verify-results.json` (verdicts and model names, no keys or text) in your ModelSpend folder, which `doctor` reads. A new table `dup_stats` holds the request hashes.
- OpenAI's `gpt-4.1-nano` is retired. If your catalog or routing still lists it, `modelspend models disable openai/gpt-4.1-nano`.

**Full changelog:** https://github.com/rmencke2/modelspend/compare/v0.8.0b3...v0.8.0b4
