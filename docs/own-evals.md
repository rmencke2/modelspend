# Your own evals and evidence bars (0.5)

*Tested with unit tests, and run live once at small scale: 7 models on 12 simple classification cases cost about $0.02, the evidence table and a route using the measured verdict worked. Every model passed every case, so that run did not show models being told apart: use cases where a cheaper model can plausibly fail.*

Public benchmarks rarely match your prompts: a cheaper model often handles *your* task as well as an expensive one, and sometimes
worse. ModelSpend lets you settle that with your own cases, and set **how much proof you require** before it picks a model.

```
write cases  ->  eval check  ->  eval calibrate (live)  ->  evidence  ->  thresholds set  ->  route
```

## 1. Write cases

One JSON object per line, in a `.jsonl` file:

```bash
modelspend eval template extraction > my-cases.jsonl      # a starting point; edit it, add one case per line
```

| Field | Required | Meaning |
|---|---|---|
| `id` | yes | unique name |
| `category` | yes | `classification`, `extraction`, `rewriting`, `summarization`, `translation`, `qa_context`, `code` or `analysis` |
| `messages` | yes | the prompt, as chat messages (needs a user message) |
| `validators` | yes | the checks that decide **pass or fail** (below). A case with none could never fail, so it is rejected |
| `reference` | no | an answer that should pass; `eval check` runs your validators on it |
| `wrong` | no | an answer that should fail; `eval check` warns if your validators accept it (too lax) |

**Validators** are deterministic checks you declare, for example
`{"type": "allowed_labels", "labels": ["billing", "bug", "other"]}`, `{"type": "json_schema", "schema": {...}}`,
`{"type": "json_equals", "expected": {...}}`, `{"type": "contains", "substrings": ["INV-20418"]}`, `{"type": "regex", "pattern": "^\\d{4}$"}`,
`exact`, `valid_json`, `required_fields`, `not_contains`, `max_chars`, `min_chars`, `python_syntax` (and `python_tests` with the Docker sandbox).
**Your validators are your definition of "good".** ModelSpend measures only what they check.

**Make the cases discriminating.** If every model passes every case (as in an easy set of obvious tickets), the evidence cannot tell you which cheaper model is good enough. Include the cases you actually worry about: ambiguous inputs, misleading wording, edge cases, things that need a rule you define.

Keep two sets if you can, `calibration/` (used to measure models) and `heldout/` (used to check the result), so you are not grading on
cases you tuned against: `<folder>/calibration/*.jsonl` and `<folder>/heldout/*.jsonl`. A single file or a plain folder of `.jsonl` files also works.

A worked, deliberately harder example (misleading keywords, negation, hidden instructions, other languages) lives in
[`examples/evals/support-tickets-hard/`](../examples/evals/support-tickets-hard/README.md): copy it and change the tickets to yours.

## 2. Check them (free, no model is called)

```bash
modelspend eval check --fixtures my-cases.jsonl
```

It reports bad JSON (with file and line), unknown fields or categories, duplicate ids, validators that are unknown or misconfigured, cases whose
`reference` your own validators reject, cases whose `wrong` answer they accept, sets that share ids, and **whether you have enough cases per
category to ever clear your bar** (see below). Exit code 1 means fix something first.

## 3. Measure the models (spends money)

```bash
modelspend eval calibrate --mode live --budget 1 --fixtures my-cases.jsonl --dataset my-tickets --write
```

Every model that passes the hard constraints (key, price, availability, limits) is run on every case, so the cost is roughly
*cases x models x a small call*; `--budget` is a hard USD ceiling and the run stops before a call that could exceed it. Pass counts are stored in
`~/.modelspend/evidence.json` under the dataset name you give (it appears in route explanations). Writing **replaces** the earlier result for
that model and category; it does not add to it, so measure a category with all its cases in one run.

## 4. See who clears your bar

```bash
modelspend evidence                       # all models and categories against the balanced profile's bars (plus your own)
modelspend evidence --category extraction --profile high_assurance
```

For each model and category: passed / total, the observed rate, the 95% Wilson lower bound, and a verdict: *clears the bar*, *below the bar*,
*needs more cases*, or *bar unreachable at this n*. It also names the cheapest model (by blended list price) that clears each category. The router
uses exactly the same functions, so this table and the routing decision cannot disagree. (`GET /v1/evidence` returns the same data.)

## 5. Set your own bar

Built-in bars: economy n >= 10 and lower bound >= 0.65; balanced n >= 20 and >= 0.80; high assurance n >= 40 and >= 0.90 with measured evidence
required. Override them per category (or `*` for all):

```bash
modelspend thresholds set extraction --min-n 80 --min-pass-rate 0.95
modelspend thresholds set classification --min-pass-rate 0.9 --basis observed
modelspend thresholds set code --require-evidence            # never pick a model for code on its tier prior alone
modelspend thresholds list --profile balanced                # the bar that applies to each category, and where it came from
modelspend restart                                           # a running server reads settings at start-up
```

- Unset fields fall back to `*`, then to the profile's built-in bar. Settings apply under every profile.
- **`basis`**: `lower_bound` (default) means the 95% Wilson lower bound must clear the rate. That is conservative on purpose: with few cases even a
  perfect record proves little. A 95% bar needs **at least 73 cases** (a 90% bar needs 35) even if every one passes. If your evidence has fewer, the
  verdict is *bar unreachable at this n* and ModelSpend says how many cases it needs. `observed` judges by the plain pass rate (passes / n).
- **`require_evidence`** removes the fallback to the editorial tier prior for that category: a model without enough measured proof is simply not chosen.
  A setting can only tighten this: the `high_assurance` profile always requires evidence.
- Invalid settings (for example `min_pass_rate: 95` instead of `0.95`) stop ModelSpend with an error rather than being ignored.

## Limits

- Measured quality is **only as good as your validators and cases**, and small n gives wide intervals. A pass means "your checks passed", not "the answer is right".
- Evidence is per model and per **task category**. It applies when a request is classified into that category (by the rules, or by passing `task_type`);
  requests that cannot be classified fall back to the conservative default.
- Evidence ages: models change under the same name, and ModelSpend does not re-measure on its own (`measured_on` is stored). Re-run `calibrate` when it matters.
- Calibration runs each model without ModelSpend's retries or escalation, one call per case, so the numbers describe the models themselves.
