# Shadow evaluation and savings (0.7)

ModelSpend can find out, **from your own requests**, which cheaper model is safe to use for each kind of work, and show what that would save. You do not write eval cases.

```bash
modelspend shadow on                     # opt in (off by default); restart a running server: modelspend restart
modelspend shadow set --rate 0.05 --budget 5   # 5% of requests, at most $5 per month
modelspend shadow status                 # what has been measured so far
modelspend savings                       # what you could save, and how sure ModelSpend is
modelspend shadow apply                  # shows what would change; add --yes to let routing use the results
modelspend shadow revert                 # undo
```

The dashboard's **Savings** tab shows the same, with a switch for shadow evaluation.

## What it does

When shadow evaluation is on, a sample of the requests you already send is **also tried, in the background, on cheaper models** (the ones the request is allowed to go to: its allow-list, providers and sensitivity still apply). Each cheaper model's answer is compared with the answer you actually used:

1. **Your validators**, if the request has them (deterministic, preferred).
2. **An exact match** after normalizing whitespace, code fences and case (good for labels).
3. Otherwise a **judge model** decides whether the candidate serves the request at least as well as the answer you used (costs money; `--no-judge` turns it off, and differing answers are then left unscored).

Your own request is never delayed: the comparison runs after your answer has been returned, and a failure inside it can never reach you.

## What is stored

Per comparison: the two models, the task type, a **verdict** (pass / fail / unscored), a **fixed reason code**, costs and token counts. **Never the prompt, either answer, or anything the judge said**: the prompt is held in memory for the length of the background call only. (A test scans every table in the database for a planted prompt and answer.)

## What it costs, and how it is capped

Shadow calls are real calls on your keys. They are capped by `monthly_budget_usd` (default $5 per UTC month): before every call ModelSpend checks the call's **worst case** (answer plus judge) against what was already spent this month and what is in flight, and skips it if it would cross the ceiling. Every shadow call is recorded in the usage ledger under `project=modelspend-shadow` (so `modelspend report` and reconciliation include it) and is excluded from "your traffic" in the savings report.

The ceiling is checked per process: if several ModelSpend processes share one database, they can together exceed it by the calls in flight. Requests marked `sensitive` are never shadowed unless you set `include_sensitive`. Streamed requests are not shadowed. At most 4 comparisons run at once; further samples are dropped, not queued. One-shot commands (`modelspend route`) and the eval commands never shadow.

## What "passed" means (and does not)

*Passed* means **the cheaper model gave an answer as good as the one you used**, not that the answer is correct. With validators the check is against them. Without them, a candidate can agree with a wrong answer, and a judge model has its own biases. Shadow evidence therefore measures how safely a cheaper model can **stand in for the model you use today**, which is the question a routing decision asks, and it is labelled as shadow evidence everywhere it is shown. If correctness matters, give your requests validators, or measure with your own cases ([own-evals](own-evals.md)); your own eval evidence always wins over shadow evidence.

A candidate that is **cut off at the output cap** you set counts as a failure (that is what routing it there would give you). This matters for **reasoning models**: they spend output tokens thinking, so with a tight `max_output_tokens` (say 64) they can return nothing at all. The first live run showed exactly that: `gpt-5-mini` passed 1 of 27 with a 64-token cap. The savings report and `modelspend shadow status` say how many failures were cut-offs and why comparisons ended as they did (fixed reason codes only), so a model is not blamed for a cap you set. Give the requests you shadow room (a larger `max_output_tokens`) before judging a reasoning model.

## The savings report

`modelspend savings` (and `GET /v1/savings`) shows, per task type: your spend in the window, the cheapest model that **clears the bar** on shadow evidence, how many comparisons it passed, the 95% lower bound, its measured cost relative to the model you use, and the projected saving. A model is only called *ready* when the **same bar routing uses** (your thresholds, else the profile's) is met by the 95% **Wilson lower bound** of its pass rate: a few lucky samples never qualify. Everything else shows what is missing ("needs 6 more", "below the bar").

The saving is a **projection**: your recorded spend in that task type (excluding spend already on the candidate) scaled by the cost ratio measured on the sampled requests (reported tokens, catalog prices). It assumes the sampled requests are typical of what would move. Prices and traffic change.

## Letting routing use it

`modelspend shadow apply` first shows which task types would change, then (with `--yes`) lets routing treat shadow results as evidence **only where you have no eval evidence of your own** for that model and task type. It is reversible (`shadow revert`) and the results are kept either way.

**No chaining.** If routing chose a model because of shadow evidence, answers from that model are not used as the reference for further comparisons; otherwise "A is close to B", "B is close to C" could chain and quality could erode with nothing anchoring it. The consequence: once a task type is served by a shadow-chosen model, its evidence stops growing. Re-measure with your own cases, or pin the reference model for a while, to refresh it.

## Settings (`settings.json`, key `shadow`)

| key | default | meaning |
|---|---|---|
| `enabled` | `false` | sample requests at all (also `MODELSPEND_SHADOW=true`) |
| `sample_rate` | `0.05` | fraction of eligible requests |
| `monthly_budget_usd` | `5.0` | hard ceiling per UTC month |
| `max_candidates` | `2` | cheaper models tried per sampled request (least-tested first) |
| `judge` | `true` | use a judge model when answers differ and there are no validators |
| `include_sensitive` | `false` | also shadow requests marked sensitive |
| `target_n` | `100` | stop sampling a model/task pair at this many scored comparisons |
| `use_as_evidence` | `false` | routing uses shadow results (set by `shadow apply`) |

A typo or out-of-range value is an error, not ignored.

## Try it on a few cents

`python examples/shadow_live_check.py` (after `modelspend shadow set --rate 1 --budget 0.50` and `modelspend shadow on`) sends 30 ticket classifications to your premium model, lets ModelSpend try the cheaper ones in the background, and prints what was measured and the savings projection.

## First live run (2026-10-17, OpenAI, 30 ticket classifications pinned to `gpt-4.1`)

`gpt-4.1-nano` passed 29 of 29 scored comparisons (1 unscored; lower bound 0.88, 95% cheaper on those requests), so the report marked classification ready. `gpt-5-mini` passed 1 of 27: the cap above. The whole run, shadow calls and judge included, cost $0.0038 against a $0.50 cap. Thirty requests of one easy task type say little about real traffic; treat it as proof that the mechanism works end to end, not as a measured saving.

## Not verified yet

Covered by tests against simulated providers, and run once live (above). Still open: how often real judge models agree with exact-match and validator verdicts, how the savings projection compares with what happens after switching, and behaviour on varied real traffic (see [limitations](limitations.md)).
