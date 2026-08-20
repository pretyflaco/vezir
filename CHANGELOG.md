# Changelog

Notable changes per release. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## 0.14.0 — summary fallback provenance (Claude Max exhausted → Kimi K3)

Requires millet-pipeline ≥ 0.16.0 for the fallback itself; the vezir half
only *surfaces* it.  Migration `0.14.0-summary-fallback` (idempotent,
additive).

### Added

- **Opt-in summarization fallback for non-`confidential` presets.**  When
  the server operator sets `MILLET_SUMMARY_PRESET_FALLBACK=1` on the vezir
  service (inherited verbatim by the millet subprocess through the HOME
  shim), millet no longer hard-fails when an explicitly requested preset's
  backend is down or out of quota — e.g. a Claude Max subscription
  exhausted mid-month.  It continues down the fallback chain, which the
  operator can point at the generic OpenAI-compatible backend:
  `MILLET_SUMMARY_FALLBACK_ORDER=openai` +
  `MILLET_OPENAI_BASE_URL=https://api.moonshot.ai/v1` +
  `MILLET_OPENAI_API_KEY=…` + `MILLET_OPENAI_MODEL=kimi-k3` gives a
  Claude-Max→Kimi K3 fallback.  The `confidential` preset **never** falls
  back — the privacy contract stays fail-loud regardless of the opt-in.
- **`jobs.summary_fallback` column + API/TUI surfacing.**  A fallback must
  never be silent: millet's `.summary.meta.json` sidecar now records
  `preset` and `fallback_used`; the worker reads it after transcribe and
  after retry-summary and stores `"<backend>/<model>"` (e.g.
  `openai/kimi-k3`) in the new `summary_fallback` column when a fallback
  served the summary.  Session JSON carries the field; the sessions list
  shows a yellow `· fallback` badge and the detail screen a "summary
  served by fallback: …" line.  NULL when the requested preset ran.

### Fixed

- Bad UX when Claude Max is exhausted: sessions previously ended `done`
  with a `summary_error` and no summary at all.  With the opt-in they now
  get a Kimi K3 summary instead, clearly labeled as a fallback.

### Tests

- New `tests/test_worker_summary_fallback.py` (11 tests): meta-sidecar
  parsing (fallback/no-fallback/missing/malformed/old-millet/lang-suffixed),
  `update_status` sentinel semantics for the new column, migration
  idempotency, and two end-to-end `process_one` runs asserting the column
  is set on fallback and NULL otherwise.  Suite: 974 passed.

## 0.13.1 — fast NIP-46 login

Nostr sign-in drops from ~2–3 minutes to seconds of machine time plus the
user's approval tap (measured: 2m43s → 25s with the Blink signer, 1m36s →
20s with Amber; machine time alone is now ~2–5s).  No DB migration.

### Fixed

- **~2-minute NIP-46 login stall.**  With a dead relay in the default set
  (`relay.nsec.app`), `_reconnect_dead_relays` ran a synchronous 30s-blocking
  `create_connection` inside every `_publish` — including each 4s republish —
  freezing the response read loop while the signer's replies sat unread in
  socket buffers.  Both `get_public_key` and `sign_event` looked like ~34s
  signer delays but were answered in under a second.  Reconnects now run on
  deduped daemon threads; the read loop never blocks.  The per-relay connect
  timeout dropped 30s → 10s (startup opens are parallel, so a dead relay
  costs 10s once, hidden behind QR scanning).
- **Relay-side `since` filter removed** from the NIP-46 response
  subscription.  The filter is computed from *our* clock while responses are
  stamped with the *signer's* clock, so a signer >60s behind had every
  response relay-filtered.  The fresh-per-session client key has no history
  to exclude; outgoing `created_at` clock correction is unchanged.

### Added

- **`url`/`image` metadata in the `nostrconnect://` URI.**  Signers can show
  the app identity on the consent screen and origin-bind a
  `sign_event:27235` pre-approval grant (NIP-98 host match) — one-tap login
  against signers that implement it, unchanged behavior against the rest.
  The consent icon is `assets/logo/vezir.png`, served from the repo.
- **Timestamped `--verbose` login diagnostics.**  `vezir login --verbose`
  now ms-timestamps every nip46 log line and adds phase markers
  (relay-connect elapsed, `sign_event` publish, per-request response wait),
  so a slow login is attributable to a phase from a single log.

### Changed

- **Default relay set pruned and probe-verified.**  Removed
  `relay.nsec.app` (confirmed dead) and `relay.getportal.cc`; added
  `relay.primal.net` and `theforest.nostr1.com`.  All five entries verified
  reachable with sub-second handshakes; `nostr.oxtr.dev` was considered but
  failed the probe.  Current set: `relay.damus.io`, `nos.lol`,
  `offchain.pub`, `relay.primal.net`, `theforest.nostr1.com`.

### Tests

- 963 tests pass: REQ-filter shape, non-blocking reconnect, inflight
  reconnect dedup, connect-URI `url`/`image` emission.

## 0.13.0 — meeting attachments

Supporting material — slides, agendas, screenshots, PDFs — can now ride
along with a meeting (issue #16).  No DB migration: attachments are stored
on the filesystem inside the session directory.

Requires `millet-pipeline >= 0.15.0` (the `[server]` extra pins it) for the
git-archive half; an older millet stores and serves attachments fine but
never syncs them.

### Added

- **Staging folder + last-chance pause in `vezir scribe`.**  Recording
  starts by printing a fixed, well-known folder (`~/vezir-attachments/`,
  override with `VEZIR_ATTACHMENTS_DIR`) to drop files into while the
  meeting runs.  When recording stops, scribe lists what it found and waits
  for Enter — skipped when stdin is not a TTY (scribe is documented for
  headless/ssh use) or with the new `--no-pause` flag.  A Ctrl-C or EOF at
  that prompt continues into the upload rather than discarding a recorded
  meeting.  After the audio lands, the staged files are POSTed and then
  *moved* into that recording's own `attachments/`, leaving the staging
  folder empty for the next meeting.  A failed attachment upload warns and
  leaves the files staged; it never fails the meeting upload.
- **Server API** — `POST`/`GET`/`GET <name>` under
  `/api/sessions/<id>/attachments` (`vezir/server/attachments.py`).  Files
  live in `<sessions_dir>/<id>/attachments/`, which is exactly the directory
  the worker hands to `millet sync`, so millet 0.15.0's verbatim passthrough
  carries them into the team repo with no further work here.
- **TUI** — the record screen shows the staging folder and how many files
  are waiting in it, and prompts with the same last-chance list when
  recording stops (Enter/Escape continue into the upload, `r` rescans);
  attachments then upload with the meeting exactly as in the CLI.  In the
  session detail screen they appear as marked rows in the artifacts table
  and open through the existing `ArtifactScreen` (inline text, OS opener for
  binaries, save-to-disk).
- The shared workflow lives in `vezir/client/attachments.py`; `scribe.py`
  keeps only its own surface (printed announcement, blocking prompt) and the
  TUI reports through Textual messages instead of `print`.
- **`vezir pull`** — attachments are fetched into `<meeting>/attachments/`,
  names kept verbatim, so the "share a meeting without git" path doesn't
  silently omit what the git archive carries.
- `VEZIR_MAX_ATTACHMENTS` (50) and `VEZIR_MAX_ATTACHMENT_BYTES` (100 MiB)
  cap what one session can hold; both match millet's sync-side caps, so
  nothing accepted here is silently dropped on the way to the repo.

### Notes

- Attachments are deliberately *not* served through `/artifact/<id>/<name>`:
  that route rejects `/` outright, and user-chosen filenames would collide
  with millet's canonical artifact names (`summary.md`, `transcript.pdf`) in
  its flat namespace.
- Client-supplied filenames are flattened to a single path component
  (traversal, Windows paths, control characters, over-long names) and
  de-duplicated with `_N` suffixes; symlinks are never listed or served.
- `sessions._enforce_team_visibility` is now public as
  `enforce_team_visibility` (used by the new module).
- Moving a staged file next to the recording skips the move when the
  destination already holds a byte-identical copy: with client and server on
  one host and `VEZIR_RECORD_DIR` pointing into `VEZIR_DATA/sessions`, the
  file the server just stored *is* the move target, and the naive path would
  leave an `_2` duplicate behind.
- Accepted limitations: an attachment uploaded after the worker's sync step
  has already run misses that push (attachments are sent seconds after the
  audio, sync runs minutes later); attachments are not fed to summarization.

Test suite grows 906 → 957.

## 0.12.1 — security & correctness hardening; docs refresh

A codebase-wide bug/security pass.  No DB migration.  All changes are
backward compatible.  Test suite grows 897 → 906.

### Security

- **Refresh-token grace window no longer hijackable (High).**  A stale
  refresh token replayed within the lost-response grace window previously
  minted a *new* token family to whoever presented it (silent session
  hijack, no reuse detection) and re-anchored the window on every hit, so
  a thief could renew indefinitely.  The grace path now replays the exact
  pair the original rotation minted (idempotent) and never slides the
  window; a stale token in-window with no cached response is treated as
  reuse.  (`sessions_auth.py`)
- **Personal sessions are now enforced on every per-session endpoint.**
  `list_recent` already hid other users' personal sessions, but the
  detail / artifact / clip / label / sync-now endpoints only checked
  `team_id` — any same-team member who learned a personal session's ULID
  could read its transcript/audio and even force-sync it to the shared
  repo.  These now 404 for non-owners (owner + global admins excepted).
  (`sessions.py`, `labels.py`)
- **Git credentials no longer leak via error tails.**  `error` /
  `sync_error` embed the last ~2 KiB of the millet log, returned to every
  team member; a PAT-in-URL git remote (`https://ghp_…@github.com/…`)
  echoed by git's failure output is now redacted before storage.
  (`worker.py`)
- **NIP-46 auth_url phishing guard (client).**  An `auth_url` injected by
  an unbound relay author, or a non-HTTPS URL, is no longer surfaced to
  the user as an "approve" link — only HTTPS URLs from the bound signer
  are honored.  Connect-secret comparisons are now constant-time.
  (`client/nostr/nip46.py`)
- **Rate-limit buckets are bounded** (were keyed on the unvalidated
  bearer and grew without limit), and **CLI session revocation now
  propagates to the running server** within a short refresh interval
  instead of leaving already-minted access JWTs alive until `exp`.
  (`ratelimit.py`, `sessions_auth.py`)

### Fixed

- **`vezir scribe --wait` / `upload --wait` no longer silently time out.**
  `poll_status` omitted the `X-Team-Id` header (a hard 400 on v0.7.0+
  servers) and bypassed TLS trust resolution, so every poll failed and
  was swallowed until the deadline.  It now sends the team header,
  resolves the internal-CA trust store, and surfaces persistent failures.
  (`client/scribe.py`)
- **`vezir session list` / `vezir session revoke` are reachable again.**  A
  duplicate `@main.group() def session()` shadowed the auth-session group,
  making the 0.10.0 operator commands unreachable.  Merged into one group.
  (`cli.py`)
- **One-shot upload retries no longer create duplicate sessions.**  The
  client sends an `Idempotency-Key`; the server replays the existing
  session on a retry after a lost/late response.  (`uploader.py`,
  `uploads.py`)
- **Resumable-upload retry loops are bounded** (401/409/429 could spin
  forever), the **NIP-98 login event now honors the learned clock offset**
  (skewed-clock machines no longer 401 after a successful handshake), and
  the **label-apply subprocess is serialized with the worker** via a
  per-session shim lock (no more racing rmtree of the shared HOME shim).
- **Blocking DB/file work moved off the event loop** in the async upload
  handlers (`run_in_threadpool`), so DB contention can't freeze all
  requests.  (`uploads.py`)
- Assorted lows: `Authorization: Bearer ` (whitespace-only) → 401 not 500;
  `/api/sessions?limit=-1` clamped; `sync_now` reports the real `queued`
  value; titles trimmed server-side; `sync_meeting_type` slug-validated at
  write time; NIP-44 strict base64; Google device `slow_down` += 5s;
  teams.json preserves unknown keys; TUI update-check env guard fixed;
  binary-artifact temp files cleaned on unmount; TUI identity refresh moved
  to a worker thread.

### Changed

- **`millet-pipeline` pin raised to `>=0.13.0`** to match the runtime floor
  the server has enforced since 0.11.0 (removes an install-then-fail trap).
- **README refreshed** from the 0.8.3 era to 0.12.x: rotating-session model,
  runtime millet floor, MP3 uploads, `upload-multi`, session retitle,
  `empty` status, and ~9 previously-undocumented env vars.  `config.py`
  env-var docstring corrected (the `VEZIR_MEET_*` "removed in 0.6.0" claim
  contradicted still-live alias code).

### Deferred

- Larger lows left as tracked follow-ups: admin-demotion propagation to live
  sessions, resumable-sweep vs. in-flight-PATCH race, session/revoked-row
  reapers, `contextlib.closing` in migration connections, `--token-file`,
  and the client HTTP-connection-reuse / polling-backoff efficiency items.

## 0.12.0 — retitle sessions, PyPI update nudge

Two field-report features.  No DB migration (the `title` column has
existed since the earliest schema).  Test suite grows 885 → 918.

### Added

- **Edit a session's title after it was recorded.**  Scribes sometimes
  forget to name a session at record time and had no way to fix it.  New
  write path for the existing `title` field:
  - `POST /api/sessions/{id}/title` (body `{"title": "..."}`).
    Authorization mirrors delete: server-wide admin **or** the original
    uploader (cross-team → 404, other member → 403).  An empty/blank
    title clears it (the session then displays by id).
  - TUI: `[t] Edit title` on the session detail screen (pre-filled input
    modal).
  - CLI: `vezir session set-title <id> "New title"`.

  The title is **not** baked into the transcript/summary/PDF, so nothing
  is regenerated.  It does drive millet's sync folder name / schedule
  matching, which read it fresh at sync time — so a new title takes
  effect on the next sync.  If the session was already synced, the pushed
  git folder is not renamed automatically; the response (and the TUI/CLI)
  surface a warning to re-run sync.

- **The TUI nudges when a newer vezir is on PyPI.**  Some users didn't
  realise an update was available that would have fixed the problem they
  were hitting.  A background poll (every ~6h, cached across launches in
  `client.json`) checks `pypi.org/pypi/vezir/json`, and when a newer
  release exists shows an in-app toast + desktop notification with the
  exact upgrade command for how vezir was installed (`pip install
  --upgrade vezir`, `pipx upgrade vezir`, or `git pull && pip install
  -e .`).  The Record screen version line also flags the available
  update.  Disable with `VEZIR_TUI_DISABLE_UPDATE_CHECK=1`.  There is no
  in-app self-update by design — vezir is a pip/pipx package; showing the
  command is the honest, safe behaviour.

## 0.11.1 — sync fixes: long titles, empty recordings, client provenance

Three focused fixes from field reports on the 0.11.0 deployment.  No
schema migration (the new `client_agent` column is added via the
idempotent startup ALTER, like `team_id`/`personal`).

### Fixed

- **Long meeting titles no longer break sync.**  A title that slugified
  to the 60-char cap plus the disambiguating `-HHMMSSZ-<rand>` suffix
  produced a 75-char `--meeting-type`, which millet 0.13.0's folder
  validator (max 64 chars) correctly rejected — leaving the session
  stuck in `sync_failed`.  `config.sync_slug` now caps at 64 and
  `meet_runner._meeting_type_for` reserves room for the suffix (and
  re-strips a trailing separator), so the folder name is always a valid
  single path segment.

- **Empty recordings are no longer synced to the team repo.**  A
  recording with no speech (an accidental tap, a dead mic, or silence
  WhisperX reports as "no active speech") produced zero transcript
  segments; the pipeline's speaker gate treated "no speakers" as
  "nothing unresolved" and pushed empty/stub artifacts (0-byte
  transcript, placeholder summary) to git.  Such sessions now land in a
  new terminal **`empty`** status and skip sync entirely.  (Already
  pushed empty folders are left as-is; this only stops new ones.)

### Added

- **Server records the client's User-Agent per session** (`client_agent`
  on the job row, surfaced in `/api/sessions[/{id}]` and the TUI detail
  screen).  Answers "which client / version produced this?" — previously
  a blind spot.  The Python clients now self-identify as
  `vezir-cli/<version>`; the Android app sends its OkHttp UA.

## 0.11.0 — auth hardening, subprocess boundary restored, worker serialization

Server hardening release from the 2026-07 ecosystem review.  **The
server now requires millet-pipeline ≥ 0.13.0** (label apply / summary
retry go through the new `millet label --apply-json`; vezir probes for
it and fails with an upgrade message).  No new DB migration.  Test
suite grows 807 → 833.

### Fixed (auth)

* **Session expiry checks were skewed on non-UTC hosts.**
  `sessions_auth._parse_iso` parsed UTC timestamps with `time.mktime`
  (local time); every refresh/idle/absolute-cap check was off by the
  host's UTC offset.  Now `calendar.timegm`.
