# Architecture

## The plan this MVP was built from

1. **One vertical slice first**: types → catalog → pricing → routing plan → engine (reserve/call/account/validate/retry/escalate) → adapters → SDK/CLI/server → eval. The dashboard came last.
2. **Stack**: Python 3.10+, FastAPI, Pydantic v2, SQLite (stdlib), pytest. Official SDKs (`openai`, `anthropic`, `google-genai`); xAI through the OpenAI-compatible API.
3. **Determinism before models**: routing uses typed request fields, a deterministic category classifier, catalog data and measured evidence. No model call is made to route a request. An optional cheap classifier runs only for *ambiguous* prompts, only when enabled, and its cost is accounted.
4. **Honesty over polish**: suitability is an estimate; unknown values stay unknown; simulated results are labelled; unverified live behaviour is listed in [limitations](limitations.md).

## Modules (`src/modelspend/`)

| Module | Responsibility |
|---|---|
| `types.py` | `RouteRequest`, `Plan`, `RouteResult`, `Usage` (normalized), attempt/validation records |
| `catalog/` | Schema (`models.py`), loader with overrides + evidence overlay, validation, staleness, content-hashed version |
| `pricing.py` | The only cost arithmetic (estimates and accounting share it) |
| `tokens.py` | Input-token estimation, labelled with method |
| `policy.py` | Profiles, tier requirements, suitability assessment (Wilson lower bound on measured evidence) |
| `tasks.py` | Deterministic task categorization, salted-hash cache key |
| `routing.py` | `Router.plan`: hard constraints → category → estimates → suitability → ranking. No network |
| `engine.py` | `Engine.run/stream/plan`: budget `Ledger`, retries, failover, bounded escalation, judge, classifier call |
| `providers/` | Adapters normalizing messages, usage, finish reasons, errors, tool calls; `mock.py` for tests |
| `validation.py`, `sandbox.py` | Deterministic validators; Docker-only code execution |
| `store.py` | SQLite metadata store (no prompts, responses or keys) |
| `server/` | FastAPI app, OpenAI-compatible endpoint, preview, dashboard |
| `eval/` | Fixtures (calibration/held-out), harness, simulated providers |
| `client.py`, `cli.py` | SDK (sync + async) and CLI |

## Request flow

```
RouteRequest ─► validate (allowlist, provider_options, schema/tools, modalities=text)
   ─► hard constraints per catalog model: key present · privacy · allow/deny · availability · priced (+fresh) · limits ·
      capabilities (must be verified True) · output cap · context fit · worst-case cost ≤ budget · observed latency
   ─► task category (hint > cache > rules > [optional cheap classifier] > conservative fallback)
   ─► suitability per candidate (measured evidence, else tier prior where the profile allows) ─► rank by expected cost
   ─► Engine: for each attempt  reserve(worst case) → call → account(actual) → validate
        operational error → bounded retry → provider failover (allowlisted providers only)
        validator failure → escalate to a *stronger*, affordable candidate (≤ escalation_limit; never for tools/refusals)
   ─► RouteResult (answer, explanation, estimated vs actual cost, usage, attempts, validation, versions) ─► SQLite
```

## Design decisions

- **Prompts are data.** Provider allowlist, profile, budget and constraints come only from typed fields. The category classifier reads only the head/tail 600 chars of the last user message and ignores fenced code, so pasted/retrieved content in the middle cannot steer routing; an explicit `task_type` always wins.
- **Discovery is separate from metadata.** `catalog discover` lists model IDs from provider APIs; prices and capabilities come only from verified catalog entries and user overrides.
- **Unknown ≠ false ≠ true.** Capabilities are tri-state; an unknown capability never satisfies a requirement.
- **Accounting vs estimation.** `estimated_cost_usd` is a pre-call estimate; `actual_cost_usd` is computed from provider-reported usage × catalog prices across *all* attempts (including classifier and judge calls). Failed calls that may have been billed are charged to the budget as an upper bound and reported separately as `uncertain_cost_usd`. Provider invoices are authoritative.
- **Reasoning tokens** are inside `output_tokens`; `reasoning_tokens` is a memo field. Adapters normalize providers that report thinking separately (Gemini, optionally xAI via `pricing.reasoning_in_output=false`).
- **Streaming** fixes the model before the first byte. Pre-output failures may retry/fail over; after output begins, failure raises `StreamInterrupted` (HTTP: SSE `error` event) with the partial text and cost. No silent swap, no escalation.
- **Tools** are returned to the caller unexecuted with provider IDs preserved; requests with tools are never escalated or replayed.
- **Security defaults**: server binds 127.0.0.1; non-loopback requires a bearer token (constant-time compare); keys live in process memory only; DB file is `0600`.
