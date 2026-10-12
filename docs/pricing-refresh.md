# Keeping prices current

ModelSpend never trusts a price it has not verified, so refreshing prices is a **propose, review, apply** workflow, available from the CLI, from cron, and from the dashboard's **Pricing** tab.

```
pricing page ──fetch──► same as your accepted baseline? ──yes──► stop (no model call, no tokens)
                                   │ no
                                   ▼
        extract price rows with an LLM (routed through ModelSpend: tokens counted, visible in Usage)
                                   ▼
   VERIFY every row: the model name is on the page AND each price is printed on/next to that row
                                   ▼
       match page names to catalog models, diff old → new   ──►   PROPOSAL (pending)
                                   ▼
        you review (CLI or dashboard) ──► Apply: prices written to overrides.json, stamped verified today
                                         + this page version becomes your new baseline
```

## Commands

| Command | What it does |
|---|---|
| `modelspend sources set <provider> <url>` | change a provider's pricing-page URL (tracking parameters stripped; private/local addresses refused) |
| `modelspend sources check [provider] [--accept]` | fetch and report `unchanged` / `CHANGED` / `no_baseline` / `unreachable`; never reads prices |
| `modelspend pricing refresh [provider]` | fetch, extract, verify, and queue proposals. A page identical to your baseline costs **nothing** |
| `modelspend pricing paste <provider>` | the same, from table text you paste (stdin or `--file`), for pages that need JavaScript or block scripts |
| `modelspend pricing proposals` / `show <id>` | list pending proposals / show exactly what changes (old → new, %), and what was rejected |
| `modelspend pricing apply <id>` / `--all`, `reject <id>` | accept or decline. Rejecting also accepts the page as baseline so it is not re-proposed |

Options for `refresh`: `--force` (re-extract even if unchanged), `--auto-apply` with `--max-change-pct` (default 50), `--providers` / `--extractor-model` (which model does the extraction), `--max-tokens` (token budget per extraction).

**Exit codes** (for scripts): `0` nothing to review · `10` proposals are waiting for review · `1` an error occurred.

## Running it on a schedule (cron)

The keys file (`modelspend keys set ...`) means cron needs no environment variables. Use absolute paths:

```cron
# Mondays 07:30 - check all four pricing pages; log the result
30 7 * * 1 /Users/you/dev/modelspend/.venv/bin/modelspend pricing refresh >> $HOME/.modelspend/pricing-cron.log 2>&1
```

Exit code 10 means a review is waiting; wrap it however you like (mail, notification):

```bash
/path/to/.venv/bin/modelspend pricing refresh; [ $? -eq 10 ] && osascript -e 'display notification "ModelSpend: price changes to review"'
```

On macOS you can use `launchd` instead of cron. Cost: an unchanged page uses zero tokens; a changed page uses one extraction call (typically a few thousand tokens on the cheapest suitable model) and shows up in `modelspend usage` as an `extraction` task.

Unattended application is opt-in: `pricing refresh --auto-apply` applies a proposal only if **every** row verified, **no** row was ambiguous, only models already in the catalog change, and no change exceeds `--max-change-pct`. Anything else stays a pending proposal.

## What is and is not guaranteed

- **Verified:** a row is accepted only if its model name appears on the page and each price is printed on that row's line or the next few lines. Numbers the model made up, or that appear only elsewhere on the page, are rejected and listed.
- **Not a defence against a hostile page.** Text placed right next to a row can pass verification. The defences are human review and `--max-change-pct`. Use pages you trust (the official ones).
- **Not extracted:** long-context/tiered prices, batch/flex/priority prices, reasoning-token handling, context windows, capabilities. Two rows for one model (e.g. tiers) are flagged **ambiguous** for a human. Proposals carry a reminder to review the page.
- **Only catalog models are updated.** New models on the page are listed as "no catalog model"; add them with `catalog sync --add-new --only ...` first, then refresh.
- **JavaScript-rendered pages** may fetch as nearly empty; use `pricing paste`.
- Page text is capped (about 50K tokens); a truncated page is flagged, and models after the cut-off may be missing.

## Dashboard

`modelspend serve --background` (stop it with `modelspend stop`, reload it after an update with `modelspend restart`), then open the **Pricing** tab: edit each provider's URL, **Check**, **Refresh prices**, paste table text, and **Apply/Reject** proposals. Applying updates the running server's catalog immediately. State-changing requests (and the spend endpoints) require `Content-Type: application/json` and a same-origin, loopback `Host`/`Origin` unless `MODELSPEND_API_TOKEN` is set (then the bearer token is required instead). If you reach the server through another hostname, add it to `MODELSPEND_ALLOWED_HOSTS` (comma-separated). A `curl -X POST` without a JSON body and header is refused by design: use `-H 'Content-Type: application/json' -d '{}'`.