* **A lost `/api/auth/refresh` response no longer kills the session.**
  The documented one-generation grace window is implemented: replaying
  the just-consumed refresh token within `VEZIR_REFRESH_GRACE`
  (default 60 s) of its rotation re-issues the pair (lost-response
  retry).  Outside the window — or anything older — is confirmed reuse
  and revokes the family (RFC 9700).  `VEZIR_REFRESH_GRACE=0` = strict.
* **Revoking a session now kills its live access JWTs immediately**
  (logout, admin revoke, revoke-all, reuse detection) via an
  in-process revoked-sid cache checked on JWT decode — still no DB hit
  on the hot path.  Previously a revoked session's access token worked
  until `exp`.
* **Login rate-limit bypass closed.**  The unauthenticated
  login/refresh bucket keyed on the *unvalidated* Bearer header — a
  random bearer per request got a fresh bucket.  Now strictly per-IP;
  and `vezir serve` runs uvicorn with `proxy_headers=True` +
  `forwarded_allow_ips=127.0.0.1` so per-IP buckets behind Caddy see
  real client IPs instead of collapsing everyone into the proxy's one
  bucket.
* `POST /api/auth/logout`, `GET /artifact/...`, and
  `POST /session/{id}/sync` are now rate-limited; a pre-existing
  `.session-secret` gets 0600 enforced before reading.

### Changed (architecture)

* **millet is a subprocess again.**  Label apply and summary retry
  imported `millet.label` in-process and mutated `os.environ["HOME"]`
  around the call — racing every other thread in the server.  Both now
  run `millet label --apply-json` through the per-job HOME shim
  (hence the millet-pipeline ≥ 0.13.0 requirement).
* **Follow-up work is serialized through the single worker.**
  Retroactive sync, retry-summary, and the post-labeling
  voiceprint-update+sync follow-up were ad-hoc daemon threads racing
  the worker and each other (double-click "Sync now" = two concurrent
  syncs mutating one session).  They are now queued tasks
  (`worker.enqueue_task`) drained by the worker thread, deduped per
  (kind, session).  API response shapes unchanged.
* **millet steps have a hard timeout** (`VEZIR_MILLET_TIMEOUT`,
  default 4 h): a wedged transcription no longer blocks the queue
  forever — the process group is killed and the job errors with the
  log tail.

### Fixed (robustness)

* **Upload cleanup on client disconnect.**  A mid-upload disconnect
  (starlette `ClientDisconnect`, not an `HTTPException`) leaked a
  partial audio file + orphan session dir forever.  Both upload
  endpoints now clean up on any exception.
* **Concurrent resumable PATCH corruption.**  Two chunks with the same
  valid offset could interleave writes into one `.part` file.  A
  per-upload lock returns 409 to the second in-flight chunk.  Orphan
  `.part` files without a meta sidecar are now swept after the TTL.
* **SQLite schema DDL ran on every connection** (twice per
  authenticated request, under the global lock, with genuine
  `OperationalError`s masked).  Now once per process per DB path.
* **`delete_team` is atomic**: the four DB steps ran in separate
  transactions; a crash mid-sequence could strand half-deleted state.
  Now one transaction (filesystem cleanup stays outside).

### Upgrade

* Server: `pip install --upgrade millet-pipeline vezir` (millet first),
  restart the service, run `vezir doctor`.

## 0.10.1 — refresh on the upload path (fix "session expired" after recording)

0.10.0 refreshed silently on the session-*polling* path but **not on
upload**: the uploader used its own HTTP calls with a fixed token snapshot,
so a `401` at upload time (after the 60-min access token lapsed during a
recording) surfaced as "session expired — sign in again" even though a
valid refresh token was stored.  This makes uploads refresh like everything
else.

### Fixed

* **Uploader refreshes on 401.** `upload_resumable`, `upload`, and
  `upload_multi` now take a `refresh_cb` and, on a `401`, silently rotate
  the session (`POST /api/auth/refresh`) and retry once with the new token
  before failing.  The record-screen wires this callback, so an upload
  after a lapsed access token "just works".
* **`app.token` stays in sync after a silent refresh.** `VezirClient` gained
  an `on_token_refreshed` hook; the TUI app uses it so a token rotated by
  the polling path is propagated to `app.token` (which the uploader reads) —
  previously the rotated token lived only inside the API client and the
  upload path kept using a stale one.
* **In-TUI re-auth now stores the refresh token.** `apply_reauth_session`
  persisted only the access JWT, so a re-login via the modal dropped the
  refresh token and forced another manual login ~an hour later.  It now
  persists `refresh_token` + `refresh_expires_in`.
* **No more spurious "session expired" warning before recording.**
  `_warn_if_session_expiring` checked the 60-min access-token expiry; it now
  skips the warning whenever a refresh token is stored (silent refresh will
  cover the lapse) and only warns for legacy/pre-0.10.0 sessions.

### Changed

* **Reauth modal is terminal-friendly.** The nostr re-auth screen now renders
  a scannable QR for phone signers and clarifies that a same-device signer
  (nsec.app) just needs approval — no copying the `nostrconnect://` URI.
  Copy fixes the confusing "scan / paste" dead-end when neither was possible.
* Shared refresh logic extracted to `api.refresh_active_session()` so the
  client and uploader refresh identically.

### Tests

* +9 tests: `test_uploader_refresh.py` (resumable / one-shot / multi refresh
  on 401, and no-retry when refresh unavailable) and `test_client_api.py`
  (`on_token_refreshed` hook fires, `refresh_active_session` rotate + persist,
  none-without-token).

## 0.10.0 — rotating refresh-token sessions (no more 24h forced logout)

### Added

* **Rotating refresh-token sessions.**  An interactive login (nostr /
  Google) now returns a **pair**: a short-lived access JWT (default 60 min)
  reused as `Authorization: Bearer` exactly as before, plus a rotating
  **refresh token** (`vzrt_…`).  The client silently exchanges the refresh
  token for a fresh pair when a request 401s, so an actively-used session
  stays signed in without a new signer prompt / Google device grant — fixing
  the daily forced logout.  Follows RFC 9700 (OAuth 2.0 Security BCP,
  Jan 2025) / OAuth 2.1.

  * New endpoint `POST /api/auth/refresh` — `{"refresh_token": "vzrt_…"}` →
    a new pair; the presented token is single-use.  Rate-limited on the
    login bucket.
  * New endpoint `POST /api/auth/logout` — revokes the caller's own session
    family (self-serve).  New admin endpoints
    `GET /api/auth/sessions`, `POST /api/auth/sessions/{sid}/revoke`,
    `POST /api/auth/sessions/revoke-all`.
  * New `sessions` table (registered migration `0.10.0-sessions`, idempotent
    and additive) — the first server-side per-session revocation Vezir has
    had; previously an access JWT could only be invalidated by rotating the
    whole `.session-secret`.
  * **Reuse detection**: a session is a token *family*; replaying a consumed
    refresh token revokes the entire family and logs a security event.  A
    one-generation grace window (`prev_refresh_hash`) tolerates a legitimate
    client whose rotation response was lost.
  * **Bounded lifetime**: refresh tokens expire on idle (default 7 days,
    reset each rotation) and on an absolute cap from creation (default
    30 days), after which a full re-login is required.
  * New env vars `VEZIR_ACCESS_TTL`, `VEZIR_REFRESH_IDLE_TTL`,
    `VEZIR_SESSION_MAX_TTL` (all optional, safe defaults).
  * Client: `VezirClient` transparently refreshes-and-retries once on a 401
    and persists the rotated pair to `teams.json`.  New CLI `vezir logout`;
    operator `vezir session list` / `vezir session revoke`.  Refresh tokens
    are stored hashed server-side (SHA-256) and `0600` client-side, the same
    posture as `vzr_` tokens.
  * Backward-compatible: `session_jwt` is still returned (aliased to the
    access token); `vzr_` machine tokens and pre-refresh clients are
    unaffected and degrade to the existing re-login-on-401 behavior.

### Tests

* 804 passing (was 780 in 0.9.0).  +24 refresh-session tests:
  `test_session_refresh.py` (create/rotate, single-use refresh, reuse-
  detection family revocation, idle + absolute-cap expiry, revoke/revoke-all,
  the `/api/auth/refresh` + `/api/auth/logout` endpoints), `test_client_api.py`
  (transparent refresh-and-retry, rotated-pair persistence, no-retry without a
  refresh token), and `test_reauth.py` (refresh-token storage / read /
  rotation / preserve-on-none).  The strict mypy allowlist gains
  `vezir/server/sessions_auth.py`.

## 0.9.0 — multiple audio files as one meeting

### Added

* **Multi-audio meetings.**  A single meeting can now be uploaded as several
  audio files (e.g. a batch of Telegram voicenotes saved as separate `.ogg`
  files).  The files are concatenated, in filename order, into one continuous
  recording on the server before transcription — so the team gets one
  transcript, one summary, and one PDF.

  * New endpoint `POST /upload/multi` — multipart with repeated `audio`
    fields.  Each part is magic-validated, stored as
    `sessions/<id>/<id>.part-NNN<ext>` in upload order, and the aggregate size
    is capped by `VEZIR_MAX_UPLOAD_BYTES`.  All parts must share one audio
    type.  Enqueues a single job with the new `multi_audio` flag.
  * New `jobs.multi_audio` column (idempotent additive migration; legacy rows
    default `0`).
  * Worker step 0: `_merge_multi_audio()` stitches the part files with
    ffmpeg's concat demuxer (`-c copy`; re-encode-to-Opus fallback) into the
    canonical `<id><ext>` before `millet transcribe`.  Idempotent and
    safe to re-run after a restart.
  * New CLI `vezir upload-multi <files...>` / `--dir <dir>` — orders by
    filename, de-dups, and uploads as one meeting.  Mirrors `vezir upload`
    options (`--title/--preset/--auto-label/--sync/--personal/--wait`).
  * New client `uploader.upload_multi()` — whole-batch retry; a `404/405`
    surfaces a clear "server requires vezir >= 0.9.0" message.

### Changed

* **`millet transcribe <dir>` no longer silently transcribes only the first of
  several audio files.**  When a directory holds more than one audio file it
  now errors with the file listing instead of quietly dropping the rest of the
  meeting (millet-pipeline change, coordinated with this release).  A single
  file in a directory still resolves as before — which is what Vezir relies on
  after the worker merges the parts.

### Tests

* 780 passing (was 766 in 0.8.13).  +14 tests: `/upload/multi` ordering,
  magic-validation per part, mixed-type rejection, aggregate size cap, the
  `multi_audio` flag round-trip, `vezir upload-multi` filename ordering / dir
  expansion / empty-input error, and the worker merge (order, single-part
  rename, no-op, re-encode fallback, hard-fail).

## 0.8.13 — don't route to needs_labeling on a spurious tiny REMOTE

### Fixed

* **A single near-empty, noisy `REMOTE` (or raw `SPEAKER_n`) repeatedly sent
  sessions to `needs_labeling`.**  In practice every session picked up one
  REMOTE with a handful of seconds of backchannel ("Thank you.", "Fine.") or
  heavily distorted noise that voiceprint never matched; that lone placeholder
  forced a human labeling round even when the real conversation was fine.

  `_has_unresolved_speakers()` now ignores an unresolved raw speaker
  (`YOU`/`REMOTE`/`REMOTE_N`/`SPEAKER_N`) that is *tiny* — at or below
  `VEZIR_TINY_SPEAKER_MAX_SECONDS` (default 5.0 s) of speech **and**
  `VEZIR_TINY_SPEAKER_MAX_SEGMENTS` (default 3) segments.  A session routes to
  `needs_labeling` only when a *substantial* speaker is still unlabeled.
  `_speaker_resolution()` (used by `vezir relabel`) applies the same rule so
  reporting stays consistent.

  This is the service-layer safety net for the millet-side `0.12.15`
  `absorb_tiny_speakers()` fix; it takes effect immediately for new sessions
  and, via `vezir relabel`, clears already-stuck sessions.  Two new env vars
  (`VEZIR_TINY_SPEAKER_MAX_SECONDS`, `VEZIR_TINY_SPEAKER_MAX_SEGMENTS`) let
  operators tune the noise threshold.

## 0.8.12 — remove sessions from a team

### Added

* **Delete a session.**  An admin or the session's original uploader can now
  permanently remove a session and its on-disk artifacts.
  * New endpoint `DELETE /api/sessions/{id}`.  Authorization: the server-wide
    admin token bit **OR** `row.github == caller` (the original uploader).
    A same-team non-owner non-admin gets **403**; a caller from another team
    gets **404** (existence-hiding, matching `_enforce_team_visibility`).
  * New `queue.delete_session()` — hard delete, modeled on `delete_team`:
    removes the `jobs` row, `session_teams` rows, the on-disk
    `sessions/<id>/` directory, and the `logs/<id>.log` file.
  * New CLI `vezir session rm <id>` — confirms first (`--yes`/`-y` to skip);
    talks to the server over HTTP.
  * New TUI action: `ctrl+d` on the session-detail screen (plus a `[^d]
    Delete` button) shows a confirm modal before deleting.
  * Client API: `VezirClient.delete_session()` + a new `_delete` plumbing
    helper.
* **Local-only delete (documented limitation).**  Deletion does not un-sync:
  artifacts already pushed to the team's git remote remain.  The response
  carries a `warning` when the session looks synced; remove the git copy
  manually if needed.

## 0.8.11 — accept MP3 uploads

### Added

* **MP3 audio uploads.**  The service now accepts `.mp3` alongside `.wav` and
  `.ogg`, end-to-end:
  * Server (`uploads.py`): `.mp3` added to `ACCEPTED_EXTS`; `audio/mpeg` and
    `audio/mp3` added to the Content-Type allowlist.  Magic-byte validation
    accepts either an ID3v2 tag (`ID3`) or a raw MPEG frame sync
    (`0xFF`, high-3-bits-set) — MP3 has no single fixed prefix — on both the
    one-shot and resumable (tus.io) paths.
  * Client (`uploader.py`): `.mp3` added to `ACCEPTED_AUDIO_EXTS` and the
    Content-Type map.  The WAV-only pre-upload compression step no-ops on MP3,
    so MP3 uploads untouched.
  * TUI file picker (`record_screen.py`): `.mp3` shown and selectable.
  * Server-side audio discovery for speaker-clip extraction (`labels.py`) and
    audio cleanup (`worker.py`) now include `*.mp3`.
* Decoding already worked — millet's audio loader is ffmpeg-backed.  Native
  MP3 *discovery* in millet session directories ships in millet-pipeline
  0.12.13 (companion release).
* Tests: added one-shot MP3 accept (ID3 + frame-sync variants), spoofed-MP3
  rejection, and resumable MP3 accept.

## 0.8.10 — don't attempt sync for teams without a git remote

### Fixed

* **Sessions for teams with no `sync_remote` ended in `sync_failed` /
  `sync_error`, even though no sync should be attempted.**  The worker's sync
  gate only checked `VEZIR_SKIP_SYNC` and the per-job `sync_enabled` flag — it
  never checked whether the team actually has a git remote.  For a remote-less
  team, `_resolve_team_sync_config` fell through to the operator's personal
  `~/.config/meet/sync_config.json`, which on a typical install holds millet's
  placeholder `https://example.com/global.git`; millet then tried to clone it
  and failed.  Affected every team without a `sync_remote` (only `blink` had
  one configured).

  Now:
  * New `meet_runner.team_has_sync_target(team_id)` — True only when a real,
    team-scoped target exists (per-team override file, non-empty
    `team.sync_remote`, or the legacy global `~/vezir-data/sync_config.json`).
  * The worker (main pipeline, post-label finalize, summary-retry re-sync)
    skips sync entirely for remote-less teams and keeps the session
    **`done`, local-only, with no `sync_error`** — not a failure.
  * `_resolve_team_sync_config` **no longer falls back** to the operator's
    personal `~/.config/meet/sync_config.json` for team jobs (it could only
    ever supply a non-team-scoped — and here placeholder — remote).
  * `POST /session/{id}/sync` now returns **409** with a clear message when the
    team has no remote, instead of queueing a job that would fail.

## 0.8.9 — in-TUI re-auth + session-expiry warning (no more CLI round-trip on 401)

### Added

* **Sign in again from inside the TUI when your session expires.**  The ~24h
  session JWT expiring used to mean: the upload failed with a generic 401, and
  the only fix was to quit the TUI, run `vezir login` in a shell, then re-run
  `vezir upload <path>` (a non-copyable path from the TUI).  Now an expired
  upload is recognized as a 401 and opens an in-TUI re-auth modal
  (`ReauthScreen`): sign in with **nostr (NIP-46)** or **Google** without
  leaving the TUI.  On success the new session is persisted and re-bound in
  memory, and **the failed upload is retried automatically** — no restart.
  Bound to `^g` on the record pane (also offered automatically on a 401).

* **Proactive session-expiry warning.**  Login now stores the session's expiry
  (`expires_at`) in `teams.json`; the record pane warns when the session is
  expired or expiring within 30 minutes, so you can `^g` re-auth *before*
  recording/uploading instead of discovering it after.

### Fixed

* The upload path now classifies HTTP 401 specifically (previously every
  failure surfaced the same generic "Retry with: vezir upload …" hint, never
  the auth-aware path).

## 0.8.8 — worker recovers jobs orphaned by a restart/crash

