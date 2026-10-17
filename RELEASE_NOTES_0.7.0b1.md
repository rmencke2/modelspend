# ModelSpend 0.7.0b1 (beta)

**Find out which cheaper model is safe to use for your work, from your own requests, and see what it would save. No eval cases to write.**

ModelSpend is a small open-source tool (Apache-2.0) that sits between your code and the LLM providers you already use (OpenAI, Anthropic, Google, xAI, with **your own keys**). It shows where your tokens and money go per project and agent, enforces budgets *before* the spend, and can route each task to the lowest-cost model expected to do the job. It runs locally, stores no prompts, and is built for **one developer or a small team (1 to 25 people)**.

## Try it in two minutes

```bash
git pull && pip install -e .          # or: pip install -e . in a fresh clone
modelspend shadow set --rate 0.05 --budget 5    # sample 5% of requests, never spend more than $5 a month
modelspend shadow on
# ... use your tools as normal through ModelSpend (SDK, CLI, or the local OpenAI-compatible server) ...
modelspend savings
```

Want to see it work right now for a few cents? `python examples/shadow_live_check.py` sends 30 ticket classifications to your premium model, lets ModelSpend try the cheaper ones in the background, and prints what it measured.

## What's new

**Shadow evaluation (off by default; you opt in)**
- A small sample of the requests you already send is **also tried, in the background, on cheaper models**, and each answer is compared with the one you actually used: **your validators** if the request has them, else an **exact match** (good for labels), else a **judge model**.
- **Private by design:** only a verdict, a fixed reason code, costs and token counts are stored. **Never the prompt, either answer, or anything the judge said.** A test scans every table in the database for a planted prompt and answer.
- **Spend is capped:** a monthly ceiling (default $5), checked against each call's worst case before it is made, and every shadow call is in your usage ledger under `project=modelspend-shadow`. Requests marked sensitive are never shadowed unless you allow it. Your own request is never delayed, and a failure inside shadowing cannot reach you.

**Savings projection** (`modelspend savings`, `GET /v1/savings`, and a **Savings** tab in the dashboard)
- Per task type: what you spend, the cheapest model that **clears the evidence bar**, how many comparisons it passed, its 95% lower bound, its measured cost relative to the model you use, and the projected saving.
- A model is only "ready" when the **same bar routing uses** (your own thresholds, else the profile's) is met by the **95% Wilson lower bound**. A handful of lucky samples never qualifies; everything else says what is missing ("needs 6 more", "below the bar").
- It is a **projection, not a measurement**, and "passed" means *as good as the answer you used*, not *correct*. The report says so.

**Let routing use it, safely**
- `modelspend shadow apply` shows which task types would change, then (with `--yes`) lets routing treat shadow results as evidence **only where you have no eval evidence of your own**. `modelspend shadow revert` undoes it.
- **No chaining:** answers from a model that qualified by shadow evidence are never used as the reference for further comparisons, so "A is close to B, B is close to C" cannot quietly erode quality.

**See why, not just what**
- `modelspend shadow status` lists, per model, why each comparison ended as it did (same answer, passed your validators, judge said no, the call failed and with what kind of error, ...). Reason codes only, never text.
- A candidate **cut off at your `max_output_tokens`** is not counted for or against it and is shown separately. Reasoning models spend output tokens thinking, and found live: `gpt-5-mini` returned empty answers at a 64-token cap and passed 28 of 28 comparisons at 512. After changing prompts or limits, `modelspend shadow reset` forgets stale comparisons (your spend records are kept, so the monthly cap still holds).

**Using it with a few people**
- Run one server for the team (`modelspend serve --host 0.0.0.0` with `MODELSPEND_API_TOKEN` set; a non-loopback bind is refused without a token). Everyone points an OpenAI-compatible client at it and tags requests with `X-ModelSpend-Project` / `X-ModelSpend-Agent`. Spend, budgets and shadow evidence are then shared, and evidence builds faster because it pools everyone's requests.
- Today that is **one shared token** with no per-person identity or roles: anyone with the token can change settings. Fine for a few people who trust each other; not a multi-tenant service.

Guide, privacy, cost and limits: [docs/shadow.md](docs/shadow.md).

## Fixed

- `modelspend prune` kept shadow comparisons when there were no old requests; it now deletes them.

## Verified live (OpenAI, 2026-10-17)

- Two runs of 30 ticket classifications pinned to `gpt-4.1`, with `gpt-4.1-nano` and `gpt-5-mini` as the cheaper candidates, for **$0.0073 of shadow spend in total** against a $0.50 cap.
- `gpt-4.1-nano`: **57 of 57 scored comparisons passed** (3 unscored: the judge's reply could not be read, which is now parsed more tolerantly), 95% lower bound 0.94, about 95% cheaper on those requests, so the report marked classification ready.
- `gpt-5-mini`: cut off at a 64-token cap, then 28 of 28 at 512 (above).
- The dashboard's Savings tab was checked in a browser in light, dark and phone width; the settings form saves and the server rejects invalid values.

## Not yet verified

- **Thirty easy requests of one task type prove the mechanism, not a saving.** Real, varied traffic is the real test: how often real judge models agree with validator and exact-match verdicts, and whether the projected saving matches what happens after you switch.
- **Agreement with your current model is not correctness.** Without validators a candidate can agree with a wrong answer, and a judge model has its own biases. Use validators where correctness matters; your own eval evidence always wins over shadow evidence.
- The shared-server setup for a few people is tested but has not been used by a real team yet.
- The monthly cap is checked per process (one server process is fine). Streamed requests are not shadowed.
- Everything listed as not yet verified in [0.6.0b1](RELEASE_NOTES_0.6.0b1.md) and [0.6.0b2](RELEASE_NOTES_0.6.0b2.md) still applies (Gemini and xAI live, cost reconciliation against a real costs export, the Docker sandbox, automatic price refresh, Windows).

## Upgrade

```bash
git pull && pip install -e .
```

Shadow evaluation does nothing until you run `modelspend shadow on`.

**Full changelog:** https://github.com/rmencke2/modelspend/compare/v0.6.0b2...v0.7.0b1
