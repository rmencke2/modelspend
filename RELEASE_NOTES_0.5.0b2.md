# ModelSpend 0.5.0b2 (beta)

A small fix release on top of 0.5.0b1. **If you call the Python SDK's sync methods more than once, upgrade.**

## Fixed

- **`Event loop is closed` on the second sync SDK call.** `ModelSpend.route()`, `preview()` and `stream()` ran every call on a fresh event loop and closed it afterwards. The OpenAI, Anthropic and Google SDK clients are cached and keep their connections tied to the first loop, so a script that made two sync calls on one `ModelSpend` object failed on the second one with `all attempts failed ... Event loop is closed`. Sync calls now share one background loop, and a regression test covers repeated `route()` and `stream()` calls.
- **Not affected:** the async API (`aroute`, `astream`), the CLI and the local server. Only the sync SDK methods, used more than once on the same object, hit this.

## Verified live

- **OpenAI structured output (`response_schema`)** on `gpt-4.1-nano`, strict and non-strict: valid JSON, validation passed, no escalation, with token counts and cost reported. With a non-strict schema the model may use its own wording (it answered `"Billing Issue"` where the strict enum forced `billing`); use a strict schema when you need exact labels.
- This test is also what found the bug above.

## Not yet verified

- **Telling models apart at scale.** On the harder set only one of seven models fell short, on one task type with 22 cases.
- **OpenAI cached-token reporting**, and a comparison of totals with the provider's billing page.
- **Gemini and xAI** adapters (unit-tested against simulated responses only).
- **Concurrent budget enforcement** across processes and the dashboard's budget form.
- The **Docker code sandbox** and **automatic price refresh** against the real provider pages.
- macOS and Linux with Python 3.10+ only; Windows is untested.

## Upgrade

```bash
git pull && pip install -e .
```

Everything else is as in [0.5.0b1](RELEASE_NOTES_0.5.0b1.md): your own evidence bars, your own eval cases, `modelspend evidence`.

**Full changelog:** https://github.com/rmencke2/modelspend/compare/v0.5.0b1...v0.5.0b2
