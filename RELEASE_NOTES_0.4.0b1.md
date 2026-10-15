# ModelSpend 0.4.0b1 (beta)

ModelSpend shows **where your LLM tokens and money go, per project and per agent**, enforces **budgets before the spend**, and can **route each task to the lowest-cost model expected to suit it**. It uses your own API keys for OpenAI, Anthropic, Google and xAI, stores no prompts, runs locally, and is Apache-2.0.

This release is about **choice and consistency**: you can now pick the model yourself by name, and a multi-turn chat no longer hops between models. It is still a **beta**; what was and was not tested live is listed below.

## What's new

**Model aliases: pick the model by the kind of work**
- `modelspend alias set coding anthropic/claude-sonnet-5-5` and `modelspend alias set classify openai/gpt-4.1-nano`, then `--model classify` on the CLI, `model="classify"` on the local server (any OpenAI client), or `pin="classify"` in the SDK.
- An alias is a **pinned model**: exactly that model runs, with no routing, failover or escalation. Tokens, cost and tags are still recorded, so per-project / per-agent reports and budgets work even if you never use automatic routing.
- `modelspend alias list` tells you if an alias's target is missing from the catalog, has no price, or has no API key. The server has `GET /v1/aliases` and `PUT/DELETE /v1/aliases/{name}`; changes apply immediately.

**Conversation stickiness: keep a chat on one model**
- A request that carries earlier turns now keeps the model chosen for the conversation, instead of being re-routed on every message. Switching models mid-conversation can lose provider-side prompt caching (ModelSpend does not measure this) and changes the model's style partway through.
- It is released, with the reason in the explanation, if that model is no longer suitable for the turn, is denied, or is unavailable. Short follow-ups such as "and the refund?" cannot be classified on their own, so they **inherit the task category** of the earlier turns instead of jumping to a stronger, more expensive model.
- Results say what happened (`sticky: "held"` or `"released: ..."`), and the explanation names the cheaper model you would have got for that turn alone, so the price of stickiness is visible.
- Only a salted hash, a model name, a task category and a time are stored (no content); entries expire after 24 hours. Turn it off per request with `sticky=False` (or `"modelspend": {"sticky": false}` on the server) or for everything with `MODELSPEND_STICKY=false`. Pass a `run_id` if your client rewrites or compacts its history.

**Also since 0.3.0b1**
- Fix: the overhead tile and report showed overhead cost as "unknown" when there were no overhead calls; it is now $0.
- Fix: the daily chart repeated the same date on its x-axis for a one-day window.
- Fix: `preview --assume-keys --model ...` now assumes a key for the pinned model's provider.

## Behaviour change to know about
**Stickiness is on by default.** If you send multi-turn conversations through the server and relied on every turn being routed independently, set `MODELSPEND_STICKY=false`. Single-turn requests, pinned models and aliases are unaffected.

## Verified live
- **Aliases and stickiness (OpenAI and Anthropic):** an alias ran exactly its model; a second turn stayed on the first turn's model and inherited its category; with stickiness off the same follow-up was routed afresh (to a more expensive tier-2 model, about five times the cost in that one example, which is a single data point, not a benchmark).
- **Failover on a bad key:** an OpenAI auth error was not retried, Anthropic answered, and the key did not appear in the output.
- **Cross-vendor escalation:** with an impossible validator, one escalation from OpenAI `gpt-4.1-nano` to Anthropic `claude-sonnet-5-5`, then it stopped; tokens and cost reconciled and the first call showed as overhead.
- **Reporting, budgets and the Costs tab** (from earlier releases): per-project / per-agent reports, hard and soft budgets from the CLI, and the Costs tab on live data.

## Not yet verified
- **OpenAI strict structured output** (a `response_schema` call).
- **Gemini and xAI** adapters (unit-tested against simulated responses only).
- **Concurrent budget enforcement** across processes and the dashboard's budget form (tested with threads and generated data).
- The **Docker code sandbox** and **automatic price refresh** against the real provider pages.
- Totals have not been compared with the providers' own usage or billing pages.
- macOS and Linux with Python 3.10+ only; Windows is untested.

## Know before you use it
- Stickiness does **not** weigh the cost of switching against a saving: it switches only when the held model is unusable, never because a cheaper one appeared. A cache-aware comparison is a possible next step. ModelSpend does not send Anthropic cache markers, so for Anthropic the benefit is consistency rather than cache savings.
- Conversations are recognised by their first message (or `run_id`): two unrelated chats that start with the identical first message share a key.
- Only calls made through ModelSpend are counted. Budget windows are UTC. A hard USD budget refuses a model that has no price. Your provider's invoice is always the authority on cost.
- Only Anthropic prices ship; add other providers' models and prices with `catalog sync` / `catalog price` or the Pricing tab. See [docs/aliases-and-stickiness.md](docs/aliases-and-stickiness.md), [docs/cost-attribution.md](docs/cost-attribution.md), [docs/limitations.md](docs/limitations.md) and [docs/alternatives.md](docs/alternatives.md).

## Upgrade
```
cd modelspend && git pull origin main && pip install -e . && modelspend restart
```
Your usage database upgrades itself on first use (a small new table for conversations).
