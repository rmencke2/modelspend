# Cost attribution: tokens and cost per project and per agent

*New in 0.2.0b1. Tested with mocked providers; a comparison against the providers' own usage pages is still to be done (see
[roadmap](roadmap-cost-attribution.md)).*

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

## Limits you should know

- **Only calls made through ModelSpend are counted.** Traffic that bypasses it is invisible. Importing the providers' own usage data
  is planned, not built.
- Everything is stored locally in SQLite (`~/.modelspend/usage.db`), metadata only: no prompts, no responses, no keys.
- A single machine and SQLite are assumed. Persistent per-agent budgets and a dashboard view are planned for 0.3.
