# Duplicate requests

`modelspend duplicates` lists requests that were sent more than once, word for word, and what the repeats cost.

```
modelspend duplicates                       # last 30 days
modelspend duplicates --since 7d --project web
modelspend duplicates --format json
modelspend duplicates --forget              # erase everything it has stored
```

## Why it matters

A repeated identical request is billed in full each time. A provider's prompt cache discounts the repeated *input*, never the answer you pay for again, so a client that resends after a timeout, a retry loop, an agent that asks the same question on every step, or a job that re-processes the same input is pure waste whenever the answer could not have changed. It is usually the cheapest saving there is: you do not change the model or the prompt, you stop asking twice.

## What you get

One row per request that was sent at least twice: how many times, the cost of the repeats (every send after the first), the project and agent, the model, and a pattern from the gaps between sends:

- **burst**: every repeat followed the previous send within a minute. Usually a retry loop, or a client that resends after a timeout. Check the caller.
- **later**: every repeat came more than a minute after the previous one. The same input again, hours or days later; if the answer cannot have changed, keep the first answer in your app.
- **mixed**: both.

A `+` after a cost means some repeats were on a model with no price, so the cost is at least that much. Totals say how much of what was compared the repeats make up.

**A repeat can be on purpose**: sampling at a temperature above zero (the answers differ, and you wanted that), evals, tests. This lists what happened and does not say it was wrong. Two requests count as the same only if their messages, tools, response schema and temperature are identical, so a prompt that carries a timestamp or request id never matches itself; `modelspend caching` is the tool that spots that.

## What is stored (and what is not)

Per request of at least 200 characters, ModelSpend keeps a **keyed hash** (HMAC-SHA256 with a random key that lives only in your own database, shortened to 16 hex characters) and nothing else beyond what the ledger already holds. The text is never stored. Shorter requests are not tracked at all, because a short prompt could be guessed and confirmed against its hash, and repeating one costs little. A hash from your database matches nothing anywhere else. Requests that ModelSpend's own eval and shadow runs make are left out, and so are requests that failed.

Turn it off with `"track_duplicates": false` in `settings.json` (or `MODELSPEND_TRACK_DUPLICATES=0`); `modelspend duplicates --forget` erases what is stored, and `modelspend prune` removes old rows with the requests they belong to. A failure in tracking can never affect a request.

## Limits

Not yet run against real traffic: the arithmetic is tested on constructed timelines and the hooks through the engine with fakes. Only requests made after tracking began (this version) are compared. It cannot see near-duplicates (the same text with one word changed), and a chat history that grows each turn never repeats.
