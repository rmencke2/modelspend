# ModelSpend 0.1.0b1 (beta)

A cost-aware router for LLM calls. It sends each task to the lowest-cost model **expected to suit it**, using **your own API keys** for
OpenAI, Anthropic, Google and xAI, and keeps every call, retry and escalation inside a token and dollar budget you set.
Python SDK, CLI, a local OpenAI-compatible server (`model="auto"`), a usage dashboard and an eval harness. Apache-2.0.

This is a **beta**. It does what is listed below, and it has not been tested in the places listed under "Not yet verified".
It does not guarantee the cheapest model or the best answer; it guarantees a documented policy, a budget ceiling and a record of why.

## What it does
- Routes by task category, cost and suitability, with three documented profiles (economy, balanced, high assurance).
- Reserves the worst case against your USD and/or token budget before every call, and counts retries, failovers and escalations.
- Token-first accounting from provider-reported usage; cost is shown as unknown when a model has no price.
- Bounded retry and failover for operational errors (including "no credits"), and at most one escalation, only on validator evidence.
- Stores no prompts; keys come from the environment or a 0600 keys file and are scrubbed from errors; the server binds to localhost.
- Guarded price updates: fetch the provider's pricing page, propose changes, verify against the page text, you review and apply.

## Verified live (2026-10-12)
- **Anthropic:** model sync, routing, escalation, token budgets, streaming, dashboard; token counts and cost matched provider-reported usage.
- **OpenAI:** routing, chat and streaming on `gpt-4.1-nano` and `gpt-5-mini`; input-token estimate matched the provider's count;
  reasoning tokens are reported inside the output count; usage arrives in the stream; cost matches catalog prices.
  (Not yet compared against OpenAI's billing page.)

## Not yet verified
- **Gemini and xAI** adapters: unit-tested against simulated responses only.
- **OpenAI** strict structured output, failover on a bad key, cross-vendor escalation.
- The **Docker code sandbox** (`python_tests` validator) has not been run against a real Docker daemon.
- **Automatic price refresh** against the real OpenAI, Google and xAI pages (tested against fake pages and a real browser).
- Only macOS and Linux with Python 3.10+ have been considered; Windows is untested.

## Know before you use it
- Only Anthropic prices ship. Add other providers' models and prices with `catalog sync` / `catalog price`, or the dashboard's Pricing tab.
  Check prices against the provider's official page; the tool warns when they get stale but cannot detect changes on its own.
- Until you add eval cases, suitability comes from editorial tier priors (labelled as such in every route explanation).
- Tasks it cannot categorize go to a mid-tier model by default; pass `task_type` to override.
- See [docs/limitations.md](docs/limitations.md) and [docs/alternatives.md](docs/alternatives.md) for the full picture and where other tools fit better.

## Install
```
git clone https://github.com/rmencke2/modelspend && cd modelspend
python3 -m venv .venv && source .venv/bin/activate
python -m pip install --upgrade pip && pip install -e .
```
Then follow the README's setup section. Issues and reports from people running Gemini, xAI or Windows are especially welcome.
