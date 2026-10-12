# ModelSpend

An open-source router that picks **the lowest-cost AI model expected to suit each task**, using **your own API keys** for OpenAI, Anthropic (Claude), Google (Gemini) and xAI (Grok). Python SDK, CLI, local FastAPI server with an OpenAI-compatible `/v1/chat/completions` (`model="auto"`), route preview, and an evaluation harness.

> **Honest status (v0.1, MVP).**
> - ModelSpend does **not** guarantee the cheapest successful model. "Suitable" is an *estimate* from catalog tier priors and, as you run evaluations, measured evidence.
> - **The shipped catalog contains no models.** The build environment could not reach the providers' official documentation, so no ID, price, limit or capability was entered unverified. Until you add verified entries (see [docs/catalog-verification.md](docs/catalog-verification.md)), nothing is eligible for automatic routing. This is deliberate: models with unknown pricing are excluded from cost optimization.
> - Provider adapters use the official SDKs but have been tested only against **recorded-shape fakes**, not live APIs ([docs/limitations.md](docs/limitations.md)).
> - Savings figures exist only as measured output of `modelspend eval`; mock runs are labelled **SIMULATED** and are not benchmarks.

## Five-minute quickstart (no API keys needed)

```bash
git clone https://github.com/rmencke2/modelspend.git && cd modelspend
python3 -m venv .venv && source .venv/bin/activate   # needs Python 3.10+ (check: python --version)
python -m pip install --upgrade pip                  # old pip (e.g. 21.x) cannot do editable installs from pyproject.toml
pip install -e ".[dev]"

pytest -q                                     # 120+ tests, all mocked
modelspend policy                             # the documented routing policy

# See routing decisions using the fictional SYNTHETIC catalog (prices are made up)
modelspend preview "Classify this ticket: I was charged twice" \
    --providers openai,anthropic --catalog synthetic --assume-keys --budget 0.01

# Compare premium vs cheapest-eligible vs ModelSpend on simulated data
modelspend eval run --mode mock --out /tmp/modelspend-report.md
```

### Using it for real

1. **Add verified catalog entries.** `modelspend catalog template openai/<model-id>` prints a skeleton; fill it from the provider's official models and pricing pages and save it in `~/.modelspend/overrides.json` (`add_models`). `modelspend catalog validate` checks it; `modelspend catalog discover` lists IDs your keys can actually see. Details: [docs/catalog-verification.md](docs/catalog-verification.md).
2. **Set keys and an explicit allowlist** (copy `.env.example`):
   ```bash
   export ANTHROPIC_API_KEY=... OPENAI_API_KEY=...
   export MODELSPEND_PROVIDERS=anthropic,openai      # or pass providers per request
   ```
3. **Preview, then route:**
   ```bash
   modelspend preview "Summarize: ..." --budget 0.02          # no call, no spend
   modelspend route   "Summarize: ..." --budget 0.02          # real call; prints cost + explanation
   ```
4. **Serve it** (localhost only unless `MODELSPEND_API_TOKEN` is set):
   ```bash
   modelspend serve            # http://127.0.0.1:8787  (dashboard at /dashboard)
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
