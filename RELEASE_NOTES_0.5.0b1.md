# ModelSpend 0.5.0b1 (beta)

ModelSpend shows **where your LLM tokens and money go, per project and per agent**, enforces **budgets before the spend**, and can **route each task to the lowest-cost model expected to suit it**. It uses your own API keys for OpenAI, Anthropic, Google and xAI, stores no prompts, runs locally, and is Apache-2.0.

This release is about **proof**: you can now measure models on your own cases and say how much evidence you require before ModelSpend picks one. It is still a **beta**; what was and was not tested live is listed below.

## What's new

**Your own evidence bars**
- `modelspend thresholds set extraction --min-n 80 --min-pass-rate 0.95` (also `list` and `remove`). Per task category, or `*` for all, you set how many eval cases a model needs, the pass rate it must show, and whether the bar is judged on the conservative **95% Wilson lower bound** (default) or the **observed** pass rate (`--basis observed`). `--require-evidence` means a model is never chosen for that category on its tier prior alone.
- Bars resolve field by field: the category's own setting, then `*`, then the profile's built-in bar. A setting can only **tighten** `high_assurance`'s evidence requirement, never loosen it. Settings that cannot be valid (for example `min_pass_rate: 95` instead of `0.95`) stop with an error rather than being ignored.
- A bar the evidence cannot clear is reported as such, with the number of cases needed, instead of silently failing: a 95% lower-bound bar needs at least 73 cases even if every one passes.

**Your own eval cases**
- Only `id`, `category`, `messages` and `validators` are required. Cases can come from one `.jsonl` file, a folder of them, or `<dir>/calibration` plus `<dir>/heldout`.
- `eval calibrate --show-failures` lists which cases each model failed and its shortened answer (printed to the screen only, never stored).
- `modelspend eval check --fixtures my-cases.jsonl` costs nothing and catches bad JSON (with file and line), unknown fields or categories, duplicate ids, unknown or misconfigured validators, validators that reject a case's own `reference` answer or accept its `wrong` one, sets that share ids, and whether you have enough cases to ever clear your bar. `modelspend eval template <category>` prints a starter case, and `eval calibrate --dataset NAME` names the evidence you store.

**See who clears your bar**
- `modelspend evidence` (and `GET /v1/evidence`) lists each model and category with passes out of total, the observed rate, the lower bound and a verdict (clears, below the bar, needs more cases, bar unreachable at this n), and names the cheapest model that clears each category. It uses the router's own functions, so the table and the routing decision cannot disagree. `GET /v1/policy` now includes your thresholds.

Guide, validators and limits: [docs/own-evals.md](docs/own-evals.md).

## Verified live
- **Calibration on a harder case set (OpenAI and Anthropic):** the 22 calibration cases of `examples/evals/support-tickets-hard` on 7 models cost about $0.13. Six models scored 22/22 and cleared the default bar; `gpt-4.1-nano` scored 19/22 (lower bound 0.67), fell below it, and is no longer chosen for classification. The check, evidence table and a route that selected a model on its measured verdict all worked.
- **Held-out comparison:** on 8 cases the calibration never saw, ModelSpend matched the premium model's pass rate (8/8) at about 77% lower spend, while the cheapest model missed one (7/8). Eight cases give wide intervals (68-100%), ModelSpend receives your validators and may escalate while the baselines do not, and it did not escalate here.
- **OpenAI structured output** (`response_schema`, strict and non-strict) on `gpt-4.1-nano`: valid JSON, validation passed, no escalation. The live test also found the repeated-sync-call `Event loop is closed` bug, fixed after the 0.5.0b1 tag (see CHANGELOG), so use the next release or `main` if you call the sync SDK more than once.
- **From earlier releases:** per-project and per-agent reports, hard and soft budgets, the Costs tab, aliases and conversation stickiness, failover on a bad key, and cross-vendor escalation.

## Not yet verified
- **Telling models apart at scale.** Only one of seven models fell short, on one task type with 22 cases. An easy 12-case set separated nothing. Use cases where a cheaper model can plausibly fail (ambiguous inputs, misleading wording, edge cases).
- **Gemini and xAI** adapters (unit-tested against simulated responses only).
- **Concurrent budget enforcement** across processes and the dashboard's budget form (tested with threads and generated data).
- The **Docker code sandbox** and **automatic price refresh** against the real provider pages.
- Totals have not been compared with the providers' own usage or billing pages.
- macOS and Linux with Python 3.10+ only; Windows is untested.

## Know before you use it
- **Your validators are your definition of "good".** ModelSpend measures only what they check, and small numbers of cases give wide intervals. A pass means your checks passed, not that the answer is right.
- **One calibration run is one sample.** Models are not perfectly repeatable (in a live check the same model scored 19/22, then 20/22 on a re-run); a model close to a bar can flip between runs. Each case counts once.
- Evidence is per model and per **task category**. It applies when a request is classified into that category (by the rules, or via `task_type`). Evidence ages: models change under the same name, and ModelSpend does not re-measure on its own. Writing evidence **replaces** the earlier result for that model and category.
- `eval calibrate` runs every eligible model on every case, so the cost is roughly cases x models x a small call. `--budget` is a hard USD ceiling.
- Your thresholds apply under every profile. A lax bar (for example `--basis observed` on a dozen cases) makes ModelSpend trust thin evidence, which is your call; `modelspend thresholds list` shows the bar that applies to each category, and where it came from.
- Only calls made through ModelSpend are counted. Your provider's invoice is always the authority on cost. Only Anthropic prices ship; add other providers' models and prices with `catalog sync` / `catalog price` or the Pricing tab. See [docs/limitations.md](docs/limitations.md) and [docs/alternatives.md](docs/alternatives.md).

## Upgrade
```
cd modelspend && git pull origin main && pip install -e . && modelspend restart
```
Nothing to migrate. Thresholds live in `~/.modelspend/settings.json` and evidence in `~/.modelspend/evidence.json`; a running server reads them at start-up, so restart it after changing thresholds.
