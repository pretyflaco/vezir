<p align="left">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/logo/vezir-logo-light.svg">
    <img src="assets/logo/vezir-logo.svg" alt="vezir" width="320">
  </picture>
</p>

Self-hosted scribe service for team-scale meeting capture. Vezir wraps
[meetscribe](https://github.com/pretyflaco/meetscribe) and turns it into a
multi-user, VPN-hosted service: a designated scribe records a meeting
on their laptop, the audio uploads to a central GPU-equipped box, and the
team gets back a diarized transcript, AI summary, and PDF — with speaker
labels resolved to GitHub handles via a shared web UI.

## Status

Alpha (0.1.11). Designed for small teams that want to keep meeting audio
inside their own infrastructure: one private mesh VPN + one server (Linux
GPU box or Apple Silicon Mac). Currently dogfooded by the Blink team.

**Highlights of the 0.1.9 - 0.1.11 line:**

- **Summarization preset selector** (per upload) — pick between
  `high-quality` (Sonnet 4.6), `confidential` (DeepSeek V4 Pro in a
  Tinfoil hardware-attested TEE), or `alternative` (Kimi K2.6).
  CLI: `--preset`; GUI: dropdown; Android: dropdown.  When the
  `confidential` preset is chosen the PDF carries a red CONFIDENTIAL
  watermark on every page.
- **Auto-label opt-out** (per upload) — `--no-auto-label` skips
  server-side voiceprint matching and always routes the session to
  manual labeling.
- **Sync opt-out** (per upload) — `--no-sync` keeps the session
  `local-only` on the dashboard; artifacts stay on the vezir server
  and aren't pushed to the configured destination repo.
  Retroactively syncable from the dashboard's "Sync now" button.
- **Brand-lockup PNG in the GUI header** (replaces the older
  "vezir scribe" text label).

Supported VPN options:
- [nostr-vpn](https://github.com/mmalmi/nostr-vpn) — decentralized,
  no accounts or fees, Nostr keys for identity (see `infra/nvpn/README.md`)
- [Tailscale](https://tailscale.com/) — established, easy setup,
  requires third-party account

Linux and macOS Apple Silicon clients are fully supported.  An
Android thin client lives at
[vezir-android](https://github.com/pretyflaco/vezir-android) (v0.1.4
ships the same three opt-outs + preset selector).  On Apple Silicon
the server auto-selects MLX Whisper ASR + PyTorch MPS when available,
falling back to CPU/MPS-split mode otherwise.

Requires **`meetscribe-offline >= 0.8.1`** (pinned via the `[server]`
extra).  0.8.x adds the preset system, the Tinfoil TEE backend, the
CONFIDENTIAL PDF watermark, and several voiceprint / relabel
correctness fixes that vezir's worker depends on.

## Architecture

```
[Scribe laptop / phone]               [GPU server]
  vezir scribe / gui / upload ──▶     vezir serve (FastAPI)
   (wraps meet record;                   │   form fields:
    sends summary_preset,                │     summary_preset
    auto_label, sync as                  │     auto_label
    multipart form fields)               │     sync
                                         ├── sqlite job queue
                                         │   (summary_preset,
                                         │    auto_label_enabled,
                                         │    sync_enabled columns)
                                         ▼
                                       worker
                                         │ shells out via HOME-shim
                                         ▼
                                       meet transcribe --summary-preset <id>
                                       meet label --auto  (if auto_label_enabled)
                                       meet sync          (if sync_enabled
                                                           and VEZIR_SKIP_SYNC unset)
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
exposes the three per-upload toggles end-to-end (client → form field
→ queue column → worker gate).

## Summarization presets

A preset picks the backend + model the server will use for the AI
summary.  The client sends the preset id as the multipart form field
`summary_preset`; the worker passes it to
`meet transcribe --summary-preset <id>` which meetscribe resolves to
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
muscle, the Tinfoil SDK is pulled by the `[tee]` extra of meetscribe-
offline (`pip install 'meetscribe-offline[tee]'`) and the API key
lives in `TINFOIL_API_KEY` or at `~/models/tinfoil/tinfoil.txt`.

## Privacy toggles

Two further per-upload toggles, both **default ON** (preserves
pre-0.1.11 behavior):

| Toggle | Default | When OFF |
|---|---|---|
| `auto_label` | ON  | Server skips `meet label --auto`; session always routes to manual labeling.  Useful when you don't want the server attempting to identify speakers from previously enrolled voiceprints. |
| `sync` | ON | Server keeps the session local-only (status `done (local-only)` on the dashboard).  Artifacts stay on the vezir server but aren't pushed to the configured destination repo. |

CLI: `--auto-label/--no-auto-label` and `--sync/--no-sync` on both
`scribe` and `upload`.  Explicit choices are persisted to
`~/.config/vezir/client.json` so the next session remembers them.

GUI: two checkboxes between the preset combobox and the recorder row.

Android: two `Switch` rows on the record screen, persisted in
EncryptedSharedPreferences.

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
    cli.py                  # serve, scribe, upload, token issue
    config.py               # paths, env
    server/                 # FastAPI app, queue, worker, meet_runner
    client/                 # vezir scribe (wraps meet record + uploads)
    web/                    # templates + static
  data/
    team.json.example
  infra/
    systemd/vezir.service
  tests/
```

Runtime data lives **outside** the repo at `~/vezir-data/`.

## Install profiles

| Role | Install command | Footprint |
|---|---|---|
| **Scribe client only** (record + upload, GUI optional) | `pip install --user vezir` (or `pip install --user 'vezir[gui]'` if you also want `apt install python3-tk`) | ~30 MB |
| **Server** (FastAPI + worker + dashboard + labeling UI) | `pip install --user 'vezir[server]'` | ~3 GB on Linux/CUDA (meetscribe-offline = whisperx + torch + pyannote); on Apple Silicon also pulls `mlx-whisper` for the MLX ASR backend (~few hundred MB extra) |

The split is enforced by `pyproject.toml`'s `[project.optional-dependencies]`:
the base install uses [meetscribe-record](https://github.com/pretyflaco/meetscribe-record)
(capture only). The `[server]` extra adds [meetscribe-offline](https://github.com/pretyflaco/meetscribe)
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

# Seed voiceprints from existing meetscribe profile DB
mkdir -p ~/vezir-data
vezir voiceprints seed --from ~/.config/meet/speaker_profiles.json

# Sync target — sandbox repo for development.
# vezir's worker invokes `meet sync --force --meeting-type sandbox-<HHMMSSZ>-<rand>`
# which bypasses meetscribe's schedule and team-presence gates and
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

# Issue a token for yourself
vezir token issue --github kasita

# Start the service
vezir serve

# Or, to skip git sync (artifacts stay only in ~/vezir-data/sessions/<id>/)
VEZIR_SKIP_SYNC=1 vezir serve
```

### Sync target governance

This is intentionally pointed at a private **dev sandbox** repo
(`pretyflaco/vezir-meetings`) during the pilot. Two reasons:

- production meeting-archive repos (e.g. `blinkbitcoin/blink-wip`) get
  schedule + team-presence gating from meetscribe; vezir uses `--force`
  to override that, which is appropriate for a dev sandbox but not for
  production
- vezir may rewrite history or recreate the repo while the pipeline is
  being shaken down

To graduate to production: change `repo_url` in
`~/vezir-data/sync_config.json`, drop `--force` (planned: env var
`VEZIR_SYNC_FORCE=0`), and let meetscribe's existing schedule/team-gate
decide what to push.

## Quick start (scribe client)

```bash
# Install vezir + meetscribe-record (lightweight; ~30 MB).
pip install --user vezir

# Optional: GUI widget (Tkinter); on Debian/Ubuntu:
sudo apt install python3-tk

# Configure (one-time): server URL = your vezir server's VPN hostname or tunnel IP.
# For nostr-vpn: use the server's tunnel IP (see infra/nvpn/README.md).
# For Tailscale: use the MagicDNS name or Tailscale IP.
export VEZIR_URL=http://your-vezir-server:8000
export VEZIR_TOKEN=<token-issued-on-server>

# CLI scribe
vezir scribe --title "what this meeting is about"
# Talk; Ctrl+C when done.
# By default, the recorded WAV is compressed to OGG/Opus before upload.
# Use --no-compress to upload the raw WAV instead.

# Pick a summarization preset.  Default is whatever the GUI/CLI last
# used (sticky in ~/.config/vezir/client.json); flag overrides for one
# session and updates the stickied value.
vezir scribe --preset confidential --title "board meeting"

# Privacy toggles.  Both default ON; passing the negative form opts out
# AND stickies the OFF state for next session until explicitly toggled
# back on.
vezir scribe --no-auto-label --title "research interview"
vezir scribe --no-sync --title "draft notes"
vezir scribe --preset confidential --no-sync --no-auto-label    # paranoid mode

# GUI scribe (always-on-top widget): preset dropdown + 'Auto-label
# speakers' + 'Sync to git' checkboxes are visible directly above the
# Record button; choices persist across launches.
vezir gui

# Or upload an existing recording (WAV/OGG)
vezir upload ./previous-meeting.wav --title "previous meeting"

# Compress an existing WAV before uploading it
vezir upload ./previous-meeting.wav --compress --title "previous meeting"

# Same flags work on upload:
vezir upload ./private-call.wav --preset confidential --no-sync --title "1:1"
```

When the recording is uploaded, vezir prints a dashboard URL. Open it in
your browser; the GUI's "Open dashboard" button does this for you. The
URL flows through `/login?token=...` so the browser is signed in via
HttpOnly cookie before it lands on the session page; subsequent access
from the same browser does not require re-passing the token.

Live client recordings remain on the scribe machine under
`~/meet-recordings/` by default. `vezir status` is a server-side/local
diagnostic command; on a thin client it inspects that machine's local
`~/vezir-data` and does not query the remote server.

Standalone uploads currently accept `.wav` and `.ogg`, matching what the
server-side meetscribe pipeline consumes from session folders. Use
`vezir upload --compress file.wav` to compress a WAV to OGG/Opus before
uploading. Other formats such as `.mp3`, `.m4a`, and `.webm` should be
transcoded to WAV/OGG first until server-side transcoding is added.

The client reports upload progress, retries from byte 0 after transient
connection failures, and sends the expected audio byte count so the server can
reject incomplete uploads instead of processing partial meetings.

### macOS thin client (Apple Silicon)

The same `pip install vezir` command works on macOS Apple Silicon. The base
install pulls `meetscribe-record`, which ships a Swift sidecar binary
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
for the full end-to-end (`meetscribe-offline[mlx]`) pipeline.

For end-to-end local transcription on Apple Silicon (no server needed), see
[meetscribe](https://github.com/pretyflaco/meetscribe) directly.

## Environment variables

| Variable | Default | Effect |
|---|---|---|
| `VEZIR_DATA` | `~/vezir-data` | All runtime state — sessions, voiceprints, queue, tokens, sync_config |
| `VEZIR_HOST` | `0.0.0.0` | Bind address for `vezir serve` |
| `VEZIR_PORT` | `8000` | Port for `vezir serve` |
| `VEZIR_URL` | `http://localhost:8000` | Server URL for `vezir scribe` clients |
| `VEZIR_TOKEN` | — | Bearer token for `vezir scribe` clients |
| `VEZIR_SUMMARY_PRESET` | unset | Default summarization preset (`high-quality` \| `confidential` \| `alternative`).  Read by both CLI commands and the GUI as the initial value when `~/.config/vezir/client.json` has no `summary_preset` key.  CLI `--preset` flag takes precedence. |
| `VEZIR_LOG_LEVEL` | `INFO` | Logging level |
| `VEZIR_MEET_BIN` | `$(which meet)` | Path to meetscribe `meet` binary |
| `VEZIR_MEET_DEVICE` | `mps` on Apple Silicon when supported by the installed meetscribe stack, `cuda` when CUDA is available elsewhere, otherwise `cpu` | Device passed to `meet transcribe` |
| `VEZIR_MEET_COMPUTE_TYPE` | `int8` on CPU, `float16` on CUDA, `float32` on MPS | Compute type passed to `meet transcribe` |
| `VEZIR_MEET_TORCH_DEVICE` | auto | PyTorch device passed to `meet transcribe --torch-device` when the installed meetscribe supports split ASR/PyTorch devices |
| `VEZIR_MEET_ASR_BACKEND` | `mlx` on Apple Silicon when available | ASR backend passed to `meet transcribe --asr-backend` when supported |
| `VEZIR_MEET_MLX_MODEL` | meetscribe default | MLX Whisper model path/repo passed to `meet transcribe --mlx-model` |
| `VEZIR_SKIP_SYNC` | unset | Set to `1` to skip the `meet sync` step entirely |
| `VEZIR_DELETE_AUDIO` | unset | Set to `1` to delete audio after artifacts are produced (storage policy). Default OFF during pilot. |
| `VEZIR_SYNC_MEETING_TYPE` | `sandbox` | Subfolder name (under `meetings/`) used by `meet sync --force`. Will be removed once vezir respects schedules. |
| `VEZIR_MAX_UPLOAD_BYTES` | `2147483648` | Maximum accepted upload size (default 2 GiB). Oversized uploads return HTTP 413. |

On Apple Silicon, vezir prefers meetscribe's MLX Whisper ASR backend when
`mlx-whisper` is installed and the installed `meet transcribe` supports
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

## License

MIT — see [LICENSE](LICENSE).
