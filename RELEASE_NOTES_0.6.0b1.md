# ModelSpend 0.6.0b1 (beta)

ModelSpend shows **where your LLM tokens and money go, per project and per agent**, enforces **budgets before the spend**, and can **route each task to the lowest-cost model expected to suit it**. It uses your own API keys for OpenAI, Anthropic, Google and xAI, stores no prompts, runs locally, and is Apache-2.0.

This release is about **checking ModelSpend's numbers against the provider's**. The first real check found a gap in ModelSpend's own accounting, which is fixed here. It is still a **beta**; what was and was not tested live is listed below.

## What's new

**Reconcile with the provider's usage**
- Download a usage export from the provider (CSV, JSON or JSON lines), then:
  ```bash
  modelspend reconcile import ~/Downloads/completions_usage.csv --provider openai
  modelspend reconcile run              # per model, over the days the export covers
  modelspend reconcile run --by-day     # one row per model per day (UTC)
  modelspend reconcile run --check      # exit status 1 if anything needs a look (cron/CI)
  ```
  Nothing is fetched and no extra key is needed. Only numbers are read from the file; no text from it is stored.
- **ModelSpend recording *more* than the provider billed is always flagged** (token estimates, a different key or project than the one exported, or provider data that has not caught up). **The provider reporting more is normal**, because the provider counts everything on the key; it is only flagged with `--exclusive`.
- Differences are judged within a tolerance (default 2%, with a small floor so rounding on tiny amounts is ignored). Cached tokens follow each provider's convention and can be overridden. Model names with a date suffix merge (`gpt-4.1-nano-2025-04-14` is `gpt-4.1-nano`). Days the provider reports empty count as covered, so a ModelSpend call on such a day is flagged.
- **A match does not prove either side right: the provider's invoice is authoritative.** Guide and limits: [docs/reconcile.md](docs/reconcile.md).

## Fixed

- **Live evals were missing from the usage ledger.** The first reconciliation against OpenAI's export showed about 35,000 tokens (about 190 requests) on one day that were in OpenAI's numbers but not in `modelspend report`. They were `eval run` and `eval calibrate` calls: those commands ran without a usage store, so their real spend was invisible to reports, the Costs tab and budgets. Their calls are now recorded under `project=modelspend-eval` (`agent=calibrate` or `eval-run`), so they show up everywhere and can be isolated or excluded with `--project`. Stickiness is switched off for eval requests, so recording cannot change which model a case is routed to. **Evals run before this release were not recorded and cannot be recovered**, so a reconciliation over those days will show the provider above ModelSpend.
- **First use from two processes on one database could crash after the provider call.** Two processes both seeing "no salt yet" made the second fail with `UNIQUE constraint failed: meta.k`. It now keeps whichever salt won. This surfaced as a flaky concurrent-budget test (about 1 run in 6).
- **Reconcile output:** a ModelSpend row with zero tokens and nothing from the provider (a call that failed before billing) is no longer flagged, and the result line explains why provider-higher rows are not flagged.

## Verified live

- **Token counts match OpenAI's exactly** on the days checked: on 2026-10-12 and 2026-10-14, ModelSpend's totals per model equalled OpenAI's usage export for `gpt-4.1-nano` (111 and 181 tokens) and `gpt-5-mini` (201 tokens), with no difference.
- The **OpenAI completions usage CSV export** imports correctly, including its blank rows for quiet days and its float-formatted numbers.
- The reconciliation is also what found the eval-ledger gap above. **After the fix, a live calibration (`eval calibrate --mode live`, OpenAI, 3 models, 16 cases each) was recorded in full:** 48 requests under `modelspend-eval`, and the ledger's total ($0.020541) equalled the calibration's own printed spend exactly.
- **From earlier releases:** routing, streaming, structured output (strict and non-strict) and failover and escalation on OpenAI; Anthropic end to end; per-project and per-agent reports, budgets, the Costs tab, aliases, conversation stickiness and your own evidence bars.

## Not yet verified

- **Cost reconciliation.** OpenAI's usage export has no cost column, so only tokens were compared. Cost handling is tested but has not been checked against a real costs export.
- **Whether OpenAI's `input_tokens` already includes cached tokens** is assumed (the default for OpenAI). The sample export had no cached tokens.
- **Anthropic, Google and xAI export formats.** Their column names and JSON shapes are based on documentation, not real files. `--map` covers most naming differences.
- **The eval-ledger fix against the provider's numbers:** a live calibration after the fix was recorded correctly (see above), but a provider export covering that day has not been reconciled yet.
- Fetching usage straight from the providers' admin usage APIs is **not built**; it would need an admin-level key.
- Gemini and xAI adapters (tested against simulated responses only), concurrent budget enforcement across processes and the dashboard's budget form, the Docker code sandbox, and automatic price refresh against the real pages.
- macOS and Linux with Python 3.10+ only; Windows is untested.

## Upgrade

```bash
git pull && pip install -e .
```

**Full changelog:** https://github.com/rmencke2/modelspend/compare/v0.5.0b2...v0.6.0b1
