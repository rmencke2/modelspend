# OpenAI compatibility (`POST /v1/chat/completions`)

Point any OpenAI client at `http://127.0.0.1:8787/v1` and use `model="auto"` to let ModelSpend route, or a catalog model to only measure (see below).

## Supported subset

| Field | Support |
|---|---|
| `model` | `"auto"` (or `"modelspend/auto"`) routes. `"provider/model-id"` or a bare catalog id **pins** that model: observe-only, no routing, failover or escalation; tokens, cost and tags are recorded. Unknown or ambiguous models → 400 `model` |
| `messages` | roles `system`/`developer`/`user`/`assistant`/`tool`; **text content only** (string or text parts). Images/audio → 400 `unsupported_input` |
| `max_tokens` / `max_completion_tokens` | yes (output cap; also the worst case used for budgeting; default 1024 if omitted) |
| `temperature` | yes, only to models whose catalog says temperature is supported |
| `stream`, `stream_options` | yes (SSE). Usage and routing metadata are in the final chunk |
| `response_format` | `json_schema` (native where the provider supports it, validated always) and `json_object` (validated as JSON; not enforced natively); `text` |
| `tools`, `tool_choice` | function tools; tool calls are returned with provider IDs, never executed; not combinable with `json_schema` |
| `n` | only 1 |
| `top_p`, `stop`, `seed`, penalties, `reasoning_effort` | **rejected**; use `modelspend.provider_options.<provider>.<key>` (allowlisted per provider) |
| `logprobs`, `logit_bias`, `audio`, `modalities`, `functions`, `store`, `metadata`, … | rejected with 400 and the parameter name |
| unknown fields | 400 (no silent ignoring) |

Responses use the standard `chat.completion` / `chat.completion.chunk` shapes: `choices[0].message`, `finish_reason`, `usage` (`prompt_tokens` includes cached tokens, `completion_tokens` includes reasoning tokens, details under `*_tokens_details`), `model` = the model actually used (final one after any failover/escalation).

## `modelspend` extension

Request (`body["modelspend"]`, via `extra_body` in the OpenAI SDK): `providers` (allowlist; falls back to `MODELSPEND_PROVIDERS`, otherwise 400 `provider_allowlist_required`), `profile`, `task_type`, `hard`, `acceptance_criteria`, `max_budget_usd`, `max_budget_tokens`, `allow_unpriced`, `latency_target_ms`, `validators`, `judge`, `escalation_limit`, `sensitive`, `allow_models`, `deny_models`, `provider_options`, and the attribution tags `project`, `agent`, `run_id`, `labels` (see [cost-attribution.md](cost-attribution.md); also readable from the headers `X-ModelSpend-Project`, `-Agent`, `-Run`, `-Labels`; the body wins). Unknown keys → 400.

Response `modelspend`: `provider`, `model`, `explanation`, `task`, `usage` and `total_tokens` (exact, summed over all attempts; also the standard top-level `usage`), `estimated_cost_usd`, `actual_cost_usd` (`null` when any attempt was unpriced), `uncertain_cost_usd`, `budget_tokens_remaining`, `attempts[]` (every call incl. retries/escalations/judge), `retries`, `failovers`, `escalations`, `validation`, `budget_*`, `uncertainty[]`, `catalog_version`, `policy_version`. Streaming: first chunk carries `modelspend.routing`; the last carries full metadata. If a stream fails after output began, an `error` event (`code: stream_interrupted`, `partial_text`) precedes `[DONE]`.

## Other endpoints

`POST /v1/route` (native request/response) · `POST /v1/route/preview` (candidates and estimates; no generation call, no spend) · `GET /v1/catalog` · `GET /v1/policy` · `GET /v1/usage` · `GET /v1/usage/report` (tokens and cost grouped by project, agent, model, …) · `GET /dashboard` · `GET /healthz`. Errors use `{"error": {message, type, param, code}}`; routing failures include the plan under `error.modelspend`.

## Auth

`MODELSPEND_API_TOKEN` set → `Authorization: Bearer <token>` required on everything except `/healthz`. Binding to a non-loopback address without a token is refused at startup.
