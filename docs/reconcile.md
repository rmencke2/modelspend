# Reconciling with the provider's usage (0.6)

ModelSpend's totals come from its own records. This feature checks them against **what the provider says it billed**, so you can see where the two disagree. You download a usage export from the provider, import it, and compare.

```bash
modelspend reconcile import openai-usage.csv --provider openai
modelspend reconcile run                 # per model, over the days the export covers
modelspend reconcile run --by-day        # one row per model per day (UTC)
modelspend reconcile run --check         # exit status 1 if anything needs a look (cron/CI)
modelspend reconcile list                # what has been imported
modelspend reconcile remove openai-usage.csv
```

Nothing is fetched from the provider and no extra key is needed: you export the file yourself. Only numbers are read from it (day, model, token counts, requests, cost); no text from the file is stored.

## What it can and cannot tell you

- ModelSpend counts only calls made **through ModelSpend**. A provider's numbers cover everything on that key, project or organization. So **"provider higher" is normal**, and is not flagged unless you say the exported key is used only by ModelSpend (`--exclusive`).
- **"ModelSpend higher" is always flagged.** ModelSpend recorded more than the provider billed. Likely causes: token estimates (a call that failed before usage was reported), exporting a different key/project/organization than the one ModelSpend used, or provider data that has not caught up yet.
- **"ModelSpend only"** (the export has no row for a model ModelSpend used in the covered days) is flagged for the same reasons.
- Only the days the export covers are compared, per provider. Days are **UTC**; calls near midnight can fall on different days in the two systems, so compare ranges before single days.
- A match means the two agree within tolerance (default 2%, with a floor of 100 tokens / $0.0005 so rounding on small amounts is not flagged). It does not prove either is right: **the provider's invoice is authoritative.**
- Cost is compared when the export has a cost column and every ModelSpend call in that row had a price. Otherwise only tokens are compared and the row says why.
- Model names are matched after removing a trailing date, so `gpt-4.1-nano-2025-04-14` and `gpt-4.1-nano` are one row.

## Eval runs are in the ledger too (from 0.6)

`modelspend eval run --mode live` and `eval calibrate --mode live` spend real money. From 0.6 their calls are recorded like any other, under `project=modelspend-eval` (`agent=calibrate` or `eval-run`, task `eval`). They appear in `modelspend report`, the Costs tab and this comparison. To look at them alone or leave them out: `modelspend report --project modelspend-eval`. Evals run before 0.6 were not recorded; that is why a first reconciliation can show the provider far above ModelSpend on the days you ran them. (This was found by the first real reconciliation: about 35,000 tokens on one day were missing.)

## Request counts

The table's `calls prov/MS` column compares the provider's request count with the number of calls ModelSpend recorded that reached the provider: answered calls (retries, failovers, escalations, classifier and judge calls included) and failed calls that may have been billed (timeouts, server errors). Calls that failed before reaching the provider's meter (a bad key, a refused budget) are not counted: the provider never saw them. Counts are whole numbers, so they show *how many* calls are missing even when token differences are ambiguous: if the provider is 16 calls above ModelSpend and you know you ran 16 calls outside ModelSpend, the gap is explained. 

## Cached tokens

Providers disagree on whether the input-token column already contains cached tokens. The defaults: OpenAI, Google and xAI **include** them in input; Anthropic **excludes** them (cache read and cache write are separate columns). If your export differs, override per provider:

```bash
modelspend reconcile run --cached-separate openai
modelspend reconcile run --cached-included anthropic
```

A wrong assumption shows up as a systematic difference on cached traffic.

## File formats

CSV, JSON (a list of rows, or `{"data": [...]}`, including time buckets with a `results` list) and JSON lines. Columns are recognized by common names, case-insensitively:

| field | recognized names |
|---|---|
| day | day, date, usage_date, start_date, start_time, starting_at, bucket_start, timestamp (ISO date/time, `MM/DD/YYYY`, or epoch seconds/milliseconds) |
| model | model, model_id, model_name, snapshot_id |
| input tokens | input_tokens, uncached_input_tokens, prompt_tokens |
| output tokens | output_tokens, completion_tokens, generated_tokens |
| cached read / write | cache_read_tokens, input_cached_tokens, cached_tokens / cache_write_tokens, cache_creation_input_tokens |
| requests | requests, num_model_requests |
| cost (USD) | cost_usd, cost, amount_usd, amount, spend |

If your export uses other names, map them: `--map day=Date --map model=Snapshot --map cost=Amount`. A missing required column, a non-numeric or negative number, or an unreadable date stops the import with the row number; nothing is partially imported. Limits: 50 MB, 500,000 rows.

Importing the same `--source` name again **replaces** that import. Two *different* imports that cover the same days are added together by `reconcile run`, which double counts; the import warns when that happens, and you can compare one at a time with `--source` or delete one with `reconcile remove`.

## Quiet days

Providers write an empty row for a day with no usage (OpenAI's export does: only the time columns are filled in). ModelSpend keeps that as "the provider reports nothing this day", so the day counts as covered and any ModelSpend call on it is flagged. The last day in an export is often today, and the provider's numbers for today lag, so a flag on the final day can simply mean the data has not arrived yet.

## Verified against a real export

The OpenAI **completions usage CSV export** (Usage page; columns such as `start_time`, `num_model_requests`, `model`, `input_tokens`, `input_cached_tokens`, `output_tokens`) imported correctly on 2026-10-16, including its blank quiet-day rows and float-formatted numbers. That export has **no cost column**, so only tokens are compared. The cost handling is tested but not checked against a real provider cost export.

## Not verified yet

- Whether OpenAI's `input_tokens` already includes `input_cached_tokens` is assumed (the default for OpenAI) and was not exercised: the sample export had no cached tokens.
- The other column names and JSON shapes above are based on the providers' documentation and common export layouts. They have **not been checked against real exports** (Anthropic, Google, xAI, any cost export); if a real file does not import, `--map` covers most differences and a report of the file's header row helps us add it.
- Fetching usage directly from the providers' admin usage APIs is not built. It would need an admin-level key, which is a larger permission than the inference keys ModelSpend holds today, so it is a separate, opt-in step.
