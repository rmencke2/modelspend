# ModelSpend

An open-source router that picks **the lowest-cost AI model expected to suit each task**, using **your own API keys** for OpenAI, Anthropic (Claude), Google (Gemini) and xAI (Grok). Python SDK, CLI, local FastAPI server with an OpenAI-compatible `/v1/chat/completions` (`model="auto"`), route preview, and an evaluation harness.

> **Honest status (v0.1, MVP).**
> - ModelSpend does **not** guarantee the cheapest successful model. "Suitable" is an *estimate* from catalog tier priors and, as you run evaluations, measured evidence.
> - **Only Anthropic pricing ships, and models are not routable until you run `modelspend catalog sync`.** The four Anthropic entries carry prices verified on 2026-10-12 against Anthropic's official pricing pages; their model IDs, availability and limits are confirmed from *your* key's live Models API by `catalog sync`. OpenAI, Google and xAI have no entries yet (their official pages haven't been verified): add them with `catalog template` / `catalog set` ([docs/catalog-verification.md](docs/catalog-verification.md)).
> - **Tokens first.** Token usage (input, cached, output, reasoning) is exact, provider-reported and summed over every attempt; it is the headline number in the CLI, API and dashboard. Cost is derived from it and shown as `unknown` for models without a price (`--allow-unpriced`). Budgets can be set in tokens (`--budget-tokens`) as well as dollars.
> - Provider adapters use the official SDKs but have been tested only against **recorded-shape fakes**, not live APIs ([docs/limitations.md](docs/limitations.md)).
> - Savings figures exist only as measured output of `modelspend eval`; mock runs are labelled **SIMULATED** and are not benchmarks.

## Five-minute quickstart (no API keys needed)

Requires **Python 3.10+** (check with `python3 --version`; on macOS the system Python is often 3.9, so `brew install python@3.12` and use `python3.12`). Old pip (e.g. 21.x) cannot do editable installs from `pyproject.toml`, hence the upgrade. The blocks below contain no inline comments so they paste cleanly into zsh.

```bash
git clone https://github.com/rmencke2/modelspend.git
cd modelspend
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e ".[dev]"
```

Run the tests (122, all mocked) and print the documented routing policy:

```bash
pytest -q
modelspend policy
```

Preview routing decisions using the fictional SYNTHETIC catalog (its prices are made up; no call is made):

```bash
modelspend preview "Classify this ticket: I was charged twice" --providers openai,anthropic --catalog synthetic --assume-keys --budget 0.01
```

Compare premium vs cheapest-eligible vs ModelSpend on simulated data (mechanics only, not a benchmark):

```bash
modelspend eval run --mode mock --out /tmp/modelspend-report.md
```

### Using it for real (Anthropic example)

1. **Set your key** (never paste it anywhere; consider a low spend limit in the provider console):
   ```bash
   export ANTHROPIC_API_KEY=...
   ```
2. **Confirm models and limits from your key's live Models API** (dry run first, then write). Prices are never touched; the shipped Anthropic prices come from the official pricing pages. `--assume` records capabilities *you* assert after reading the docs:
   ```bash
   modelspend catalog sync --provider anthropic
   modelspend catalog sync --provider anthropic --assume streaming,tools --write
   modelspend catalog list
   ```
3. **Preview (no call, no spend), then route.** Output leads with token usage:
   ```bash
   modelspend preview "Classify this ticket as billing, bug or other. Label only. Ticket: I was charged twice." --providers anthropic --max-output-tokens 16
   modelspend route "Classify this ticket as billing, bug or other. Label only. Ticket: I was charged twice." --providers anthropic --max-output-tokens 16 --budget-tokens 2000
   modelspend usage
   ```
4. **Other providers:** `modelspend catalog template openai/<model-id>` prints a skeleton to fill from the official docs; `modelspend catalog set <ref> tier=2 capabilities.streaming=true pricing.input_per_mtok=...` edits one model safely (reverted if invalid). Without a price a model only runs with `--allow-unpriced`, and its cost is reported as unknown while tokens stay exact.
5. **Serve it** (localhost only unless `MODELSPEND_API_TOKEN` is set). It listens on http://127.0.0.1:8787 with the dashboard at `/dashboard`:
   ```bash
   modelspend serve
   ```

## SDK

```python
from modelspend import ModelSpend

ms = ModelSpend(providers=["anthropic", "openai"], profile="balanced")   # explicit allowlist required
plan = ms.preview("Extract the total from: ...", max_budget_usd=0.01)    # sync; aroute/apreview/astream for async
res = ms.route(
    [{"role": "user", "content": "Classify: 'refund me' as billing|bug|other. Label only."}],
    max_budget_usd=0.01,
    validators=[{"type": "allowed_labels", "labels": ["billing", "bug", "other"]}],
)
print(res.text, res.provider, res.model, res.validation.status)
print(res.estimated_cost_usd, res.actual_cost_usd, res.escalations, res.explanation)
```

More: [examples/](examples/) (per-provider, streaming, tools, structured output, OpenAI client).

## OpenAI-compatible endpoint

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8787/v1", api_key="unused-unless-token-set")
r = client.chat.completions.create(
    model="auto", messages=[{"role": "user", "content": "Translate to French: hello"}],
    extra_body={"modelspend": {"providers": ["openai", "google"], "max_budget_usd": 0.01, "profile": "economy"}},
)
print(r.choices[0].message.content, r.model_extra["modelspend"]["explanation"])
```
Supported subset and the `modelspend` extension: [docs/openai-compat.md](docs/openai-compat.md).

## How it decides (summary)

hard constraints (allowlisted provider with a key, availability, known fresh-enough pricing, context, capabilities, privacy, budget) → task category (explicit hint > deterministic rules > cached/optional cheap classifier) → cost estimates → **cheapest candidate with sufficient suitability evidence** → execute → validate → bounded retry / failover (operational) or escalation (quality, only with validator evidence, default ≤ 1). Every attempt is reserved against the remaining budget before it starts and recorded afterwards. Full policy: [docs/routing-policy.md](docs/routing-policy.md); architecture: [docs/architecture.md](docs/architecture.md).

## Docs

[Architecture](docs/architecture.md) · [Routing policy](docs/routing-policy.md) · [Validation & escalation](docs/validation-and-escalation.md) · [Catalog verification](docs/catalog-verification.md) · [OpenAI compatibility](docs/openai-compat.md) · [Evaluation](docs/evaluation.md) · [Limitations & roadmap](docs/limitations.md) · [Contributing](CONTRIBUTING.md)

## License

Apache-2.0. See [LICENSE](LICENSE).
