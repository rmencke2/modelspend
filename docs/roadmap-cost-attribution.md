# Roadmap: cost attribution per project and per agent

Status: 0.2 is built and its numbers were checked live against Anthropic and OpenAI. 0.3 (dashboard Costs tab, per-agent budgets, retention) is built and tested with mocked providers; it has not been exercised live. 0.4 is intention, not promise.

ModelSpend has two pillars:

1. **Know your spend** - tokens and cost per project and per agent, by model and by token type.
2. **Spend less** - route each task to the lowest-cost model expected to suit it, inside a budget.

Each feeds the other: the report shows where the money goes; the router and budgets act on it.

## Starting point (0.1.0b1)

The store records, per request and per attempt, provider, model, tokens (input, cached, output, reasoning), cost, outcome and
latency. It had no notion of *who* made the call. Per-attempt cost is stored, so the overhead of retries, failovers and
escalations can be reported per agent.

## Design decisions

1. **Tags.** Optional `project`, `agent`, `run_id` (groups the calls of one agent run) and up to 8 free-form `labels`.
   SDK keyword arguments and defaults; server headers `X-ModelSpend-Project` / `X-ModelSpend-Agent` / `X-ModelSpend-Run`
   and the `modelspend` body extension; CLI flags and `MODELSPEND_PROJECT` / `MODELSPEND_AGENT`. Tags are validated (short, restricted
   characters, capped count) so prompt text cannot end up in them. Tags are attribution, **not authorization**: any caller can set any tag.
2. **Observe-only mode.** An agent you do not want routed can still be measured: pin a model (`pin=` in the SDK, `--model` in the CLI,
   `model: "<provider>/<id>"` on the server). Tokens, cost and tags are recorded; there is no routing, failover or escalation.
   Adoption becomes a base-URL change.
3. **Calls outside ModelSpend are invisible.** Only traffic through the SDK, CLI or server is counted. Importing the providers' own
   usage data (to reconcile and to see outside traffic) is a later phase and needs design-time verification of each provider's API.
4. **History must not change.** Costs are computed when the call happens, with the price in effect then. A later price update does
   not rewrite earlier reports.

## What the reports show

- Tokens by type (input, cached, output, reasoning), by project, agent, model, provider, task and day.
- Cost, with an explicit **unpriced** count: calls on models without a price keep exact tokens and show cost as partial, never guessed.
- **Overhead:** the cost and tokens of attempts that did not produce the final answer (failed calls, retries, superseded answers,
  classifier and judge calls).
- Later: cost per run (median and p95), cache hit rate, trends, top spenders, an estimated "what if" against a baseline model
  (labelled an estimate: tokenizers differ and prompts are not stored, so there is no true replay).

## Phases

| Version | Scope | Done when |
|---|---|---|
| **0.2** | Tags in SDK, CLI and server; schema migration; observe-only mode; `report` CLI (table, CSV, JSON) and `/v1/usage/report`; README restructure to two pillars | A tagged call on Anthropic and OpenAI appears in the report, and its token counts match the provider's usage page |
| **0.3** | Dashboard Costs tab; persistent per-project and per-agent budgets (soft and hard); overhead and unknown-cost views; retention (`prune`) | Budget refuses correctly under concurrent calls; dashboard totals equal the CLI's |
| **0.4** | Provider usage import and reconciliation; savings what-if; run-level views | Reconciliation shows the gap between ModelSpend and provider totals |

## Quality bar

- Migration test: a database created by 0.1.0b1 opens in 0.2 without data loss.
- Totals invariant: for any grouping, the group totals sum to the overall total (tokens and cost).
- Concurrency test for budgets (0.3): parallel calls against one limit.
- Privacy tests: tags cannot carry content; no prompt text reaches storage or reports.
- Live check: tag real calls on both providers, compare the report with each provider's usage page.

## Risks, stated plainly

- **Reporting is a crowded space.** Gateways and observability products already track spend. The edge is the join with routing and
  budgets, attempt-level overhead, local-only storage without prompts, and tokens as the headline.
- **Only traffic through ModelSpend is counted** until the import ships.
- **Multi-user access control is out of scope.** This is a local tool; tags are not a security boundary.
