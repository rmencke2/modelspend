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

## Not verified yet

All of this is tested against fakes that mirror the SDK's response shapes (including DeepSeek's usage fields and token-limit parameter). **Checked live (2026-10-19) against a local Ollama server:** the provider is recognised, `catalog discover` listed the model through the OpenAI-compatible endpoint, `catalog sync --add-new` added it and `catalog price` priced it. **Not yet confirmed live:** a routed call that returns an answer, and any hosted provider including DeepSeek. If a provider's "OpenAI-compatible" API differs in a detail (for example a different usage field or an unsupported parameter), `modelspend keys test` and a first routed call will show it; please report what you see.
