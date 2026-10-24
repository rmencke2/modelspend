# Spend digest

`modelspend digest` prints a short summary of the last day (or week) against the one before it: what it cost, the projects whose spend changed most, the models behind it, any budget near its limit, and anything that looks off. It reads the same ledger as `modelspend report`, so the numbers agree with it: only calls made through ModelSpend, costed at the prices in effect when each call ran, and **your provider's invoice is authoritative**.

```
modelspend digest                    # the last 24 hours against the 24 before
modelspend digest --period week      # the last 7 days against the 7 before
modelspend digest --format json
```

## What is in it

- **Spend and requests**, with the change against the previous period. If the previous period had no spend there is no percentage, and it says so rather than printing infinity. ModelSpend's own shadow and eval runs are shown as a separate line and are never listed as one of your projects.
- **Biggest changes by project** (up to three, by the size of the change, new projects marked) and **by model** with each model's share.
- **Budgets near or over their limit.** Healthy budgets are not listed.
- **Worth a look**, only when true:
  - spend at least twice the previous period and at least $0.50 more (a tripling from ten cents to thirty is not an alarm);
  - spend after a silent period (at least $0.50);
  - calls on a model with no price, which makes the total partial (and how to fix it);
  - more than 20% of at least ten requests without an answer;
  - more than 30% of tokens spent on retries, failovers, classifier or judge calls rather than final answers.

The thresholds are fixed numbers in `src/modelspend/digest.py`, chosen to be quiet rather than to be right for every team.

## Sending it somewhere (optional, off until you set it up)

`--post` sends the digest to **one webhook you configured**: Slack, Discord or a generic JSON endpoint. It is the only thing in ModelSpend that sends your usage metadata anywhere, so:

- nothing is sent unless you pass `--post` **and** have set a webhook;
- **project names are left out of what is posted** unless you also pass `--include-projects` (they are always shown on your own screen);
- the digest holds numbers, project names, model names and flags, never prompt text;
- the address must be `https` (plain `http` only for localhost), with no credentials or query string; redirects are not followed; the address, which usually contains a secret, is never printed, logged or shown in an error.

Configure it in `~/.modelspend/settings.json` (or with `MODELSPEND_DIGEST_WEBHOOK` and `MODELSPEND_DIGEST_FORMAT`):

```json
{ "digest": { "webhook": "https://hooks.slack.com/services/...", "format": "slack" } }
```

`format` is `slack` (the default; the digest as a code block), `discord` (the same, cut to Discord's 2,000-character limit) or `json` (`{"text": ..., "digest": {...}}`). Some services, such as Microsoft Teams, use addresses with a query string, which are refused.

## Running it every morning

ModelSpend has no scheduler; use cron (or launchd). Use absolute paths, and note that the keys and settings come from `~/.modelspend`, so cron needs no environment variables:

```cron
# every weekday at 08:07: post yesterday's digest
7 8 * * 1-5 /Users/you/dev/modelspend/.venv/bin/modelspend digest --post >> $HOME/.modelspend/digest-cron.log 2>&1
```

The log gets the digest text (with project names) and, if posting fails, the reason; never the webhook address.

## Limits

Not yet run against a real Slack or Discord channel: the request shapes come from their documentation and were tested against fakes. The windows are rolling (the last 24 hours), not calendar days, so two runs an hour apart overlap.
