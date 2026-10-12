# Validation and escalation

## Validators (cheap and deterministic first)

`response_schema` (JSON Schema), plus declarative `validators`: `json_schema`, `valid_json`, `required_fields`, `allowed_labels`, `exact`, `json_equals`, `contains`, `not_contains`, `regex`, `max_chars`, `min_chars`, `python_syntax` (parse only), `python_tests` (sandbox only). SDK callers can also pass `callable_validators` (return bool or `(bool, reason)`). Misconfigured or unknown validators **fail closed**.

A passing validator proves only what it checks. Valid JSON is not a correct answer; `acceptance_criteria` alone are *not* verified — without a validator the result is `unverified_criteria`.

## Generated code

`python_tests` runs only inside Docker: no network, read-only root, all capabilities dropped, `no-new-privileges`, pids/memory/CPU limits, wall-clock timeout, source mounted read-only. The pass marker is a random per-run nonce known only to the trusted runner, printed only after the tests complete; this is best effort, not adversary-proof (candidate and tests share a process). If Docker is missing the check **fails** with "generated code is never run on the host". `python_syntax` uses `ast.parse` and never executes code.

## Optional judge (`judge=true`)

A second model grades the answer against `acceptance_criteria`. It runs only after all deterministic checks passed, its cost is reserved against the budget and recorded as a `judge` attempt, and it uses the cheapest eligible tier ≥ 2 model. Limitations: judges are fallible and can share the answerer's blind spots; they favour fluent answers; the judged text and criteria are treated as data but injection is not impossible. Prefer deterministic validators when you can write them.

## Operational vs quality failures

| Kind | Examples | Response |
|---|---|---|
| Operational | rate limit, timeout, connection, 5xx | up to `max_retries` (default 2) with backoff, then provider **failover** to the next suitable affordable candidate among allowlisted providers |
| Operational, not retryable | auth | no retry; fail over to another provider |
| Request errors | bad_request / unsupported / unknown | fail fast (no failover; the same request would likely fail again) |
| Quality | a configured validator failed (including truncation when validators exist) | **escalate** to a stronger candidate (higher tier, or clearly better measured evidence) that is affordable; at most `escalation_limit` (default: economy 0, balanced/high 1) |
| Refusal | provider-signalled content filter/refusal | **not** a quality failure; no escalation; status `refused` |
| Tool calls | response contains tool calls | returned to the caller unexecuted; never validated-and-retried or escalated |

If the final attempt still fails validation, the answer is returned with `status: "validation_failed"` and the evidence in `validation.results`.

## Budget

Before every call (initial, retry, failover, escalation, classifier, judge) the engine requires `spent + uncertain + worst_case(call) ≤ budget`; otherwise the call is not started (recorded as `skipped_budget`). Exact pre-call spend cannot be guaranteed: usage is finalized by the provider, so a call can cost less than its reservation but the reservation is a conservative bound built from the output cap and a safety-margined input estimate. Failed calls that may have been billed (timeouts, 5xx, interrupted streams) are charged to the budget as an upper bound and shown separately as `uncertain_cost_usd`.

## Streaming

Model selected before output. Before the first chunk: retries/failover allowed. After: no replacement; failure → `StreamInterrupted` (SDK exception; SSE `error` event with `partial_text`). Validation runs at the end and is *reported*, but streamed text is not replaced or escalated.