### Fixed

* **A job interrupted mid-transcription got stuck in `transcribing` forever.**
  The single worker claims jobs with `claim_next()`, which only selects
  `status = 'queued'`.  If the service was restarted (e.g. for a deploy) or
  crashed while `millet transcribe` was running, the millet subprocess was
  killed and the job was left in `transcribing` — a state no code path ever
  re-claims.  The session showed "transcribing" indefinitely even though no
  work was happening; the uploaded audio was intact on disk but never
  re-processed.

  The worker now runs **`_recover_orphaned_jobs()` at startup** (after the DNS
  warmup, before the poll loop): any job still in an in-progress state
  (`transcribing` / `summarizing` / `syncing`) is reset to `queued` and logged
  loudly, so the poll loop re-claims and replays it.  This is safe under the
  single-writer model — at startup the worker isn't processing anything, so an
  in-progress row is by definition orphaned — and `millet transcribe` is
  idempotent (it re-runs from the audio and overwrites artifacts).

  New `queue.requeue_orphans()` helper performs the atomic reset and returns
  the affected job ids.

## 0.8.7 — TUI import picker lists all recordings (fixes "only one recording")

### Fixed

* **TUI Import picker (`^u` / Upload) showed only a single recording and
  couldn't navigate.**  `ImportScreen` mounted a `DirectoryTree` rooted at
  `last_import_dir`, which the previous import persisted as the *leaf session
  directory* of the file just picked (e.g.
  `~/vezir-meetings/<team>/meeting-YYYYMMDD-HHMMSS/`).  A `DirectoryTree`
  cannot navigate above its root, so the picker was trapped showing the one
  `.ogg` in that folder.

  The picker now defaults to a **flat, scrollable, newest-first `OptionList`
  of every recording under `~/vezir-meetings/`** (all teams), labeled
  `<team>/<session>  ·  <size>  ·  <date>` and sorted by the
  `meeting-YYYYMMDD-HHMMSS` timestamp.  Both `.ogg` and `.wav` files are
  listed.  Pressing **`b`** toggles a "browse files" `DirectoryTree`
  fallback (rooted at the last browsed dir / recordings base / `~`) for
  importing an arbitrary audio file from elsewhere.  The first row is
  pre-highlighted so **Enter** selects immediately.  `last_import_dir` is no
  longer used to root the primary view (only the browse fallback honors it).

## 0.8.6 — `vezir upload` sends X-Team-Id (fixes 400) + `--team` flag

### Fixed

* **`vezir upload <file>` failed with HTTP 400 Bad Request.**  The
  after-the-fact CLI upload resolved credentials via the team-less
  `config.server_url()` / `config.client_token()` and called
  `uploader.upload()` without `team_id=`, so the request carried **no
  `X-Team-Id` header**.  Every team-scoped endpoint has required that header
  since 0.7.0 — a missing one is a hard 400 (`require_team_context`), which
  fires *after* the body streams, hence "uploads to 100% then 400".
  `vezir scribe` / TUI were fixed in 0.7.2 but the `upload` command was
  never updated (regression-by-omission).

  `upload_cmd` now resolves the active team (via `resolve_credentials()` —
  env `VEZIR_TEAM_ID` → teams.json active → client.json) and threads
  `team_id` into the uploader, so `X-Team-Id` is sent.  It also **prefers
  the resumable endpoint** (with one-shot fallback), matching `scribe`.
  When no team can be resolved it now **fails fast with a clear message**
  instead of letting the server 400.

### Added

* **`vezir upload --team <slug|id>`** — target a specific team (resolved
  against `teams.json`; an unknown slug is passed through and resolved
  server-side).  Parity with the other team-scoped commands.

## 0.8.5 — fresh-Mac onboarding: Python cap, doctor recorder preflight, CI publish

Follow-up to the first-teammate Mac onboarding pain (k9ert, then
destinysmart, who lost an evening to undocumented install blockers).
Most of those were the empty-`_bin/` packaging bug (fixed in millet-record
0.4.3/0.4.4) — this release closes the remaining software gaps.

### Fixed

* **`requires-python` capped at `>=3.10,<3.14`.**  `brew install python3`
  on a fresh Mac gives 3.14, for which `coincurve` (via the `[nostr]`
  extra) has no wheel — the install fell back to a failing source build.
  pip/pipx now select 3.13 or refuse with a clear message instead.  Relax
  once coincurve ships cp314 wheels.

### Added

* **`vezir doctor` macOS recorder preflight.**  On Apple Silicon, `doctor`
  now shells out to `millet check` and reports ffmpeg, the bundled
  `meet-record-mac` sidecar, and Microphone/System-Audio permission status
  as a single check — plus a Gatekeeper-quarantine hint
  (`xattr -d com.apple.quarantine …`) when the sidecar resolves but won't
  run.  Advisory (warn, never error).

### Changed

* **Pin `millet-record>=0.4.4`** so a fresh install gets the recorder wheel
  that actually bundles the macOS binary (and is itself capped at <3.14).
* **CI: tag-triggered PyPI publish via Trusted Publishing.**  New
  `release.yml` builds the wheel + sdist and publishes on a `v*` tag (OIDC,
  no stored token), replacing the manual `twine upload` step.

## 0.8.4 — quiet false-positive token/TLS warnings for identity sign-in

After a `vezir login` (Nostr/Google), the client emitted warnings written
for the old `vzr_`-token + internal-CA world. They were misleading, not
errors — the JWT bearer and public HTTPS cert work fine. This release
teaches the client and `vezir doctor` to recognise the 0.8.x setup.

### Fixed

* **`vzr_` token warning no longer fires for session JWTs.**
  `vezir scribe` / `upload` / `pull` printed
  `token does not start with 'vzr_'` even though a session JWT is the
  correct bearer. `config.validate_token_format` now recognises a JWT
  (`a.b.c` with an `eyJ` payload prefix, matching the server's fast-path)
  and skips the opaque-token heuristics.
* **`vezir doctor` understands session JWTs.** Reports
  `token is a session JWT (identity sign-in)` instead of the spurious
  `vzr_` warning.
* **`vezir doctor` no longer warns about `SSL_CERT_FILE` on public-cert
  servers.** A missing `SSL_CERT_FILE` / `VEZIR_CADDY_ROOT_CERT_PATH` is
  normal when the server uses a public (Let's Encrypt) cert; the no-cert
  case is now an informational line, not a warning. (Set those vars only
  for an internal-CA server, e.g. Caddy.)

### Notes

* Purely diagnostic wording — no behaviour change. Clients and servers
  work identically against any vezir ≥ 0.8.0; these were misleading
  warnings, not failures.

## 0.8.3 — Google device-flow DNS resilience

### Fixed

* **`/api/auth/google/device/start` retries transient DNS/network errors.**
  On a host with flaky DNS, the first call to Google's device-code endpoint
  could fail to resolve and surface as `502 "could not reach Google to start
  sign-in"` (it worked on the next tap).  The POST is now retried with
  backoff (same `_is_transient_network_error` classifier as the 0.8.1
  JWKS/token path); only a genuine network failure after all retries returns
  502.
* **`device/poll` token exchange is now also retry- and
  unreachable-tolerant.**  A DNS/network failure reaching Google's token
  endpoint during polling returns **202 `authorization_pending`** (keep
  polling) instead of a hard 502, so a mid-flow blip no longer aborts the
  sign-in.

## 0.8.2 — security hardening + Google prefill

Addresses a security audit of the 0.8.x auth code plus a Google sign-in
prefill fix.

### Security

* **NIP-98 replay protection (H1).**  A valid login event could be replayed
  within its freshness window (~180 s) to mint a second session JWT.  The
  server now keeps an in-memory TTL set of consumed event ids and rejects
  any reuse (401).
* **Header-injection-resistant login URL (H2).**  `_login_url` reconstructed
  the NIP-98 `u`-tag target from `X-Forwarded-Proto` / `Host`, which a
  caller reaching uvicorn directly could spoof.  Set **`VEZIR_PUBLIC_URL`**
  (or `server.json` `public_url`) and the server validates against that
  fixed base instead of the request headers.  **Set this in production.**
  (Falls back to the old header behavior when unset, for local/dev.)
* **Exact Google domain match (H3 hardening).**  The Workspace-domain check
  now compares the email's domain part exactly (`rsplit("@",1)`) instead of
  a suffix `endswith`, removing any lookalike/subdomain ambiguity.  (The
  allowlist already gated this; the change tightens defense-in-depth.)
* **`/health` no longer discloses `data_dir` (M5).**  The unauthenticated
  endpoint returns only `status` + `version`.
* **Loud warning when rate limiting is disabled (M4).**  Startup logs a
  warning if `VEZIR_DISABLE_RATELIMIT` is set, so a CI flag can't silently
  remove brute-force protection in prod.
* **DST-correct timestamp parsing.**  `_parse_iso` (token expiry) and the
  `doctor` / `cli` siblings now use `calendar.timegm` (true UTC) instead of
  `time.mktime(...) - time.timezone`, which was off by the DST offset for
  part of the year.

### Fixed

* **Google sign-in prefill.**  Google's device-code endpoint returns only a
  bare `verification_url`; the server now **synthesizes**
  `verification_url_complete` (`…/device?user_code=<CODE>`) so clients can
  open a pre-filled verification page instead of making the user type the
  code.  (Pairs with vezir-android 0.6.1+.)

### Deferred (from the audit)

* JWT per-token revocation (a feature; the session secret is the current
  nuclear option), session-secret hot-rotation, NIP-46 "ack" tightening,
  and infra LOWs (nftables forward `policy drop`, Caddy CSP, plaintext
  token at rest with 0600).  Tracked in the operator plan.

## 0.8.1 — Google sign-in UX + JWKS/DNS resilience

### Fixed

* **Google device sign-in no longer fails on a transient DNS blip.**  The
  ID-token verification fetches Google's JWKS from `www.googleapis.com`;
  on a host with flaky DNS that first fetch could fail with `Name or
  service not known`, surfacing as a scary terminal `401 "Google ID token
  verification failed"` (it then worked on the next try).  The verify now
  retries transient network/DNS errors with backoff and, if still
  unreachable, returns **202 `authorization_pending`** so the client keeps
  polling — the user never sees the 401.  Adds `clock_skew_in_seconds=10`
  defensively.  Genuine bad-token errors still fail fast (401).

### Added

* **`verification_url_complete` passthrough** from
  `/api/auth/google/device/start`.  Google's device-code response includes
  a URL with the `user_code` embedded; surfacing it lets clients open a
  **pre-filled** verification page so the user doesn't have to read and
  type the code by hand.

## 0.8.0 — nostr (NIP-46) + Google sign-in; VPS public-access front

### Added

* **`vezir login` — nostr sign-in via a remote signer (NIP-46 / Amber).**
  Runs the client-initiated `nostrconnect://` flow (QR + URI shown in the
  terminal); the user approves in their signer (Amber / nsec.app), and the
  server mints a short-lived **session JWT** (~24h) reused as
  `Authorization: Bearer`.  Same `lookup_identity` path as `vzr_` tokens, so
  every existing route works unchanged.  Authorize a key with
  `vezir npub add --npub … --github …`.
* **`vezir login --method google` — Google Workspace sign-in.**  A second
  sign-in method for members who don't use a Nostr signer, via the OAuth 2.0
  **Device Authorization Grant** (works in a TUI / headless box): the client
  shows a short code + URL, the user approves in any browser with their
  `@blinkbtc.com` account, and the server mints the **same session JWT** as
  the nostr path.  The server proxies Google's device + token endpoints so
  the OAuth client secret never leaves the server; the ID token is verified
  (issuer, `email_verified`, `hd`/email domain == allowed domain) and the
  email must be allow-listed.  Authorize with
  `vezir google add --email …@blinkbtc.com --github …`.
* **Server NIP-98 + npub allowlist + JWT issuance** (`nip98.py`,
  `nostr_members`, `nostr_auth.py`); pure-Python NIP-46 client with NIP-04/44.
  Google support adds `google_members` (email allowlist) and `google_auth`
  (device-grant router + ID-token verification).
* **VPS public-access front** (`infra/vps/`, `infra/caddy/`): WireGuard
  (server dials out) + nftables TLS-passthrough, so clients reach the server
  over ordinary outbound HTTPS — works from CGNAT/IPv6-only links — while TLS
  terminates on the server (the VPS sees only ciphertext).  Supersedes
  per-client nvpn/Tailscale for *reaching* the server.

### Fixed (NIP-46 robustness — validated against real Amber)

* **Multi-relay fan-out** across blink-terminal's proven 5 relays: the signer
  publishes responses to the URI relays, so a single relay dropping ephemeral
  kind-24133 events would strand login.
* **Persistent subscription + periodic re-publish + relay reconnect** for
  flaky/restrictive networks.
* **Client clock-skew correction.**  The signer subscribes for our requests
  with a relay-side `since` filter on its clock; an unsynced client clock that
  ran behind made the signer never receive our requests (connect ok, then
  hang).  We learn the offset from the signer's connect-event timestamp and
  stamp requests accordingly (clamped ±300s), plus an NTP-sync preflight
  warning in `vezir login`.

### Fixed (client TLS trust)

* **Client now trusts the public *and* internal CA instead of replacing the
  store.**  The public VPS front (e.g. `vezir.twentyone.ist`) serves a
  publicly-trusted (Let's Encrypt) certificate, but the client honored
  `SSL_CERT_FILE` / `VEZIR_CADDY_ROOT_CERT_PATH` by pointing httpx at *only*
  the Caddy internal CA — so on boxes where those vars name the internal root,
  public TLS validation failed with `CERTIFICATE_VERIFY_FAILED: unable to get
  local issuer certificate`.  A new `vezir.client.trust.resolve_verify` builds
  an SSL context that seeds the public roots (from certifi, independent of
  `SSL_CERT_FILE`) and *appends* any configured internal CA, so a single
  client validates both the public front and internal `tls internal` hosts.

### Notes

* `vzr_` bearer tokens are retained for machine/CI use.

## 0.7.21 — require millet 0.12.7 (complete in-room speaker fix)

### Changed

* **Requires millet-pipeline >= 0.12.7.**  v0.12.6 began the in-room
  multi-speaker fix but a follow-up (v0.12.7) was needed so the single-source
  path keeps the diarized in-room speakers instead of collapsing them back
  onto YOU/REMOTE by channel energy.  Bump the pin so a fresh install gets the
  complete fix.

## 0.7.20 — offline HF models; require millet 0.12.6 (in-room speaker fix)

### Changed

* **Force `HF_HUB_OFFLINE=1` for the millet subprocess.**  The pyannote
  diarization model is cached locally after first download, but each load
  otherwise makes a network HEAD request to huggingface.co to check
  freshness — which adds latency and noisy retries on a host with flaky DNS.
  The worker now runs millet with `HF_HUB_OFFLINE=1` so it uses the cached
  model directly (faster, no network dependency at diarization time).
* **Requires millet-pipeline >= 0.12.6**, which fixes in-room multi-speaker
  collapse: a stereo recording where several people share the mic (system
  channel silent/duplicate) now falls back to mono diarization and splits the
  in-room speakers instead of merging them into one.

## 0.7.19 — fix incomplete folder on "open folder" (0.7.18 regression)

### Fixed

* **"Open folder" (`f`) could open an artifact-less folder.**  0.7.18 wrote a
  `session.json` stub into the recording dir at upload time so the folder is
  found (no duplicate) — but if the Record-tab auto-download never completed
  (TUI closed or moved off the tab during the minutes the server takes to
  process), the folder was *found but empty of artifacts*, and pressing `f`
  opened it as-is.  Three changes fix this:
  * **`open folder` now self-heals.**  When the found folder is missing
    artifacts the server has, they're downloaded into it (in a worker) before
    it opens — so the folder always opens complete.
  * **`record_uploaded_session` no longer writes the pull manifest.**  The
    manifest means "artifacts downloaded here"; writing it at upload time made
    `vezir pull` skip the session and leave the folder permanently empty.  The
    `session.json` stub (which prevents duplicates) is still written.
  * **`vezir pull` re-pulls incomplete folders.**  A manifest entry pointing
    at a folder that lacks artifacts no longer blocks the download; the
    artifacts are fetched into the existing folder (no duplicate).
* New helpers: `pull.missing_server_artifacts`, `pull._dir_has_artifacts`.

## 0.7.18 — stop persisting raw WAVs; no more duplicate folders on "open folder"

### Changed

* **Raw WAV is no longer kept after compression.**  `vezir scribe` and the
  TUI recorder now compress to OGG/Opus with `keep_wav=False`.  The OGG (opus
  48k, transparent for speech) is the local audio archive and the upload
  artifact; the raw PCM WAV — ~10x larger — was never reused and is dropped
  once the OGG exists.  (Use `millet record` directly if you need raw PCM.)

### Fixed

* **"Open folder" (`f`) no longer creates a duplicate meeting folder.**  A
  local recording was only linked to its server session by a `session.json`
  written at auto-download time; if that never happened (session went to
  `needs_labeling`, or the TUI moved off the Record tab), pressing `f` later
  found no local folder and pulled the artifacts into a *new*,
  differently-timestamped folder (the pull uses the server `created_at`, the
  recording uses the local start time).  Now a minimal `session.json` (plus a
  `.pull-manifest.json` entry) is written into the recording dir **at upload
  time**, so the folder is found and reused.  Auto-download upgrades that stub
  to the full record.  New helper: `pull.record_uploaded_session`.

### Notes

* Operator cleanup performed on muscle alongside this release: 10 pre-existing
  duplicate folders merged (artifacts folded into the recording dir, pulled
  copy removed), and 3.8 GB of stale raw WAVs + ffmpeg logs reclaimed across
  all team meeting dirs.

## 0.7.17 — re-run auto-labeling after voiceprint (re)seeding (vezir relabel)

### Added

* **`vezir relabel` — re-run auto-labeling on already-transcribed sessions.**
  After (re)seeding a team's voiceprint DB, sessions that were processed while
  the DB was empty stay stuck in `needs_labeling` with raw speaker ids (auto-
  labeling only runs once, during the original pipeline).  `vezir relabel
  --team <slug> --all-needs-labeling` (or `--session <id>`, repeatable) re-runs
  `millet label --auto` against the now-populated per-team DB and re-routes
  status: recognized speakers are auto-applied to the artifacts; unrecognized
  ones stay raw so the session remains `needs_labeling` with the known speakers
  pre-filled.  `--no-sync` (default) updates labels/artifacts/status only;
  `--sync` pushes fully-resolved sessions like the main pipeline.  Backed by
  `worker.reauto_label_session(session_id, sync=...)`.

## 0.7.16 — inject meeting title + explicit "sync as" folder override

### Added

* **Session `title` is now injected into `*.session.json`.**
  `ensure_session_json` writes the meeting `title` (read from the job queue)
  so millet v0.12.5's title-aware schedule matching can engage.  Previously
  millet only saw `started_at`, so an ad-hoc titled meeting recorded *inside*
  a schedule window (e.g. a "post-scrum" at 09:03 inside the 06:30–09:30
  standup window) was misfiled as the scheduled meeting — and could overwrite
  the genuine one in the shared folder.  A pre-existing session.json missing a
  title is back-filled on (re-)sync.

* **Explicit "sync as" folder override (end-to-end).**
  * `POST /session/{id}/sync` accepts an optional JSON body
    `{"meeting_type": "<slug>"}`; the value is slugified/validated server-side
    (empty-after-slug → HTTP 422).
  * `meet_runner.sync(..., meeting_type=...)` skips schedule/title detection
    and force-syncs straight into `meetings/<date>_<slug>/`.
  * `worker.finalize_after_labeling(session_id, meeting_type_override=...)`
    threads the override through.
  * Client `api.sync_now(session_id, meeting_type=None)` sends the body.
  * TUI: the **Sync now** action now opens a `SyncAsScreen` dialog
    (pre-filled with the title slug, plus an Auto-detect option and a custom
    folder input).  Empty selection = current auto behavior (back-compat).

### Notes

* Requires millet **v0.12.5** (title-aware matching + collision guard).

## 0.7.15 — default-language passthrough + sync-flow hardening

Pairs with millet 0.12.4.

### Added

* **Per-team default language.**  `build_transcribe_args` passes
  `--default-language` from the team's `sync_config.json`
  (`default_language`) or the global `VEZIR_MILLET_DEFAULT_LANGUAGE`,
  preventing summary-language drift for single-language teams (blink set to
  `en`).

### Fixed

* **Duplicate-folder guard.**  `meet_runner.sync` only falls through to
  `--force --meeting-type` when step 1 was genuinely *Skipped* (no schedule
  match).  A schedule match that merely failed to push no longer
  force-creates a duplicate folder (`_sync_log_shows_skipped`).
* **`sync_failed` status.**  The explicit sync paths (Sync now / post-label
  finalize) set `status=sync_failed` when the push fails — surfaced as a red
  badge — while the main transcribe pipeline keeps `done` + a sync-error
  note.

## 0.7.14 — choose summary language in retry-summary

Pairs with millet 0.12.3.  Lets a user regenerate a session's summary in a
chosen language, saved **alongside** the original auto-detected summary.

### Added

* `POST /api/sessions/{id}/retry-summary` accepts an optional `language`
  (Auto + en/de/fr/es/tr/fa).  The "summary already succeeded" guard is
  relaxed ONLY when a real language override is supplied, so a completed
  session can get an additional-language summary (`auto` is not an override).
* `worker.retry_summary_for_session` passes `language_override` →
  `apply_labels(summary_language=...)`; language-aware success check.
* `_find_artifacts` exposes per-language summaries as `summary_<lang>` and
  resolves the real transcript JSON over frontmatter/autoid sidecars.
* TUI `PresetPickerScreen` gains a language selector; returns
  `(preset, language)`.

## 0.7.13 — fix 'Open folder' for recorded sessions (slug/UUID mismatch)

### Fixed

* Recordings are written under the team **slug**
  (`~/vezir-meetings/<slug>/`), but the TUI looked them up by the server
  team **UUID**, building a nonexistent `~/vezir-meetings/<uuid>/` and
  reporting "No artifacts available".
  * `find_local_session_dir`: global-scan fallback across all team subdirs
    for a `session.json` matching the id (robust to the slug/UUID split).
  * `app.team_slug_for()`: map a server UUID → on-disk slug via cached
    `/api/me` memberships.
  * `detail_screen._resolve_local_dir`: translate `session.team_id`
    UUID→slug before lookup (fixes both `f` open-folder and `d` copy-path).

## 0.7.12 — fix Label Speakers crash + clip fetch for named speakers

Once voiceprint auto-labeling persists matched names into the transcript,
the labeling screen receives real names (e.g. "Juan Pablo") instead of
placeholder ids — which exposed three latent bugs.

### Fixed

* **TUI `BadIdentifier` crash**: widget ids built as `play-{sid}` /
  `input-{sid}` broke on names with spaces.  Now index-based ids with a
  `_row_sid` map.
* **Clip endpoint 400**: `^[A-Za-z0-9_]+$` rejected spaced names.  Now a
  path-safety guard (`_is_safe_clip_id`) + slugified cache filename
  (`_safe_clip_filename`).
* **Fragile clip filenames**: temp/cache names now slug+sha1 derived.

## 0.7.11 — pre-fill recognized speaker names in the labeling screen

### Added

* **Labeling screen now pre-fills voiceprint-recognized names + shows match
  confidence.**  `GET /api/label/{id}` reads millet's `*.autoid.json` sidecar
  and returns `suggested_name` + `confidence` per speaker.  The TUI labeling
  screen pre-fills the name input from the suggestion (even when the
  transcript id is still a raw `SPEAKER_N`) and annotates the row with the
  match confidence, so you only type names for genuinely unknown speakers.

### Fixed

* Pairs with millet v0.12.1, which fixes `label --auto` discarding confident
  matches in the non-interactive worker context — previously every
  multi-speaker meeting with one unmatched speaker landed in the labeling
  screen with all raw ids.

## 0.7.10 — fix sync schedule detection for long meetings

### Fixed

* **`ensure_session_json` used upload time, not recording start.**  The
  ULID's embedded timestamp approximates session creation (≈ meeting end /
  upload), not start.  For a 62-minute standup the 62-min skew pushed the
  meeting 4 minutes outside the ±60-min schedule window → "not a scheduled
  meeting."  Now subtracts the meeting's duration (from frontmatter/transcript
  JSON) to recover the true recording start.

### Changed

* **Dev Standup Daily window widened** 60 → 90 min as defense-in-depth.

## 0.7.9 — tech-debt: FastAPI lifespan + mypy in CI

Code-health release.  No user-facing behavior change.

### Changed

* **FastAPI startup/shutdown migrated to a `lifespan` context manager**,
  replacing the deprecated `@app.on_event("startup"/"shutdown")` handlers
  (which emitted a `DeprecationWarning` on every server start and test
  run).  Same behavior: resumable-upload sweep + worker start on entry,
  worker stop on exit.
* **mypy now runs in CI** on a strict allowlist (it was configured since
  0.6.4 but never actually executed).  Widened the allowlist from 3 to 5
  modules — added `server.app`, `server.ratelimit`, `server.enroll`
  (dropped `client.tui.record_screen`, which had pre-existing strict-mode
  gaps that were never enforced; deferred).  Global mypy config gained
  `ignore_missing_imports` + `follow_imports = "silent"` for unstubbed
  third-party deps.  A few `no-any-return` sites in `config.py` /
  `doctor.py` annotated to pass.

## 0.7.8 — fix resumable upload 429 on larger meetings

A meeting larger than ~36 MB failed mid-upload with
`429 Too Many Requests`. The resumable protocol sends one `PATCH` per
4 MB chunk, but every chunk was counted against the `upload` rate-limit
bucket (capacity 10/min), which is meant to limit *uploads started*,
not *chunks appended*. The 11th chunk got a 429 and the client aborted.

### Fixed

* **Server: the resumable `PATCH /upload/resumable/{id}` chunk endpoint
  is no longer rate-limited.** It's already authenticated,
  offset-validated, and total-size-capped at create time, so a runaway
  client can't write unbounded data. The `upload` bucket stays on the
  creation endpoints (`POST /upload`, `POST /upload/resumable`), which
  is the correct granularity.
* **Client: `upload_resumable` now honors `429` + `Retry-After`.**
  Previously a 429 was an uncaught `HTTPStatusError` that hard-failed
  the upload; now the client waits for `Retry-After` (capped 1–60 s) and
  re-sends the same chunk. Defence-in-depth against any future limiter.

## 0.7.7 — serve the CA cert over HTTP for onboarding

### Added

* **`GET /ca.crt`** — an unauthenticated endpoint that serves the
  server's internal Caddy **public** CA certificate (the same PEM the
  QR enrollment payload embeds, read from `VEZIR_CADDY_ROOT_CERT_PATH`).
  Onboarding teammates can now fetch it over the tunnel with
  `curl -k https://<server>/ca.crt -o vezir-ca.crt` instead of needing
  an SSH login on the server box. Returns 404 when no CA path is
  configured. Safe to serve openly: only the private key is sensitive,
  and it never leaves the server.

