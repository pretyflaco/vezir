# Changelog

Notable changes per release. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## 0.1.17 — retry-summary fixes, preset override, progress callbacks

Requires **meetscribe-offline >= 0.8.3**.

### Fixed

* **Retry-summary silently declared success without a summary.**
  `apply_labels()` in meetscribe swallowed summary exceptions and
  vezir had no post-condition check.  Now: (a) meetscribe 0.8.3
  re-raises when `summary_preset` is set, (b) vezir checks that
  `.summary.md` was actually produced, (c) `progress_callback` is
  passed so diagnostics are visible in the worker log.

### Added

* **`POST /api/sessions/{id}/retry-summary` accepts optional preset
  override** via JSON body `{"preset": "high-quality"}`.  Lets the user
  switch to a different summarization backend when the original fails
  (e.g. Tinfoil outage → retry with Claude Max).  Omitting the field
  or sending an empty body uses the session's original preset.

* **Progress callbacks** passed to `apply_labels()` in both
  `retry_summary_for_session()` and `_apply_and_finalize()`.  Summary
  generation progress is now visible in the worker log.

## 0.1.16 — fix voiceprint DB drift, merge command

Requires **meetscribe-offline >= 0.8.2**.

### Fixed

* **Voiceprint updates went to the wrong file.** `labels.py`'s
  in-process call to `update_profiles_from_confirmed_labels()` used the
  module-level `PROFILES_PATH` constant from `meet/voiceprint.py`, which
  was frozen at import time to `~/.config/meet/speaker_profiles.json`
  (the real `$HOME`).  The per-job `HOME` shim had no effect on
  in-process calls.  Result: the central vezir DB
  (`~/vezir-data/speaker_profiles.json`) went stale while the real-home
  DB accumulated 47 new labeling sessions, causing auto-label confidence
  to drift below the 0.65 threshold for some speakers.

  **Fix**: pass `profiles_path=config.speaker_profiles_path()` explicitly
  to `update_profiles_from_confirmed_labels()` (new kwarg in meetscribe
  0.8.2).  Also set `MEET_PROFILES_PATH` in the subprocess env for
  belt-and-suspenders.

### Added

* **`vezir voiceprints seed --merge`** flag.  Merges an external
  profiles file into the existing central DB (per-name policy: higher
  `n_sessions` wins).  Without `--merge`, the existing behavior is
  preserved (refuses if the target is populated).

### Changed

* **Dependency pin**: `meetscribe-offline>=0.8.2`.

## 0.1.15 — fix retry-summary, new summarizing status, DNS warmup

### Fixed

* **Retry-summary was broken: interactive prompt aborted the subprocess.**
  `retry_summary()` shelled out to `meet label --auto --no-audio
  --summary-preset <preset>`, but `meet label --auto` always runs voice
  identification from scratch. When the speaker label (e.g. "Kemal") was
  assigned via the Android/web UI rather than a voiceprint auto-match,
  the speaker appeared as "unrecognized" and meetscribe dropped into an
  interactive `click.prompt()` which immediately aborted on the piped
  stdin.  The summary step was never reached.

  **Fix**: replaced the subprocess call with an in-process call to
  meetscribe's `apply_labels(label_map={}, regenerate_summary=True,
  summary_preset=...)` API.  This is the same codepath used by the
  labeling web/API handlers.  With an empty `label_map`, no relabeling
  occurs — only summary regeneration.

### Added

* **`summarizing` job status** (`queue.py`).  `retry_summary_for_session()`
  now sets status to `summarizing` (not `transcribing`) while the summary
  is being generated.  Android 0.2.3 renders this as a green badge.

* **DNS warmup at worker startup** (`worker.py`).  After a server restart,
  `systemd-resolved` may take several seconds to become operational.  The
  worker now blocks (up to 60 s) until `huggingface.co` and `github.com`
  resolve successfully before claiming its first job.  This prevents the
  cascade of summary and sync failures that occurred on every restart.

### Removed

* **`meet_runner.retry_summary()`** — replaced by in-process
  `apply_labels()` in `retry_summary_for_session()`.

## 0.1.14 — graceful sync failures, ratelimit fix

Mirrors the 0.1.13 summary-failure work for the sync step: when `meet
sync` fails (DNS, git auth, network) but all artifacts are on disk, the
job completes as `done` with a `sync_error` field instead of `error`.
The user can retry via the dashboard's "Sync now" button or the Android
app.

### Added

* **`sync_error` column** on the jobs table (`queue.py`). Same sentinel-
  default pattern as `summary_error`. Idempotent migration for existing
  DBs.

### Changed

* **Worker no longer treats sync failures as hard errors.** In
  `process_one()`, `finalize_after_labeling()`, and
  `retry_summary_for_session()`, sync failures are captured into
  `sync_error` and the job proceeds to `done`. The existing
  `POST /session/{id}/sync` endpoint already serves as the retry
  mechanism.
* **`summary_error` is no longer lost when sync fails.** Previously,
  if summary failed and then sync also failed, the early-return in the
  sync-error path skipped the `done` update that would have persisted
  `summary_error`. Both fields are now written in a single final update.

### Fixed

* **Ratelimit crash on empty bearer token** (`ratelimit.py`).
  `Authorization: Bearer ` (trailing space, no token) caused an
  `IndexError` in `_client_key()` returning a 500. Now falls through
  to per-IP keying.

## 0.1.13 — graceful summary failures, retry-summary endpoint

When `meet transcribe` succeeds but the summary backend is unreachable
(e.g. transient DNS after a server restart), the job now completes as
`done` instead of `error`. The transcript, SRT, JSON, and PDF artifacts
are available immediately; only the AI summary is missing. Users can
retry the summary from the Android app or API once connectivity is
restored.

### Added

* **`summary_error` column** on the jobs table (`queue.py`). When the
  summary step fails but the transcript succeeded, this field stores the
  failure message. The session reaches `done` status (not `error`) so
  transcript artifacts are accessible. Idempotent migration for existing
  DBs.
* **`POST /api/sessions/{id}/retry-summary`** endpoint (`sessions.py`).
  Accepts sessions in `done` status with a non-empty `summary_error`.
  Re-runs summary generation in a background thread. The client polls
  `GET /api/sessions/{id}` to observe the transition:
  `done` -> `transcribing` -> `done` (with `summary_error` cleared on
  success).
* **`retry_summary()` in `meet_runner.py`**: runs
  `meet label --auto --no-audio --summary-preset <preset>` to regenerate
  summary and PDF without re-transcribing.
* **`retry_summary_for_session()` in `worker.py`**: full pipeline for
  retrying summary, including re-sync if sync is enabled.
* **`_extract_summary_error()` in `worker.py`**: parses the job log tail
  for meetscribe's preset-guard RuntimeError or Tinfoil-specific DNS
  failures.

### Changed

* **Worker no longer treats summary-only failures as hard errors.** When
  `meet transcribe` exits non-zero but `.txt` + `.json` artifacts exist
  on disk, the worker infers that transcription succeeded and only the
  summary failed. The job proceeds through auto-labeling, unresolved-
  speaker detection, and sync (if enabled) with `summary_error` set.
* **`queue.update_status()` accepts a `summary_error` kwarg** with
  sentinel default (`...`) to distinguish "don't touch" from "clear to
  None".

## 0.1.12 — security hardening pass

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
