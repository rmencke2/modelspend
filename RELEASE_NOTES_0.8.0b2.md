# ModelSpend 0.8.0b2 (beta)

**Checking providers on real accounts turned up real bugs. This release fixes them, and says exactly what has and has not been confirmed.**

ModelSpend is a small open-source tool (Apache-2.0) that sits between your code and the LLM providers you already use (OpenAI, Anthropic, Google, xAI, DeepSeek and anything OpenAI-compatible, with **your own keys**). It shows where your tokens and money go per project and agent, enforces budgets *before* the spend, and can route each task to the lowest-cost model expected to do the job. It runs locally, stores no prompts, and is built for **one developer or a small team (1 to 25 people)**.

0.8.0b1 added `modelspend verify`, which sends a few tiny requests to a provider on your own key and checks what your cost numbers depend on. This release is what happened when it was run against real accounts (and when the rehearsals that followed were run against fakes): bugs that no earlier test had found, a much more careful `verify`, and the first confirmed results.

## Try it

```bash
git pull && pip install -e .
modelspend keys set openai            # or deepseek, google, xai
modelspend verify openai --cache
```

It asks first and prints the most it can cost (typically a fraction of a cent). Progress appears as each check finishes. If something fails, `--report check.md` writes a Markdown report (ids, counts and error kinds only) you can paste into an issue.

## Bugs found while checking providers, now fixed

Some were seen on a real account (marked *live*); the others came from reading the code or from rehearsing a provider's documented behaviour with its real SDK against a local fake, and have not been seen live.

- **The Gemini adapter had no timeouts at all** (found by inspection after `verify google` appeared to hang on a real account). The OpenAI-compatible and Anthropic adapters pass the request's timeout; the Gemini one ignored it, and Google's SDK has no default, so a stalled request would wait forever and ModelSpend's own timeout and failover would never fire. Generate, stream (including a stall midway), the model list and token counting are now bounded.
- **There was no way to change the 60-second request timeout.** On a real account a Gemini thinking model took 43 seconds for a one-word answer, and the next, heavier calls hit the 60-second limit (*live*). Set `MODELSPEND_REQUEST_TIMEOUT_S` or `"request_timeout_s"` in `settings.json` (1 to 3600).
- **Several "no credit" and "bad key" errors were classified wrongly, which stops failover from working.** DeepSeek answers HTTP 402 "Insufficient Balance" and xAI answers HTTP 403 "doesn't have any credits or licenses yet" (both *live*): these are now `quota`, so a request fails over to another provider and an empty account is not mistaken for a bad key. From Google's documented behaviour, not seen live: Gemini answers a bad key with HTTP 400, not 401 (now `auth`), and its free tier uses the "exceeded your current quota" wording for per-minute limits, with the wait ("Please retry in 22.5s") after ~400 characters of text that used to be cut off at 300 (now a retryable `rate_limit`).
- **`modelspend pricing refresh` crashed with `KeyError: 'openrouter'`** (found while preparing the DeepSeek price check) for anyone with the OpenRouter preset or a custom provider such as a local Ollama server (neither has a pricing page). It now skips them; `sources check` and `sources reset` explain instead of failing. A price row whose printed name is not a catalog model id now shows its verified prices and a ready `modelspend catalog price ...` command.
- **A silent cost risk for reasoning models** (found by reading the adapter while preparing xAI). The adapter decides whether reasoning tokens are already inside `completion_tokens` from a per-model catalog flag that defaults to "inside", but xAI is documented to report them separately. `verify` now compares the provider's own `total_tokens` with both possibilities and, on a mismatch, FAILs with the exact one-line fix.

## `modelspend verify`, as it is now

