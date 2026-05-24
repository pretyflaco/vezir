<p align="left">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/logo/vezir-logo-light.svg">
    <img src="assets/logo/vezir-logo.svg" alt="vezir" width="320">
  </picture>
</p>

Self-hosted scribe service for team-scale meeting capture. Vezir wraps
[millet](https://github.com/pretyflaco/millet) and turns it into a
multi-user, VPN-hosted service: a designated scribe records a meeting
on their laptop, the audio uploads to a central GPU-equipped box, and the
team gets back a diarized transcript, AI summary, and PDF — with speaker
labels resolved to GitHub handles via a shared web UI.

## Status

Alpha (0.4.0). Designed for small teams that want to keep meeting audio
inside their own infrastructure: one private mesh VPN + one server (Linux
GPU box or Apple Silicon Mac). Currently dogfooded by the Blink team.
Full release history in [`CHANGELOG.md`](CHANGELOG.md).

> **What's new in 0.4.0**: the underlying meeting-transcription package
> has been renamed `meetscribe` → `millet` (named after the Ottoman
> *millet system*).  Distribution names on PyPI are
> [`millet-pipeline`](https://pypi.org/project/millet-pipeline/) and
> [`millet-record`](https://pypi.org/project/millet-record/).  The
> `meet` CLI continues to work as a deprecation alias of the new
> `millet` CLI for two minor versions.  Vezir 0.4.0 pins these
> renamed packages; the wire format is unchanged.

**Highlights of the 0.3.x line (current):**

- **`vezir tui` — Textual terminal thin client** (v0.3.0+).  Native
  desktop UI with feature parity to the Android client plus
  desktop-only conveniences: artifact viewing via `xdg-open`/`open`,
  system clipboard integration (OSC 52 + xclip/wl-copy/pbcopy
  fallback for VTE terminals), ffplay-driven speaker-sample
  playback during labeling, and a background poll that surfaces
  "needs labeling" notifications via `notify-send`/`osascript`.
  Installed with `pip install vezir[tui]`.
- **`vezir scribe-widget` — slimmed always-on-top Tkinter
  recorder** (v0.3.0+).  Replaces the floating-widget half of
  `vezir gui` for users who want a small recorder without the
  full GUI's session browser.
- **`--personal` per-recording flag** (v0.3.0+).  Forces sync off
  for one recording regardless of session default; the session is
  tagged "Personal" in the UI and stays private to you.  Available
  on `vezir scribe`, `vezir upload`, the Tkinter GUI, and the TUI.
  Per-recording only — not persisted to client config.
- **Library-direct recording** (v0.3.0+).  `vezir scribe` now drives
  `meet_record.capture.RecordingSession` in-process so the CLI gains
  an interactive `p` keystroke for pause/resume.  Subprocess
  fallback retained for older deployments and `--virtual-sink`.
- **Auto-refresh Sessions tab** (v0.3.1).  Switching to the
  Sessions tab in the TUI re-fetches `/api/sessions`; toasts when
  a freshly uploaded session reaches `done` / `needs_labeling` /
  `error` so you don't have to eyeball the screen.
- **`o` opens session in browser** (v0.3.1).  From the Sessions
  list or DetailScreen, `o` opens the dashboard URL via
  `webbrowser.open`.
- **HTTPS verify against internal CAs.**  The thin client honors
  `SSL_CERT_FILE` then `VEZIR_CADDY_ROOT_CERT_PATH` (default httpx
  uses `certifi.where()` which doesn't include internal CAs).
  Needed when Caddy fronts the server with its internal CA.

**Established features (0.1.x line, still current):**

- **Summarization preset selector** (per upload) — pick between
  `high-quality` (Sonnet 4.6), `confidential` (DeepSeek V4 Pro in a
  Tinfoil hardware-attested TEE), or `alternative` (Kimi K2.6).
  CLI: `--preset`; TUI/GUI: dropdown; Android: dropdown.  When the
  `confidential` preset is chosen the PDF carries a red CONFIDENTIAL
  watermark on every page.
- **Auto-label opt-out** (per upload) — `--no-auto-label` skips
  server-side voiceprint matching and always routes the session to
  manual labeling.
- **Sync opt-out** (per upload) — `--no-sync` keeps the session
  `local-only` on the dashboard; artifacts stay on the vezir server
  and aren't pushed to the configured destination repo.
  Retroactively syncable from the dashboard's "Sync now" button.
- **Retry summary with preset override** — `POST /api/sessions/{id}/retry-summary`
  accepts a JSON body `{"preset": "high-quality"}` for swapping
  backends when the original fails (e.g. Tinfoil outage → retry
  with Claude Max).

Supported VPN options:
- [nostr-vpn](https://github.com/mmalmi/nostr-vpn) — **recommended.**
  Decentralized, no accounts or fees, Nostr keys for identity.
  No third-party gatekeeper.  See [`infra/nvpn/README.md`](infra/nvpn/README.md).
- [Tailscale](https://tailscale.com/) — established alternative,
  easy setup, requires a third-party account.

Linux and macOS Apple Silicon clients are fully supported.  An
Android thin client lives at
[vezir-android](https://github.com/pretyflaco/vezir-android) (v0.2.5
ships the same three opt-outs + preset selector + the `--personal`
flag).  On Apple Silicon the server auto-selects MLX Whisper ASR +
PyTorch MPS when available, falling back to CPU/MPS-split mode otherwise.

Requires **`millet-pipeline >= 0.8.3`** (pinned via the `[server]`
extra).  0.8.x adds the preset system, the Tinfoil TEE backend, the
CONFIDENTIAL PDF watermark, and several voiceprint / relabel
correctness fixes that vezir's worker depends on.  0.8.3 specifically
re-raises summary exceptions when `summary_preset` is set, fixing
silent retry-summary "success" without an actual summary.

## Architecture

```
[Scribe laptop / phone]               [GPU server, over nvpn / Tailscale]
  vezir tui            ──▶            vezir serve (FastAPI, 127.0.0.1
   (Textual; record,                    │  fronted by Caddy w/ TLS)
    list, label, view                   │
    artifacts, copy,                    │   multipart form fields:
    open-in-browser)                    │     summary_preset
                                        │     auto_label
  vezir scribe         ──▶              │     sync
   (CLI; library-direct                 │     personal
    record w/ pause)                    │
                                        ├── sqlite job queue
  vezir scribe-widget  ──▶              │   (summary_preset,
   (Tk floating recorder;               │    auto_label_enabled,
    start / pause / stop)               │    sync_enabled, personal)
                                        ▼
  vezir gui            ──▶            worker
   (Tk full UI; recorder                 │ shells out via HOME-shim
    + sessions list)                     ▼
                                       millet transcribe --summary-preset <id>
  vezir upload <file>  ──▶             millet label --auto  (if auto_label_enabled)
   (resume any WAV/OGG)                millet sync          (if sync_enabled
                                                           and not personal
  vezir-android (Kotlin) ──▶                               and VEZIR_SKIP_SYNC unset)
                                                  ──▶ private git repo
                                                      OR sit at `done (local-only)`
                                                         on the dashboard
                                         │
                                         ▼
                                       web UI (labeling, dashboard,
                                               "Sync now" button)
                                        ◀── scribe browser
```

Meetscribe is invoked as an unmodified subprocess. Vezir owns its own
job queue, voiceprint database, team roster, and browser auth, and
exposes the per-upload toggles end-to-end (client → form field
→ queue column → worker gate).

## Clients

Vezir ships five client surfaces; pick whichever fits the moment.
All speak the same multipart upload + JSON API; you can mix and match
across devices.

| Client | Best for | Install |
|---|---|---|
| **`vezir tui`** | Day-to-day desktop use.  Record + browse sessions + view artifacts + label speakers, all in one terminal-native UI.  Has system-clipboard integration, ffplay sample playback, background "needs labeling" toasts. | `pip install vezir[tui]` |
| **`vezir scribe`** | Headless / ssh / dotfile-driven workflows.  Pure CLI; pause/resume with `p` key during recording. | `pip install vezir` |
| **`vezir scribe-widget`** | "Just a recorder, get out of my way."  Always-on-top Tkinter floating widget — start/pause/stop and upload, no session browser. | `pip install vezir` + `apt install python3-tk` |
| **`vezir gui`** | Legacy Tkinter full UI (recorder + session list + dashboard launcher).  Still works; future of this surface depends on dogfood feedback. | `pip install vezir[gui]` + `apt install python3-tk` |
| **`vezir upload <file>`** | Existing WAV/OGG you recorded another way (e.g. phone, Audio Hijack, OBS).  Resumes from byte 0 after transient failures. | `pip install vezir` |
| **[vezir-android](https://github.com/pretyflaco/vezir-android)** | Field recording from phone.  Mirrors TUI feature set (v0.2.5); confidential preset is the Android default. | Play Store / sideload |

All desktop clients honor `VEZIR_URL` + `VEZIR_TOKEN` from env or
`~/.config/vezir/client.json`; the TUI / scribe-widget / GUI persist
preset + auto-label + sync preferences across launches, while the
`--personal` flag stays per-recording and is never persisted.

For internal-CA HTTPS endpoints (Caddy with its built-in CA), set
`SSL_CERT_FILE=/etc/caddy/certs/vezir-internal-ca.crt` or
`VEZIR_CADDY_ROOT_CERT_PATH=<same>` so the thin clients trust the
server.  (Default httpx uses `certifi.where()` which doesn't include
internal CAs.)

## Summarization presets

A preset picks the backend + model the server will use for the AI
summary.  The client sends the preset id as the multipart form field
`summary_preset`; the worker passes it to
`millet transcribe --summary-preset <id>` which millet resolves to
a `(backend, model)` pair.

| Preset | Backend | Model | Use case |
|---|---|---|---|
| `high-quality` | claudemax | Sonnet 4.6 | Default on desktop; highest summary quality (requires a Claude Max subscription on the server) |
| `confidential` | tinfoil | DeepSeek V4 Pro | Hardware-attested TEE — prompts not visible to model provider / cloud operator.  Default on Android.  PDF gains a red CONFIDENTIAL header + footer on every page. |
| `alternative` | openrouter | Kimi K2.6 | Cheapest cloud option (~$0.017/meeting); useful when claudemax credentials are unavailable on the server |

When a preset is explicitly chosen, the server **does not silently
fall back** to a different backend on failure — a silent
tinfoil → claudemax fallback would defeat the entire point of the
Confidential preset.  Jobs whose chosen preset fails end up in
`error` state on the dashboard with a clear reason.

Set the preset via:
- CLI: `vezir scribe --preset confidential`, `vezir upload --preset alternative <file>`
- GUI: dropdown above the record button (stickied to last choice via
  `~/.config/vezir/client.json`)
- Android: dropdown above the title field (stickied to
  EncryptedSharedPreferences; default `confidential` on Android)

The server needs the matching backend credentials/SDK installed.  On
muscle, the Tinfoil SDK is pulled by the `[tee]` extra of millet-
offline (`pip install 'millet-pipeline[tee]'`) and the API key
lives in `TINFOIL_API_KEY` or at `~/models/tinfoil/tinfoil.txt`.

## Privacy toggles

Three per-upload toggles.  `auto_label` and `sync` default ON
(preserves pre-0.1.11 behavior) and are sticky; `personal` defaults
OFF and is **per-recording only — never persisted** (treating the
"I want this one to be private" intent as ephemeral by design).

| Toggle | Default | When set | Persisted? |
|---|---|---|---|
| `auto_label` | ON | OFF skips `millet label --auto`; session always routes to manual labeling.  Useful when you don't want the server attempting to identify speakers from previously enrolled voiceprints. | Yes (`~/.config/vezir/client.json`) |
| `sync` | ON | OFF keeps the session local-only (status `done (local-only)` on the dashboard).  Artifacts stay on the vezir server but aren't pushed to the configured destination repo. | Yes (`~/.config/vezir/client.json`) |
| `personal` | OFF | ON marks the session "Personal" in the UI, forces `sync` off for this recording regardless of session default, and keeps the session private to the uploader.  Useful for 1:1s, draft notes, anything you might not want to publish even if you forget to flip the sync toggle. | **No** — per-recording only |

CLI: `--auto-label/--no-auto-label`, `--sync/--no-sync`,
`--personal` on both `scribe` and `upload`.  Explicit choices for
the first two are persisted to `~/.config/vezir/client.json` so the
next session remembers them; `--personal` is intentionally
per-recording.

TUI: three checkboxes between the title row and the recorder controls
(personal checkbox greys out the sync checkbox when active to make
the forced-OFF behavior visible).  `ctrl+x` toggles personal from
the keyboard.

GUI: two checkboxes between the preset combobox and the recorder row;
personal toggle on the same line as Record.

Android: three `Switch` rows on the record screen, persisted in
EncryptedSharedPreferences (personal resets to OFF on each launch
per v0.2.5 semantics).

### Retroactive sync

A session uploaded with `--no-sync` can be promoted to git later from
the dashboard.  The session detail page (`/s/<id>`) renders a
"Sync now" button when the session reached `done` with `sync_enabled=0`.
Clicking it POSTs to `/session/<id>/sync`, which flips the queue row's
`sync_enabled = 1` and re-runs the finalize-sync flow in a background
thread.  The page polls and refreshes through `syncing → done`.

The status badge reads `local-only` (purple) instead of `done`
(green) for these sessions so they're visually distinct in the
dashboard.

### Operator-side kill switches

The two **server-side** env vars are unchanged:

- `VEZIR_SKIP_SYNC=1` — global sync kill switch.  Wins over the
  per-job `sync_enabled = 1`.  Both must allow sync for sync to
  happen.
- `VEZIR_DELETE_AUDIO=1` — delete recorded audio after artifacts are
  produced (storage retention policy).

## Compatibility

The 0.1.11 wire format is forward- and backward-compatible:

- **Older clients (< 0.1.11) talking to a 0.1.11 server** don't send
  the `summary_preset` / `auto_label` / `sync` fields; the server
  treats absent fields as the safe defaults (no preset → server
  default backend; auto_label=ON; sync=ON).
- **A 0.1.11 client talking to an older server (< 0.1.11)** sends the
  fields but the older server ignores them.  Behavior is exactly
  today's: server uses its default backend, always auto-labels,
  always syncs.

Both cases are silent — no errors.  If a teammate is on an older
client and you want them to use a preset / opt-out, ask them to
`pip install --upgrade vezir`.

## Repo layout

```
vezir/
  vezir/                    # python package
    cli.py                  # serve, scribe, tui, scribe-widget,
                            # gui, upload, token, voiceprints
    config.py               # paths, env
    server/                 # FastAPI app, queue, worker, meet_runner
    client/
      api.py                # VezirClient (httpx) — shared by TUI + scribe-widget
      uploader.py           # multipart upload w/ resume + verify discovery
      scribe.py             # library-direct recorder (pause/resume)
      scribe_widget.py      # Tkinter floating recorder
      gui.py                # Tkinter full UI (recorder + sessions list)
      audio.py              # ffplay wrapper + notify_desktop
      tui/                  # Textual TUI
        app.py              # VezirTuiApp + MainScreen (TabbedContent)
        record_screen.py
        sessions_screen.py
        detail_screen.py
        artifact_screen.py
        label_screen.py
        help_screen.py
        notify.py           # background "needs labeling" poll
    web/                    # templates + static (dashboard, labeling UI)
  data/
    team.json.example
  infra/
    nvpn/                   # nostr-vpn join guide (Linux + macOS)
    caddy/                  # Caddy install script + Caddyfile templates
    systemd/                # vezir.service unit
  assets/
    logo/                   # vezir wordmark, lockup, icons
  tests/                    # 225 passing as of v0.3.1
```

Runtime data lives **outside** the repo at `~/vezir-data/` (server)
or `~/.config/vezir/` (client preferences).

## Install profiles

| Role | Install command | Footprint |
|---|---|---|
| **Scribe client (CLI only)** | `pip install --user vezir` | ~30 MB |
| **Scribe client + Textual TUI** *(recommended for desktop)* | `pip install --user 'vezir[tui]'` | ~35 MB |
| **Scribe client + Tkinter GUI / scribe-widget** | `pip install --user 'vezir[gui]'` plus `apt install python3-tk` (Debian/Ubuntu) | ~30 MB |
| **Server** (FastAPI + worker + dashboard + labeling UI) | `pip install --user 'vezir[server]'` | ~3 GB on Linux/CUDA (millet-pipeline = whisperx + torch + pyannote); on Apple Silicon also pulls `mlx-whisper` for the MLX ASR backend (~few hundred MB extra) |

You can combine extras: `pip install --user 'vezir[tui,gui]'` gets you
both the TUI and the Tkinter widgets on the same box.

The split is enforced by `pyproject.toml`'s `[project.optional-dependencies]`:
the base install uses [millet-record](https://github.com/pretyflaco/millet-record)
(capture only). The `[tui]` extra adds [Textual](https://textual.textualize.io/);
the `[server]` extra adds [millet-pipeline](https://github.com/pretyflaco/millet)
for the heavy transcription/diarization/summarization pipeline.

On Apple Silicon, the same `[server]` extra additionally installs
`mlx-whisper` via a PEP 508 environment marker so the MLX ASR backend is
available out of the box. Auto-detection selects it at runtime; see the
env-var table below for overrides (`VEZIR_MEET_ASR_BACKEND`,
`VEZIR_MEET_MLX_MODEL`).

## Quick start (server, on a GPU box reachable over VPN)

```bash
git clone https://github.com/pretyflaco/vezir.git
cd vezir
pip install --user -e '.[server]'

# Seed voiceprints from existing millet profile DB
mkdir -p ~/vezir-data
vezir voiceprints seed --from ~/.config/meet/speaker_profiles.json

# Sync target — sandbox repo for development.
# vezir's worker invokes `millet sync --force --meeting-type sandbox-<HHMMSSZ>-<rand>`
# which bypasses millet's schedule and team-presence gates and
# guarantees a unique per-session folder. Every successful job lands in
# meetings/<date>_sandbox-<HHMMSSZ>-<rand>/ on the configured repo
# (e.g. meetings/2026-04-25_sandbox-194051Z-VZJJ3P/).
cat > ~/vezir-data/sync_config.json <<'EOF'
{
  "repo_url": "https://github.com/pretyflaco/vezir-meetings.git",
  "meetings": [],
  "team_members": [],
  "min_team_members": 0
}
EOF

# Initialize team roster (used by labeling UI autocomplete)
cp data/team.json.example ~/vezir-data/team.json
$EDITOR ~/vezir-data/team.json

# Issue an admin token for yourself (admin is required for /admin/enroll).
# Default expiry is 90 days; pass --expires-in 'never' to keep the old
# behavior. Re-running `token issue` is the only way to "rotate" a
# token; there is no edit-in-place.
vezir token issue --github kasita --admin --label "linux-laptop"

# Start the service. As of 0.1.12 vezir binds to 127.0.0.1:8000 by
# default and expects a reverse proxy in front (see infra/caddy/).
vezir serve

# Or, to skip git sync (artifacts stay only in ~/vezir-data/sessions/<id>/)
VEZIR_SKIP_SYNC=1 vezir serve
```

### TLS via Caddy (recommended)

The vezir server binds to loopback by default; expose it on the VPN with
Caddy:

```bash
cd infra/caddy
./install-caddy.sh
# edit the dropped Caddyfile to use your hostnames, then:
sudo systemctl enable --now caddy        # Linux
# brew services start caddy              # macOS
```

For nvpn deployments, point Caddy at its internal CA root and tell
vezir where to find it so the enrollment QR embeds it for joiners:

```bash
export VEZIR_CADDY_ROOT_CERT_PATH=/etc/ssl/caddy-root.crt
export VEZIR_COOKIE_SECURE=1   # cookie's Secure flag now safe to set
```

See [`infra/caddy/README.md`](infra/caddy/README.md) for the migration
plan and per-transport TLS strategy.

### Sync target governance

This is intentionally pointed at a private **dev sandbox** repo
(`pretyflaco/vezir-meetings`) during the pilot. Two reasons:

- production meeting-archive repos (e.g. `blinkbitcoin/blink-wip`) get
  schedule + team-presence gating from millet; vezir uses `--force`
  to override that, which is appropriate for a dev sandbox but not for
  production
- vezir may rewrite history or recreate the repo while the pipeline is
  being shaken down

To graduate to production: change `repo_url` in
`~/vezir-data/sync_config.json`, drop `--force` (planned: env var
`VEZIR_SYNC_FORCE=0`), and let millet's existing schedule/team-gate
decide what to push.

## Quick start (scribe client)

```bash
# Install vezir + millet-record (lightweight; ~30 MB base).
pip install --user vezir

# Add the Textual TUI (recommended for daily desktop use; ~5 MB extra).
pip install --user 'vezir[tui]'

# Optional: Tkinter widgets (GUI / scribe-widget); on Debian/Ubuntu:
sudo apt install python3-tk

# Configure (one-time): server URL = your vezir server's VPN hostname or tunnel IP.
# For nostr-vpn: use the server's tunnel IP (see infra/nvpn/README.md).
# For Tailscale: use the MagicDNS name or Tailscale IP.
export VEZIR_URL=https://your-vezir-server     # https when fronted by Caddy
export VEZIR_TOKEN=<token-issued-on-server>

# Internal CA (Caddy default): tell the thin client where to find it.
export SSL_CERT_FILE=/etc/caddy/certs/vezir-internal-ca.crt

# ── Textual TUI (recommended) ────────────────────────────────────────
# Record + browse sessions + label speakers + view artifacts in one
# terminal-native UI.  Press F1 for the in-app help.
vezir tui

# Boot a local server in the background and connect to it (single-box
# self-hosted setups):
vezir tui --serve

# ── CLI scribe ───────────────────────────────────────────────────────
vezir scribe --title "what this meeting is about"
# Talk; press `p` to pause / resume; Ctrl+C when done.
# By default, the recorded WAV is compressed to OGG/Opus before upload.
# Use --no-compress to upload the raw WAV instead.

# Pick a summarization preset.  Default is whatever the GUI/CLI/TUI
# last used (sticky in ~/.config/vezir/client.json); flag overrides
# for one session and updates the stickied value.
vezir scribe --preset confidential --title "board meeting"

# Privacy toggles.  --auto-label / --sync default ON and stick to the
# last chosen state; --personal defaults OFF and never persists.
vezir scribe --no-auto-label --title "research interview"
vezir scribe --no-sync --title "draft notes"
vezir scribe --personal --title "1:1 with Alice"                 # forces sync off, never sticks
vezir scribe --preset confidential --personal --no-auto-label    # maximum-privacy mode

# ── Always-on-top floating recorder (Tkinter; minimal) ───────────────
vezir scribe-widget       # just start/pause/stop + upload, no browser

# ── Legacy Tkinter full UI (recorder + session list) ─────────────────
vezir gui

# ── Upload an existing recording (WAV/OGG) ───────────────────────────
vezir upload ./previous-meeting.wav --title "previous meeting"

# Compress an existing WAV before uploading it
vezir upload ./previous-meeting.wav --compress --title "previous meeting"

# Same flags work on upload:
vezir upload ./private-call.wav --preset confidential --personal --title "1:1"
```

When the recording is uploaded, vezir prints a dashboard URL. Open it in
your browser; the GUI's "Open dashboard" button does this for you. The
URL flows through `/login?token=...` so the browser is signed in via
HttpOnly cookie before it lands on the session page; subsequent access
from the same browser does not require re-passing the token.

Live client recordings remain on the scribe machine under
`~/millet-recordings/` by default. `vezir status` is a server-side/local
diagnostic command; on a thin client it inspects that machine's local
`~/vezir-data` and does not query the remote server.

Standalone uploads currently accept `.wav` and `.ogg`, matching what the
server-side millet pipeline consumes from session folders. Use
`vezir upload --compress file.wav` to compress a WAV to OGG/Opus before
uploading. Other formats such as `.mp3`, `.m4a`, and `.webm` should be
transcoded to WAV/OGG first until server-side transcoding is added.

The client reports upload progress, retries from byte 0 after transient
connection failures, and sends the expected audio byte count so the server can
reject incomplete uploads instead of processing partial meetings.

### macOS thin client (Apple Silicon)

The same `pip install vezir` command works on macOS Apple Silicon. The base
install pulls `millet-record`, which ships a Swift sidecar binary
(`meet-record-mac`) inside the macOS arm64 wheel. This binary captures mic +
system audio via Apple's native APIs (AVAudioEngine + Core Audio Process Tap)
— no virtual audio drivers, no reboot, no Audio MIDI Setup configuration.

```bash
pip install vezir                # ~31 MB total; no ML dependencies
export VEZIR_URL=http://your-vezir-server:8000
export VEZIR_TOKEN=<token-issued-on-server>
vezir scribe --title "team sync"
# Records mic + system audio via the Swift sidecar.
# Ctrl+C to stop → compresses to OGG/Opus → uploads to server.
```

The server handles transcription, diarization, labeling, and sync. The Mac
only needs to record and upload — total install footprint is ~31 MB vs ~5 GB
for the full end-to-end (`millet-pipeline[mlx]`) pipeline.

For end-to-end local transcription on Apple Silicon (no server needed), see
[millet](https://github.com/pretyflaco/millet) directly.

## Environment variables

| Variable | Default | Effect |
|---|---|---|
| `VEZIR_DATA` | `~/vezir-data` | All runtime state — sessions, voiceprints, queue, tokens, sync_config |
| `VEZIR_HOST` | `127.0.0.1` | Bind address for `vezir serve`.  Front with Caddy in production; set to `0.0.0.0` only as an opt-in escape hatch. |
| `VEZIR_PORT` | `8000` | Port for `vezir serve` |
| `VEZIR_URL` | `http://localhost:8000` | Server URL for `vezir scribe` / `vezir tui` clients |
| `VEZIR_TOKEN` | — | Bearer token for `vezir scribe` / `vezir tui` clients |
| `SSL_CERT_FILE` | unset | **Primary** CA bundle path the thin clients check first when verifying HTTPS endpoints (e.g. Caddy internal CA at `/etc/caddy/certs/vezir-internal-ca.crt`).  Falls back to `VEZIR_CADDY_ROOT_CERT_PATH` then `certifi.where()`.  Needed because httpx defaults to certifi which doesn't include internal CAs.  Standard env var name; honored by many other Python HTTP libraries. |
| `VEZIR_CADDY_ROOT_CERT_PATH` | unset | Secondary CA bundle path.  Also embedded into the device-enrollment QR payload (v2 format) so Android clients can trust the server on first contact.  Ignored / falls back to v1 QR when unset or the file is invalid. |
| `VEZIR_COOKIE_SECURE` | unset | Set to `1` to add `Secure` to the session cookie. Recommended once Caddy is in front. |
| `VEZIR_SUMMARY_PRESET` | unset | Default summarization preset (`high-quality` \| `confidential` \| `alternative`).  Read by CLI / TUI / GUI as the initial value when `~/.config/vezir/client.json` has no `summary_preset` key.  CLI `--preset` flag takes precedence. |
| `VEZIR_TUI_DISABLE_NOTIFY_POLL` | unset | Set to `1` to disable the TUI's background "needs labeling" poll (runs every 60s by default).  Mostly useful in tests; also handy if you find the notifications noisy. |
| `VEZIR_LOG_LEVEL` | `INFO` | Logging level |
| `VEZIR_MEET_BIN` | `$(which meet)` | Path to millet `meet` binary |
| `VEZIR_MEET_DEVICE` | `mps` on Apple Silicon when supported by the installed millet stack, `cuda` when CUDA is available elsewhere, otherwise `cpu` | Device passed to `millet transcribe` |
| `VEZIR_MEET_COMPUTE_TYPE` | `int8` on CPU, `float16` on CUDA, `float32` on MPS | Compute type passed to `millet transcribe` |
| `VEZIR_MEET_TORCH_DEVICE` | auto | PyTorch device passed to `millet transcribe --torch-device` when the installed millet supports split ASR/PyTorch devices |
| `VEZIR_MEET_ASR_BACKEND` | `mlx` on Apple Silicon when available | ASR backend passed to `millet transcribe --asr-backend` when supported |
| `VEZIR_MEET_MLX_MODEL` | millet default | MLX Whisper model path/repo passed to `millet transcribe --mlx-model` |
| `VEZIR_SKIP_SYNC` | unset | Set to `1` to skip the `millet sync` step entirely (server-side kill switch; wins over per-job `sync_enabled=1`). |
| `VEZIR_DELETE_AUDIO` | unset | Set to `1` to delete audio after artifacts are produced (storage policy). Default OFF during pilot. |
| `VEZIR_SYNC_MEETING_TYPE` | `sandbox` | Subfolder name (under `meetings/`) used by `millet sync --force`. Will be removed once vezir respects schedules. |
| `VEZIR_MAX_UPLOAD_BYTES` | `2147483648` | Maximum accepted upload size (default 2 GiB). Oversized uploads return HTTP 413. |
| `VEZIR_DISABLE_RATELIMIT` | unset | Set to `1` to disable the in-process rate limiter. Test/CI only. |

On Apple Silicon, vezir prefers millet's MLX Whisper ASR backend when
`mlx-whisper` is installed and the installed `millet transcribe` supports
`--asr-backend`. Alignment and diarization still use PyTorch, so vezir also
passes `--torch-device mps` when that option is available. If MLX ASR is not
available, the fallback Apple Silicon route is CPU ASR via CTranslate2 plus
PyTorch MPS for alignment/diarization. `VEZIR_MEET_ASR_BACKEND`,
`VEZIR_MEET_MLX_MODEL`, and `VEZIR_MEET_TORCH_DEVICE` override the automatic
selection.

## Performance expectations

End-to-end processing time depends on audio quality, model size, language
detection, diarization, summary generation, and whether alignment models are
already cached. For a one-hour recording with the default large-v3-turbo-style
pipeline, use these as rough operator estimates:

| Runtime | ASR path | PyTorch alignment/diarization path | Expected time for 1h audio |
|---|---|---|---|
| NVIDIA CUDA GPU | CUDA, float16 | CUDA | ~5-20 min end-to-end |
| Apple Silicon MLX mode | MLX Whisper | MPS | ~10-30 min end-to-end |
| Apple Silicon split mode | CPU, int8 via CTranslate2 | MPS | ~20-45 min end-to-end |
| CPU only | CPU, int8 | CPU | ~1.5-10 hours end-to-end |

ASR is automatic speech recognition: the stage that turns audio into text.
In Apple Silicon MLX mode, ASR uses MLX Whisper on the Apple GPU while
alignment and diarization use PyTorch MPS. The first run downloads the selected
MLX model; subsequent runs use the local Hugging Face cache.

The most useful future improvements are:

- Add per-stage timing to worker logs so real deployments can compare ASR,
  alignment, diarization, summary, and sync costs instead of relying on broad
  estimates.
- Benchmark `mlx-community/whisper-large-v3-turbo`, `-q4`, and `-4bit` variants
  on representative meeting audio to choose the best speed/quality default.

Runtime directories are created private (`0700`) and sensitive runtime files
are written private (`0600`). The systemd unit also sets `UMask=0077` so
artifacts created by subprocesses inherit private defaults.

## What's next

The post-v0.3.1 backlog, in roughly the order I'm thinking about it:

- **`GET /api/me`** server endpoint so the TUI's background poll can
  filter "needs labeling" notifications to the user's own sessions
  rather than broadcasting team-wide.
- **Web dashboard deprecation track.**  v0.4 will restrict the
  dashboard to admin-only; v0.5 will remove it.  All new end-user
  features ship to TUI + Android first.
- **Tkinter `vezir gui` future.**  Depends on dogfood feedback from
  the team.  If `vezir tui` and `vezir scribe-widget` cover the use
  cases, `vezir gui` joins the dashboard on the deprecation track.
- **TUI paper-cut polish** as it surfaces from week-by-week dogfood.
- **Backfill `region.height` regression tests** for the older TUI
  screens (lesson from the v0.3.0 layout-clip bug: unit tests that
  check widget existence don't catch terminal-clipped rendering).

## License

MIT — see [LICENSE](LICENSE).