## 0.7.6 — TUI auto-discovers teams + Teams tab

Client-only. No server, schema, or migration changes. Brings the TUI to
parity with the android app: it now shows **every** team you belong to
(from the server's `/api/me`), without needing `vezir team config add`
for each one.

### Added

* **Teams tab** (`ctrl+e`). A visual picker listing every team you
  belong to — auto-discovered from the server, unioned with any
  explicit `teams.json` entries — with the active team marked. Press
  `enter` on a row to switch. New widget `client/tui/teams_screen.py`.
* `VezirTuiApp.all_teams()` merges `/api/me` memberships with
  `teams.json` (config entries win on collision; discovered teams
  inherit the current server/token). `VezirTuiApp.switch_to_team()`
  performs a token-preserving switch.

### Changed

* **`ctrl+t` now cycles every team you belong to**, not just the ones
  in `teams.json`. A single bearer token authorizes all of them; only
  the per-request `X-Team-Id` changes. This fixes the case where the
  switcher showed only manually-added teams.
* Discovered-only team selections are kept **in-memory** for the
  session (the server is the source of truth); `teams.json` is reserved
  for explicit multi-server / multi-token setups and is not auto-written.
* `_refresh_identity()` caches the full membership list on the app
  (`self.memberships`) for the Teams tab and the `ctrl+t` cycle.
* Help screen documents the Teams tab and the new `ctrl+t`/`ctrl+e`
  bindings.

## 0.7.5 — fix TUI team switcher after 0.7.4 UUID migration

Client-only patch. No server, schema, or migration changes. The 0.7.4
migration made the server key team memberships by UUID while clients
still configure teams by slug in `teams.json`; that mismatch broke the
TUI's `^t` team switcher (it showed only one team and refused to
switch) and produced a false-positive `vezir doctor` error.

### Fixed

* **TUI `^t` team switcher.** `_refresh_identity()` matched the
  slug-configured active team against `/api/me`'s UUID-keyed
  membership list, never matched, and silently overwrote
  `active_team_id` with the first membership's **UUID**. That UUID
  then wasn't in `next_team_id()`'s slug list, so cycling snapped back
  to the same team. Now memberships match on **slug OR uuid**
  (each membership carries both), and the fallback adopts the
  membership's **slug**, keeping `active_team_id` consistent with
  `teams.json`.
* **`vezir doctor` false 403.** The C9 token-membership check compared
  the configured slug against the UUID-only membership list and
  reported `configured team_id=... is not in this token's
  memberships`. It now matches on slug OR uuid and shows the slug in
  the team summary. Logic extracted to `_check_token_membership()`.

### Changed

* `config.next_team_id()` resolves a UUID `current` back to its slug
  (via any `uuid`/`team_id` field on a team entry) before cycling, so
  a stray UUID can't wedge the switcher.

## 0.7.4 — team UUID keys + slug rename

Teams now have a stable UUID primary key; the slug becomes a mutable
display identifier.  This makes `vezir team rename` a pure single-row
update instead of a cascade across jobs, memberships, on-disk dirs, and
in-flight sessions.

### Changed

* **`teams.id` is now a UUID**; new `teams.slug` column holds the
  mutable display slug (unique).  FK discriminators (`jobs.team_id`,
  `memberships.team_id`, `session_teams.team_id`) and on-disk
  `teams/<id>/` dirs are all keyed by the uuid.
* **`X-Team-Id` carries the uuid** (a slug is still accepted and
  resolved, for curl/debug).  `/api/me` memberships return both
  `team_id` (uuid) and `slug`.  Clients key on the uuid, so a rename
  never orphans them.
* `queue` functions (`create_team` returns the uuid; `get_team`,
  `is_member`, `set_job_team`, `enqueue`, `delete_team`,
  `list_recent`, membership + session-share helpers) accept slug OR
  uuid and resolve to the uuid internally.

### Added

* **`vezir team rename --id <slug|uuid> --new-slug <slug>`** and
  `PATCH /admin/teams/{id}` accepting `slug`.  Pure DB update — no
  cascade.
* `queue.resolve_team_uuid()` + `queue.rename_team_slug()`.

### Migration

* `migrate_0_7_4` assigns a uuid to every legacy slug-keyed team,
  rewrites child FKs, and renames `teams/<slug>/` → `teams/<uuid>/`.
  Idempotent (keyed on the `slug` column being populated).  FK-safe
  under `foreign_keys=ON`: inserts the new uuid row, repoints children,
  then deletes the old row (no in-place PK mutation).

### Fixed

* `_seed_teams` existence check now matches on **slug** as well as id,
  so a team pre-created in the v0.7.4 (uuid) model is no longer
  double-seeded as a duplicate slug-keyed row.

### Tests

* `test_team_uuid_rename.py` (NEW): uuid assignment, slug↔uuid resolve,
  rename preserves uuid + data, collision/bad-slug rejection, the
  migration's FK rewrite + dir rename, CLI rename.
* Existing team/voiceprint/label/session-move tests updated to expect
  uuid-keyed `team_id` and slug-keyed display.

## 0.7.3 — resumable uploads (tus.io subset)

Adds a resumable upload path so a dropped transfer resumes from the
last byte the server received instead of restarting at zero — directly
mitigating the documented nvpn/Tailscale tunnel flakiness.

### Added

* **Server: tus.io 1.0 subset** on `POST /upload/resumable` (create),
  `HEAD /upload/resumable/{id}` (offset), `PATCH /upload/resumable/{id}`
  (append).  Staging lives in `~/vezir-data/uploads-tmp/` as
  `<id>.part` + `<id>.meta.json`; on completion the file is assembled
  into `sessions/<session_id>/` and enqueued exactly like the one-shot
  path.  Same auth (`X-Team-Id`), magic-byte validation, size cap, and
  rate-limit bucket.  Ownership-scoped 404s (a caller never learns
  another user's/team's upload exists).  The legacy `POST /upload`
  endpoint is unchanged for older clients.
* **Abandoned-staging sweep** — `.part`/`.meta.json` older than 24h are
  swept at startup and hourly from the worker loop.
* **Desktop client** — `uploader.upload_resumable()` (PATCH loop with
  HEAD-based offset resync on network error) plus
  `uploader.server_supports_resumable()` probe.  `vezir scribe` and the
  TUI prefer resumable and fall back to one-shot on older servers.

### Fixed

* **`uploader.upload()` now sends `X-Team-Id`** (new `team_id=` param).
  The desktop one-shot path previously sent only `Authorization`, which
  would 400 against a v0.7.0+ server.  `vezir scribe` resolves the
  active team via `resolve_credentials()` and passes it through.

### Tests

* `test_resumable_upload.py` (NEW) — create/HEAD/PATCH happy path,
  resume-after-drop, offset-mismatch 409, overshoot 413, bad-magic 415,
  cross-user 404, TTL sweep.
* `test_uploader_resumable_e2e.py` (NEW) — desktop client against a live
  uvicorn server (happy path + legacy X-Team-Id header).

## 0.7.2 — SQLite hardening: WAL + token store migration

Two database-layer hardening changes. No API or client-visible
behavior change; both are operational/correctness improvements.

### Changed

