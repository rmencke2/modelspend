# ModelSpend 0.8.0b1 (beta)

**See what your LLM usage would have cost on other models, in the first minute, from the export your provider already gave you. Then check that your provider's numbers can be trusted.**

ModelSpend is a small open-source tool (Apache-2.0) that sits between your code and the LLM providers you already use (OpenAI, Anthropic, Google, xAI, DeepSeek and anything OpenAI-compatible, with **your own keys**). It shows where your tokens and money go per project and agent, enforces budgets *before* the spend, and can route each task to the lowest-cost model expected to do the job. It runs locally, stores no prompts, and is built for **one developer or a small team (1 to 25 people)**.

## Try it in two minutes

```bash
git pull && pip install -e .          # or: pip install -e . in a fresh clone
# Save your provider's usage export (OpenAI: the export on its Usage page) as usage.csv, then:
modelspend whatif --from-export usage.csv --export-provider openai
```

No traffic through ModelSpend needed, nothing stored. On a fresh install it prices your tokens on the four shipped Anthropic models and tells you which commands add prices for the rest (`modelspend keys set openai`, `modelspend catalog sync`, `modelspend catalog price ...`).

## What's new

**What-if price table** (`modelspend whatif`, `GET /v1/whatif`, and a table on the dashboard's Savings tab)
- Replays the tokens in your own usage ledger against every priced model in your catalog and shows what the same traffic **would have cost**, cheapest first, next to what you actually spent. It uses the same arithmetic that priced the real calls, so it cannot disagree with your ledger about a model you really used.
- Beside each price: the shadow evidence (passed/scored) and the task types where the model clears the same evidence bar routing uses. **A model with no evidence says `unmeasured`, never "safe".** A `total if moved only there` column prices your traffic with only those task types moved, so the headline is never "everything on the cheapest model".
- Models with no known price are listed apart, never guessed. A model priced at $0 (local or free) is labelled as having no per-token price.
- **`--from-export`** does the same from a provider's own usage export (CSV, JSON or JSON lines; the files `reconcile import` reads). Totals per model per day, so long-context price tiers cannot be applied (it says so); cached tokens are split out once for providers that count them inside input; spend with no cost column is called an **estimate from catalog prices, not an invoice**.

**A report you can hand to someone** (`modelspend share`, a download button on the Savings tab)
- One static HTML file (or `--format md` to paste into an issue): spend, projected saving, the evidence per task type, spend by model and the what-if table, with the caveats printed on the page.
- **Safe to share by construction:** numbers, model names and task types only. No prompts, responses, keys or file paths. Project names only with `--include-projects`. The HTML has no scripts and no network access (a Content-Security-Policy forbids both), every value is escaped, and it follows light and dark mode and works at phone width.

**Check a provider on your own key** (`modelspend verify`)
- A few tiny requests per provider (it asks first and prints the most it can cost: a fraction of a cent) check what your cost numbers depend on: the key works, an unknown model fails with an error ModelSpend understands, a completion **reports token usage**, a stream reports matching usage, structured output parses where the catalog claims it, and with `--cache` that **cached tokens are counted once**.
- A FAIL means a number ModelSpend shows for that provider would be wrong. `--report` writes a Markdown report (ids, counts and error kinds only) you can paste into an issue. This exists because fakes cannot find the failures that matter most.

**Prompt-caching advice** (`modelspend caching`, a table on the Savings tab)
- Shows which long prompt starts (system prompt, few-shot examples, a document, plus tools and schema) repeat in your traffic, how many **cached tokens the provider actually reported** for them, how many it missed, what caching has saved you and what is still on the table. When a repeated prefix is not being cached it names the usual cause (a timestamp or id near the top of the prompt).
- Anthropic needs explicit cache markers, which ModelSpend does not send yet, so those rows are information only. Providers other than OpenAI, DeepSeek and Anthropic are reported as "unknown" rather than assumed; `modelspend verify <provider> --cache` tests them.
- **This stores something new, so read this:** per request, for each prompt start of at least ~1,000 tokens, a **keyed hash** (HMAC with a random key that lives only in your own database) and a **token count**. Never the text. Nothing is tokenized for short prompts, and a tracking failure cannot affect a request. Turn it off with `"track_prefixes": false` in `settings.json`; `modelspend caching --forget` erases it.

**More providers**
- **DeepSeek** is a built-in preset, and **any OpenAI-compatible endpoint** (Groq, Together, Mistral, a local Ollama or vLLM, ...) can be described in `settings.json` under `custom_providers`, validated strictly (https only except localhost, no credentials in the URL, no clash with a built-in name).
- **`modelspend catalog import-openrouter --only <ids>`** brings OpenRouter models into your catalog with USD prices, context and output limits taken from OpenRouter's own model list. Dry run by default; the response is treated as untrusted; models arrive with **no tier**, so routing will not pick them on a prior, but shadow evaluation can measure them. `:batch` variants (half price, apparently asynchronous) are skipped unless you pass `--include-batch`.
- **`modelspend catalog remove 'openrouter/*:batch' --yes`** takes back models you added (dry run first; shipped models are never touched; the catalog is re-validated and the old file kept as `overrides.json.bak`).

**Smaller**
- "No eligible model" now says what to run for the usual causes. The README opens with install-and-try. CI builds the wheel, checks it and runs the installed commands on every push. A PyPI publishing workflow exists but **is not set up yet**: install from GitHub for now.

## Verified live (2026-10-19 to 2026-10-20)

- **OpenAI `gpt-4.1-nano`, `modelspend verify --cache`: every check passed, twice.** Including cached tokens: the same ~1,300-token prompt sent twice reported 183 uncached + 1,152 cached = 1,335, the same total as the first call, so cached tokens are counted once.
- **`whatif --from-export` on a real OpenAI usage export** (202 requests, 3 models, 4 days): the dated model names matched the catalog, and the model prices, recomputed by hand from the token counts, matched.
- **`whatif` and `share` on a real ledger** (92 calls). The shared file was scanned for keys, key-shaped strings and local paths: only the repository link matched.
- **Local Ollama (`llama3.2:1b`):** the adapter answers a direct and a streamed call with token usage, and an unknown model fails with a classified error.
- **OpenRouter importer against the live model list:** the response parsed as documented, slash ids came through intact, and the three OpenAI models' prices matched the maintainer's own catalog. It also showed the `:batch` twins above, which the fakes could not.

## Not yet verified

- **The caching advisor has not been run on real traffic.** The arithmetic is tested on constructed timelines and the provider behaviours it assumes (OpenAI and DeepSeek cache automatically, Anthropic needs markers) come from their documentation. Only OpenAI's cached-token reporting has been confirmed live.
- **DeepSeek, Gemini and xAI have not been run against the live APIs** (`modelspend verify` exists to do exactly that; reports are welcome). **A call routed through OpenRouter** and a **routed call to Ollama** are also unconfirmed; so is whether OpenRouter's per-token prices include every fee you will pay.
- **Savings are projections, not measurements**, and "passed" means *as good as the answer you used*, not *correct*. A cheaper model that matches your current one on classification says nothing about other task types.
- **A cost comparison against a provider's invoice** (token counts were reconciled against OpenAI's export in earlier releases); the Docker sandbox; automatic price refresh against real pages; concurrent budgets across processes; Windows.
- **One intermittent test failure** was seen once in about twelve full test runs and has not been identified. Eleven consecutive runs passed afterwards. If CI shows an intermittent failure, the test name would help.
- Everything listed as not yet verified in [0.7.0b1](RELEASE_NOTES_0.7.0b1.md) that is not mentioned above still applies.

## Upgrade

```bash
git pull && pip install -e .
```

Prompt-prefix tracking is on by default (hashes and counts only; see above). To turn it off before your next request: add `"track_prefixes": false` to `~/.modelspend/settings.json`.

**Full changelog:** https://github.com/rmencke2/modelspend/compare/v0.7.0b1...v0.8.0b1
