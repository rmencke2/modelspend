# ModelSpend 0.6.0b2 (beta)

A fix release on top of 0.6.0b1, found by running live calibrations against OpenAI and reading the failures. **If you run `eval calibrate` or `eval run` with a reasoning model, upgrade: the earlier results can understate it.**

## Fixed

- **Reasoning models were scored as wrong when they ran out of output tokens.** Evals cap each answer at 400 tokens. On harder cases `gpt-5-mini` spent almost all of that thinking (4,032 of its 4,156 output tokens were reasoning) and returned **no visible answer**, so its 0/2 on analysis, rewriting and summarization measured the cap, not its quality. Saved as evidence, that would have unfairly kept it out of routing. Now `eval calibrate` and the two baseline arms of `eval run` **retry a cut-off call once with 1,024 more output tokens** and judge the retry. Both calls are recorded in the usage ledger, the retry is skipped if it would break `--budget`, and a call that is still cut off counts as a failure labelled `truncated at the output limit`. `eval calibrate` prints how many calls were retried; `eval run` reports `output_limit_retries`. The ModelSpend arm of `eval run` is unchanged: it is the product's real behaviour, which escalates on a cut-off answer. See [docs/own-evals.md](docs/own-evals.md).
- **A ledger reused for two calls double counted the first.** `run_single` recorded the whole ledger on every call, so the retry above would have re-recorded the first call's attempts. Each call now records only its own attempts. (Found by the new tests, not in live use.)
- **An ambiguous built-in calibration case.** `cal-extraction-2` asked for "every person named... in order of appearance" but expected only the three distinct people, so `gpt-4.1` and `gpt-5-mini` answered `[Maria Chen, Dr. Okafor, Luis Prado, Maria, Luis]`, a fair reading, and failed. It now asks for each distinct person once, using the fullest name given. **The built-in dataset is renamed `modelspend-calibration-v2`** so evidence measured on the old wording is not mixed with the new; re-run calibration if you stored evidence from the built-in set.

## Verified live (OpenAI, `gpt-4.1`, `gpt-4.1-nano`, `gpt-5-mini`, 2026-10-16)

- The retry works: across two runs, 12 and then 10 calls stopped at the output limit and were retried; 11 and 9 of them recovered and were scored on their answers. `gpt-5-mini` went from 0/2 to 2/2 on rewriting and summarization and from 0/2 or 1/2 to 2/2 on code.
- What is still cut off is reported honestly: `gpt-5-mini` on one open-ended analysis case stays truncated even with 1,424 tokens, on both runs. That is worth knowing before routing analysis to it.
- After the case fix, all three models pass the extraction case (2/2).
- A 48-call calibration costs about $0.02 without retries and about $0.04 with them, inside a `--budget 0.10`; the retries are visible as extra requests under `project=modelspend-eval`.

## Not yet verified, and known limits

- **Routed (non-eval) calls are not changed.** If you set a small `max_output_tokens` and the router picks a reasoning model, the answer can come back empty. With validators it shows as a failed `not_truncated` check and may escalate (costing more); without validators the status is `truncated`. Give reasoning models room, or pin a non-reasoning model for short answers. Sizing the cap automatically changes budget arithmetic and is not done. See [docs/limitations.md](docs/limitations.md).
- **The post-fix day has not been reconciled against OpenAI's own usage export yet.** The ledger total matched the calibration's printed spend exactly; comparing it with OpenAI's numbers is the remaining step.
- **Sixteen cases cannot rank models.** The built-in set is a smoke test (almost everything passes); use your own cases, or the harder example set in `examples/evals/support-tickets-hard`, to compare.
- Everything listed as not yet verified in [0.6.0b1](RELEASE_NOTES_0.6.0b1.md) still applies: cost reconciliation against a real costs export, whether OpenAI's input tokens include cached tokens, Anthropic/Google/xAI export formats, Gemini and xAI adapters, concurrent budget enforcement, the Docker sandbox, automatic price refresh, Windows.

## Upgrade

```bash
git pull && pip install -e .
```

**Full changelog:** https://github.com/rmencke2/modelspend/compare/v0.6.0b1...v0.6.0b2
