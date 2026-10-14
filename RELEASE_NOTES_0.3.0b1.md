# ModelSpend 0.3.0b1 (beta)

ModelSpend now does two things: it **shows where your LLM tokens and money go, per project and per agent**, and it **routes each task to the lowest-cost model expected to suit it**. This release adds the first half (and makes it enforceable with budgets). It uses your own API keys for OpenAI, Anthropic, Google and xAI, stores no prompts, and is Apache-2.0.

Still a **beta**. This is the first release since 0.1.0b1 (the 0.2 work was never tagged separately, so it is included here). What was and was not tested live is listed below.

## What's new

**Cost per project and per agent**
- Tag any call with `project`, `agent`, an optional `run_id` and up to 8 `labels`: SDK arguments, CLI flags (`--project`, `--agent`, `--run-id`, `--label`), `MODELSPEND_PROJECT` / `MODELSPEND_AGENT`, or headers on the OpenAI-compatible server (`X-ModelSpend-Project`, `-Agent`, `-Run`, `-Labels`). Tags are short identifiers (no spaces), so prompt text cannot end up in them. They are attribution, not access control.
- `modelspend report --by project,agent,model --since 7d` (also CSV and JSON, and `GET /v1/usage/report`): tokens by type (input, cached, output, reasoning), cost, and **overhead**, meaning what failed calls, retries, superseded answers after an escalation, and classifier or judge calls cost. Calls on models without a price count their tokens and show cost as partial; nothing is guessed.
- **Observe-only mode:** pin a model (`--model provider/id`, `pin=`, or a non-`auto` `model` on the server) to measure an agent without routing it. Adopting it can be as small as a new `base_url` and two headers.

**Dashboard Costs tab** (`/dashboard#costs`)
- A token total, tiles for cost, requests, overhead, cache hit rate and unpriced calls; tokens by group as stacked bars (input, cached, output); tokens per day; a full table; one filter row (window, group by, project) that scopes everything. Light and dark mode, tooltips and keyboard focus, a table view for every chart, and no external resources.

**Budgets per project and agent**
- `modelspend budget set --project shop --period monthly --usd 50` (also `list`, `remove`, `GET/PUT /v1/budgets`, `DELETE /v1/budgets/{id}`, and a form on the Costs tab). Daily or monthly (UTC), in USD and/or tokens; scope by project, agent, both, or everything.
- **Hard:** before every provider call (including retries, failovers, escalations, classifier and judge calls, and streams), ModelSpend checks *spent so far + calls in flight + this call's worst case*. If a limit would be exceeded, **nothing is sent** and you get HTTP 429 `period_budget_exceeded` with the amounts and the reset time. The check-and-reserve is one SQLite write transaction, so threads and processes sharing the database cannot jointly overshoot.
- **Soft:** never refuses; results carry `budget_warnings` (at 80% and when over, based on actual spend).
- `modelspend prune 90d --yes` deletes old usage records (budgets are kept).

**Also changed**
- A "no credits" error (for example OpenAI's `credit_balance_exhausted`) is now a distinct `quota` error: it is not retried, and it fails over to another provider if you allowed one.
- The server's `model` field accepts a catalog model to pin it (before, anything but `auto` was a 400).
- When an escalation could not run because of a budget, the earlier answer is returned as `validation_failed` with `escalations: 0` (it used to read `ok`).
- The usage database now runs in WAL mode. You will see `usage.db-wal` and `usage.db-shm` files next to it.

## Verified live
- **Anthropic and OpenAI:** routing, token counts and cost (the report's totals matched the providers' reported usage and the catalog-price arithmetic), streaming (OpenAI), and tagged calls in `report`.
- **OpenAI, from the CLI:** a hard budget refused a call before it was made (spent 73 + worst case 98 = 171 against a 150-token limit), the refused call was not recorded as spend, and a soft budget let the call run and warned. (OpenAI's own usage page was not compared.)

## Not yet verified
- The **dashboard Costs tab** and **budget form** were tested in a real browser with generated data, not yet against your own live usage.
- **Concurrent budget enforcement** is tested with threads on one machine. It is not designed for network file systems or several machines.
- **Gemini and xAI** adapters: unit-tested against simulated responses only.
- **OpenAI** strict structured output, failover on a bad key, and cross-vendor escalation.
- The **Docker code sandbox** and **automatic price refresh** against the real provider pages.
- macOS and Linux with Python 3.10+ only; Windows is untested.

## Know before you use it
- **Only calls made through ModelSpend are counted.** Traffic that bypasses it is invisible. Importing the providers' own usage data (to reconcile) is planned, not built.
- A hard **USD** budget refuses to run a model that has no price (it cannot know the cost). Use a token limit for those.
- Budget windows are UTC. Your provider's invoice is always the authority on cost.
- Only Anthropic prices ship; add other providers' models and prices with `catalog sync` / `catalog price` or the Pricing tab. See [docs/limitations.md](docs/limitations.md), [docs/cost-attribution.md](docs/cost-attribution.md) and [docs/alternatives.md](docs/alternatives.md).

## Upgrade
```
cd modelspend && git pull origin main && pip install -e . && modelspend restart
```
Your usage database upgrades itself on first use (new nullable columns; older rows show as `(untagged)`).
