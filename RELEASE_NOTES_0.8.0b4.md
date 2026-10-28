# ModelSpend 0.8.0b4 (beta)

**Claude now gets prompt caching (opt-in), and real accounts found real bugs in 0.8.0b1: in the checks and the caching advisor as much as in the providers. This release fixes them, and says exactly what has and has not been confirmed.**

ModelSpend is a small open-source tool (Apache-2.0) that sits between your code and the LLM providers you already use (OpenAI, Anthropic, Google, xAI, DeepSeek and anything OpenAI-compatible, with **your own keys**). It shows where your tokens and money go per project and agent, enforces budgets *before* the spend, and can route each task to the lowest-cost model expected to do the job. It runs locally, stores no prompts, and is built for **one developer or a small team (1 to 25 people)**.

0.8.0b1 added `modelspend verify` (a few tiny requests to a provider on your own key, checking what your cost numbers depend on) and `modelspend caching` (which long prompt starts repeat, and whether the provider is caching them). This release is what happened when both were run on real accounts: bugs that no fake had found, a much more careful `verify`, a caching advisor that now sees the commonest caching mistake, and the first confirmed results. (There were no 0.8.0b2 or 0.8.0b3 releases: their changes are included here.)

## Try it

```bash
git pull && pip install -e .
modelspend keys set openai            # or deepseek, google, xai
modelspend doctor                     # offline: what is wrong with your setup, and the command that fixes it
modelspend verify openai --cache
python examples/caching_live_check.py <provider/model>   # a fixed prompt, then one that starts with the time (well under a cent)
```

`verify` asks first and prints the most it can cost (typically a fraction of a cent). Progress appears as each check finishes. If something fails, `--report check.md` writes a Markdown report (ids, counts and error kinds only) you can paste into an issue.

## New: ModelSpend can ask Claude to cache your repeated prompts (off by default)

OpenAI and DeepSeek cache a repeated prompt start by themselves. Anthropic's Claude does not: it caches only what a request marks with `cache_control`, so a long system prompt sent over and over was billed in full every time, and `modelspend caching` could only say so. Now:

```json
{ "anthropic_cache_markers": "repeats" }
```

in `~/.modelspend/settings.json` (or `MODELSPEND_ANTHROPIC_CACHE_MARKERS`). `repeats` marks a prompt start only after the same one was sent to the same model in the last five minutes, so the first request is never marked and a prompt that never repeats never pays the cache-write premium. `always` marks every long prefix, including the first. `off` (the default) leaves requests exactly as they were. The write premium comes off "saved so far" in `modelspend caching`, so the number is net. **Confirmed live on `claude-haiku-5-5` (2026-10-24):** the first request unmarked, the second wrote 7,489 tokens, the next ten each read 7,489 of about 7,503 input tokens; a prompt whose first line was the current time was never marked, paid no premium and was named as drifting. 74,890 cached tokens, nothing missed, $0.0066 saved, matching the catalog prices by hand. Not yet seen live: `always` mode, a marker on a long few-shot or document context (only the system-prompt marker ran), and a prefix with tools or a response schema.

## Also new: three commands for running it day to day

