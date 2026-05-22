# Changelog

Notable changes per release. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## 0.1.12 — security hardening pass (unreleased)

This is a small follow-up to 0.1.11 focused on the auth / login surface
and on getting TLS in front of vezir before non-founding-team members
join the pilot. The bigger v0.2 work (unified one-time enrollment that
collapses npub + nvpn invite + bearer into a single QR) is deferred.

### Backward compatibility

* **Existing browsers**: stale `vezir_session` cookies (which used to be
  the plaintext bearer token) are accepted once with a warning log line.
  The next `/login` round-trip migrates the cookie to an opaque session id.
* **Older clients** (vezir < 0.1.11, vezir-android < 0.1.4): unchanged.
  The new exchange-code path is emitted by the server regardless of
  client version; clients just see a different `dashboard_login_url`.
* **Legacy `?token=` URLs**: still accepted for one release, with a
  `Deprecation: true` response header. Will be removed in 0.2.0.
* **Pre-0.1.12 token rows** (no `expires_at`, no `is_admin` field):
  treated as no-expiry, scribe-tier. Operators must re-issue with
  `--admin` to keep `/admin/enroll` access.

### Added

* **Token expiry, `last_used_at`, `is_admin`, `label`** fields on every
  token row in `~/vezir-data/tokens.json` (`vezir/server/auth.py`).
  Issue with `--expires-in <30d|12h|45m|never>`, `--admin`, `--label`.
* **`require_admin` dependency** gating `/admin/enroll` so an ordinary
  scribe token can no longer mint enrollment material for *any* other
  token (`vezir/server/auth.py:require_admin`,
  `vezir/server/enroll.py`).
* **Opaque session cookies + exchange codes** (`vezir/server/web_sessions.py`,
  `vezir/server/login.py`):
  * The `vezir_session` cookie value is now an opaque random id mapped
    in process memory to a github handle, not the plaintext bearer.
  * `/login?code=vzx_...` swaps a single-use, 60-second exchange code
    for a session. Upload responses build this URL automatically.
  * The bearer token never appears in URLs, browser history, or Caddy
    access logs.
* **In-process rate limiter** (`vezir/server/ratelimit.py`):
  * `/upload` — 10 / token / minute
  * `/login`  — 20 / IP / minute (catches token spraying)
  * `/api/*`  — 60 / token / minute
  * 429 responses include `Retry-After`. Disable with `VEZIR_DISABLE_RATELIMIT=1`.
* **Caddy reverse-proxy infrastructure** (`infra/caddy/`):
  * `Caddyfile.example` with Tailscale + nvpn listeners.
  * `install-caddy.sh` idempotent installer for macOS / Debian / Ubuntu.
  * Access-log scrubbing of `Authorization` and `Cookie` headers.
  * Security headers (HSTS, CSP, X-Content-Type-Options, Referrer-Policy).
* **Enrollment QR payload v2**: when `VEZIR_CADDY_ROOT_CERT_PATH` points
  at a PEM, the QR JSON additionally carries `ca_pem` so Android can
  trust the Caddy internal CA before its first request
  (`vezir/server/enroll.py`).
* **`vezir token list --dormant N`**: prints handles with no successful
  use in the last N days, including `(expired)` annotations. Useful for
  routine rotation reviews.
* **nvpn offboarding runbook** (`infra/nvpn/README.md` "Offboarding a
  teammate"): the two-step revoke (vezir token + nvpn participant) that
  was previously undocumented.

### Changed

* **`vezir serve` binds to `127.0.0.1:8000` by default** (was `0.0.0.0`).
  Front with Caddy or opt back in via `VEZIR_HOST=0.0.0.0`. See
  `infra/caddy/README.md` for the migration path
  (`vezir/config.py:host`).
* **Token hash comparison switched to `hmac.compare_digest`** (was `==`).
  Closes a small but real timing leak on the bearer-token lookup
  (`vezir/server/auth.py:_lookup_entry`).
* **`vezir token issue` defaults to 90-day expiry**. Pass
  `--expires-in never` to opt out (recommended only for tokens used by
  unattended services).
* **`vezir token enroll` no longer issues admin-tier tokens** by
  default. Device tokens (Android, scribe laptops) are scribe-tier; use
  `vezir token issue --admin` for operator tokens.
* **`dashboard_login_url` in upload responses** now contains
  `?code=vzx_...` (single-use exchange code) instead of `?token=vzr_...`
  (`vezir/server/uploads.py`).

### Security

* Bearer tokens no longer appear in `/login` URLs, browser history,
  reverse-proxy access logs, or persistent cookies. Closes a leakage
  channel that was the highest-severity surface flagged in the
  pre-dogfood review.
* `/admin/enroll` is now gated by `is_admin`, fixing the
  documented-but-unfixed "any valid token can mint enrollment material"
  limitation in pre-0.1.12 `enroll.py`.
* Rate limiter protects against token-spraying `/login` and runaway
  upload loops without requiring an external WAF.

### Tests

* 21 new tests in `tests/test_token_hardening.py` covering:
  expiry semantics, `last_used_at` debouncing, `hmac.compare_digest`,
  legacy row tolerance, `require_admin`, exchange-code round trips,
  expired/revoked-code rejection, opaque session cookies, legacy
  `?token=` deprecation header, logout invalidation, /api/ vs /
  auth semantics, rate-limit burst behavior, per-token isolation,
  env disable.
* 6 new tests in `tests/test_enroll.py` for QR payload v1/v2,
  CA-PEM env loading, graceful fallback on bad/missing/oversized cert.
* `tests/conftest.py` disables the rate limiter globally; focused tests
  re-enable it via a fixture.

### Migration checklist for operators upgrading from 0.1.11

1. `pip install --upgrade vezir`
2. `vezir token issue --admin --github <your-handle>` — issue yourself
   an admin token. Your old scribe token now returns 403 on
   `/admin/enroll`.
3. `cd infra/caddy && ./install-caddy.sh`, edit the Caddyfile, start
   Caddy.
4. `export VEZIR_COOKIE_SECURE=1` and `export VEZIR_CADDY_ROOT_CERT_PATH=…`
   in the vezir service environment.
5. Restart `vezir serve`. It binds to `127.0.0.1:8000`; verify with
   `ss -ltn | grep 8000`.
6. Update teammate `VEZIR_URL` to the new HTTPS endpoint. Re-enrol
   Android devices via the QR (the v2 payload trusts the CA root
   automatically on 0.1.4+).
7. Run `vezir token list --dormant 14` to spot tokens nobody is using —
   good candidates to revoke before they sit forever.

### Deferred to v0.2.0

* Unified one-time enrollment (npub + nvpn invite + bearer in a single
  short-lived QR payload). Biggest UX win remaining.
* ffmpeg sandbox in the worker.
* Signed/notarized macOS sidecar in `meetscribe-record`.
* Android certificate pinning (waits on the Caddy CA strategy bedding in).
* Auto-delete Android recordings on upload success.
* nvpn binary checksum pinning + signed releases.
* Dependency lockfiles across the four repos.
* meetscribe path sanitization in `meet/cli.py` (not reachable through
  vezir's upload path, so genuinely lower priority).
