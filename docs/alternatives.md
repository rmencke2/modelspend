# Alternatives and where ModelSpend fits

Last reviewed: 2026-10-12. This is a good-faith overview from public documentation, not a benchmark. Several details come from
third-party summaries rather than vendor docs and are marked *(secondhand)*. Products change quickly; check the linked sources and
please open an issue or PR if something here is wrong or out of date.

**ModelSpend is not the first model router.** LiteLLM, OpenRouter, AWS, Azure and several research and commercial projects route
requests between models. ModelSpend's niche is narrower: routing with *verifiable cost control* on your own keys, for tasks where
you can define what a good answer looks like.

## The landscape

| Project | Type | What it does | Difference from ModelSpend |
|---|---|---|---|
| [LiteLLM](https://docs.litellm.ai/docs/auto_router/) | Open-source gateway | One OpenAI-compatible endpoint over 100+ providers, with budgets and spend tracking. Its Auto Router maps request complexity tiers to models you choose | Far broader provider coverage. Complexity is a heuristic, not measured pass rates. Budgets are per key or team, not a reserved ceiling across retries and escalations of one request |
| [OpenRouter](https://openrouter.ai/docs/guides/features/routers/auto-router) | Hosted marketplace | `openrouter/auto` selects a model per prompt | Hosted; traffic and billing go through a third party instead of your own keys |
| [RouteLLM](https://lmsys.org/blog/2024-07-01-routellm/) | Open-source research | Routers trained on preference data send easy queries to a cheap model and hard ones to a strong one | Two-model, research-oriented; no budgets, accounting or catalog |
| [Not Diamond](https://docs.notdiamond.ai/docs/key-concepts), Martian | Commercial routers | A learned model recommends a model per prompt | Closed and hosted. Likely better at predicting difficulty out of the box. *(Martian: not verified)* |
| [AWS Bedrock Intelligent Prompt Routing](https://aws.amazon.com/bedrock/intelligent-prompt-routing) | Cloud-native | Routes within a model family on Bedrock | Locked to AWS and its models |
| Azure Foundry Model Router | Cloud-native | Trained router with balanced / cost / quality modes *(secondhand)* | Locked to Azure |
| [NVIDIA LLM Router](https://github.com/NVIDIA-AI-Blueprints/llm-router) | Open-source blueprint | Intent-based routing | Marked deprecated in favour of another NVIDIA project at last review |
| Portkey, Kong, Cloudflare, Vercel, Helicone | Gateways | Routing rules, fallbacks, caching, guardrails, observability | Governance and observability products, not quality-aware routers |

## What ModelSpend does differently

1. **A cost ceiling that covers every attempt.** Each call is reserved at its worst case against a USD and/or token budget before it
   starts; retries, failovers, escalations and failed-but-possibly-billed calls all count. We did not find an equivalent
   per-request guarantee in the projects above, but we have not audited all of them.
2. **Evidence, not a black box.** Suitability comes from measured pass rates (a Wilson lower bound) on your own eval cases. Without
   them it falls back to a labelled editorial tier prior and says which it used.
3. **Cost per successful task.** Cheapest suitable model first, deterministic validators, escalation only on validator evidence. The
   eval harness compares a premium model, the cheapest model and the router on your cases.
4. **Local, token-first.** Your keys, no stored prompts, exact token counts as the headline and cost derived from them (shown as
   unknown when a model has no price).
5. **Guarded price updates.** Propose, verify against the page, human review, then apply.

## Where it is weaker

- Four shipped Anthropic catalog entries; other providers must be added by you. LiteLLM covers 100+ providers.
- Without eval data, routing quality is only as good as the coarse tiers (and they are editorial).
- Only Anthropic has been verified live. OpenAI, Gemini and xAI adapters are being validated ([limitations](limitations.md)).
- No semantic cache, per-team keys or hosted option, no TypeScript SDK, and no published benchmark.
- It will not beat a trained router on open-ended chat. It is aimed at extraction, classification, structured output and code with
  tests, where correctness can be checked.

## Ideas we are considering (not commitments)

- Submit to [RouterArena](https://arxiv.org/html/2510.00202v3), an open comparison of routers on accuracy and cost, for independent numbers.
- A **LiteLLM backend adapter**, so ModelSpend's policy, validation and budgets can sit on top of its provider coverage rather
  than compete with it.

## Could not verify

Star counts and recent activity of the projects above, any vendor-published savings figure for Azure, and details of Martian.
