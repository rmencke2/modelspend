# Launch post: ModelSpend 0.8.0b1

Numbers below come from the maintainer's own runs (the what-if table over 92 test calls on 20 Oct 2026, `modelspend verify --cache` on OpenAI, an OpenRouter dry run). Re-run `modelspend whatif --since 30d` and check them before posting. Image: `launch-card-1200.png` (source `launch-card.html`).

## LinkedIn

**I priced my own LLM usage on seven models with one command. The same tokens cost between $0.01 and $1.51.**

That's a 108x spread, and it's the easy part. The hard part is knowing which of the cheaper models is good enough for your work. In my table, only one of the seven had cleared an evidence bar. Five had no evidence at all, so they say "unmeasured", not "safe".

That's the idea behind the new version of ModelSpend, an open-source tool (Apache-2.0) that runs locally with your own API keys:

• Paste your provider's usage export and get the table on day one, before it has seen any traffic. Nothing is stored.
• If you opt in, it also tries a small sample of your real requests on cheaper models in the background. A model is only recommended when the evidence clears a statistical bar.
• A new `verify` command checks a provider on your own key: does it report token usage, are cached tokens counted once? A wrong cost number is worse than none. On OpenAI, 183 uncached + 1,152 cached tokens added up to the same 1,335 as the uncached first call.

Honest limits. My table is 92 calls of my own test traffic, mostly classification. It proves the mechanism, not a saving. "Passed" means as good as the answer you used, not correct. Price says nothing about quality. I've used it live with OpenAI and Anthropic and checked a local Ollama model and OpenRouter's model list; DeepSeek, Gemini and xAI are not tested live yet.

Skepticism about software that picks your model is fair, which is why it shows its evidence and says "unmeasured" when it has none.

It's a beta, built for one developer or a small team.

What's the most expensive model you're using for something a cheaper one could probably do?

#OpenSource #LLM #AI #DevTools

*First comment: github.com/rmencke2/modelspend. Install: `pip install git+https://github.com/rmencke2/modelspend.git`. Swap in `pip install modelspend` once PyPI is live.*

## Show HN / Reddit (shorter, more technical)

> **Show HN: ModelSpend 0.8 – price your LLM usage on every model from your provider's usage export**
>
> Open source (Apache-2.0), runs locally, your own keys (OpenAI, Anthropic, Google, xAI, DeepSeek, any OpenAI-compatible endpoint). New in 0.8: `modelspend whatif --from-export usage.csv --export-provider openai` prices the tokens in your provider's own export on every model in your catalog, before ModelSpend has seen any traffic (nothing is stored). Each row carries the evidence we have for it from opt-in shadow evaluation (a sample of real requests tried on cheaper models and compared with the answer you used; a model counts only when the 95% Wilson lower bound of its pass rate clears a bar), and says "unmeasured" when there is none. On my own 92 test calls the same tokens priced 108x apart across seven models; one cleared a bar.
>
> Also: `modelspend verify` runs a few tiny requests against a provider on your key and checks what your cost numbers depend on (usage reported, streaming usage, a classified error for an unknown model, cached tokens counted once); `modelspend caching` finds long prompt starts that repeat and whether the provider is caching them (it stores keyed hashes and token counts, never text, and you can turn it off); `modelspend share` writes a static report with no scripts or network access.
>
> Found live: OpenRouter's model list has a `:batch` twin of every model at exactly half the price. They look asynchronous, so the importer skips them unless asked.
>
> Honest limits: "passed" means "as good as the answer you used", not "correct"; my test traffic is small and mostly classification; used live with OpenAI and Anthropic, plus a local Ollama model and OpenRouter's model list; DeepSeek/Gemini/xAI not yet. Beta, for solo devs and small teams. Feedback on the method very welcome.
>
> github.com/rmencke2/modelspend

## Notes on choices

- **The number is a price spread, not a saving.** 108x is the most expensive model's cost for these exact tokens divided by the cheapest's. The post and card say price says nothing about quality, and the card's other figure ($0.0156 against $0.0201 actually spent) is the cost if only the task with evidence moved, which is a small, honest saving on small traffic.
- **"Seven models" is the seven you call directly** (gpt-4.1-nano, gpt-4.1, gpt-5-mini, claude-haiku-5-5, claude-sonnet-5-5, claude-opus-5-5, claude-fable-5-1). The full table also has a free local Ollama model (no per-token price, so it would make any ratio meaningless) and OpenRouter copies of models already listed at identical prices; both are left off the card on purpose.
- **The unverified list is in the post on purpose.** It names what was and wasn't checked live, so nobody finds the gap for you.
- **The link goes in the first comment**, because LinkedIn tends to show posts with outbound links to fewer people.
- **If you have a one-sentence reason you built this, add it after the second paragraph.** I haven't invented one.
