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
- `modelspend eval check --fixtures my-cases.jsonl` costs nothing and catches bad JSON (with file and line), unknown fields or categories, duplicate ids, unknown or misconfigured validators, validators that reject a case's own `reference` answer or accept its `wrong` one, sets that share ids, and whether you have enough cases to ever clear your bar. `modelspend eval template <category>` prints a starter case, and `eval calibrate --dataset NAME` names the evidence you store.

**See who clears your bar**
- `modelspend evidence` (and `GET /v1/evidence`) lists each model and category with passes out of total, the observed rate, the lower bound and a verdict (clears, below the bar, needs more cases, bar unreachable at this n), and names the cheapest model that clears each category. It uses the router's own functions, so the table and the routing decision cannot disagree. `GET /v1/policy` now includes your thresholds.

Guide, validators and limits: [docs/own-evals.md](docs/own-evals.md).

## Verified live
- **A small calibration run (OpenAI and Anthropic):** 7 models on 12 simple classification cases cost about $0.02. The check, calibration, evidence table and a route that selected a model on its measured verdict (with your bar named in the explanation) all worked.
- **From earlier releases:** per-project and per-agent reports, hard and soft budgets, the Costs tab, aliases and conversation stickiness, failover on a bad key, and cross-vendor escalation.

## Not yet verified
- **Telling models apart.** In the live run every model passed every case, so it did not show cheaper models being separated from stronger ones. Use cases where a cheaper model can plausibly fail (ambiguous inputs, misleading wording, edge cases).
- **OpenAI strict structured output** (a `response_schema` call).
- **Gemini and xAI** adapters (unit-tested against simulated responses only).
- **Concurrent budget enforcement** across processes and the dashboard's budget form (tested with threads and generated data).
- The **Docker code sandbox** and **automatic price refresh** against the real provider pages.
- Totals have not been compared with the providers' own usage or billing pages.
- macOS and Linux with Python 3.10+ only; Windows is untested.

## Know before you use it
- **Your validators are your definition of "good".** ModelSpend measures only what they check, and small numbers of cases give wide intervals. A pass means your checks passed, not that the answer is right.
- Evidence is per model and per **task category**. It applies when a request is classified into that category (by the rules, or via `task_type`). Evidence ages: models change under the same name, and ModelSpend does not re-measure on its own. Writing evidence **replaces** the earlier result for that model and category.
- `eval calibrate` runs every eligible model on every case, so the cost is roughly cases x models x a small call. `--budget` is a hard USD ceiling.
- Your thresholds apply under every profile. A lax bar (for example `--basis observed` on a dozen cases) makes ModelSpend trust thin evidence, which is your call; `modelspend thresholds list` shows the bar that applies to each category, and where it came from.
- Only calls made through ModelSpend are counted. Your provider's invoice is always the authority on cost. Only Anthropic prices ship; add other providers' models and prices with `catalog sync` / `catalog price` or the Pricing tab. See [docs/limitations.md](docs/limitations.md) and [docs/alternatives.md](docs/alternatives.md).

## Upgrade
```
cd modelspend && git pull origin main && pip install -e . && modelspend restart
```
Nothing to migrate. Thresholds live in `~/.modelspend/settings.json` and evidence in `~/.modelspend/evidence.json`; a running server reads them at start-up, so restart it after changing thresholds.