* **SQLite now runs in WAL mode** with a 5s `busy_timeout`,
  `synchronous=NORMAL`, and `foreign_keys=ON`.  Applied on every
  connection in both `queue._conn` and `migrations._conn` via a new
  `queue._apply_pragmas` helper.  WAL lets readers and the single
  writer proceed without blocking each other and makes concurrent
  multi-process access (server + a `vezir` CLI invocation) safe
  instead of raising `database is locked`.  `foreign_keys=ON`
  promotes the schema's `REFERENCES` clauses
  (`memberships`/`session_teams` → `teams`, `session_teams` →
  `jobs`) from documentary to enforced; the existing
  `delete_team` cascade already deletes child rows before parents,
  so enforcement is order-compatible (verified against the full
  suite).

* **Token storage moved from `tokens.json` to a `tokens` table in
  `vezir.sqlite`.**  The old flat file did an unlocked full-file
  read-modify-write, which had a lost-update race on concurrent
  `issue`/`revoke` and on the `last_used_at` touch that fires on
  nearly every authenticated request.  Storage now goes through
  `queue._conn` (global `_LOCK` + WAL), so each read-modify-write
  is atomic.  Public `auth` signatures are unchanged.

### Migration

* New one-shot `migrate_0_7_2` (`0.7.2-tokens-to-sqlite`) imports
  every row from `tokens.json` into the `tokens` table
  (`INSERT OR IGNORE` on `token_hash`), then renames the file to
  `tokens.json.migrated` as a backstop.  Idempotent; runs after the
  v0.7.0 memberships migration (which strips `team_id` from the
  file first).  Audit log at
  `~/vezir-data/logs/migration-0.7.2-tokens-to-sqlite.log`.

* **`vezir doctor`** S1/S2/S3 repointed to the `tokens` table; S1
  now warns if a live `tokens.json` (not the `.migrated` backstop)
  is still present.

### Tests

* `test_queue.py`: pragma assertions, FK enforcement, concurrent-
  writer no-lost-update.
* `test_tokens_sqlite_migration.py` (NEW): JSON import, idempotence,
  no-file no-op, end-to-end issue→lookup, concurrent issue.
* Existing token/doctor/permissions tests adapted to the SQLite
  store.

## 0.7.1 — fix: `vezir token enroll` missing CA cert in QR

### Fixed

* **`vezir token enroll` now embeds the Caddy CA cert in the QR
  payload** when ``VEZIR_CADDY_ROOT_CERT_PATH`` is set.  v0.7.0
  shipped with the inline comment promising "automatic CA cert
  embedding" but the actual ``build_payload()`` call passed no
  ``ca_pem`` argument, so every QR came out as a v1 payload.  The
  Android app then couldn't verify the self-signed Caddy TLS cert
  and every API call failed with ``CertPathValidatorException:
  Trust anchor for certification path not found``.

  Now matches the v0.6.x ``/admin/enroll`` HTML handler behavior:
  calls ``_load_caddy_root_cert()`` and passes the result through
  to ``build_payload()``.  When the env var is unset (or the file
  unreadable) we still fall back to v1 cleanly.

## 0.7.0 — JSON-only API, membership-based teams

This release is a **breaking** simplification of the v0.6.x team
model.  Tokens no longer carry a team scope; instead every human
has explicit memberships in zero or more teams, and every team-
scoped HTTP request supplies an `X-Team-Id` header.  The HTML
dashboard, login flow, and Tkinter desktop widget are removed --
all interaction is now via JSON API (TUI, Android, CLI).

Net diff: **-3,042 lines** of HTML/Tk client code, **+812 lines**
of memberships + auth rewrite (29 files changed total).

### Breaking changes

* **HTML dashboard removed.**  `/`, `/s/{id}`, `/login`, `/logout`,
  `/admin/enroll`, static assets -- all gone.  The `vezir/web/`
  directory and `vezir.server.login` / `vezir.server.web_sessions` /
  `vezir.server.templating` modules deleted.  `jinja2` is no longer
  a server dependency.  See `vezir tui` for the replacement.
* **Tkinter `vezir gui` + `vezir scribe-widget` commands removed.**
  ~1,500 lines of Tk-based UI deleted (`vezir/client/gui.py`,
  `scribe_widget.py`, and the bundled logo PNGs).  Use `vezir tui`
  (Textual-based) instead -- it has full parity except the
  always-on-top floating record widget.
* **Tokens no longer carry `team_id`.**  `vezir token issue` lost
  the `--team` flag; tokens identify a human + privilege tier.  Team
  scope per request comes from the `X-Team-Id` HTTP header,
  validated against the new `memberships` table.  One token can
  now operate against every team its handle is a member of -- no
  more re-issuing per team.
* **API: every team-scoped endpoint requires `X-Team-Id`.**  Missing
  -> 400; non-member -> 403; the team itself not existing also
  surfaces as 403 (we deliberately don't distinguish to avoid
  leaking team existence).  `/api/me` and `/health` are the only
  routes that don't.
* **`/api/me` response shape changed.**  Was
  `{github, team_id, team_name, is_admin}`; now
  `{github, is_admin, memberships: [{team_id, team_name, role}, ...]}`.
  Clients use the list to populate a team-picker.
* **`vezir.client.config.resolve_credentials()` returns a 4-tuple.**
  Was `(url, token, source)`; now `(url, token, team_id, source)`.
  Callers that destructure must update.  `VEZIR_TEAM_ID` env var
  added.
* **CLI: `vezir token list --team` and `vezir token revoke --team`
  removed.**  Tokens aren't team-scoped any more; use the new
  `vezir team members <slug>` to see who's on each team.
* **Server endpoints removed:** `POST /api/exchange-code`, `GET
  /login`, `POST /login`, `GET /logout`, `GET /admin/enroll`,
  `POST /admin/enroll`, `GET /` (dashboard), `GET /s/{id}` (session
  detail HTML).  `VezirClient.exchange_code()` removed accordingly.

### Added

* **`memberships` table** (`github, team_id, role, added_at,
  added_by`).  Role is `'admin'` or `'scribe'`; the server-wide
  admin bit on the token is independent.
* **`session_teams` table** (junction, `session_id, team_id`).
  Reserved slot for future cross-team session sharing; not yet
  wired into any UI.
* **`vezir team add-member --team <id> --github <h> [--role
  admin|scribe]`** -- grants a user access to a team.
* **`vezir team remove-member --team <id> --github <h>`** -- revokes
  access.  Tokens are NOT rotated; use `vezir token revoke
  --github <h>` if you also want to rotate.
* **`vezir team members <id>`** -- lists members + role + added_at.
* **Admin HTTP endpoints:**
  - `GET    /admin/teams/{team_id}/members`
  - `POST   /admin/teams/{team_id}/members`
  - `DELETE /admin/teams/{team_id}/members/{github}`
* **`vezir token enroll` now renders the QR directly in the
  terminal.**  Uses segno's half-block ANSI art; no more browser
  hop to `/admin/enroll`.  v2 CA-cert payload still supported
  when `VEZIR_CADDY_ROOT_CERT_PATH` is set.
* **`X-Team-Id` HTTP header** validated by the new
  `auth.require_team_context` FastAPI dependency on every team-
  scoped route.  Cross-team requests return 404 (sessions, label
  page, artifact) or 403 (auth-layer non-member).
* **Migration 0.7.0** -- populates `memberships` from every existing
  token's `team_id` field, then strips the field from `tokens.json`.
  Idempotent; safe to re-run.  Audit log at
  `~/vezir-data/logs/migration-0.7.0-memberships.log`.

### Changed

* `queue.delete_team` cascade now drops memberships + session_teams
  rows instead of revoking tokens.  A human deleted from a team may
  still hold a valid token; revoke explicitly if needed.
* `auth.lookup_full()` -> `auth.lookup_identity()` returning
  `(github, is_admin)`; team is no longer baked in.
* `vezir doctor`: credentials check displays the resolved team_id;
  `/api/me` parse updated for the memberships list; warns if the
  configured `team_id` isn't in the user's memberships.
* TUI title bar still shows the active team; team picker rebuilt to
  use `/api/me`'s membership list rather than locally cached
  `teams.json` only.
* CLI `vezir pull` requires `VEZIR_TEAM_ID` (or a teams.json active
  entry) -- the pull endpoint is team-scoped.

### Removed

* `vezir.server.login`, `vezir.server.web_sessions`,
  `vezir.server.templating` modules.
* `vezir.client.gui`, `vezir.client.scribe_widget` modules.
* `vezir/web/` template + static directory; `vezir/client/assets/`
  PNGs.
* `auth.count_tokens_for_team`, `auth.revoke_all_for_team` --
  memberships replace tokens as the per-team accounting surface.
* `auth.require_bearer_or_cookie`, `auth.require_bearer_or_cookie_full`,
  `auth._resolve_auth`, `auth.COOKIE_NAME` -- bearer-only now.
* `auth.is_admin_in_request` (used only by the dashboard).
* TUI `o` / "Open in browser" bindings on Sessions list and detail
  screens.
* `VezirClient.exchange_code()` client method.

### Android client (separate repo `vezir-android`)

Required follow-on work, NOT shipped with this server release:

* Drop `dashboard_url` / `dashboard_login_url` from the upload
  response model.
* Drop the in-app webview / browser-handoff flow that used
  `vzx_` exchange codes.
* Add `X-Team-Id` to every request the app makes.  Pick the team
  from `/api/me`'s membership list at first launch; persist the
  choice; expose a switcher.
* Update the QR-scan flow: the payload schema is unchanged but
  there's no longer a server-side enrollment page to launch
  alongside.  The desktop operator now uses
  `vezir token enroll --github <h>` in a terminal.

## 0.6.9 — code quality + label screen fixes

### Added

* **Ruff linting** — `[tool.ruff]` config with 7 rule groups (E, F, W, I,
  B, UP, RUF).  42 import-sorting auto-fixes, 15 manual fixes across 46
  files.  Per-file ignores for Textual/FastAPI/tkinter patterns.
* **mypy type checking** — strict mode on 3 well-annotated files
  (`config.py`, `doctor.py`, `record_screen.py`).
* **Structured logging** — `configure_logging()` with JSON file handler
  (`~/vezir-data/logs/vezir.log`, 10 MB rotation, 5 backups).  Console
  format configurable via `server.json` `log_format` key.
* **Client-side logging** — `basicConfig(level=WARNING)` in CLI entry
  point so `vezir.client.*` loggers surface warnings/errors.

### Fixed

* **TUI label prefill** — resolved speaker names (from auto-labeling)
  are now prefilled in the input widgets.  Only unresolved placeholders
  (YOU, REMOTE\_N, SPEAKER\_N) start empty.
* **Label submit timeout** — voiceprint update (pyannote model load +
  embedding inference) moved to background thread; HTTP response returns
  in ~2s instead of 30-60s.  Client read timeout increased to 120s.
* **CI green** — all 411 tests pass across py3.10/3.11/3.12.  Fixed
  env var leakage (VEZIR\_COOKIE\_SECURE, VEZIR\_CADDY\_ROOT\_CERT\_PATH)
  and sys.modules caching in scribe library path test.  Added
  pytest-timeout (60s default).

## 0.6.8 — alternate server URLs for client failover

### Added

* **`server.json` config file** — optional `~/vezir-data/server.json` with
  server-side settings.  First supported key: `alternate_urls` (list of
  fallback URLs for clients when the primary enrollment URL is unreachable).
* **`/api/me` → `alternate_urls`** — response now includes the list of
  alternate URLs from `server.json`.  Android v0.4.3+ clients use this
  for automatic VPN failover (e.g., nvpn → Tailscale).
* **`vezir doctor`: server.json validation** — checks schema if present
  (valid JSON, `alternate_urls` is a list of URL strings).

## 0.6.7 — network resilience (doctor + TUI retry)

### Added

* **`vezir doctor`: TCP tunnel probe** — for private/tunnel IPs (10.x,
  100.x, 192.168.x), probes raw TCP before HTTP health check.  Reports
  "VPN tunnel may not be established" on timeout instead of generic
  "unreachable".
* **`vezir doctor`: nvpn version check** — reports installed nvpn
  version if found on PATH.
* **TUI sessions retry** — `_refresh_worker` retries up to 3 times on
  network errors with 10s delays.  HTTP errors don't retry.  Error
  message includes VPN tunnel hint.

### Fixed

* **Pull directory timestamps** — use local timezone (`dt.astimezone()`)
  instead of UTC, so pulled directories sort alongside local recordings.

## 0.6.6 — audio spectrometer + hybrid sync naming + TUI usability

### Added

* **Audio level spectrometer** — real-time 12-bar Unicode waveform per
  channel (mic + system) during recording, with dB-scaled bars and
  color-coded signal detection (green/yellow/red with 10s silence
  debounce).  Cross-platform data contract (`AudioLevelSample`) for
  vezir-android.  Shared `read_chunk_levels()` utility in `audio.py`.
* **Hybrid sync naming** — `millet sync` first tries schedule-matched
  naming (e.g. `dev-standup-daily`); falls back to title-based
  `--force` naming for unscheduled meetings (e.g.
  `board-meeting-160000Z-GVXGJ0`).  New `config.sync_slug()` for
  repo-convention-friendly slugs.
* **Open folder / Copy path** — `[f]` opens the local meeting
  artifacts directory in the OS file manager (`xdg-open`/`open`);
  `[d]` copies the path to clipboard.  Available on both Sessions
  list and Detail screen.  Auto-pulls artifacts from the server when
  no local folder exists.
* **`find_local_session_dir()`** — maps a session ID to its local
  recording/pull directory (manifest lookup + directory scan fallback).
* 22 new unit tests for spectrometer, sync slug, and pull utilities.

### Fixed

* **TUI Ctrl+Q exit hang** — thread workers now use
  `worker.cancelled_event.wait()` instead of `time.sleep()`, making
  sleep interruptible on app shutdown.  No more stuck terminal or
  `KeyboardInterrupt` traceback after quitting.
* **GUI "Open Dashboard" expired login** — now mints a fresh exchange
  code on click instead of using the stale 60s code from upload time.
* **GUI window resize on signal text change** — fixed-width level
  label prevents the window from growing/shrinking.

### Changed

* Sync meeting-type default changed from `sandbox` to `meeting` for
  untitled/unscheduled sessions.
* Removed deprecated `VEZIR_SYNC_MEETING_TYPE` env var fallback.
* TUI help screen (F1) updated with new keybindings and `vezir pull`.
* README refreshed for v0.6.5 features.

### Tests

122 passing (was 389 in v0.6.4; test files reorganized).


## 0.6.5 — recording path harmonization + vezir pull + TUI back-to-back fix

Consolidates all recording paths under `~/vezir-meetings/<team>/`,
adds `vezir pull` for team meeting artifact sharing, and fixes
back-to-back recording in the TUI.

### Added

* **`vezir pull`** — download meeting artifacts (summaries, transcripts,
  PDFs) from the server into `~/vezir-meetings/<team>/`.  Enables
  team-wide meeting sharing without relying on git sync.
  Options: `--limit`, `--since`, `--session`, `-o`.
* **Auto-download artifacts** — TUI and GUI automatically download
  meeting artifacts into the local recording directory when server
  processing completes (`done` status).
* **`config.recordings_dir(team_id)`** — new unified recording path
  (`~/vezir-meetings/<team>/`), replacing the fragmented
  `~/millet-recordings/` and `~/meet-recordings/` directories.
* **`config.sanitize_title()` / `config.rename_session_dir_with_title()`**
  — recording directories get a `_TITLE` suffix after stop
  (e.g. `meeting-20260526-143041_ABBOARD`).
* **`since` query parameter on `GET /api/sessions`** — enables efficient
  incremental pulls.  Filters to sessions created at or after the
  given ISO date/datetime.
* **`Session.team_id`** field on the client-side `Session` dataclass.
* New modules: `vezir/client/artifacts.py`, `vezir/client/pull.py`.

### Fixed

* **TUI back-to-back recording race conditions** — session generation
  counter prevents stale messages from old recordings clobbering the
  UI state of a new recording.  `is_recording` is no longer set by
  `ServerStatus` messages from poll/upload workers.
* **GUI credential resolution** — `vezir gui` now honors `teams.json`
  (same precedence as TUI/CLI).
* **GUI `_meet_bin()`** — searches for `millet` binary before legacy
  `meet` fallback.

### Changed

* All recording entry points (TUI, GUI, CLI scribe, scribe-widget)
  now write to `~/vezir-meetings/<team>/` instead of the previous
  fragmented paths.  The `VEZIR_RECORD_DIR` env var is still respected
  as an override.  Old recordings in `~/millet-recordings/` and
  `~/meet-recordings/` remain untouched.


## 0.6.4 — vezir doctor

New ``vezir doctor`` command that diagnoses configuration and
environment issues.  Auto-detects whether the local machine is a
server (queue DB exists) and runs additional server-side checks.

### Added

* **``vezir doctor``** — 17 checks across client and server:
  - **Client:** credential resolution summary + source disagreement
    detection, env-var shadow detection (``/etc/environment`` vs
    ``~/.profile``), teams.json schema validation, client.json
    coexistence warning, token format validation, SSL cert
    configuration, file permissions, server connectivity (``/health``),
    token auth (``/api/me``), deprecated env vars.
  - **Server:** orphaned tokens (missing ``team_id``), expired tokens,
    data directory permissions, migration status, millet binary
    availability, per-team voiceprint DB existence, stale jobs.
