# Cost attribution: tokens and cost per project and per agent

*Reporting (0.2) was checked live against Anthropic and OpenAI usage. Budgets (0.3) were verified live on OpenAI from the CLI (hard refusal before the call, soft warning). The dashboard Costs tab was checked on live data (per-agent and per-model totals add up to the headline); the budget form is tested in a browser with generated data, and concurrent enforcement is tested with threads, not live (see [roadmap](roadmap-cost-attribution.md)).*

ModelSpend does two things: it **routes** each task to a suitable low-cost model, and it **shows where your tokens and money go**.
This page is about the second. You do not have to use routing to get it.

## 1. Tag your calls

A call can carry up to four kinds of tag. All are optional; calls without them are reported as `(untagged)`.

| Tag | Meaning | Example |
|---|---|---|
| `project` | the product or service | `billing` |
| `agent` | the agent or job inside it | `invoice-bot` |
| `run_id` | groups the calls of one agent run | `run-2026-10-14-0042` |
| `labels` | up to 8 extra `key=value` tags | `env=prod`, `team=growth` |

Tags are short identifiers: 1-64 characters from letters, digits and `. _ : / -`, no spaces. `project` and `agent` are lower-cased
so `Billing` and `billing` land in the same group. The restriction is deliberate: it makes it impossible to put prompt text in a tag,
so no content reaches the store or a report. **Tags are attribution, not authorization:** any caller can set any tag.

**Python SDK**

```python
ms = ModelSpend(providers=["anthropic", "openai"], project="billing", agent="invoice-bot")   # defaults for every call
res = ms.route("Classify: ...", run_id="run-42", labels={"env": "prod"})                      # per-call values win
```

**OpenAI-compatible server** (any OpenAI client; only headers and `base_url` change)

```python
client = OpenAI(base_url="http://127.0.0.1:8787/v1", api_key="unused",
                default_headers={"X-ModelSpend-Project": "billing", "X-ModelSpend-Agent": "invoice-bot"})
```

Headers: `X-ModelSpend-Project`, `X-ModelSpend-Agent`, `X-ModelSpend-Run`, `X-ModelSpend-Labels: env=prod,team=growth`.
The `modelspend` body extension (`project`, `agent`, `run_id`, `labels`) wins over headers, which win over the environment defaults
`MODELSPEND_PROJECT` / `MODELSPEND_AGENT`.

**CLI**: `modelspend route "..." --project billing --agent invoice-bot --run-id run-42 --label env=prod`.

## 2. Measure an agent without routing it (observe-only)

If you only want to see what an existing agent costs, **pin a model**. ModelSpend runs exactly that model, with no routing, no
failover and no escalation, and records tokens, cost and tags.

- Server: `model="anthropic/claude-haiku-5-5"` (or the bare catalog id when it is unique) instead of `"auto"`.
- SDK: `ms.route(..., pin="anthropic/claude-haiku-5-5")`.
- CLI: `modelspend route "..." --model anthropic/claude-haiku-5-5`.

The model must be in your catalog. It may be **unpriced**: tokens are still exact and cost is reported as unknown
(`modelspend catalog sync --add-new --only <id> --exact --write` adds a model without a price).

## 3. Read the report

```bash
modelspend report                                    # last 30 days by project, agent and model
modelspend report --by agent,model --since 7d
modelspend report --project billing --by agent,day   # one project, day by day
modelspend report --by project,label.env --format csv > spend.csv
modelspend report --format json
```

`--by` takes up to four of: `project`, `agent`, `run`, `provider`, `model`, `task`, `profile`, `day` (UTC), `purpose`, `label.<name>`.
Filters: `--project`, `--agent`, `--run`, `--provider`, `--model` (as `provider/id`), `--task`; `'(untagged)'` selects calls without a tag.
`--since` accepts `7d`, `24h`, `2w`, a date like `2026-10-01`, or `all`.

