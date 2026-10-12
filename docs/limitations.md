# Limitations and roadmap

## Known limitations

- **Anthropic has been exercised live** (2026-10-12: model sync, routing, escalation, token-budget gating, streaming, dashboard; token counts and cost matched the provider's reported usage). **OpenAI has been exercised live for non-streaming chat only** (2026-10-12, `gpt-4.1-nano` and `gpt-5-mini`: routing, input-token estimate vs provider count, reasoning tokens reported inside the output count, cost from catalog prices); the OpenAI items in the list below that concern streaming, strict JSON-schema mode, cached tokens and failover are still untested, as is a comparison with OpenAI's billing page. The Gemini and xAI adapters implement the official SDK / documented API surfaces but were tested only against fakes shaped like the SDK responses. Not yet exercised live for them: request acceptance (e.g. `max_completion_tokens` and `reasoning_effort` on xAI; strict JSON-schema mode; Gemini `response_json_schema`), real usage-field semantics (Gemini thinking tokens, xAI reasoning tokens, cached tokens), streaming usage, count-token endpoints. The Docker code sandbox (`python_tests`) is likewise untested against a real Docker daemon.
- **Only Anthropic pricing ships**, verified 2026-10-12; OpenAI, Google and xAI have no shipped entries (OpenAI works once you add models and prices yourself). Model IDs/limits are not on the pricing pages and must be confirmed with `catalog sync` (see [catalog-verification](catalog-verification.md)). Users must add and maintain entries; the tool warns on staleness but cannot detect provider price changes by itself.
- **Suitability is an estimate.** Tier priors are editorial; measured evidence from small fixture sets is statistically weak; validators check form and key facts, not truth.
- **Expected cost ≠ cost per success.** Selection minimizes expected first-attempt cost among suitable models; escalation cost is bounded by budget but not modelled probabilistically (no fabricated success rates).
- **Token estimates are approximate** (chars/4, or tiktoken if installed, which is OpenAI-specific); reservations add a 20% margin when approximate. With `MODELSPEND_EXACT_TOKEN_COUNTS=true`, the selected candidate's input count is replaced by the provider's count endpoint (Anthropic, Gemini adapters; unverified live) before the call; ranking still uses estimates.
- **Text only.** Images/audio/files are rejected explicitly. Tool calls are returned, never executed; tool use cannot be combined with a response schema.
- **Streaming**: tool calls arrive whole at the end; no escalation on streams; partial-stream cost is estimated.
- **Task categorization** is keyword-based (English-centric). Ambiguous prompts get the stricter category; use `task_type` for reliability.
- **Latency targets** are enforced only from locally observed history (≥ 5 samples).
- **Privacy**: no prompts/responses stored; but request text still goes to the providers you allow. The `sensitive` flag only restricts to providers you declared acceptable — ModelSpend makes no claims about providers' data handling.
- Single-process SQLite; not designed for multi-tenant hosting.

- **Unpriced models** can run with `allow_unpriced`; their cost is `unknown` and they are ranked by tier then tokens, not cost.

- **Automatic price refresh is LLM-assisted and unverified live.** It is tested with scripted models and a real browser against fake pages, but not yet against the real OpenAI/Google/xAI pages. Verification catches invented numbers, not a hostile page; review before applying (see [pricing-refresh](pricing-refresh.md)).

## Roadmap

1. Verified catalog snapshots per provider, plus a script that diffs official pricing pages (when reachable) against the catalog.
2. Live smoke-test suite (opt-in, capped budget) per adapter; recorded-fixture contract tests from real responses.
3. Provider token-count endpoints in planning; prompt-cache aware estimates when callers opt into caching.
4. Larger, versioned calibration/held-out sets; per-user evidence dashboards.
5. Batch-API routing; multimodal inputs with explicit capability matrix; multi-turn tool-loop helpers (caller-driven).
6. Pluggable classifier and policy modules; per-key budgets and quotas.
