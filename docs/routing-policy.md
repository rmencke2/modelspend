# Routing policy (version `2026.10-1`)

Print it any time: `modelspend policy`. Policies are defined in `src/modelspend/policy.py`; every response carries `policy_version`.

## Task categories and base tier

Tiers are an **editorial prior** assigned in the catalog (1 small/fast, 2 mid, 3 frontier). They are not measurements.

| Category | Base tier required |
|---|---|
| classification, extraction, rewriting, summarization, translation, qa_context | 1 |
| code (generation/debugging) | 2 |
| analysis (complex) | 3 |
| unknown (uncategorizable → conservative fallback) | 2 |

`hard=true` (explicit caller hint) adds 1. Prompt length is **not** used as a difficulty signal.

## Profiles

| Profile | Tier requirement | Evidence rule | Escalations (default) | Validation |
|---|---|---|---|---|
| `economy` | base − 1 (min 1) | tier prior accepted; measured evidence counts at n ≥ 10 with Wilson 95% lower bound ≥ 0.65 | 0 | optional |
| `balanced` (default) | base | tier prior accepted; measured evidence counts at n ≥ 20, lower bound ≥ 0.80 | 1 | optional |
| `high_assurance` | base + 1 (max 3) | **measured evidence required**: n ≥ 40, lower bound ≥ 0.90; priors are not accepted | 1 | **required** (schema, validators, callable or judge) |

Measured evidence beats the prior in both directions: a tier-1 model with strong evidence for `code` qualifies; a tier-2 model with sufficiently many, poor results is disqualified. Evidence below the sample minimum is ignored (and reported as "evidence insufficient") rather than extrapolated.

With no evidence anywhere, `high_assurance` refuses with an explanation instead of guessing; run `modelspend eval calibrate --mode live --budget … --write` (with your own fixtures) to produce evidence.

### Your own evidence bars

The `min_n` and lower-bound figures above are the **built-in** bars. `modelspend thresholds set <category|*> --min-n N --min-pass-rate R [--basis lower_bound|observed] [--require-evidence]`
overrides them per task category in every profile, resolved field by field (category, then `*`, then the profile). The measured verdicts are *clears*, *below the
bar*, *needs more cases* and *bar unreachable at this n* (a perfect record at that n still could not show the rate; the cases needed are reported); in the last two
the model falls back to its tier prior unless the profile or your setting requires evidence. A setting can only tighten `high_assurance`. See [own-evals.md](own-evals.md).

## Selection

Among candidates that pass all hard constraints and are suitable, choose the lowest **expected** cost when every candidate is priced. If *any* candidate has no price (`allow_unpriced`), costs are not comparable, so ranking falls back to **smallest suitable tier, then fewest expected tokens** (`plan.ranking_basis = "tier_then_tokens"`); ties → higher measured lower bound, higher tier, then provider/model name. No success probabilities are fabricated and a model's self-reported confidence is never used.

Expected output tokens are assumptions per category (classification 16, extraction 200, summarization 250, qa 200, code 700, analysis 1000, unknown 400; rewriting/translation scale with the input; capped by `max_output_tokens`; always-on reasoning models add `reasoning_overhead_tokens`, default 1024). The *worst case* used for budget reservation always assumes the output cap. Both are listed in `plan.assumptions`.

## Budgets: dollars and tokens

`max_budget_usd` and `max_budget_tokens` (total input + output across **all** attempts) are independent ceilings; both are enforced before every call using the worst case (output cap plus a margin-adjusted input estimate) and both are tracked after every call from provider-reported usage. A USD budget needs prices, so unpriced models are excluded when one is set; use a token budget for them. Failed calls that may have been billed are charged to both budgets as an upper bound.

## Hard constraints (never relaxed by suitability)

Allowlisted provider with a configured key · `sensitive` requests only to providers in `MODELSPEND_SENSITIVE_PROVIDERS` · `allow_models`/`deny_models` · availability `available` (preview only if enabled) · pricing known (unless `allow_unpriced`), USD, and (optionally) fresh · limits known · capabilities verified true (structured output, tools, streaming, temperature) · requested output ≤ model max · input + output cap fits context · worst-case cost ≤ remaining budget · observed p50 latency ≤ target when ≥ 5 samples exist (otherwise the latency target is reported as unverified).

## Uncertainty reporting

`plan.uncertainty` / `result.uncertainty` say when: the category was low-confidence; the pick rests on a tier prior; token counts are approximate; a latency target is unverified; acceptance criteria were given without a validator; escalation was impossible for budget reasons.

## Cost-affecting provider options

Only allowlisted namespaced keys are forwarded (`provider_options.<provider>.…`; see `routing.ALLOWED_PROVIDER_OPTIONS`). Options that change pricing (e.g. service tiers) are rejected. `reasoning_effort` changes token usage and is allowed; estimates then understate actual cost.
