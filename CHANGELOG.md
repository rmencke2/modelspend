# Changelog

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
