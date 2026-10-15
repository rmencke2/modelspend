# Evaluation

`modelspend eval run` compares, on **identical tasks** and graded by the **same deterministic task validators**:

1. **premium** – one fixed, user-configured model (`--premium provider/model`), no validation, no escalation.
2. **cheapest_eligible** – the lowest-cost model passing hard constraints, ignoring suitability, no validation, no escalation.
3. **modelspend** – the real router: suitability, task validators, one bounded escalation.

Reported: pass rate (Wilson 95% interval), total spend, cost per success, latency p50/p95, escalation rate, cost-quality trade-off vs premium, and per-category pass counts. JSON next to Markdown with `--out`.

## Fixtures

> Using your own cases? See [own-evals.md](own-evals.md): only `id`, `category`, `messages` and `validators` are required, and `modelspend eval check` lints them. The built-in fixtures below keep the extra `difficulty`, `reference` and `wrong` fields that the simulated (mock) models use.

`src/modelspend/eval/fixtures/{calibration,heldout}/<category>.jsonl`, all eight categories in both splits (two tasks each, so 16 per split — a smoke-test size, not a benchmark). `calibrate` reads **only** calibration; `run` defaults to **held-out**; a test enforces that ids and prompts are disjoint. Add your own with `--fixtures DIR` (same layout). Each task: `messages`, `validators`, `difficulty` (1–3, used only by the simulator), `reference`/`wrong` (simulator only), optional `sandbox_validators` (Docker, `--sandbox`).

## Mock vs live

- **Mock (default, CI)**: simulated models answer correctly iff `tier ≥ difficulty`. Output is stamped **SIMULATED DATA**. It tests the harness, routing and escalation mechanics; the numbers are not evidence of real quality or savings and must not be quoted.
- **Live (`--mode live --budget USD --premium p/m`)**: real calls with your keys. A hard USD ceiling is enforced across all arms: the run stops before any task whose worst case could exceed what remains, and every call is budget-gated. Requires verified catalog entries. Live paths have not been exercised in this repository's CI.

## Calibration

`modelspend eval calibrate --mode live --budget 2 --write` runs every eligible model on the calibration tasks and merges pass counts into `~/.modelspend/evidence.json`. Routing uses evidence only when `n` reaches the profile minimum (10/20/40), so build larger calibration sets for real use. Do not calibrate and evaluate on the same tasks.

## Caveats

Deterministic graders check format and key facts, not full correctness. Small n gives wide intervals. ModelSpend receives validators and baselines do not — that is the product's intended use, but it means pass-rate gains partly reflect validator feedback.
