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

## Defences worth knowing about

- **SSRF:** user-set pricing URLs and every redirect hop are resolved **once**, must be globally routable (private, loopback, link-local,
  CGNAT and multicast ranges are refused, including IPv4-mapped IPv6), and the fetch connects to that exact IP, so DNS cannot change
  its answer between check and use. Fetches ignore `HTTP(S)_PROXY`, stream the body with a 3 MB cap and a 30 s deadline. Request-supplied
  JSON schemas can never make the server fetch a URL (only local `$ref`s are accepted).
- **ReDoS:** request-supplied regex validators and schema `pattern`s are evaluated in a worker process with a 2 s hard timeout (patterns
  are capped at 500 characters, text at 100,000), so a catastrophic pattern cannot stall the server.
- **Price changes:** an extracted price must be printed on or next to its model's row. Unattended `--auto-apply` (CLI only, never over
  HTTP) measures drift from the last price a *person* approved, and never applies a price that has no reviewed predecessor.
- **Browser safety:** `Host` check on every data endpoint, JSON content type + same-origin `Origin` for writes and spend endpoints,
  `X-Frame-Options: DENY`, `frame-ancestors 'none'`, `nosniff`, no CORS, no auto-generated docs pages, 2 MB request body cap.
- **Process control:** `modelspend stop` signals a process only if the PID file is well-formed, the process answers like a ModelSpend
  server, and (where `lsof` exists) that PID owns the port.

## Known limits

- `python_tests` runs generated code and its tests in one process inside a no-network Docker container. A random per-run nonce makes a
  faked pass hard, but it is **not adversary-proof**: treat it as a quality signal for ordinary model output, not a security boundary.
- If you run behind an HTTP(S) proxy, pricing-page fetches are not possible (proxies defeat the IP check); use `pricing paste`.
- There is no request rate limit or brute-force throttle on the bearer token, and the token travels in clear text over HTTP. If you expose
  the server beyond localhost, use a long random token and put a TLS reverse proxy in front. A token holder has full spend authority:
  request-level `providers`, `allow_models`, `allow_unpriced` and budgets override your defaults.
- ModelSpend sends your prompts to the providers you allow. It makes no claims about their data handling.
- `redact.scrub` is best effort: unusual key formats may not be recognised. Never paste logs, `keys.env`, or your database into public places.
