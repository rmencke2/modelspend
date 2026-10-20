# More providers: DeepSeek and anything OpenAI-compatible (0.8)

ModelSpend ships adapters for OpenAI, Anthropic, Google and xAI. Many other providers speak the **OpenAI chat protocol**, so ModelSpend can use them too:

- **DeepSeek** is built in as a *preset* (it only needs a key).
- **Anything else** (Groq, Together, Mistral, OpenRouter, a local Ollama or vLLM, ...) is described in one block of `settings.json`. Check the provider's own docs for its OpenAI-compatible base URL.

**No models and no prices are shipped for these providers.** Model names and prices change too often to ship, and prices must come from the provider's own page. You pull the model list from the provider's API and set prices from its pricing page, on your machine.

## DeepSeek

```bash
modelspend keys set deepseek
modelspend keys test deepseek
modelspend catalog discover --provider deepseek                      # the model IDs your key can use
modelspend catalog sync --provider deepseek --add-new --only <id> --write
modelspend catalog set deepseek/<id> tier=2 limits.context_tokens=<n> limits.max_output_tokens=<n>
modelspend catalog price deepseek/<id> --input <usd per 1M> --output <usd per 1M> --cached <usd per 1M>
```

An OpenAI-style model list gives **IDs only**, so a new model stays unroutable until you set its context length and output limit (from the provider's docs) and a `tier` (your own prior: 1 small and cheap, 3 strongest). `catalog sync` and the "no eligible model" message print this exact command for you.

Take the prices from DeepSeek's own pricing page (`modelspend sources list` shows where ModelSpend expects it; `modelspend sources check deepseek` says whether the page is reachable and has changed). `modelspend pricing refresh --provider deepseek` can propose prices from that page for you to review, with the usual checks that every number appears on the page. DeepSeek reports cache hits as `prompt_cache_hit_tokens`; ModelSpend counts them as cached input and prices them at the cached rate. Reasoning tokens are counted inside the output, and the model's reasoning text is never returned as the answer.

DeepSeek's hosted API is run by a company in China. Check your own data-handling requirements before sending anything sensitive to it; marking a request `sensitive` keeps it off every provider you have not approved (`MODELSPEND_SENSITIVE_PROVIDERS`).

## OpenRouter (one key, many models)

[OpenRouter](https://openrouter.ai) is a hosted gateway with one key for many models. It is built in as a preset, and ModelSpend can bring in the models you name **with their prices and limits taken from OpenRouter's own model list**, so there is no hand-typing of prices.

```bash
modelspend keys set openrouter
modelspend catalog import-openrouter --only claude-sonnet,gpt-4.1,llama-3.3          # a dry run: what would be added, at what price
modelspend catalog import-openrouter --only claude-sonnet,gpt-4.1,llama-3.3 --write
```

- **You name the models** (`--only`, a substring of the id; `--exact` for whole ids). The list has hundreds of models and importing all of them would bury the ones you use.
- **Prices, context length and output limit** come from OpenRouter (USD per token, converted to per million exactly). **Capabilities** (tool calling, structured output, temperature) are taken from the parameters OpenRouter advertises for the model; streaming and everything else is never assumed.
- **No tier.** A tier is your own prior about a model's strength and OpenRouter does not publish one, so imported models are **not chosen by routing on a prior**. They are still **measured by shadow evaluation** against the model you use (`modelspend shadow on`), and the evidence decides. Or set a tier yourself: `modelspend catalog set openrouter/<id> tier=2`.
- Model ids contain a slash, so references look like `openrouter/openai/gpt-4.1`.
- **Skipped on purpose:** models that are not text in, text out; models without a fixed per-token price (router entries such as `openrouter/auto`); prices over $1,000 per million tokens (a unit mistake); and free-tier `:free` models, which are rate limited and may be logged by the provider (`--include-free` imports them). A model that lists no output limit stays unroutable until you give one (`--default-max-output <n>` or `catalog set`).
- **Keep prices current:** `modelspend catalog import-openrouter --refresh --write` updates the OpenRouter models you already imported, reports price changes, replaces a price you set by hand with OpenRouter's current one, and keeps your tier. `--file saved.json` imports from a saved copy of the list instead of fetching it.
- The response is **untrusted network data**: its size is capped, every number is checked, names are cleaned and odd entries are skipped with a reason.

OpenRouter bills you and may charge fees on top of model prices; check its pricing. ModelSpend prices each call from the tokens OpenRouter reports and the imported price, so compare the totals with your OpenRouter activity page.

## Your own provider

```json
{
  "custom_providers": {
    "mylab": {
      "base_url": "https://api.example.com/v1",
      "key_env": "MYLAB_API_KEY",
      "max_tokens_param": "max_tokens",
      "cached_included": true,
      "pricing_url": "https://example.com/pricing"
    },
    "ollama": { "base_url": "http://localhost:11434/v1", "no_key": true }
  }
}
```

Then the same steps as above (`keys set mylab`, `catalog discover --provider mylab`, `catalog sync --provider mylab --add-new --only ... --write`, `catalog set ... tier=... limits...`, `catalog price ...`).

## Local models with Ollama (no key, no cost)

```bash
ollama pull llama3.2:1b
modelspend catalog discover --provider ollama
modelspend catalog sync --provider ollama --add-new --only llama --write
modelspend catalog set ollama/llama3.2:1b tier=1 limits.context_tokens=4096 limits.max_output_tokens=1024
modelspend catalog price ollama/llama3.2:1b --input 0 --output 0
modelspend route "Classify this ticket: I was charged twice" --providers ollama --task-type classification
```

Use a context length your Ollama setup really serves (its default window is small), and `--input 0 --output 0` since a local model costs nothing per token (the electricity is yours). Use `--providers ollama` on a request, or `modelspend providers enable ollama`, to let it be chosen.

| setting | default | meaning |
|---|---|---|
| `base_url` | required | the OpenAI-compatible endpoint. **`https` only**, except plain `http` for `localhost` (a local server, where no key crosses a network). No credentials, query or fragment in the URL. |
| `key_env` | `<NAME>_API_KEY` | environment variable that holds the key (or put it in `~/.modelspend/keys.env` with `modelspend keys set`) |
| `max_tokens_param` | `max_tokens` | `max_tokens` or `max_completion_tokens`, whichever the provider expects |
| `cached_included` | `true` | does the provider's input-token count already include cached tokens? (used by reconciliation) |
| `no_key` | `false` | the server takes no key (a local model) |
| `pricing_url` | none | the provider's pricing page, for `sources check` and `pricing refresh` |

A mistake here would send your key to the wrong place or silently drop a provider, so **anything unexpected is an error**: unknown settings, a name that clashes with a built-in (`openai`, `claude`, `deepseek`, ...), `http` to a remote host, credentials in the URL. Names are 2 to 32 characters (lowercase letters, digits, `-`, `_`).

## What works the same

Routing, budgets, per-project reports, shadow evaluation, savings and reconciliation treat these providers like the built-in ones: they are listed by `modelspend keys list` and `providers list`, appear in the dashboard, and use the same evidence bars. Structured output and tool calling stay **unverified** until you test them and mark them (`modelspend catalog set <provider>/<model> capabilities.structured_output=true`), so a request that needs them will not be sent to a model you have not confirmed.

## Taking models back out

`modelspend catalog remove 'openrouter/*:batch'` shows what it would remove; add `--yes` to do it. It only removes models you added (never shipped ones; for those use `modelspend models disable <ref>`), keeps the previous file as `overrides.json.bak`, and names any alias that pointed at a removed model. Quote patterns so your shell does not expand the `*`.

## Check a provider yourself: `modelspend verify`

```
modelspend verify                  # every provider that has a key
modelspend verify deepseek --cache # one provider, plus a cached-token check
modelspend verify --report check.md
```

It asks first, shows the most it can cost (a fraction of a cent: tiny prompts, the provider's cheapest priced model in your catalog), then checks what the accounting depends on:

| check | what it proves |
|---|---|
| key | the key lists models |
| errors | an unknown model fails with an error ModelSpend understands (not a raw SDK exception, not silently accepted) |
| generate | a tiny completion reports non-zero token usage and finishes (a provider that reports none would make every cost read $0) |
| stream | the same call streamed also reports usage, and it matches |
| structured | a JSON-schema reply parses (only if your catalog says the model supports it) |
| cache (`--cache`) | the same ~1,500-token prompt twice: uncached + cached tokens add up the same both times, so cached tokens are counted once |

A **FAIL** means a number ModelSpend shows for that provider would be wrong; a **WARN** means it would be incomplete or needs a look; **skip** means the check could not run or was inconclusive (a provider that does not cache automatically skips the cache check). The calls go straight to the provider and are not recorded in your ledger, so a usage export for that day will show a few requests ModelSpend did not record. The report holds model ids, counts and error kinds, never keys or prompts: paste it into an issue.

## Not verified yet

All of this is tested against fakes that mirror the SDK's response shapes (including DeepSeek's usage fields and token-limit parameter). **Checked live (2026-10-19) against a local Ollama server:** the provider is recognised, `catalog discover` listed the model through the OpenAI-compatible endpoint, `catalog sync --add-new` added it and `catalog price` priced it. **Also checked live (2026-10-20, `modelspend verify`):** the Ollama adapter answers a direct call and a streamed call with token usage, and an unknown model fails with a classified error (a call routed through ModelSpend's router is not yet confirmed); OpenAI passes every check including cached-token accounting. **Not yet confirmed live:** any hosted third-party provider including DeepSeek and OpenRouter. The OpenRouter importer was **run against the live model list on 2026-10-20 as a dry run** (no key needed; it wrote nothing): the shape matched, slash ids and limits parsed, and the OpenAI prices matched the ones in the maintainer's own catalog. It exposed `:batch` variants (half price, apparently asynchronous), which are now skipped unless you pass `--include-batch`. A *routed call through OpenRouter* has not been confirmed live. If a provider's "OpenAI-compatible" API differs in a detail (for example a different usage field or an unsupported parameter), `modelspend keys test` and a first routed call will show it; please report what you see.
