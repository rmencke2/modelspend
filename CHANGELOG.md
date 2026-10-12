# Changelog

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
