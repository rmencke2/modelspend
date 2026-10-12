# Security policy

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Use GitHub's private reporting:
**Security tab → "Report a vulnerability"** on this repository. Include what you found, how to reproduce it, and the version
(`modelspend --version` / the commit). You will get an acknowledgement; fixes are released as soon as practical and credited
unless you prefer otherwise.

## Scope and threat model

ModelSpend is a local-first tool that spends **your own** provider API keys. In scope: key or token leakage, authentication or
browser-safety bypasses of the local server (CSRF, DNS rebinding, cross-origin access), SSRF, injection, unsafe file permissions,
and anything that lets a prompt, a model output, or a pricing page change routing, budgets, allowlists or prices without review.

Design choices to be aware of:

- Keys live in memory from the environment, or in `~/.modelspend/keys.env` (plain text, `chmod 600`, directory `0700`). Real environment
  variables override the file. Keys are never written to SQLite, logs or API responses; known key shapes are scrubbed from error text.
- The server binds to `127.0.0.1` and refuses a non-loopback address without `MODELSPEND_API_TOKEN`. Without a token it also checks the
  `Host` header on every data endpoint, and requires a JSON content type and a same-origin `Origin` for anything that spends money or
  changes settings.
- Prompts and responses are not stored. Usage metadata (models, token counts, costs, timestamps) is stored in SQLite.
- Generated code is only ever executed inside a Docker container with no network; never on the host.
- Price updates from pricing pages are **proposals** that require review (or an explicit, guarded `--auto-apply`).

## Known limits

- Hostname checks for user-set pricing URLs resolve DNS before the request; a hostile DNS server could still race that check
  (time-of-check/time-of-use). Only point the pricing URLs at pages you trust, and prefer the default official pages.
- Request-supplied regex validators run in-process without a timeout; do not expose the server to untrusted callers.
- ModelSpend sends your prompts to the providers you allow. It makes no claims about their data handling.
