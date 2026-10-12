# Catalog verification

## State of the shipped catalog

`src/modelspend/catalog/data/catalog.json` (version `0.2.0-anthropic-pricing`) contains four Anthropic entries: Haiku 5.5, Sonnet 5.5, Opus 5.5 and Fable 5.1. Their **prices** (input, output, cache read, 5-minute cache write, and Haiku 5.5's >100K-token tier) were read on 2026-10-12 from Anthropic's official pricing pages (https://platform.claude.com/docs/en/about-claude/pricing and https://claude.com/pricing), which agreed with each other. Not on those pages, and therefore **not shipped**: model IDs as API strings (taken from Anthropic's Claude API reference), availability, context/output limits, and per-model capabilities. They stay `availability: "unverified"` (so not routable) until `modelspend catalog sync` confirms them from your key's live Models API. The `tier` values are an editorial prior from Anthropic's model line (haiku 1, sonnet 2, opus/fable 3), not a measurement.

Pricing multipliers ModelSpend does not model because it does not use those features: Batch API (50% off), US-only inference (1.1x), fast mode, 1-hour cache writes, server-side tool charges.

OpenAI, Google and xAI have **no entries**: their official pages could not be reached when this was built and have not been verified since. `synthetic_test_catalog.json` contains **fictional** models and prices for tests and mock evaluations (`--catalog synthetic`); never use it for real routing.

## What a key can and cannot tell you

| | From the provider's list endpoint with your key |
|---|---|
| Which model IDs you can call | yes (all providers) |
| Context window, max output, structured-output support, lifecycle, model line | Anthropic: yes (`max_input_tokens`, `max_tokens`, `capabilities`, `lifecycle`, `line`). Gemini: token limits. OpenAI/xAI: IDs only |
| **Prices** | no, never: take them from the official pricing page |
| Your account's rate limits / spend limit / plan | not with a normal key (Anthropic's Admin API with an admin key can report some of it) |

`modelspend catalog sync` applies the first two rows to the catalog; it never writes prices.

## Adding verified entries

1. `modelspend catalog sync --provider <p>` (dry run, then `--write`) fills availability, limits, structured-output support and a suggested tier for models already in the catalog; `--add-new` also adds unknown models (without prices). `modelspend catalog discover` lists IDs only.
2. `modelspend catalog template <provider>/<model-id>` — prints an `add_models` skeleton.
3. For each model, read the provider's **official** models page and pricing page and fill: exact ID, context and max output tokens, capabilities (leave `null` if you did not verify), input/output price per million tokens, cached-input and cache-write prices when the provider documents them, long-context price tiers (`long_context_tiers`) and other billing conditions (`conditions`), `effective_date`, `source_url`, `verified_on` (today), and `reasoning_in_output` (does the API's reported output-token count already include reasoning tokens?).
4. Save in `~/.modelspend/overrides.json`:
   ```json
   {"add_models": [ { ...filled template... } ],
    "models": {"<provider>/<model-id>": {"availability": "available", "verified_on": "2026-10-12",
               "pricing": {"input_per_mtok": 0, "output_per_mtok": 0, "source_url": "...", "verified_on": "2026-10-12"}}}}
   ```
   (`0` above is a placeholder, not a price.) Overrides can also mark a model `"availability": "unavailable"` for your account or replace prices (e.g. negotiated rates).
5. Assign `tier` (1–3) honestly; it is an editorial prior. `modelspend catalog validate` checks the result; `modelspend catalog stale` lists pricing older than 30 days.

Measured evidence goes in `~/.modelspend/evidence.json` (written by `modelspend eval calibrate --write`) and is merged on load. The effective catalog version is `<catalog_version>+<hash of effective content>`, so any override or evidence change changes the reported version.
