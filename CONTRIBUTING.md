# Contributing

Thanks for helping. Ground rules:

- **No invented facts.** Catalog entries (IDs, prices, limits, capabilities) must come from official provider documentation, with `source_url`, `effective_date` and `verified_on`. If you cannot verify a field, leave it `null`.
- **No fabricated performance.** Savings or quality claims must come from `modelspend eval` output, with simulated data labelled as such.
- **Never store or log secrets, prompts or responses** by default.
- Keep routing deterministic; do not add a model call to the per-request routing path.

## Dev setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
modelspend catalog validate && modelspend eval run --mode mock
```

Tests never need keys or network; provider tests use fakes shaped like the SDK responses. When you change an adapter, add a test for the new request/response mapping, usage normalization and error kind. When you change accounting or budgeting, add a test that fails if the gate is removed (budget tests must exercise the *runtime* ledger, not only plan-time filtering).

## Adding a provider

Implement `ProviderAdapter` (`generate`, `stream`, `list_models`, optionally `count_tokens`) in `providers/`, normalize usage so `output_tokens` includes reasoning exactly once, map errors via `classify_exception`, register it in `build_adapters`, add it to `types.PROVIDERS`/`config.KEY_ENV`, add allowed options to `routing.ALLOWED_PROVIDER_OPTIONS`, and document unverified live behaviour in `docs/limitations.md`.

## PRs

Small, focused diffs; tests included; docs updated when behaviour changes. License: contributions are accepted under Apache-2.0.