- **`modelspend doctor`** checks your setup offline (nothing is sent, nothing spent, no file created) and prints the command that fixes each finding: no key; a provider with no usable model; a failed last `verify`; stale or missing prices; cached tokens costed as plain input because no cached price is set; calls with no price; a high failure rate; a model your key was refused but the catalog still lists; a keys file others can read; a server set to listen beyond this machine with no token. Exit status 1 only for real problems. Run on a real setup (2026-10-24) it first raised four false or noisy findings (a provider you chose not to enable, one unusable provider among working ones, calls from before a model was priced, six identical "never checked" lines); all fixed.
- **`modelspend digest`**: the last day (or `--period week`) against the one before: spend and the change, the three projects that moved most, the models behind it, budgets near their limit, and flags only when true (spend doubling by at least $0.50, calls with no price, a failure rate above 20%, more than 30% of tokens on retries and judge calls). `--post` sends it to one webhook you configure (Slack, Discord or JSON): off until set, project names left out unless `--include-projects`, https only, redirects not followed, and the address never printed or logged. Run on a real ledger it read sensibly with one false alarm (calls from before a model was priced), fixed. **Posting to a real channel has not been tried.** Cron example in `docs/digest.md`.
- **`modelspend duplicates`** lists requests sent more than once, word for word, with the cost of the repeats, a pattern (burst: within a minute, usually a retry loop or a client resending after a timeout; later: the same input hours or days on) and what to do. It keeps one keyed hash per request of 200+ characters (never the text; shorter requests are not tracked); on by default, `"track_duplicates": false` turns it off, `modelspend duplicates --forget` erases it. Confirmed live only as plumbing (a script that sends the same four requests three times each: it found four groups of three, repeat cost $0.0007 of $0.0136). **It has not been run on real traffic**, so whether it finds waste in a real app is unknown.

## Bugs found by running it on real accounts, now fixed

Each is marked *live* if it was seen on a real account. The others came from reading the code, or from rehearsing a provider's documented behaviour against a local fake, and have not been seen live.