* **``VezirClient.get_me()``** — new client API helper for
  ``GET /api/me`` (backing ``vezir doctor``'s auth check).
* 30 new tests in ``tests/test_doctor.py``.

### Tests

389 passing (was 359 in v0.6.3).


## 0.6.3 — token revoke ergonomics + first CI workflow

Quality-of-life release: per-device token revocation, team-scoped
token listing, and the project's first GitHub Actions CI gate.

### Added

* **`vezir token revoke` now accepts `--label`, `--token-id`, and
  `--team` filters** (combinable with `--github`).  Lost-phone
  scenario no longer requires nuking every token for a handle and
  re-issuing.  Shows a preview of matched tokens and prompts for
  confirmation unless `--yes` is passed.
* **`vezir token list` gained `--team <slug>` filter and `--show-id`
  flag**.  A `team` column is now shown by default.  The `--show-id`
  column prints a 12-char token id prefix usable with
  `vezir token revoke --token-id <prefix>`.
* **`auth.revoke_by_filter()`** and **`auth.list_tokens()`** server
  helpers backing the CLI surface above.
* **GitHub Actions CI** (`.github/workflows/ci.yml`): ruff lint on
  3.12 + pytest matrix on 3.10/3.11/3.12.  First automated gate on
  the project.
* 23 new tests in `tests/test_token_revoke_filters.py`.

### Fixed

* **Ruff cleanup**: 36 pre-existing lint errors resolved (35
  auto-fixable unused imports / dead f-strings + 1 manual fix for a
  forward-reference `F821` in the TUI app).
* **TUI test `test_personal_toggle_disables_sync`** (was
  `test_personal_checkbox_disables_sync`): test still referenced
  the `#personal` Checkbox removed in v0.4.2; updated to use the
  `#personal-btn` toggle-Button API.

### Tests

359 passing, 2 skipped (was 285 in v0.6.2).


## 0.6.2 — per-team voiceprints + per-team sync + session move + team lifecycle

Completes the multi-team isolation work started in v0.6.0.  After
this release every team owns its own voiceprint training surface, its
own millet sync remote, and can be renamed / deleted as a unit.
Operators can move individual sessions between teams (e.g. when a
recording was uploaded under the wrong scribe token).

### Added

#### Per-team voiceprint DB (Feature A)

* **Per-team voiceprint files**: each team now holds its own
  ``~/vezir-data/teams/<id>/speaker_profiles.json`` instead of
  sharing a single central DB.  The per-job HOME shim symlinks the
  caller's team DB into the millet subprocess.
* Migration ``0.6.2-per-team-voiceprints`` moves the legacy
  ``~/vezir-data/speaker_profiles.json`` under
  ``teams/blink/speaker_profiles.json`` and seeds an empty
  ``teams/twentyone/speaker_profiles.json`` (locked-in decision:
  twentyone starts with a clean training surface).  Idempotent;
  audit log at ``~/vezir-data/logs/migration-0.6.2-per-team-voiceprints.log``.
* New helper: ``vezir.config.team_speaker_profiles_path(team_id)``.
* CLI ``vezir voiceprints seed/list`` gained a required ``--team``
  flag (defaults to the only team when exactly one exists,
  mirroring the ``vezir token issue --team`` UX from v0.6.0).

#### Per-team sync remote (Feature B)

* **Per-team sync wiring**: the worker now reads the team row's
  ``sync_remote`` and ``sync_meeting_type`` columns (added as
  reserved schema slots in v0.6.0) and materializes a per-team
  ``sync_config.json`` that gets symlinked into the per-job HOME
  shim.  Different teams push to different git repos with zero
  operator config changes after ``vezir team set-sync --remote``.
* **B2 escape hatch**: if
  ``~/vezir-data/teams/<id>/sync_config.json`` exists, the worker
  uses it verbatim and ignores ``team.sync_remote``.  Lets ops
  hand-tune millet's full sync config (branch, ssh key, etc.) per
  team without losing it on the next worker pass.  Vezir's
  auto-managed copy lives at
  ``sync_config.materialized.json`` (sibling file, never
  overwrites the operator override).
* Materialization is idempotent: the worker only rewrites
  ``sync_config.materialized.json`` when ``team.sync_remote``
  drifts from the stored value.
* The legacy ``VEZIR_SYNC_MEETING_TYPE`` env var is now a
  deprecation fallback (kept for muscle's existing install; removed
  in v0.7.0).  Set ``team.sync_meeting_type`` via
  ``vezir team set-sync --meeting-type ...`` instead.

#### Session move (Feature C)

* **`vezir session move <id> --to-team <slug>`**: reassigns a
  session to a different team in one DB-row update.
  - Refuses when the destination team doesn't exist (closes a
    silent-orphan hole in ``queue.set_job_team`` from v0.6.0).
  - Refuses when source == destination.
  - Requires interactive confirmation unless ``--yes``.
  - Session artifacts on disk (``~/vezir-data/sessions/<id>/``)
    are team-agnostic so nothing needs to move.
  - **Known limitation**: voiceprint backwash.  Any embeddings
    trained from previously-confirmed labels on this session live
    in the SOURCE team's voiceprint DB.  They stay there; nothing
    is copied to the destination.  Documented in CLI help and
    locked-in policy decision per vezir_plan.md.

#### Team lifecycle (Feature D)

* **`vezir team set-name --id <slug> --name <new>`**: rename a
  team's display name.  Slug rename is intentionally NOT
  implemented (deferred to v0.7.0; would cascade across
  jobs.team_id, the token store, and on-disk dirs, and break
  in-flight web sessions).
