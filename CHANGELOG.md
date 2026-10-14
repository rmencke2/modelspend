# Changelog

## 0.3.0b1 - unreleased

- **Dashboard Costs tab.** Hero token count, cost, overhead, cache hit rate and unpriced-call tiles; tokens by project / agent / model /
  provider / task / purpose as stacked bars (input, cached, output); tokens per day; a details table; one filter row (window, group by,
  project) that scopes everything. Light and dark, keyboard and tooltip support, a table-view twin for every chart.
- **Persistent budgets per project and/or agent** (`modelspend budget set|list|remove`, `GET/PUT /v1/budgets`, `DELETE /v1/budgets/{id}`,
  dashboard form): daily or monthly (UTC) limits in USD and/or tokens, **hard** (the call is refused before anything is sent; HTTP 429
  `period_budget_exceeded`) or **soft** (a warning on the result). Checked before every provider call, including retries, failovers,
  escalations, classifier and judge calls and streams, in one SQLite write transaction so concurrent threads and processes sharing the
  database cannot jointly overshoot. In-flight calls hold their worst case; crashed holds expire after 15 minutes.
- `modelspend prune <age> --yes` deletes old usage records (budgets and settings are kept).
- The usage database now uses WAL mode so reports never block writers.
- Fix: when an escalation could not run because of a budget, the earlier answer is now returned as `validation_failed` with
  `escalations: 0` (it used to be reported as `ok`).
- Soft budget warnings are based on actual spend (they no longer fire on a call's worst-case projection).
- Verified live on OpenAI (CLI): hard budget accounting and refusal before the call, soft budget warning. The dashboard Costs tab and concurrent enforcement are not yet verified live; the concurrency guarantee is tested with threads on one machine, not across machines or network file systems.

## 0.2.0b1 - unreleased

- **Cost attribution.** Optional `project`, `agent`, `run_id` and `labels` tags on every call (SDK, CLI flags, `MODELSPEND_PROJECT` / `MODELSPEND_AGENT`,
  server headers and `modelspend` body extension). Tags are restricted identifiers so prompt text cannot reach the store.
- **`modelspend report`** and `GET /v1/usage/report`: tokens (input, cached, output, reasoning) and cost grouped by project, agent, run, provider,
  model, task, profile, day, purpose or label; filters, time windows, table / CSV / JSON output; overhead (cost of attempts that did not
  produce the answer); unpriced calls are counted, never guessed.
- **Observe-only mode.** Pin a model (`pin=`, `--model`, or a non-`auto` `model` on the server) to measure an agent without routing it.
- The usage database migrates itself from 0.1 (new nullable columns; old rows report as `(untagged)`).
- Not yet verified against the providers' own usage pages. Dashboard view and per-agent budgets are planned for 0.3.

## 0.1.0b1 - first public beta

- Cost-aware router across OpenAI, Anthropic, Google and xAI using your own API keys; within-provider and cross-provider routing.
- Quality profiles (economy, balanced, high assurance) as documented policies; deterministic task categorization; bounded retries,
  failover and a single validator-evidenced escalation; budgets in dollars and tokens enforced before every call.
- Token-first accounting: exact provider-reported usage summed over every attempt, cost derived when a price is known.
- Python SDK (sync and async), CLI, local FastAPI server with an OpenAI-compatible `/v1/chat/completions` (`model="auto"`),
  route preview, usage dashboard and a Pricing tab.
- Catalog with verified Anthropic pricing, `catalog sync` from provider model lists, overrides, and a propose/review/apply price refresh.
- Evaluation harness with separate calibration and held-out sets; simulated mode for CI.
- Verified live: Anthropic (sync, routing, escalation, token budgets, streaming, dashboard); OpenAI for chat and streaming (routing, token counts incl. reasoning tokens, provider-reported streaming usage, cost). Gemini and xAI adapters, OpenAI structured output/failover/cross-vendor escalation and the Docker code sandbox are tested against fakes only. See docs/limitations.md.