- **It never looks like a hang.** Each check prints as it finishes (to stderr), waits are announced, and no single call may take longer than its allowance. If the first answer shows a model is slow, the allowance for the rest is raised in proportion (`--timeout` sets the start).
- **FAIL means a number would be wrong, and nothing else.** An account with no credit, a rate limit, a provider that says it is overloaded (HTTP 503), a model the key is refused: all of these read **NOT VERIFIED**, because they say nothing about the adapter. A plain server error is still a FAIL.
- **It copes with real accounts.** A rate-limited provider is waited for once; a model whose quota is 0 on your plan, or that your key is refused (it can be listed and still refused), is skipped and the next candidate tried; with nothing priced yet it prefers plain text models over image, video and speech variants and `-latest` aliases over dated ones.
- **`--structured`** tests structured output on demand and, when it works, prints the exact `catalog set ... capabilities.structured_output=true` command (ModelSpend only routes a schema request to a model after you have recorded that).
- **The cache check tries a longer prompt** if the short one gets no hit, because providers cache only above a minimum size.
- **A pasted report carries no identifiers:** account and request UUIDs in provider error text are replaced by `<id>`.

## Confirmed live (2026-10-20)

| Provider | Result |
|---|---|
| **OpenAI** | `gpt-4.1-nano`: every check passes, including cached tokens counted once (183 uncached + 1,152 cached = 1,335, the same total as the first call). `gpt-4.1`: usage, streaming and structured output (`verify --structured`). |
| **DeepSeek** `deepseek-flash` | Every check it ran passes: usage on plain and streamed calls, an unknown model fails as `bad_request`, cached tokens counted once (207 + 1,152 = 1,359). |
| **Google Gemini** `gemini-flash-lite-latest` (free tier) | Every check it ran passes, including cached tokens (3,255 + 2,035 = 5,290). On this model a ~1,330-token prompt got no cache hit and a ~5,300-token one did, so it caches only above some minimum size. |
| **xAI** | Not verified: the account had no credit, and ModelSpend now says so correctly. |

## Not yet verified

- **xAI** generation, streaming, cached tokens and reasoning-token accounting: they need a key with credit.
- **The reasoning-token check has been rehearsed against a fake with xAI's documented behaviour, not run live on a model that reports reasoning separately.** DeepSeek's reasoning accounting is therefore still unconfirmed too.
- **Gemini's thinking model `gemini-flash-latest`** reports its thought tokens as the adapter assumes, but took 30 to 45 seconds for one word and once answered HTTP 503 "high demand". Structured output and tool calls are unchecked on Gemini and DeepSeek.
- **DeepSeek prices** (`pricing refresh` has never been run against its real page) and **a call routed through OpenRouter or Ollama** (OpenRouter's model list and a local Ollama adapter call were checked in 0.8.0b1).
- **The caching advisor has been run on real OpenAI traffic once (`examples/caching_live_check.py`, `gpt-4.1-nano`, 2026-10-21): it named a prompt that starts with the time (0 of 8 calls cached) and read a fixed ~5,000-token prompt as working, after two bugs in its own size estimate were fixed (changelog, Unreleased). Not yet seen on DeepSeek, Gemini or Anthropic, or with tools or a schema in the prefix.** Savings from `shadow`/`savings`/`whatif` remain projections.
- **One intermittent test failure** was seen once and has not been identified (eleven consecutive runs passed afterwards).
- Everything listed as not yet verified in [0.8.0b1](RELEASE_NOTES_0.8.0b1.md) and earlier that is not mentioned above still applies (the Docker sandbox, automatic price refresh against real pages, Windows, a cost comparison against a provider's invoice).

## Upgrade

```bash
git pull && pip install -e .
```

Two behaviour changes to know about. **Gemini calls now time out after 60 seconds like every other provider's** (they used to wait with no limit): if you use a slow Gemini thinking model, raise the limit with `MODELSPEND_REQUEST_TIMEOUT_S=180` or `"request_timeout_s": 180` in `settings.json`. And **failover now happens on the no-credit errors above**, which used to fail the request. `modelspend verify` no longer exits with an error status for a NOT VERIFIED result (only for a FAIL), and its progress lines go to stderr so the final table can still be piped.

**Full changelog:** https://github.com/rmencke2/modelspend/compare/v0.8.0b1...v0.8.0b2
