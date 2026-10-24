# Examples

All examples need verified catalog entries and keys (see ../docs/catalog-verification.md). Without keys, try the previews against the synthetic catalog (`modelspend preview ... --catalog synthetic --assume-keys`).

| File | Shows |
|---|---|
| `single_provider.py <openai\|anthropic\|google\|xai>` | within-provider routing (allowlist of one) with a validator and a budget |
| `cross_provider.py` | cross-provider routing across an explicit allowlist; preview then route |
| `streaming_and_tools.py` | async streaming, interrupted-stream handling, tool calls returned unexecuted |
| `caching_live_check.py [provider/model]` | a real-provider check of `modelspend caching`: a fixed prompt (should be cached) and one that starts with the time (should be named); on Claude it switches the cache markers on for the run |
| `openai_client.py` | the OpenAI Python client against `modelspend serve` with `model="auto"` |
| `overrides.example.json` | skeleton for verified prices/availability (placeholders are `null`) |
