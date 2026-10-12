# Catalog verification

## State of the shipped catalog

`src/modelspend/catalog/data/catalog.json` ships **empty on purpose**. When v0.1 was built (2026-10-12), the build sandbox could not reach the providers' official documentation (docs.anthropic.com, platform.openai.com, ai.google.dev, docs.x.ai were blocked by the network policy). Rather than enter model IDs, prices or capabilities from third-party aggregators or memory, no model is shipped. Until you add verified entries, nothing is eligible for automatic routing. Maintainers: replacing this with a verified snapshot is the top roadmap item.

`synthetic_test_catalog.json` contains **fictional** models and prices for tests and mock evaluations (`--catalog synthetic`). It must never be used for real routing.

## Adding verified entries

1. `modelspend catalog discover` — which IDs do your keys actually return? (IDs only.)
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
