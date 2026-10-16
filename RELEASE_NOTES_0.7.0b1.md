# ModelSpend 0.7.0b1 (beta)

ModelSpend shows **where your LLM tokens and money go, per project and per agent**, enforces **budgets before the spend**, and can **route each task to the lowest-cost model expected to suit it**. It uses your own API keys for OpenAI, Anthropic, Google and xAI, stores no prompts, runs locally, and is Apache-2.0.

This release is about **finding the savings without writing a single eval case**: ModelSpend learns from your own traffic which cheaper model is safe to use for each kind of work, shows what that would save, and only then lets routing use it. It is a **beta**; the feature has had one small live run (see "Not yet verified").

## What's new

**Shadow evaluation (off by default; you opt in)**
- A small sample of the requests you already send is **also tried, in the background, on cheaper models** (only models the request is allowed to go to), and each answer is compared with the one you actually used: **your validators** if the request has them, else an **exact match** (good for labels), else a **judge model** (`--no-judge` to leave differing answers unscored).
- **Privacy:** only a verdict, a fixed reason code, costs and token counts are stored. **Never the prompt, either answer, or anything the judge said.** The prompt lives in memory for the length of the background call. A test scans every table in the database for a planted prompt and answer.
- **Cost is capped:** a monthly ceiling (default $5, checked against each call's *worst case* before it is made), recorded in your usage ledger under `project=modelspend-shadow`. Requests marked sensitive are never shadowed unless you allow it. Your own request is never delayed, and a failure inside shadowing cannot reach you.
- `modelspend shadow on | off | set --rate 0.05 --budget 5 | status`. The dashboard has a switch.

**Savings projection**
- `modelspend savings` (also `GET /v1/savings` and a dashboard **Savings** tab): per task type, your spend, the cheapest model that **clears the evidence bar**, how many comparisons it passed, its 95% lower bound, its measured cost relative to the model you use, and the **projected saving**.
- A model is only "ready" when the **same bar routing uses** (your thresholds, else the profile's) is met by the **95% Wilson lower bound**: a few lucky samples never qualify. Everything else says what is missing ("needs 6 more", "below the bar").
- It is a **projection, not a measurement**, and "passed" means *as good as the answer you used*, not *correct*. The report says so.

**Let routing use it, safely**
- `modelspend shadow apply` shows which task types would change, then (with `--yes`) lets routing treat shadow results as evidence **only where you have no eval evidence of your own**. `modelspend shadow revert` undoes it.
- **No chaining:** answers from a model that qualified by shadow evidence are never used as the reference for further comparisons, so "A is close to B, B is close to C" cannot erode quality with nothing anchoring it. (Consequence: that task type's evidence stops growing until you re-measure.)

Guide, privacy, cost and limits: [docs/shadow.md](docs/shadow.md). Try it for a few cents: `python examples/shadow_live_check.py`.

**Why a comparison ended as it did** is shown: `modelspend shadow status` lists the reason codes per model (including the kind of any provider failure), and a candidate **cut off at your `max_output_tokens`** (reasoning models need room to think) is reported separately and not counted for or against it.

## Fixed

- **`modelspend prune` kept shadow comparisons when there were no old requests**, and now deletes them.

## Not yet verified

- **Run live once, on a small sample:** 30 ticket classifications through OpenAI. `gpt-4.1-nano` passed 29 of 29 scored comparisons (lower bound 0.88, 95% cheaper on those requests) for $0.0038 of shadow spend; `gpt-5-mini` returned empty answers at a 64-token output cap (its reasoning used it all) and passed 28 of 28 at 512; cut-offs are now shown separately and **not counted for or against a model**, and `modelspend shadow reset` clears stale comparisons. Not yet checked: how often real judge models agree with validator and exact-match verdicts, whether the projected saving matches what happens after switching, varied real traffic, and the dashboard tab with live data (it was checked in a browser against synthetic data, in light, dark and phone width).
- **Agreement with your current model is not correctness.** Without validators a candidate can agree with a wrong answer, and a judge model has its own biases. Use validators where correctness matters; your own eval evidence always wins over shadow evidence.
- The monthly cap is checked per process: several processes sharing one database can exceed it together by the calls in flight. Streamed requests are not shadowed.
- Everything listed as not yet verified in [0.6.0b1](RELEASE_NOTES_0.6.0b1.md) and [0.6.0b2](RELEASE_NOTES_0.6.0b2.md) still applies (Gemini and xAI live, cost reconciliation against a real costs export, the Docker sandbox, automatic price refresh, Windows).

## Upgrade

```bash
git pull && pip install -e .
```

Shadow evaluation does nothing until you run `modelspend shadow on`.

**Full changelog:** https://github.com/rmencke2/modelspend/compare/v0.6.0b2...v0.7.0b1
