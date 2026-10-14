# ModelSpend

Open-source cost control for LLM usage, using **your own API keys** for OpenAI, Anthropic (Claude), Google (Gemini) and xAI (Grok). Two halves that work together:

1. **Know where the money goes.** Tag calls with a project and an agent, and see **tokens and cost per project, per agent, per model** (including what retries and escalations cost). Works even if you do not want routing: pin a model and ModelSpend only measures. See [docs/cost-attribution.md](docs/cost-attribution.md).
2. **Spend less.** Route each task to **the lowest-cost model expected to suit it**, inside a token and dollar budget that covers every retry and escalation.

Python SDK, CLI, a local FastAPI server with an OpenAI-compatible `/v1/chat/completions` (`model="auto"` to route, or a pinned model to just measure), route preview, a usage dashboard and an evaluation harness.

> **Honest status (v0.1, MVP).**
> - ModelSpend does **not** guarantee the cheapest successful model. "Suitable" is an *estimate* from catalog tier priors and, as you run evaluations, measured evidence.
> - **Only Anthropic pricing ships, and models are not routable until you run `modelspend catalog sync`.** The four Anthropic entries carry prices verified on 2026-10-12 against Anthropic's official pricing pages; their model IDs, availability and limits are confirmed from *your* key's live Models API by `catalog sync`. OpenAI, Google and xAI have no shipped entries (add them yourself; see below): add them with `catalog template` / `catalog set` ([docs/catalog-verification.md](docs/catalog-verification.md)).
> - **Tokens first.** Token usage (input, cached, output, reasoning) is exact, provider-reported and summed over every attempt; it is the headline number in the CLI, API and dashboard. Cost is derived from it and shown as `unknown` for models without a price (`--allow-unpriced`). Budgets can be set in tokens (`--budget-tokens`) as well as dollars.
> - **Verified live (2026-10-12): Anthropic** — model sync, routing, one-step escalation, token budgets, streaming and the dashboard, with token counts and cost checked against the provider's reported usage. **Verified live (2026-10-12): OpenAI, partially** — chat and streaming calls on `gpt-4.1-nano` and `gpt-5-mini`: routing, catalog sync, token counts (the pre-call input estimate matched the provider's count exactly, and reasoning tokens arrive inside the output count), provider-reported usage in the stream, and cost computed from the catalog prices. **Not yet exercised live for OpenAI:** strict structured output, failover on a bad key, cross-vendor escalation, and a comparison against the provider's billing page. The **Gemini and xAI adapters are implemented with the official SDKs but tested only against recorded-shape fakes**, not yet live ([docs/limitations.md](docs/limitations.md)). Reports from people running them are welcome.
> - Savings figures exist only as measured output of `modelspend eval`; mock runs are labelled **SIMULATED** and are not benchmarks.
> - **New in 0.2.0b1: per-project / per-agent reporting.** Tested with mocked providers; its numbers have not yet been compared against the providers' own usage pages, and only calls made through ModelSpend are counted ([docs/cost-attribution.md](docs/cost-attribution.md)).

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

Run the tests (all provider calls are mocked; no keys or network needed) and print the documented routing policy:

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

### Setup: keys, providers, models, pricing links

Everything below is a command; nothing needs hand-edited files.

```bash
modelspend keys set anthropic
modelspend keys set openai
modelspend keys test
modelspend providers enable anthropic openai
modelspend status
```

- **API keys.** `keys set <provider>` asks for the key with hidden input and saves it to `~/.modelspend/keys.env` (plain text, `chmod 600`, never stored in SQLite or logs). A real environment variable (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `XAI_API_KEY`) always overrides the saved key, so CI and Docker keep working. `keys list` shows what is set and where it comes from (never the key); `keys test` checks each key with a read-only model-list call; `keys remove <provider>` deletes one.
- **Choosing vendors.** `providers enable/disable` sets the default allowlist used when a request doesn't name providers; a request (`providers=[...]`, `--providers`, the `modelspend` extension) always overrides it. Models from different vendors compete only when you enable more than one.
- **Choosing models.** `models list` shows every model, whether it is routable and, if not, why (no key, unverified, no pricing, ...). `models disable anthropic/claude-opus-5-5` keeps it out of routing; `models enable` brings it back. The quality profile (`economy`, `balanced`, `high_assurance`) then decides how strong a model each task needs.
- **Pricing links.** Each provider's official pricing page URL lives in one place (`sources.json`), not in the code. Change it with `modelspend sources set <provider> <url>` (tracking parameters such as `utm_source` are stripped), `MODELSPEND_PRICING_URL_<PROVIDER>`, or by editing `~/.modelspend/sources.json`; `sources reset <provider>` restores the shipped link. Catalog prices without their own `source_url` cite the configured page. `modelspend sources check` fetches each page and tells you if it **changed** since your last accepted baseline (it never edits prices: re-verify, update with `catalog set`, then `sources check --accept`).
- **One-screen overview:** `modelspend status`.

### Running the server

```bash
modelspend serve --background
modelspend status
modelspend restart
modelspend stop
```

`serve` runs in the foreground (Ctrl-C stops it). `serve --background` (or `-d`) runs it detached, logging to `~/.modelspend/server.log`; `restart` is the thing to run after `git pull`; `stop` shuts it down gracefully. Only one server runs at a time. `stop` finds the server through a PID file and **only signals a process that actually answers like a ModelSpend server**, so a reused PID is never killed; add `--force` to override. A server started by an older version (no PID file) is found by its port (needs `lsof` on macOS/Linux).

