# Changelog

## 0.5.0b2 - unreleased

- **Fix: second sync call failed with `Event loop is closed`.** `ModelSpend.route/preview/stream` ran each call on a fresh `asyncio.run()` loop, but provider
  SDK clients are cached and stay bound to the first loop, so any script making two sync calls on one object failed (found in the OpenAI structured-output live test, which then passed live for both strict and non-strict schemas).
  Sync calls now share one background loop. Regression test included. The async API and the CLI/server were not affected.
- Verified live: OpenAI structured output (`response_schema`), strict and non-strict, on `gpt-4.1-nano`.

## 0.5.0b1

- **Your own evidence bars.** `modelspend thresholds set|list|remove`: per task category (or `*`) set how many eval cases a model needs (`min_n`),
  the pass rate it must show (`min_pass_rate`), whether that is judged on the conservative 95% Wilson lower bound (default) or the observed rate (`basis`),
  and whether a model may be chosen on its tier prior alone (`require_evidence`). Resolved field by field: category, then `*`, then the profile. A setting can
  only tighten `high_assurance`'s evidence requirement. Bars that cannot be cleared at the evidence's n are reported as such (with the n needed) instead of
  silently failing. Invalid settings raise an error.
- **Your own eval cases.** Only `id`, `category`, `messages` and `validators` are required (`difficulty`, `reference`, `wrong` are optional); cases can come from a single
  `.jsonl` file, a folder of them, or `<dir>/calibration` + `<dir>/heldout`. `modelspend eval check` lints them for free (bad JSON with file:line, unknown fields,
  duplicate ids, unknown or misconfigured validators, validators that reject the `reference` or accept the `wrong` answer, enough cases for your bar);
  `modelspend eval template <category>` prints a starter case; `eval calibrate --dataset NAME` names the evidence.
- **`modelspend evidence`** (and `GET /v1/evidence`; `GET /v1/policy` now lists thresholds): which models clear the bar for which category, and the cheapest that
  does. It uses the router's own functions, so the two cannot disagree.
- Observed live: the same model scored 19/22 on one calibration run and 20/22 on the next, so one run is one sample (documented). `--show-failures` showed both failures to be fair (a keyword trap and an instruction hidden in the ticket).
- `eval calibrate --show-failures` lists which cases each model failed and its shortened answer (printed only, never stored).
- Example: `examples/evals/support-tickets-hard/` (22 calibration + 8 held-out cases with explicit labelling rules and traps: misleading keywords, negation, hidden instructions, other languages).
- Docs: docs/own-evals.md. Unit-tested (including a property test that the table and the router agree) and run live at small scale: 7 models on the 22 calibration cases of examples/evals/support-tickets-hard for about $0.13: six scored 22/22 and cleared the default bar, `gpt-4.1-nano` scored 19/22 (lower bound 0.67, below the bar) and is no longer chosen for classification; on the 8 held-out cases ModelSpend matched the premium model's pass rate (8/8) at about 77% lower spend, while the cheapest model missed one (7/8). Small samples: wide intervals, one model separated.

## 0.4.0b1

- **Model aliases.** `modelspend alias set coding anthropic/claude-sonnet-5-5`, then `--model coding`, `model="coding"` on the local server, or `pin="coding"`
  in the SDK. An alias is a pinned model (no routing); tokens, cost, tags, reports and budgets still work. `GET/PUT/DELETE /v1/aliases`.
- **Conversation stickiness.** Requests that carry earlier turns keep the model chosen for the conversation unless it is no longer suitable
  or available (then it is released, with the reason). Follow-ups that cannot be classified inherit the conversation's task category.
  Salted-hash key only, 24 h expiry. Off with `sticky=False`, `MODELSPEND_STICKY=false` or `sticky_conversations: false`. Results carry `sticky`.
- Fix: `preview --assume-keys --model ...` now assumes a key for the pinned model's provider.
- Verified live on OpenAI and Anthropic (alias pin; held conversation; switch with stickiness off; unclassifiable follow-up inherits the category). See docs/aliases-and-stickiness.md.
- Fix: the overhead tile and report showed overhead cost as "unknown" when there were no overhead calls; it is now $0.
- Fix: the daily chart repeated the same date on its x-axis for a one-day window.
- Cross-vendor escalation was verified live (OpenAI nano -> Anthropic Sonnet, exactly one escalation, tokens and cost reconcile, overhead reported).
- Provider failover on a bad key was verified live (OpenAI `auth` error -> not retried -> Anthropic answered; the key did not appear in the output).
- The Costs tab was checked on live data (tokens by agent and by model each add up to the headline total).

## 0.3.0b1

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
