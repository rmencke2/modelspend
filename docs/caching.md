# Prompt-caching advice

Providers bill a repeated prompt start (a system prompt, few-shot examples, a document) at a fraction of the normal input price, but only when the start is **byte-identical** and **long enough** (about 1,024 tokens or more). It is easy to miss: one timestamp near the top of a prompt, and nothing is ever cached. `modelspend caching` shows where that is happening in your own traffic.

```
modelspend caching                 # last 30 days, five-minute window
modelspend caching --window 1h     # count repeats up to an hour apart
modelspend caching --project web --format json
modelspend caching --forget        # erase everything it has stored
```

The dashboard's Savings tab has the same table under **Prompt caching**; `GET /v1/cache-advice` serves it.

## What you get

One row per long prompt start that was sent more than once: its size, how many repeats fell inside the window, how many **cached tokens the provider actually reported**, how many it **missed**, what caching has already saved you, what is still on the table, and what to do:

- *caching is working*: the provider reported cache hits for (almost) every repeat;
- *differs between calls only in numbers, dates or ids* (marked "drifting" in JSON): the start of the prompt changes from call to call in a timestamp, request id or counter, so a provider can never cache it. Put fixed text first and variable text last. ModelSpend recognises this by a second keyed hash of the same prefix with its numbers, dates and ids blanked: two different prefixes with one such hash are the same text apart from those. It cannot see a change in the words themselves, or in the order of items, which look like unrelated prompts;
- *the same prompt start was sent again but no cached tokens were reported*: the text did not change, so the usual causes are a prompt below the model's minimum cacheable size (our token count is an estimate, and some models need more than 1,024), a model that does not cache, or a cache that expired. `modelspend verify <provider> --cache` tests the provider on your key;
- *needs cache markers*: Anthropic caches only where the request marks what to cache (`cache_control`). **ModelSpend does not send those markers yet**, so for Claude the row is information, not something ModelSpend can fix for you;
- *repeats are more than N minutes apart*: a cache would have expired between them;
- *unknown provider*: ModelSpend does not assume a provider caches. `modelspend verify <provider> --cache` tests it on your key.

## Check it on your own key

`python examples/caching_live_check.py` sends 12 requests with one fixed system prompt, then 8 whose system prompt starts with the current time, to `openai/gpt-4.1-nano` (well under a cent), and prints the table: the first group should read *caching is working*, the second *differs between calls only in numbers, dates or ids*. Dollar figures need a cached-input price for the model in your catalog.

## What is stored (and what is not)

Per request, for each reusable prefix of at least 1,024 estimated tokens, ModelSpend keeps a **keyed hash** (HMAC-SHA256 with a random key that lives only in your own database, shortened) and a **token estimate**, plus a second keyed hash of the same prefix with numbers, dates and ids blanked (that is how a changing timestamp is recognised). The text is never stored, and a hash from your database matches nothing anywhere else. Short prompts are skipped before anything is even tokenized. This is the same technique the classification cache already uses.

Turn it off with `"track_prefixes": false` in `settings.json` (or `MODELSPEND_TRACK_PREFIXES=0`); `modelspend caching --forget` erases what is stored, and `modelspend prune` removes old rows with the requests they belong to. A failure in tracking can never affect a request.

## How it decides

Two prefixes are tracked per request, each including the tools and response schema (providers cache those too): the leading **system** message(s), and the **context**, everything except the last message. A repeat counts when the same prefix went to the same model within the window before (default five minutes, the shortest any major provider documents; change it with `--window`). A cache serves the longest prefix it holds, so repeats, cached tokens and savings are credited to the longest prefix that repeated, never to both the system prompt and the context.

Savings are `tokens x (input price - cached-input price)` from **your catalog**, so they need a cached-input price (`modelspend catalog set <ref> pricing.cached_input_per_mtok=<usd/M>`, from the provider's page); without one the table says so instead of showing a zero. For Anthropic the projection subtracts the cache-write premium when your catalog has a write price.

## Limits

- **Estimates.** Prefix sizes are estimates (exact only with the optional tiktoken extra), and a real cache hit also depends on the provider's own rules and load. Realized savings use the provider's reported cached tokens; potential savings are a projection.
- **A growing chat history is not detected as a repeat**, because each turn's context is new. A provider may still cache it, and the cached tokens it reports are counted.
- **Only requests made through ModelSpend** after this feature are tracked, and requests that ModelSpend's own eval and shadow runs make are excluded.
- **Not yet run against live traffic.** The arithmetic is tested on constructed timelines; the provider behaviours it assumes (OpenAI and DeepSeek automatic, Anthropic explicit) come from their documentation. Whether a provider reports cached tokens as ModelSpend expects was confirmed live for OpenAI with `modelspend verify --cache` (2026-10-20).