**Prompt-caching advice**
- **It could not see the commonest caching mistake.** A timestamp or id near the top of a prompt gives every request a different start, so nothing repeats and the table said nothing, although the docs promised it would name it (found by simulating it before the first live run). It now also keeps a second keyed hash of each prefix with numbers, dates and ids blanked, and reports prefixes that differ only in those as "differs between calls only in numbers, dates or ids", with the saving priced. Confirmed live on OpenAI and DeepSeek. It cannot see a change in the words or in the order of items.
- **An identical prompt with no cache hits was blamed on "the start of the prompt probably changes".** An identical hash cannot have changed. The advice now lists the real causes (below the model's minimum cacheable size, a model that does not cache, an expired cache) and points to `modelspend verify <provider> --cache`.
- **The advisor's token estimate disagreed with the provider's, both ways** (*live*). On OpenAI it overshot (6,193 against about 5,100), so a working cache read as "partly cached"; on Claude it undershot (6,193 against 7,489), which clipped the cached tokens and the saving. The provider's own counts now win: a prefix is capped at the request's reported input, and cached or written tokens are never clipped to the estimate. My first version of the cap double-counted cached tokens and did nothing for cached requests; the second live run showed it, and a test that assumed the same wrong layout had passed regardless.

**Providers and `verify`**
- **A retired model stays in the provider's own model list** (*live*, OpenAI): `gpt-4.1-nano` answered HTTP 404 "has been deprecated" while `modelspend catalog sync` still saw it and kept it available, so a sync cannot find retirements. `verify` now falls through to the next model (it did: `gpt-5-mini` passed), remembers which model was refused, and `doctor` warns about it with `modelspend models disable <model>`.
- **The Gemini adapter had no timeouts at all.** The OpenAI-compatible and Anthropic adapters pass the request's timeout; the Gemini one ignored it, and Google's SDK has no default, so a stalled request would wait forever and ModelSpend's own timeout and failover would never fire. Generate, stream (including a stall midway), the model list and token counting are now bounded. (Found by inspection after `verify google` appeared to hang on a real account.)
- **There was no way to change the 60-second request timeout.** A Gemini thinking model took 43 seconds for a one-word answer, and the heavier calls after it hit the limit (*live*). Set `MODELSPEND_REQUEST_TIMEOUT_S` or `"request_timeout_s"` in `settings.json` (1 to 3600).
- **Several "no credit" and "bad key" errors were classified wrongly, which stops failover.** DeepSeek answers HTTP 402 "Insufficient Balance" and xAI answers HTTP 403 "doesn't have any credits or licenses yet" (both *live*): these are now `quota`, so a request fails over and an empty account is not mistaken for a bad key. From Google's documented behaviour, not seen live: Gemini answers a bad key with HTTP 400 (now `auth`), and its per-minute limit uses the "exceeded your current quota" wording with the wait ("Please retry in 22.5s") past 300 characters that used to be cut off (now a retryable `rate_limit`).
- **A silent cost risk for reasoning models** (found by reading the adapter while preparing xAI). The adapter decides whether reasoning tokens are already inside `completion_tokens` from a per-model catalog flag that defaults to "inside", but xAI is documented to report them separately. `verify` now compares the provider's own `total_tokens` with both possibilities and, on a mismatch, FAILs with the exact one-line fix.

**Price refresh**
- **`modelspend pricing refresh` crashed with `KeyError: 'openrouter'`** for anyone with the OpenRouter preset or a custom provider such as a local Ollama server (neither has a pricing page). It now skips them.
- **It could hide a page it had not understood**, and call an empty extraction a success. A page with rows that matched no catalog model was accepted as the baseline, so every later run said "unchanged; no model call made"; and a page that yielded no price rows at all was reported as "no price differs" (*live*, DeepSeek). Both are now handled: such a page is not accepted, and no rows is an error pointing to `modelspend pricing paste <provider>`.
- **The verifier refused wrong numbers on a real table** (*live*, DeepSeek). The page lists models as columns, with cache-hit, cache-miss and output prices on separate rows, each at a peak and an off-peak rate. The extractor returned the cache-hit price as the input price and the cache-miss price as a cache-write price, either of which would have understated costs about 50 times. The row check rejected both because the numbers were not on the model's own row, so nothing wrong was applied. The extraction prompt now says what each field means and to use the peak price when a page lists two. **A table with models as columns still cannot be verified automatically**, so the prices for such pages are set by hand with `modelspend catalog price`. ModelSpend has one price per model and no time-of-day pricing, so use the peak price to avoid understating a budget.

## `modelspend verify`, as it is now

- **It never looks like a hang.** Each check prints as it finishes (to stderr), waits are announced, and no single call may take longer than its allowance. If the first answer shows a model is slow, the allowance for the rest is raised in proportion (`--timeout` sets the start).
- **FAIL means a number would be wrong, and nothing else.** An account with no credit, a rate limit, a provider that says it is overloaded (HTTP 503), a model the key is refused: all of these read **NOT VERIFIED**. A plain server error is still a FAIL.
- **It copes with real accounts.** A rate-limited provider is waited for once; a model whose quota is 0 on your plan, or that your key is refused, is skipped and the next candidate tried; with nothing priced yet it prefers plain text models over image, video and speech variants and `-latest` aliases over dated ones.
- **`--structured`** tests structured output on demand and, when it works, prints the exact `catalog set ... capabilities.structured_output=true` command.
- **The cache check tries a longer prompt** if the short one gets no hit, because providers cache only above a minimum size.
- **A pasted report carries no identifiers:** account and request UUIDs in provider error text are replaced by `<id>`.

## Confirmed live (2026-10-20 to 2026-10-28)

| What | Result |
|---|---|
| **OpenAI** `verify` | `gpt-4.1-nano` (OpenAI has since retired it): every check passes, including cached tokens counted once (183 uncached + 1,152 cached = 1,335, the same total as the first call). `gpt-4.1`: usage, streaming and structured output. |
| **DeepSeek** `verify` | `deepseek-flash`: every check it ran passes, including cached tokens counted once (207 + 1,152 = 1,359). |
| **Google Gemini** `verify` | `gemini-flash-lite-latest` (free tier): every check it ran passes, including cached tokens (3,255 + 2,035 = 5,290). A ~1,330-token prompt got no cache hit and a ~5,300-token one did. |
| **Caching advisor, OpenAI** | `gpt-4.1-nano` (before its retirement), `examples/caching_live_check.py`: a prompt starting with the time got 0 of 8 calls cached and was named; a fixed ~5,000-token prompt was cached (4,992 tokens a call) and read "caching is working". |
| **OpenAI reasoning accounting** | `gpt-5-mini` (after `gpt-4.1-nano` was refused as retired): one word cost 74 completion tokens of which 64 were reasoning, reported inside the completion count as the catalog says, counted once. |
| **Caching advisor, Anthropic** | `claude-haiku-5-5` with the cache markers on: one write, then reads of 7,489 tokens on every call; a time-first prompt never marked and named; $0.0066 saved, checked by hand. |
| **Caching advisor, DeepSeek** | `deepseek-flash`: the same two results. With the peak prices set by hand ($0.30 uncached, $0.006 cached per million tokens) it showed $0.0176 saved and $0.0106 on the table, both matching the token counts recomputed by hand. |
| **`doctor`, `digest`, `duplicates`** | Each run once on the maintainer's own setup (594 requests of test traffic); `duplicates` only on a script that repeats requests on purpose. See above for what each found. |
| **Pricing refresh, DeepSeek** | Did not produce prices (JavaScript-drawn page, then a column table). It refused the wrong numbers it was offered, as designed. |
| **xAI** | Not verified: the account had no credit, and ModelSpend now says so correctly. |

## Not yet verified

- **xAI** generation, streaming, cached tokens and reasoning-token accounting: they need a key with credit.
- **Reasoning-token accounting on a provider that reports it separately.** Confirmed live for OpenAI (inside the completion count); the check has only been rehearsed against a fake for xAI's documented behaviour, and DeepSeek's is unconfirmed.
- **`modelspend digest --post` on a real Slack or Discord channel, and `modelspend duplicates` on real traffic.**
- **The caching advisor on Gemini**, `always` mode and a context-level marker on Claude, and any prefix with tools or a response schema.
- **Gemini's thinking model `gemini-flash-latest`** took 30 to 45 seconds for one word and once answered HTTP 503 "high demand". Structured output and tool calls are unchecked on Gemini and DeepSeek.
- **A call routed through OpenRouter or Ollama** (OpenRouter's model list and a local Ollama adapter call were checked in 0.8.0b1). **Automatic price refresh on a page whose table is readable as rows**: the only real page tried was DeepSeek's.
- **Savings from `shadow`, `savings` and `whatif` remain projections.**
- **One intermittent test failure** was seen once in about twelve full runs and has not been identified (eleven consecutive runs passed afterwards).
- Everything listed as not yet verified in [0.8.0b1](RELEASE_NOTES_0.8.0b1.md) and earlier that is not mentioned above still applies (the Docker sandbox, Windows, a cost comparison against a provider's invoice).

## Upgrade

```bash
git pull && pip install -e .
```

Behaviour changes to know about:
- **Gemini calls now time out after 60 seconds** like every other provider's (they used to wait with no limit). For a slow Gemini thinking model, set `MODELSPEND_REQUEST_TIMEOUT_S=180` or `"request_timeout_s": 180` in `settings.json`.
- **Failover now happens on the no-credit errors above**, which used to fail the request.
- **A new setting, `anthropic_cache_markers`, defaults to off.** Nothing changes for Claude calls until you turn it on. `repeats` refuses to start with `track_prefixes` off.
- **New settings, all with safe defaults:** `track_duplicates` (on; hashes only), and a `digest` block (a webhook, off until set). `verify` now writes `verify-results.json` (verdicts and model names, no keys or text) in your ModelSpend folder, which `doctor` reads. A new table `dup_stats` holds the request hashes.
- **Your database gets one new nullable column** (`prefix_stats.hn`) the first time it is opened. Prompt sightings recorded earlier have none and show no drift. Prefix tracking still stores only keyed hashes and token counts, never text, and `"track_prefixes": false` turns it off.
- `modelspend verify` exits non-zero only for a FAIL (not for NOT VERIFIED), and its progress lines go to stderr so the table can still be piped.
- A price-refresh page is no longer remembered as "accepted" when some of its rows were not understood. Use `--force` to re-read one.

**Full changelog:** https://github.com/rmencke2/modelspend/compare/v0.8.0b1...v0.8.0b4
