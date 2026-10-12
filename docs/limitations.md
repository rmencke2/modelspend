# Limitations and roadmap

## Known limitations

- **Live providers unverified.** All four adapters implement the official SDK / documented API surfaces but were tested only against fakes shaped like the SDK responses. Not exercised live: real request acceptance (e.g. `max_completion_tokens` and `reasoning_effort` on xAI; strict JSON-schema mode; Gemini `response_json_schema`; Anthropic forced-tool structured output), real usage-field semantics (Gemini thinking tokens, xAI reasoning tokens, cached tokens), streaming usage, count-token endpoints, model listing.
- **No model data ships** (empty catalog; no verified IDs or prices) (see [catalog-verification](catalog-verification.md)). Users must add and maintain entries; the tool warns on staleness but cannot detect provider price changes by itself.
- **Suitability is an estimate.** Tier priors are editorial; measured evidence from small fixture sets is statistically weak; validators check form and key facts, not truth.
- **Expected cost ≠ cost per success.** Selection minimizes expected first-attempt cost among suitable models; escalation cost is bounded by budget but not modelled probabilistically (no fabricated success rates).
- **Token estimates are approximate** (chars/4, or tiktoken if installed, which is OpenAI-specific); reservations add a 20% margin when approximate. With `MODELSPEND_EXACT_TOKEN_COUNTS=true`, the selected candidate's input count is replaced by the provider's count endpoint (Anthropic, Gemini adapters; unverified live) before the call; ranking still uses estimates.
- **Text only.** Images/audio/files are rejected explicitly. Tool calls are returned, never executed; tool use cannot be combined with a response schema.
- **Streaming**: tool calls arrive whole at the end; no escalation on streams; partial-stream cost is estimated.
- **Task categorization** is keyword-based (English-centric). Ambiguous prompts get the stricter category; use `task_type` for reliability.
- **Latency targets** are enforced only from locally observed history (≥ 5 samples).
- **Privacy**: no prompts/responses stored; but request text still goes to the providers you allow. The `sensitive` flag only restricts to providers you declared acceptable — ModelSpend makes no claims about providers' data handling.
- Single-process SQLite; not designed for multi-tenant hosting.

## Roadmap

1. Verified catalog snapshots per provider, plus a script that diffs official pricing pages (when reachable) against the catalog.
2. Live smoke-test suite (opt-in, capped budget) per adapter; recorded-fixture contract tests from real responses.
3. Provider token-count endpoints in planning; prompt-cache aware estimates when callers opt into caching.
4. Larger, versioned calibration/held-out sets; per-user evidence dashboards.
5. Batch-API routing; multimodal inputs with explicit capability matrix; multi-turn tool-loop helpers (caller-driven).
6. Pluggable classifier and policy modules; per-key budgets and quotas.