* **`vezir team delete --id <slug> [--reassign-to <other>] [--yes]`**:
  - **Default policy: refuse-if-not-empty.**  Errors out if any
    jobs or tokens are scoped to the team.  Operator must
    ``vezir session move`` / ``vezir token revoke`` first.
  - **`--reassign-to <slug>`**: cascade.  Jobs are moved to the
    destination team; tokens are REVOKED (not migrated — the
    destination's members are probably different humans).
  - On success, removes the on-disk ``teams/<id>/`` directory
    (roster, voiceprints, sync_config).
  - No ``--force-purge``; deletion never silently drops jobs.
* Admin HTTP endpoints:
  - ``PATCH /admin/teams/{id}`` now accepts ``name`` (alongside
    the v0.6.0 ``sync_remote`` and ``sync_meeting_type`` fields).
    Uses ``model_fields_set`` so omitted fields no longer
    accidentally clear stored values.
  - ``DELETE /admin/teams/{id}?reassign_to=<slug>`` exposes the
    cascade-delete to admin web clients.  Returns 409 when the
    team is non-empty and ``reassign_to`` is omitted.

### Changed

* ``meet_runner.transcribe / label_auto / sync / run_meet /
  build_home_shim`` all gained a required ``team_id`` parameter
  so the HOME shim's voiceprint and sync_config symlinks resolve
  per-team.  All call sites in ``worker.py`` and ``labels.py``
  updated; ``job["team_id"]`` is plumbed through (with a defensive
  early-error if a job row somehow has an empty team_id).
* ``voiceprints.ensure_db_exists / list_known_names / seed_from``
  all require an explicit ``team_id`` (no more central-DB default).
* App startup now iterates ``queue.list_teams()`` and creates an
  empty DB for any team that doesn't have one, so the HOME shim's
  symlink target always resolves.
* ``vezir status`` output now shows the ``teams/`` dir instead of
  the legacy central voiceprint DB path.
* ``queue.set_job_team`` now defaults to ``require_team_exists=True``;
  the v0.6.0 migration backfill explicitly opts out with
  ``require_team_exists=False`` so seed teams can be inserted in
  the same transaction.

### Compatibility

* **Zero schema changes.**  Everything reuses v0.6.0 columns; only
  on-disk files move (and are moved automatically by the migration).
* **Operator action required**: none for existing single-team
  installs.  The migration auto-routes the legacy voiceprint DB
  to blink.
* **Token compatibility**: unchanged.  No tokens revoked by the
  migration; no token re-issue needed.
* **Web cookies**: unchanged.

### Tests

285 passing (up from 226 in v0.6.1):

* ``tests/test_voiceprints_per_team.py`` (16) — migration moves
  legacy DB into blink, twentyone starts empty, migration is
  idempotent, voiceprints module API requires team_id, HOME shim
  symlinks per-team, CLI ``--team`` flag works.
* ``tests/test_sync_per_team.py`` (10) — meeting-type prefix
  precedence (team row > env > default), sync_config resolution
  precedence (override > materialized > legacy > real), idempotent
  materialization, HOME shim symlinks per-team sync config.
* ``tests/test_session_move.py`` (10) — destination-existence
  check, backfill mode, end-to-end visibility flip, CLI happy
  path + errors, voiceprint-backwash documented limitation.
* ``tests/test_team_lifecycle.py`` (18) — rename happy + validation
  paths, delete policy (refuse-if-not-empty, cascade with reassign,
  reject self-reassign, reject unknown reassign), CLI happy paths,
  admin HTTP endpoints (PATCH name, DELETE with/without cascade,
  admin role required).

Plus updates to:

* ``tests/test_permissions.py`` — voiceprint DB now lives under
  ``teams/blink/``.
* ``tests/test_meet_runner.py`` — ``transcribe`` now takes
  ``team_id``.
* ``tests/test_label_api.py`` — ``_apply_and_finalize`` now takes
  ``team_id`` as a fourth positional arg.

### Operator setup

Existing installs:

```
pip install --upgrade vezir
sudo systemctl restart vezir  # or however you run it
# migration 0.6.2-per-team-voiceprints runs at startup;
# audit log: ~/vezir-data/logs/migration-0.6.2-per-team-voiceprints.log
```

After upgrade, to wire per-team git sync remotes:

```
vezir team set-sync --id blink     --remote git@github.com:org/blink-meetings.git
vezir team set-sync --id twentyone --remote git@github.com:org/21-meetings.git
```

For hand-tuned millet sync configs (branch, ssh key, etc.) per team:

```
$EDITOR ~/vezir-data/teams/blink/sync_config.json
```

(If this file exists it shadows the auto-materialized one.)


## 0.6.1 — TUI team-switcher + GET /api/me + client-side teams.json

Follow-up to v0.6.0's server-side multi-team support: thin clients
can now hold credentials for multiple teams and switch between them
at runtime, instead of requiring an env-var swap + TUI restart.

### Added

* **`GET /api/me`** server endpoint returning
  ``{github, team_id, team_name, is_admin}``.  Powers the TUI title-bar
  display + the post-switch confirmation in the team-switcher.
* **`~/.config/vezir/teams.json`** client-side credentials store
  (mode 0600).  Schema:

      {
        "teams": [
          {"id": "blink",     "url": "...", "token": "vzr_...", "label": "Blink"},
          {"id": "twentyone", "url": "...", "token": "vzr_...", "label": "Twentyone"}
        ],
        "active": "blink"
      }

  When teams.json has an active entry, those credentials WIN over
  env vars and over the legacy single-team ``client.json``
  ``url``/``token`` keys.  Precedence: env vars > teams.json active >
  client.json > localhost default.  Env stays top so ad-hoc
  ``VEZIR_TOKEN=xxx vezir ...`` overrides still work.

* **TUI binding `^t` ("Switch team")** cycles through teams
  configured in ``teams.json``:
  - Persists the new ``active`` to disk.
  - Rebuilds the in-process ``VezirClient`` against the new url/token.
  - Re-fetches identity from ``/api/me`` to update the title-bar
    team label.
  - Reloads the Sessions tab so it shows the new team's sessions.
  - Refuses while a recording or upload is in flight (would orphan
    the upload on the old team's server view).
  - No-ops with a friendly toast when only one team (or none) is
    configured.

* **TUI title-bar team display**: the subtitle now reads
  ``team: <team_name>`` once ``/api/me`` resolves.  Falls back to
  ``thin client`` placeholder when the call fails (older server or
  network hiccup), so the TUI stays usable.

* **CLI `vezir team config` subgroup**:
  - ``vezir team config add --id <slug> --url <url> --token <vzr_...>
    [--label <name>] [--activate]`` — add or update a team in
    teams.json.  Implicit-activate for the first team added.
  - ``vezir team config list`` — print configured teams with an
    asterisk marking the active one.
  - ``vezir team config use <slug>`` — switch active team (CLI side;
    matches what ``^t`` does in the TUI).
  - ``vezir team config remove <slug>`` — drop a team entry; if it
    was active, falls back to the first remaining team.

### Changed

* **`vezir/config.py::server_url()` and `client_token()`** now
  consult ``teams.json`` after env vars and before the localhost
  default, so CLI commands (``vezir scribe``, ``vezir upload``,
  ``vezir token enroll``) automatically pick up the active team's
  credentials.  Env vars remain top priority.

### Deferred (still)

* Per-team voiceprint DB (planned for 0.6.2).
* Per-team git sync remote in the worker (planned for 0.6.2;
  ``teams.sync_remote`` schema slot exists since 0.6.0).
* ``vezir session move <id> --to-team <slug>`` for retroactive
  job re-classification (planned for 0.6.2).

### Operator + thin-client setup (typical flow)

On muscle, mint a token per (handle, team) pair:

    vezir token issue --github pretyflaco --team blink     --label laptop-blink
    vezir token issue --github pretyflaco --team twentyone --label laptop-twentyone

Both commands print plaintext bearers ONCE; capture them.

On the thin client (laptop), populate teams.json:

    vezir team config add --id blink     --url https://10.44.141.239 --token vzr_blink_token     --label Blink     --activate
    vezir team config add --id twentyone --url https://10.44.141.239 --token vzr_twentyone_token --label Twentyone

Confirm:

    vezir team config list

In the TUI:

    vezir tui
    # ctrl+t cycles to twentyone
    # ctrl+t again cycles back to blink

The title bar reads ``team: Blink`` / ``team: Twentyone`` to reflect
the active team.  Sessions tab auto-reloads on every switch.

### Compatibility

* **Single-team users** (using only ``client.json`` ``url``/``token``)
  see no behavior change.  teams.json is opt-in.
* **Env-var users** see no behavior change.  Env still wins.
* **Web/dashboard cookies minted under v0.6.0** keep working; they
  carry team_id from /login as captured in v0.6.0.
* **No schema migration**.  v0.6.1 is purely additive: new server
  endpoint, new client-side file, new CLI/TUI surface.  No DB
  changes; no token-file changes.

---

## 0.6.0 — multi-team support (β-minimal): team isolation, seed blink + twentyone

Vezir grew up.  Previously every authenticated bearer token saw every
non-personal session on the server.  v0.6.0 introduces a **team** as
a first-class isolation primitive: each token belongs to exactly one
team, each session lives in exactly one team, and the visibility
filter scopes every query to the caller's team.  No cross-team
sharing.  Per the design discussion in vezir_plan.md, this is
β-minimal (single SQLite, single nvpn network, one team_id column);
per-team voiceprint DB and per-team git sync target follow in v0.6.1.

### Added

* **Teams table + schema**: ``teams`` (id, name, sync_remote,
  sync_meeting_type, created_at) and ``jobs.team_id`` column with
  ``idx_jobs_team`` index.
* **Token team_id**: every row in ``~/vezir-data/tokens.json`` now
  carries ``team_id``; legacy rows without one are rejected at auth
  time with a "re-issue with --team" 401.
* **Admin endpoints**: ``GET /admin/teams``, ``POST /admin/teams``,
  ``GET /admin/teams/{id}``, ``PATCH /admin/teams/{id}``.  All
  gated by ``auth.require_admin``.
* **CLI**: ``vezir team list``, ``vezir team create --id <slug>
  --name <Name>``, ``vezir team set-sync --id <slug> --remote <url>
  --meeting-type <prefix>``.  ``vezir token issue`` now takes
  ``--team <slug>`` (auto-selects when only one team exists;
  required when >1).
* **Migration audit log**: ``~/vezir-data/logs/migration-0.6.0-multi-team.log``
  records every change made by the one-shot v0.6.0 data migration
  (job counts moved per team, tokens migrated/revoked, files
  relocated).

### Changed (auth + visibility chain)

* **Visibility filter**: ``GET /api/sessions`` now scopes to the
  caller's team_id derived server-side from the bearer token.
  Cross-team sessions are entirely invisible (not just hidden — the
  endpoint returns ``404`` on a session_id from another team to
  avoid leaking existence).
* **Every session-scoped endpoint** (artifact download, session
  detail HTML page, sync-now, retry-summary, share, labeling page,
  labeling clip, labeling submit — both HTML and JSON) now enforces
  team membership via ``_enforce_team_visibility``.
* **Auth dependencies**: added ``require_bearer_full`` and
  ``require_bearer_or_cookie_full`` that return ``(github, team_id,
  is_admin)``.  Legacy ``require_bearer`` / ``require_bearer_or_cookie``
  still return just ``github`` and internally validate team_id presence.
* **Web sessions** (``vezir_session`` cookie): the in-memory session
  entry now captures team_id at /login time.  Cookies minted before
  v0.6.0 in the same process lifetime are rejected with a "force
  re-login" 401 so they can't silently leak across teams.
* **Roster file**: ``~/vezir-data/team.json`` moved to
  ``~/vezir-data/teams/blink/roster.json``; per-team rosters are
  used by the labeling dropdown and ``/api/team`` autocomplete.
  ``/api/team`` is now scoped to the caller's team.

### Migration (one-shot, runs at first startup of v0.6.0)

Decisions locked in vezir_plan.md (Q-1..Q-7 + Q-A..Q-G + Q-H, plus
final γ-lite revoke scope confirmation):

* **Seed teams**: ``blink`` and ``twentyone`` are created
  automatically (no "default" team).
* **Job backfill**:
  - jobs where ``github = 'bettermorning'`` -> ``team_id =
    'twentyone'``
  - smoke-upload-test* jobs -> **deleted**
  - everything else -> ``team_id = 'blink'``
* **Token backfill**:
  - bettermorning's tokens -> twentyone
  - all other handles' tokens -> blink
  - **pretyflaco's UNLABELED tokens are revoked** (γ-lite per user
    decision: the labeled ``gpu-server`` admin token and
    ``android-galaxy`` token stay in blink; the two label-less
    pretyflaco tokens are dropped so the operator re-issues with
    explicit ``--team`` for each device that needs cross-team
    capability).
* **Roster file move**: ``team.json`` -> ``teams/blink/roster.json``;
  ``teams/twentyone/roster.json`` seeded with ``[{"github":
  "pretyflaco"}, {"github": "bettermorning"}]``.
* **Voiceprint DB**: unchanged in v0.6.0 (still
  ``~/vezir-data/speaker_profiles.json`` shared across teams).
  Per-team voiceprint DBs land in v0.6.1; until then, blink and
  twentyone share the central voiceprint DB.  This is suboptimal
  for strict bettermorning isolation but acceptable as a transition
  state — no team-scoped queries touch the voiceprint DB in 0.6.0.
* **Sync remote**: ``teams.sync_remote`` is a reserved schema slot
  in v0.6.0.  Worker still reads ``VEZIR_SYNC_MEETING_TYPE`` env var
  (single global value).  Per-team sync wiring lands in v0.6.1.

The migration is **idempotent** and recorded in a new
``schema_migrations`` table, so a restart that interrupts it (or a
re-run after a successful one) is safe.  An audit log of every
change made is written to ``~/vezir-data/logs/migration-0.6.0-multi-team.log``
in JSON.

### Deferred to v0.6.1

* Per-team voiceprint DB (``teams/<id>/speaker_profiles.json``)
  + worker shim wiring.  Until then, twentyone members will see
  blink-trained voiceprints in their label dropdowns (no functional
  cross-team data leakage; just shared training surface).
* Per-team sync remote (read from ``teams.sync_remote``, not env
  var).  Until then, both teams sync to the same remote if any.
* ``vezir session move <id> --to-team <slug>``.
* ``vezir team rename`` / ``vezir team delete``.

### Deferred to v0.6.2 / v0.7.x

* ``GET /api/me`` returning ``{github, team_id, team_name,
  is_admin}`` for TUI title-bar display.
* TUI team-switcher (``^t``) + ``~/.config/vezir/teams.json``
  client config.

### Operator notes

When a v0.5.0 (or earlier) vezir is upgraded to v0.6.0:

1. **Stop the running vezir** (the migration touches files that are
   open by the running daemon; running migration against a live DB
   can race).
2. **Back up ``~/vezir-data/`` first**:
   ``tar czf vezir-data-pre-0.6.0.tgz ~/vezir-data``.
3. **Upgrade vezir**: ``pip install --upgrade vezir``.
4. **Start vezir again**: the migration runs at first startup and
   logs its outcome to ``~/vezir-data/logs/migration-0.6.0-multi-team.log``.
5. **Verify**: ``vezir team list`` should show ``blink`` and
   ``twentyone``; ``vezir status`` should show per-team job counts.
6. **Re-token disrupted devices**: any pretyflaco devices using the
   two revoked unlabeled tokens will get 401s; re-issue per-device
   tokens with ``vezir token issue --github pretyflaco --team <slug>
   --label <device>`` and update ``VEZIR_TOKEN`` on the device.

---

## 0.5.0 — TUI: import existing audio file + preset Select fix + version line

### Changed (breaking UX)

* **Upload button repurposed to "Import file".**  In v0.4.x the
  Upload button (and `^u` keyboard binding) re-uploaded the most
  recently finished in-TUI recording.  In v0.5.0 it instead opens
  a modal file picker (`DirectoryTree` rooted at `~/`, filtered
  to directories + `.wav`/`.ogg` files only) for selecting an
  arbitrary audio file on disk.  The selected file is uploaded
  through the same pipeline as in-TUI recordings.

  Visual design of the Upload button is unchanged (still rightmost
  cell in row 3, same border/label `⬆ Upload`).  The footer
  binding label updates from `^u Upload last` → `^u Import`.

  **Auto-upload on Stop is unchanged**: fresh in-TUI recordings
  still upload automatically when the recorder finishes.  Manual
  retry path for a failed auto-upload: open the picker (Upload
  button or `^u`), navigate to the recording (default location
  `~/vezir-data/recordings/`), select it.  Or from the CLI:
  `vezir upload <path>`.

  Picker behavior:
  - Starts at `~/` on first use; subsequently remembers the
    parent directory of the last imported file (`last_import_dir`
    in `~/.config/vezir/client.json`).
  - Hidden files (dot-prefixed) and non-audio files are filtered
    out so the tree shows only navigable directories + importable
    `.wav`/`.ogg` files.
  - Picking a non-`.wav`/`.ogg` (shouldn't happen with the filter,
    but defensive) keeps the modal open with an inline error hint.
  - Esc cancels silently; Enter selects the cursor node.

### Fixed

* **Preset Select rendered empty in v0.4.2** (reported in
  `~/vezir-data/errors/tui_0.4.2.png`).  The universal
  `border: round $primary` rule on row-2 cells double-bordered
  the `Select` widget — outer border + inner `SelectCurrent`
  border = 4 rows of chrome on a `height: 3` cell, collapsing
  the visible text to 0 rows.  Stripped our outer border on
  `Select`; its native chrome (which includes the `▼` glyph and
  its own subtle border) provides the cell visual.

* **Footer label inconsistency**: `^u Upload last` →
  `^u Import` reflects the new button behavior.

### Added

* **Version line** under the status/error rows shows the running
  client version (`v0.5.0`) for quick "what am I running?"
  troubleshooting.  Italic, muted-gray, right-aligned.

---

## 0.4.2 — TUI Record screen redesign: uniform 4-column grid

### Changed

* **Record screen: uniform 4-column grid layout.**  Rows 2 (toggles)
  and 3 (controls) now share an identical 4-column equal-width grid
  with uniform `border: round $primary` on every cell.  The result
  is a clean, aligned layout where all interactive elements look like
  consistently-sized bordered cells.

  Layout:
  ```
  Row 1:  [──────────── optional meeting title ───────────────]
  Row 2:  [ Auto-label ] [   Sync    ] [ Personal  ] [ Preset ▼]
  Row 3:  [ ● Record   ] [ ⏸ Pause  ] [ 00:00:00  ] [ ⬆ Upload]
  ```

* **"Title:" label removed.**  The `Title:` label was redundant
  with the placeholder text; the input now spans the full row width.

* **Checkboxes replaced with toggle-buttons.**  Auto-label, Sync,
  and Personal are now `Button` widgets that toggle on/off with
  color feedback: green (`$success`) for Auto-label and Sync,
  yellow/orange (`$warning`) for Personal (privacy-mode indicator).
  Same persistence behavior as before (prefs saved to client.json).

* **"Upload last" shortened to "Upload".**  Fits better in the
  equal-width grid cell.

  Server: no changes.  Client TUI only.

---

## 0.4.1 — TUI Record screen layout polish

### Fixed

* **Record screen layout was visually scattered.**  The three rows
  (title, toggles, controls) had widgets at uneven widths with no
  vertical centering, the preset Select wrapped to two lines, the
  bare timer Label floated mid-row without visual grouping, and the
  byte counter showed `0 B` from t=0 (just the WAV header) which
  read as "nothing recording" until real audio arrived.

  CSS-only alignment fix — no Python logic, state machine, worker,
  or binding changes:
  - Vertically center contents in every Horizontal row
    (`align: left middle`).
  - Title label fixed-width gutter (10 cols); input fills remainder
    (`width: 1fr`).
  - Toggle row: all four widgets equal-fr columns with 1-cell
    gutters.
  - Controls row: buttons clustered with 1-cell margins; timer
    placed in a bordered center cell (`border: round $primary`)
    for visual parity with the buttons.
  - Shortened preset labels: "High Quality" / "Confidential" /
    "Alternative" (model-name hints removed to prevent Select
    line-wrap).
  - Byte counter suppressed until `file_bytes >= 4 KB` so the
    initial state reads `00:00:00` cleanly without misleading
    `44 B` header-only telemetry.

  Server: no changes.  Client TUI only.  Pure cosmetic.

---

## 0.4.0 — meetscribe → millet ecosystem rename

Vezir 0.4.0 follows the upstream package rename: the underlying
meeting-transcription pipeline `meetscribe-offline` is now
`millet-pipeline`, and its capture-only sibling `meetscribe-record`
is now `millet-record`.  Named after the Ottoman *millet system*.
See [the millet repo](https://github.com/pretyflaco/millet) for the
full reasoning.

This is a coordinated rebrand — no feature changes, but it is a
**breaking install change**: teammates need to `pip install --upgrade
'vezir[server]'` to pick up the renamed pipeline package.  The wire
format and HTTP API are unchanged.

### Changed

* **Dependency pins**: `meetscribe-record>=0.3.0` →
  `millet-record>=0.4.0`; `meetscribe-offline>=0.8.3` (in the
  `[server]` extra) → `millet-pipeline>=0.9.0`.

* **Python imports**: `from meet.label`, `from meet.voiceprint` →
  `from millet.label`, `from millet.voiceprint` (4 sites in
  `vezir/server/`).  `from meet_record.capture`,
  `from meet_record.audio` → `from millet_record.capture`,
  `from millet_record.audio` (5 sites in `vezir/client/`).

  The legacy import names continue to work via aliases shipped by
  `millet-record 0.4.0` (a `sys.modules` alias + meta-path finder);
  vezir 0.4.0's own code uses the canonical names.

* **Subprocess invocations**: `meet transcribe / record / label /
  sync / check` etc. → `millet transcribe / record / label / sync /
  check`.  `meet_binary()` in `vezir/config.py` searches for
  `millet` first, falls back to `meet` (legacy) — so deployments
  with only the pre-rename `meetscribe-record` installed continue to
  work until they upgrade.

* **Environment variables**: `VEZIR_MEET_*` → `VEZIR_MILLET_*`.  The
  old names are still read and forwarded with a one-time
  `WARNING: VEZIR_MEET_X is deprecated; use VEZIR_MILLET_X (will be
  removed in vezir 0.6.0)`.  Affected vars: `VEZIR_MEET_BIN`,
  `VEZIR_MEET_DEVICE`, `VEZIR_MEET_COMPUTE_TYPE`,
  `VEZIR_MEET_TORCH_DEVICE`, `VEZIR_MEET_ASR_BACKEND`,
  `VEZIR_MEET_MLX_MODEL`.

* **Recordings directory default**: `~/meet-recordings/` →
  `~/millet-recordings/`.  On first use, if the legacy directory
  exists and the new one doesn't, vezir emits a one-time stderr
  hint suggesting `mv ~/meet-recordings ~/millet-recordings`.  No
  auto-move — explicit consent per the rename handoff.

* **Docs**: full README pass — every meetscribe reference rewritten
  to millet; install profiles, env var table, architecture diagram
  refreshed; new "What's new in 0.4.0" status callout.

### Compatibility

* **`millet` CLI is the new primary**; `meet` continues to work for
  two minor versions (until `millet-record 0.6.0`) and emits a
  `DeprecationWarning` on each invocation.  Set
  `MILLET_SUPPRESS_DEPRECATION=1` to silence.

* **The bundled macOS Swift sidecar binary remains
  `meet-record-mac`** — renaming would require code-signing
  bundle-path changes that aren't worth doing as part of this PR.

* **Wire-format / HTTP API unchanged.**  Existing sessions, queue
  rows, voiceprint DB, team roster, dashboard, and Android client
  continue to interoperate unchanged.

### Migration

```bash
# Thin client (CLI / TUI only):
pip install --user --upgrade vezir

# Full server (muscle):
pip install --user --upgrade 'vezir[server]'
# (pulls millet-pipeline >= 0.9.0 + millet-record >= 0.4.0)

# Optional: rename your recordings directory once
mv ~/meet-recordings ~/millet-recordings

# Optional: update env vars if you have them set
sed -i 's/VEZIR_MEET_/VEZIR_MILLET_/g' ~/.bashrc ~/.zshrc
```

The legacy `VEZIR_MEET_*` variables, `~/meet-recordings/` directory,
`meet` CLI, and `meet_record` import path all continue to work
through the deprecation window.

### Tests

* 225 passing (unchanged from 0.3.1); the rename is a pure
  identifier change.  vezir 0.4.0 doesn't add new features or
  remove existing ones.

## 0.3.1 — auto-refresh Sessions, open-in-browser, enter-to-submit

Pure paper-cut polish on top of v0.3.0.  No breaking changes.
Two PRs, both shipped after dogfood-week feedback on the new TUI.

### Added

* **`o` key opens a session in the web dashboard** — both on the
  Sessions list (cursor row) and the Session detail screen.
  Builds `{server_url}/s/{session_id}` via Python's `webbrowser.open`.
  Graceful fallback (notifies the URL) when no default browser is
  available, e.g. headless servers.

* **LabelScreen: pressing `enter` while typing a github handle
  submits all labels.**  Matches the dialog convention; no more
  mousing to the Submit button after typing the last handle.

* **Toast notifications when a freshly uploaded session reaches
  terminal status** (done / needs_labeling / error).  The user
  no longer has to eyeball the Sessions tab to learn that an
  upload is ready.

### Changed

* **Sessions tab auto-refreshes on activation.**  Switching to
  the Sessions tab (`ctrl+s` or click) re-fetches
  `/api/sessions` automatically.  Previously the list froze at
  whatever was loaded on first mount, so a session recorded in
  the TUI didn't appear until the TUI was restarted.

* **Sessions tab auto-refreshes when a recently uploaded session
  reaches terminal status.**  Even if you stayed on the Record
  tab, the Sessions list is now fresh when you switch over.

* **HelpScreen text now matches actual bindings.**  The earlier
  text advertised `tab` / `space` / `enter` keybindings on
  LabelScreen that didn't exist; the `ctrl+r` "refresh" hint on
  the Sessions empty-state actually switched to the Record tab
  (the real refresh shortcut is `ctrl+l`).  Both fixed.

### Fixed

* **`_poll_worker` polled forever on `needs_labeling` sessions.**
  The terminal-status set was `{"done", "error"}` — sessions that
  landed in `needs_labeling` kept the poll worker spinning for
  the full 600s deadline.  Now `{"done", "error", "needs_labeling"}`.

### Tests

* 225 passing (was 218 in 0.3.0).  +7 tests covering tab-
  activation refresh, upload-complete refresh + toast,
  open-in-browser (both screens + missing-server-url fallback),
  LabelScreen enter-to-submit.

### Migration

* Nothing to do.  `pip install -U vezir[tui]` and restart your TUI.

## 0.3.0 — Textual TUI thin client, hybrid floating recorder, --personal flag

The headline change is a native desktop thin client built on
[Textual](https://textual.textualize.io/): `vezir tui` opens a
terminal UI with feature parity to vezir-android 0.2.5
(record / list sessions / view artifacts / label speakers) plus
desktop-only niceties (xdg-open / `open` for artifacts, system
clipboard integration, ffplay sample playback during labeling).

The classic Tkinter `vezir gui` floating recorder is preserved
and slimmed into `vezir scribe-widget` for users who want a
small always-on-top recorder without the full TUI.

The web dashboard remains but enters deprecation: v0.4 will
restrict it to admin-only, v0.5 will remove it.  All new
end-user features now ship to TUI + Android first.

### Added

* **`vezir tui`** — Textual TUI thin client, requires the
  `[tui]` extra (`pip install vezir[tui]`).  Screens:
  Record, Sessions, Detail, Artifact (text + binary via
  `xdg-open`), Label (with ffplay sample playback + GitHub
  handle autocomplete from team list), Help.  Background
  poll surfaces "needs labeling" notifications via
  `notify-send` (Linux) or `osascript` (macOS).  Set
  `VEZIR_TUI_DISABLE_NOTIFY_POLL=1` to skip the poll.

* **`vezir scribe-widget`** — slimmed Tkinter floating
  recorder (start / pause / stop / upload) without the
  full GUI's session-browsing pane.  Sibling to the TUI
  for users who want a small always-on-top recorder.

* **`vezir tui --serve`** — boots a local vezir server in
  the background, useful for self-hosted single-machine
  setups.

* **`--personal` flag** on `vezir scribe`, `vezir upload`,
  and the Tkinter GUI.  Marks a recording as personal:
  sync-to-git is forced off (regardless of session default),
  the session is tagged "Personal" in the UI, and the
  flag is per-recording only (not persisted to client
  config).  Matches the Android 0.2.5 behavior.

* **`VezirClient`** httpx-based API client at
  `vezir/client/api.py` — port of Android's
  `SessionApi.kt`/`LabelApi.kt`/`VezirApi.kt` for use by
  the TUI and scribe-widget.  Wraps session list/detail,
  label info, retry endpoints, and team queries.  Honors
  `SSL_CERT_FILE` then `VEZIR_CADDY_ROOT_CERT_PATH` for
  internal-CA HTTPS endpoints (default httpx uses
  `certifi.where()` which doesn't include internal CAs).

* **Clipboard integration** in the TUI: `c` copies the
  current item (session id / artifact body / temp path)
  per-screen; `ctrl+shift+c` copies the current selection.
  Both write via OSC 52 (works in Ghostty, kitty, iTerm2,
  WezTerm, modern xterm) AND via a subprocess fallback to
  `wl-copy` / `xclip` / `pbcopy` — necessary because
  VTE-based terminals (gnome-terminal, xfce4-terminal,
  Mate Terminal) disable OSC 52 by default.

* **`ctrl+shift+q`** force-quit binding in the TUI (a
  three-key chord chosen so it doesn't shadow TextArea's
  native `ctrl+c` copy).

### Changed

* **`vezir scribe` recording is now library-direct** via
  `meet_record.capture.RecordingSession` instead of
  subprocessing `meet record`.  Adds an interactive `p`
  keystroke for pause/resume.  Subprocess fallback retained
  for older meetscribe-record deployments and for
  `--virtual-sink` use cases that need the CLI surface.

* **Sync coerced off when `--personal` is set**, regardless
  of the session's default `sync_enabled`.

### Fixed

* **HTTPS verify against internal CAs.** `VezirClient` and
  `uploader.upload` now check `SSL_CERT_FILE` →
  `VEZIR_CADDY_ROOT_CERT_PATH` → True.  Fixes
  "SSL CERTIFICATE VERIFY FAILED" against Caddy
  internal-CA-fronted endpoints.

* **Multiple Textual-API pitfalls** caught during dogfood:
  - Never name a Screen/Widget helper `_render` (shadows
    `Widget._render`, returns None, crashes compositor on
    a real terminal).  Renamed to `_refresh_view`.
  - Never prefix `Message` subclasses with `_`
    (handler_name gets double-underscore, dispatcher
    silently drops the event).
  - Never assign `self.name = ...` on a Screen
    (`Screen.name` is the install-name property).
  - `priority=True` bindings need a written justification +
    a regression test confirming they don't shadow
    framework conventions.

* **DataTable double-dispatch** on `enter` key (was pushing
  ArtifactScreen twice, freezing the TUI).  Removed
  redundant `enter` bindings; DataTable handles it natively.

* **Binary-artifact open** moved off the UI thread to a
  worker (was freezing on PDFs).

* **LabelScreen layout clip** — speaker rows were sized
  for 2 rows of content when the Input needed 3, so typed
  text was invisible and the Play button rendered as a
  black box.  Fixed in PR8.

### Tests

* 218 passing (was 142 in 0.1.17).  +76 tests covering
  the TUI, clipboard, API client, scribe library path,
  scribe-widget, and notify polling.

### Dependencies

* New optional `[tui]` extra: `textual>=0.86`.
* Otherwise unchanged from 0.1.17.

### Migration

* No breaking changes.  Existing `vezir gui`, `vezir scribe`,
  `vezir upload`, and server commands work as before.
* To try the new TUI: `pip install -U vezir[tui] && vezir tui`.
* Internal-CA HTTPS users: set `SSL_CERT_FILE` to the CA
  bundle path (e.g. `/etc/caddy/certs/vezir-internal-ca.crt`).

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

## 0.1.11 — preset selector + auto-label/sync opt-outs + retroactive sync ([release](https://github.com/pretyflaco/vezir/releases/tag/v0.1.11))

Three-release rollup over [v0.1.8](https://github.com/pretyflaco/vezir/releases/tag/v0.1.8): the summarization preset selector (originally tagged 0.1.9), the GUI brand-lockup PNG (originally 0.1.10), and the two new per-upload privacy toggles (0.1.11).

Requires **`meetscribe-offline >= 0.8.1`** for the preset features; pinned in the `[server]` extra.

### Added

* **Summarization preset selector** (originally 0.1.9). `vezir scribe` and `vezir upload` accept `--preset {high-quality,confidential,alternative}`; the Tkinter GUI gets a dropdown above the recorder. Default on desktop is `high-quality`; Android default is `confidential`. The preset id flows: client → multipart form field `summary_preset` → server `Form()` → queue row → worker subprocess → `meet transcribe --summary-preset <id>` → meetscribe resolves to `(backend, model)`. When the chosen backend fails, the server refuses to silently fall back — the job ends in `error` with a clear reason.

* **Brand-lockup PNG in the GUI header** (originally 0.1.10). The "vezir scribe" text label is replaced by the pre-rendered brand mark + wordmark lockup (88×28 by default — matches the Record button height). Window title also rebranded from "vezir scribe" to "vezir". Falls back to a textual label if the PNG asset is missing.

* **Auto-label opt-out** (`--auto-label/--no-auto-label`, default ON). When off, the server skips voiceprint matching and the session always routes to manual labeling.

* **Sync opt-out** (`--sync/--no-sync`, default ON). When off, the session reaches `done (local-only)` on the dashboard. Artifacts stay on the vezir server but are not pushed to the configured destination repo.

* **Retroactive sync** — new endpoint `POST /session/<id>/sync` flips `sync_enabled=1` on the queue row and re-runs the finalize-sync flow. The dashboard's session detail page shows a "Sync now" button when the session reached `done` with `sync_enabled=0`. Status badge reads `local-only` (purple) instead of `done` (green) in that case.

### Changed

* Server schema gains `summary_preset TEXT`, `auto_label_enabled INTEGER NOT NULL DEFAULT 1`, and `sync_enabled INTEGER NOT NULL DEFAULT 1` via idempotent `ALTER TABLE`. Existing rows get the defaults; no manual migration needed.

### Backwards compatibility

* Older servers (< 0.1.11) ignore the new form fields and behave as today (always auto-label, always sync).
* Older clients (< 0.1.11) don't send the new fields; the new server treats absent fields as ON (today's behavior).
* The operator-side `VEZIR_SKIP_SYNC=1` env var continues to act as a global kill switch — per-job `sync_enabled` AND the env var both have to allow sync for sync to happen.

### Notes on the 3-version rollup

Tags `v0.1.9`, `v0.1.10`, and `v0.1.12` (later) were created in git but never published to PyPI as standalone releases. 0.1.11 is the canonical PyPI artifact carrying all three features.

## 0.1.8 — macOS Sequoia TCC pre-flight check ([release](https://github.com/pretyflaco/vezir/releases/tag/v0.1.8))

`vezir scribe` and `vezir gui` now run `meet check` before starting a recording. On macOS, this triggers the TCC permission dialog on first use so the user can grant Microphone and System Audio Recording access interactively.

### Fixed

* **macOS Sequoia silent permission failure.** On macOS 15+, new users running `vezir scribe` for the first time hit a silent failure — the microphone permission dialog never appeared because the old `probe-permissions` subcommand only *read* TCC status without triggering the prompt. Apple removed the `+` button from System Settings > Privacy > Microphone in Sequoia, so there was no manual workaround. Fix requires `meetscribe-record >= 0.3.0` which adds a `request-permissions` subcommand calling `AVCaptureDevice.requestAccess(for: .audio)`. Pre-flight check in vezir calls `meet check` and surfaces actionable errors instead of opaque subprocess failures.

### Changed

* `scribe.py`: `_check_meet_prerequisites()` runs `meet check` before spawning `meet record`.
* `gui.py`: same pre-flight check with `messagebox.showerror()` on failure.
* `pyproject.toml`: bump `meetscribe-record` from `>=0.1.0` to `>=0.3.0`.

## 0.1.7 — labeling wait, persistent cookie, session auto-refresh ([release](https://github.com/pretyflaco/vezir/releases/tag/v0.1.7))

### Added

* **`vezir scribe --wait` extended through labeling.** Polls past `transcribing` and `syncing` into `needs_labeling`, prints a clickable login URL to the labeling page, then continues polling until `done`.

* **Persistent browser cookie.** Login cookie no longer expires on browser close; survives across sessions until explicit `/logout` or token revocation.

* **Session detail page auto-refresh.** Long-running summaries no longer require a manual reload to see status transitions.

## 0.1.6 — token format validation, nvpn macOS onboarding ([release](https://github.com/pretyflaco/vezir/releases/tag/v0.1.6))

### Added

* **Client-side token format validation.** `vezir scribe` and `vezir upload` check `VEZIR_TOKEN` before starting and warn on common copy-paste errors (missing `vzr_` prefix, wrong length, trailing backslash from line-wrap, accidentally using an nvpn invite as the token).

* **Restructured nostr-vpn setup guide** (`infra/nvpn/README.md`) — separate Linux and macOS sections. Incorporates onboarding feedback: macOS native app invite import via URL scheme, root config copy step, explicit vezir upgrade step, two-secrets callout, token troubleshooting.

## 0.1.5 — CLI login URL, --wait polling, nostr-vpn guide ([release](https://github.com/pretyflaco/vezir/releases/tag/v0.1.5))

### Added

* **`vezir scribe` prints a browser-friendly URL.** Output URL includes `/login?token=...&next=...` so clicking it in a browser works immediately (sets auth cookie and redirects to the session page). Previously printed a bare URL that returned 401.

* **`vezir scribe --wait` (on by default).** After upload, polls the server for processing status and prints transitions (`transcribing`, `syncing`, `done`). When speakers need manual labeling, prints a clickable URL to the labeling page. Use `--no-wait` for fire-and-forget. `vezir upload` also gains `--wait/--no-wait` (default: off).

* **nostr-vpn onboarding guide** (`infra/nvpn/README.md`) — step-by-step setup for teammates connecting to vezir via [nostr-vpn](https://github.com/mmalmi/nostr-vpn), a decentralized mesh VPN with no accounts or fees.

### Changed

* **VPN-agnostic documentation.** README and code comments updated to support both nostr-vpn and Tailscale as network layer options.

* **Improved 401 error message.** Now tells you to visit `/login` or use the URL from CLI output, instead of referencing only the GUI.

## 0.1.4 — fix auto-labeling gate, voiceprint update, version drift ([release](https://github.com/pretyflaco/vezir/releases/tag/v0.1.4))

### Fixed

* **Auto-labeling gate no longer bypassed** ([#6](https://github.com/pretyflaco/vezir/issues/6)). `_has_unresolved_speakers()` was reading `.frontmatter.json` instead of the transcript because the glob-and-exclude pattern didn't account for frontmatter files. Now positively selects the canonical transcript by name.

* **Voiceprint update no longer silently fails** ([#8](https://github.com/pretyflaco/vezir/issues/8)). `update_profiles_from_confirmed_labels()` was called with 2 args but expects 4 (`audio_path`, `transcript_segments`, `confirmed_label_map`, `channel_map`). The `TypeError` was swallowed by a broad `except Exception`. Now loads the transcript and detects channel layout before calling, matching meetscribe-offline's canonical call shape.

* **Dynamic versioning** ([#7](https://github.com/pretyflaco/vezir/issues/7)). `vezir --version` now reads from package metadata via `importlib.metadata`. No more double-edit hazard between `pyproject.toml` and `__init__.py`.

## 0.1.3 — meetscribe 0.6.0 compatibility ([release](https://github.com/pretyflaco/vezir/releases/tag/v0.1.3))

### Changed

* Pin `meetscribe-offline >= 0.6.0` in the `[server]` extra. No vezir code changes; meetscribe 0.6.0 ships Apple Silicon auto-defaults for `--device` and `--torch-device`, runtime device-availability validation, a new GUI Advanced settings panel for ASR backend / torch device / MLX model, and the first CI workflow on the meetscribe repo.

### Compatibility

* **vezir 0.1.3 + meetscribe-offline 0.6.0** is the required pairing for the cleanest Apple Silicon UX. Vezir's `meet_supports_option` helper already absorbed the new flags in 0.1.2, so this is mostly a documentation + pin update.
* **vezir-android 0.1.x** unchanged. Android communicates only with vezir's HTTP API; no Android update needed.

## 0.1.2 — OGG/Opus compression by default + upload integrity ([commit 0f15122](https://github.com/pretyflaco/vezir/commit/0f15122))

A real Blink meeting recording (~233 MB raw WAV) exposed slow uploads over Tailscale and a silent partial-upload failure mode. This release moves to OGG/Opus by default and adds end-to-end size verification.

### Added

* **`vezir scribe` compresses to OGG/Opus before upload by default.** Raw WAV stays under `~/meet-recordings`. Use `vezir scribe --no-compress` to send raw WAV.

* **`vezir upload --compress`** for compressing an existing WAV before uploading.

* **CLI upload progress** — percent, uploaded/total bytes, upload speed, ETA.

### Changed

* **Client sends expected audio byte count with the upload.** Server verifies received bytes against expected.
* **Retry messages explicitly say retries restart from byte 0** (no resumable upload yet).

### Fixed

* **Incomplete uploads** are rejected and deleted instead of being enqueued / processed.

### Note

* Not tagged in git (`v0.1.2` was a PyPI-only release). Tag created retroactively in the 2026-05-24 housekeeping pass.

## 0.1.1 — existing-recording upload, hardened runtime permissions ([commit 56ac2ce](https://github.com/pretyflaco/vezir/commit/56ac2ce))

First post-0.1.0 follow-up addressing onboarding feedback.

### Added

* **`vezir upload AUDIO_FILE --title "..."`** — upload an existing recording (`.wav` / `.ogg`) without recording live. Closes the gap reported by `@openoms`: "what I think is lacking the most is being able to upload the previous recordings".

* **Server-side upload size/type limits.**

### Changed

* **Hardened runtime file permissions.**
  - Runtime directories: `0700`
  - Sensitive files (tokens, profiles, sync_config): `0600`
  - systemd unit gains `UMask=0077` so subprocess artifacts inherit private defaults.

### Docs

* Updated onboarding for Tailscale IP fallback when MagicDNS doesn't work, local recordings under `~/meet-recordings`, and the `vezir status` semantics (local/server-side, not remote-server status).

### Note

* Not tagged in git (`v0.1.1` was a PyPI-only release). Tag created retroactively in the 2026-05-24 housekeeping pass.

## 0.1.0 — initial public release ([release](https://github.com/pretyflaco/vezir/releases/tag/v0.1.0))

Self-hosted scribe service for team meetings: a designated scribe records on a laptop, the audio uploads to a central GPU box over Tailscale, and the team gets back a diarized transcript, AI summary, and PDF — with speaker labels resolved to GitHub handles via a shared web UI.

### Added

#### Server (`vezir serve`)

* FastAPI app with sqlite-backed job queue.
* Background worker that shells out to unmodified meetscribe (`meet transcribe` → `meet label --auto` → `meet sync`).
* Per-job `$HOME` shim so meetscribe's voiceprint DB and sync config point at vezir's central versions while the rest of the user's environment (model caches, etc.) stays visible.
* Per-session sync folders: `meetings/{date}_{meeting-type}-{HHMMSSZ}-{rand6}/` in the configured git repo.
* Silent-failure detection on `meet sync` (catches DNS hiccups, auth failures that cause `meet sync` to exit 0 even when git push failed).
* Last ~2 KB of subprocess log captured into the session's `error` field so the dashboard surfaces real failure causes.

#### Web UI

* Dashboard, session detail, label page, artifact downloads.
* Cookie-based browser auth via `/login?token=...&next=...` hand-off (HttpOnly, SameSite=Lax); JSON API stays bearer-only.
* `/logout` clears the cookie.
* Speaker labeling with GitHub-handle autocomplete (reads team roster from `team.json`).
* Audio clip preview per speaker.

#### Scribe clients

* `vezir scribe` — CLI wrapper around `meet record`; uploads on Ctrl+C.
* `vezir gui` — Tkinter widget (always-on-top; record/stop, live timer, server-side status badge, dashboard link). Cross-platform via stdlib Tkinter; needs `apt install python3-tk` on Debian-style minimal installs.
* Lightweight install footprint (~30 MB) thanks to the [meetscribe-record](https://github.com/pretyflaco/meetscribe-record) split.

#### Operations

* Bearer tokens issued / revoked / listed via `vezir token ...`.
* Central voiceprint DB seeded from existing meetscribe `~/.config/meet/speaker_profiles.json` (`vezir voiceprints seed`).
* Systemd user unit template for unattended deployment.
* All runtime state under a single env-configurable directory (`$VEZIR_DATA`, default `~/vezir-data`).

### Known limitations at 0.1.0

* macOS thin client deferred (Linux scribe clients fully supported).
* Voiceprint enrollment for non-Blink-team external participants not yet a first-class flow.