### Keeping prices current (CLI, cron, dashboard)

```bash
modelspend sources list
modelspend pricing refresh
modelspend pricing proposals
modelspend pricing show PROPOSAL-ID
modelspend pricing apply PROPOSAL-ID
```

`pricing refresh` fetches each provider's official pricing page and, **only if it changed since you last accepted it**, uses a cheap model (your own key; a few thousand tokens) to extract prices. Every extracted number must appear on the page next to its model, and the result is a **proposal you review and apply**, never a silent change. Exit code `10` means a review is waiting, so it is safe in cron: `30 7 * * 1 /path/to/.venv/bin/modelspend pricing refresh`. For pages that can't be fetched, `modelspend pricing paste <provider>` takes text you copied. The dashboard's **Pricing** tab does all of this with buttons, including editing each provider's URL. Details and limits: [docs/pricing-refresh.md](docs/pricing-refresh.md).

### Adding another vendor (OpenAI, Google, xAI)

Prices must come from the provider's **official pricing page** (see `modelspend sources list` for the link). For each model you want:

```bash
modelspend keys set openai
modelspend keys test openai
modelspend catalog discover
modelspend catalog sync --provider openai --add-new --only gpt-5 --assume streaming,tools
modelspend catalog sync --provider openai --add-new --only gpt-5 --assume streaming,tools --write
modelspend catalog price openai/MODEL-ID --input 1.25 --output 10 --cached 0.125
modelspend catalog set openai/MODEL-ID tier=2 limits.context_tokens=400000 limits.max_output_tokens=128000
modelspend models list --provider openai
```

(The numbers above are placeholders, not real prices.) Notes:

- `--only` is required with `--add-new` so you don't import every model a key can list (embeddings, audio, image, legacy). Pick the IDs from `catalog discover`. It matches substrings; add `--exact` to match whole IDs (so `gpt-5-mini` does not also pull in `gpt-5-mini-pro`), and dated snapshot IDs are skipped unless you pass `--snapshots`.
- Gemini's model list also returns token limits, so `sync` fills them in. OpenAI's and xAI's lists give IDs only, so set `limits.*` from the provider's model page.
- `tier` is your honest editorial judgement (1 small/fast, 2 mid, 3 frontier); it is a prior, not a measurement.
- If the provider reports reasoning tokens separately from output tokens, add `--reasoning-separate` to `catalog price`.
- `models list` says exactly what is still missing for each model.

### Using it for real (Anthropic example)

1. **Save your key** (consider a low spend limit in the provider console), then confirm it works:
   ```bash
   modelspend keys set anthropic
   modelspend keys test anthropic
   ```
2. **Confirm models and limits from your key's live Models API** (dry run first, then write). Prices are never touched; the shipped Anthropic prices come from the official pricing pages. `--assume` records capabilities *you* assert after reading the docs:
   ```bash
   modelspend catalog sync --provider anthropic
   modelspend catalog sync --provider anthropic --assume streaming,tools --write
   modelspend models list
   ```
3. **Preview (no call, no spend), then route.** Output leads with token usage:
   ```bash
   modelspend preview "Classify this ticket as billing, bug or other. Label only. Ticket: I was charged twice." --providers anthropic --max-output-tokens 16
   modelspend route "Classify this ticket as billing, bug or other. Label only. Ticket: I was charged twice." --providers anthropic --max-output-tokens 16 --budget-tokens 2000
   modelspend usage
   ```
4. **Other providers:** `modelspend catalog template openai/<model-id>` prints a skeleton (pre-filled with that provider's pricing URL) to fill from the official docs; `modelspend catalog set <ref> tier=2 capabilities.streaming=true pricing.input_per_mtok=...` edits one model safely (reverted if invalid). Without a price a model only runs with `--allow-unpriced`, and its cost is reported as unknown while tokens stay exact.
5. **Serve it** (localhost only unless `MODELSPEND_API_TOKEN` is set). It listens on http://127.0.0.1:8787 with the dashboard at `/dashboard`:
   ```bash
   modelspend serve
   ```

## Cost per project and agent

```bash
modelspend route "Classify: I was charged twice" --providers anthropic --project billing --agent invoice-bot
modelspend report --by project,agent,model --since 7d
```

Tag calls from the SDK (`ModelSpend(project=..., agent=...)`) or any OpenAI client (`X-ModelSpend-Project` / `X-ModelSpend-Agent` headers). To measure an existing agent **without routing it**, pin a model (`--model provider/id`, or `model="provider/id"` on the server). The report shows tokens by type, cost, and the overhead spent on attempts that did not produce the final answer; CSV and JSON output are available. Full guide, definitions and limits: [docs/cost-attribution.md](docs/cost-attribution.md).

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

[Architecture](docs/architecture.md) · [Routing policy](docs/routing-policy.md) · [Validation & escalation](docs/validation-and-escalation.md) · [Catalog verification](docs/catalog-verification.md) · [Cost per project and agent](docs/cost-attribution.md) · [Roadmap](docs/roadmap-cost-attribution.md) · [Keeping prices current](docs/pricing-refresh.md) · [OpenAI compatibility](docs/openai-compat.md) · [Evaluation](docs/evaluation.md) · [Limitations & roadmap](docs/limitations.md) · [Alternatives](docs/alternatives.md) · [Contributing](CONTRIBUTING.md)

## License

Apache-2.0. See [LICENSE](LICENSE).