The same data is available at `GET /v1/usage/report?by=project,agent&since=7d&project=billing` (same auth rules as the other
data endpoints). A dashboard view is planned for 0.3.

### What the columns mean

- **tokens**: input (uncached) + cache read + cache write + output, as reported by the provider. **reasoning** is a memo already
  inside output.
- **cost**: the sum of costs ModelSpend computed *when each call ran*, with the price in effect then, so a later price change never
  rewrites history. A trailing `+` or `cost_complete: false` means some calls used models without a price: their tokens are
  counted, their cost is not, and ModelSpend does not guess it.
- **overhead**: the share of tokens (and the cost) spent on attempts that did **not** produce the final answer: failed calls,
  retries, superseded answers after an escalation, classifier and judge calls. It is the price of reliability and routing, per agent.
- **uncertain**: a call that failed but may have been billed is charged at an upper bound and counted in `uncertain_attempts`.
- Your provider's invoice is authoritative.

## 4. The dashboard Costs tab (0.3)

Open `http://127.0.0.1:8787/dashboard#costs`. One filter row (window: 24 h / 7 days / 30 days / all; group by; project) scopes everything
below it: the token total, tiles for cost, requests, **overhead**, cache hit rate and **calls without a price**, tokens by group as stacked
bars (input, cached input, output; hover or focus a bar for the details), tokens per day (UTC), and a table with every number. The
chart shows at most 12 groups and folds the rest into "Other"; the table lists them all.

## 5. Budgets per project and agent (0.3)

```bash
modelspend budget set --project shop --period monthly --usd 50                      # hard limit for the whole project
modelspend budget set --project shop --agent invoice-bot --period daily --tokens 2000000 --mode soft
modelspend budget set --agent invoice-bot --period monthly --usd 20                 # that agent in every project
modelspend budget set --period monthly --usd 500                                    # everything through ModelSpend
modelspend budget list
modelspend budget remove 3
```

A call is subject to every budget whose scope matches its tags (an empty project or agent matches any). Limits are for the current **UTC
day or month**, in USD and/or tokens.

- **Hard:** before every provider call (including each retry, failover, escalation, classifier or judge call, and streams), ModelSpend
  checks *spent so far + calls in flight + this call's worst case*. If a hard limit would be exceeded the call is **not made** and you get
  HTTP 429 `period_budget_exceeded` (CLI/SDK: `PeriodBudgetExceeded`) naming the budget, the amounts and when it resets. If a later
  escalation would not fit, the earlier answer is returned with the reason instead of an error.
- **Soft:** never refuses. The result carries `budget_warnings` (at 80% of the limit and when over, judged on what has actually been spent, including this call) and `modelspend budget list` shows the state.
- **Atomic:** the check and the reservation happen in one SQLite write transaction, so several threads or processes sharing the same
  usage database cannot jointly overshoot a limit. A call in flight holds its worst case; the hold shrinks to what was really spent after
  each call and is dropped when the request ends. A hold left by a crashed process expires after 15 minutes.
- **USD limits count only priced calls.** A hard USD limit refuses to run a model that has no price (it cannot know the cost); use a token
  limit for those. Failed calls that may have been billed count at their upper-bound estimate.
- The dashboard shows each budget as a meter (on track / approaching / over) and can add or remove them.

## 6. Retention

`modelspend prune 90d` shows what would be deleted; add `--yes` to delete usage records older than that (budgets and settings are kept).
Reports and budget windows are computed from what remains.

## Limits you should know

- **Only calls made through ModelSpend are counted.** Traffic that bypasses it is invisible. Importing the providers' own usage data
  is planned, not built.
- Everything is stored locally in SQLite (`~/.modelspend/usage.db`), metadata only: no prompts, no responses, no keys.
- A single machine and SQLite are assumed. Budgets are enforced by the processes that share one usage database on one machine; they are not designed for network file systems or several machines.
- Budget windows are UTC. Budgets only see calls made through ModelSpend.
