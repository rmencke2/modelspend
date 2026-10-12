# Changelog

## 0.1.0 - first public release

- Cost-aware router across OpenAI, Anthropic, Google and xAI using your own API keys; within-provider and cross-provider routing.
- Quality profiles (economy, balanced, high assurance) as documented policies; deterministic task categorization; bounded retries,
  failover and a single validator-evidenced escalation; budgets in dollars and tokens enforced before every call.
- Token-first accounting: exact provider-reported usage summed over every attempt, cost derived when a price is known.
- Python SDK (sync and async), CLI, local FastAPI server with an OpenAI-compatible `/v1/chat/completions` (`model="auto"`),
  route preview, usage dashboard and a Pricing tab.
- Catalog with verified Anthropic pricing, `catalog sync` from provider model lists, overrides, and a propose/review/apply price refresh.
- Evaluation harness with separate calibration and held-out sets; simulated mode for CI.
- Not yet verified live: Gemini and xAI adapters; all provider calls are otherwise tested against fakes. See docs/limitations.md.
