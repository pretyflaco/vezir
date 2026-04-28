# vezir

Self-hosted scribe service for team-scale meeting capture. Vezir wraps
[meetscribe](https://github.com/pretyflaco/meetscribe) and turns it into a
multi-user, Tailscale-hosted service: a designated scribe records a meeting
on their laptop, the audio uploads to a central GPU-equipped box, and the
team gets back a diarized transcript, AI summary, and PDF — with speaker
labels resolved to GitHub handles via a shared web UI.

## Status

Alpha (0.1.1). Designed for small teams that want to keep meeting audio
inside their own infrastructure: one Tailscale tailnet + one GPU-equipped
box. Currently dogfooded by the Blink team. Linux clients fully supported,
macOS thin client deferred.

## Architecture

```
[Scribe laptop]                       [GPU server]
  vezir scribe / gui / upload ──▶     vezir serve (FastAPI)
   (wraps meet record)                  │
                                        ├── sqlite job queue
                                        │
                                        ▼
                                      worker
                                        │ shells out via HOME-shim
                                        ▼
                                      meet transcribe (unmodified)
                                      meet label --auto
                                      meet sync     ──▶ private git repo
                                        │
                                        ▼
                                      web UI (labeling, dashboard)
                                       ◀── scribe browser
```

Meetscribe is invoked as an unmodified subprocess. Vezir owns its own
job queue, voiceprint database, team roster, and browser auth.

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
| **Server** (FastAPI + worker + dashboard + labeling UI) | `pip install --user 'vezir[server]'` | ~3 GB (pulls meetscribe-offline = whisperx + torch + pyannote) |

The split is enforced by `pyproject.toml`'s `[project.optional-dependencies]`:
the base install uses [meetscribe-record](https://github.com/pretyflaco/meetscribe-record)
(capture only). The `[server]` extra adds [meetscribe-offline](https://github.com/pretyflaco/meetscribe)
for the heavy transcription/diarization/summarization pipeline.

## Quick start (server, on a GPU box reachable over Tailscale)

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

# Configure (one-time): server URL = Tailscale name of your vezir server.
# If MagicDNS is unavailable, use the server's Tailscale IP instead.
export VEZIR_URL=http://your-vezir-server:8000
export VEZIR_TOKEN=<token-issued-on-server>

# CLI scribe
vezir scribe --title "what this meeting is about"
# Talk; Ctrl+C when done.

# Or GUI scribe (always-on-top widget)
vezir gui

# Or upload an existing recording (WAV/OGG)
vezir upload ./previous-meeting.wav --title "previous meeting"
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
server-side meetscribe pipeline consumes from session folders. Other formats
such as `.mp3`, `.m4a`, and `.webm` should be transcoded to WAV/OGG first
until server-side transcoding is added.

## Environment variables

| Variable | Default | Effect |
|---|---|---|
| `VEZIR_DATA` | `~/vezir-data` | All runtime state — sessions, voiceprints, queue, tokens, sync_config |
| `VEZIR_HOST` | `0.0.0.0` | Bind address for `vezir serve` |
| `VEZIR_PORT` | `8000` | Port for `vezir serve` |
| `VEZIR_URL` | `http://localhost:8000` | Server URL for `vezir scribe` clients |
| `VEZIR_TOKEN` | — | Bearer token for `vezir scribe` clients |
| `VEZIR_LOG_LEVEL` | `INFO` | Logging level |
| `VEZIR_MEET_BIN` | `$(which meet)` | Path to meetscribe `meet` binary |
| `VEZIR_SKIP_SYNC` | unset | Set to `1` to skip the `meet sync` step entirely |
| `VEZIR_DELETE_AUDIO` | unset | Set to `1` to delete audio after artifacts are produced (storage policy). Default OFF during pilot. |
| `VEZIR_SYNC_MEETING_TYPE` | `sandbox` | Subfolder name (under `meetings/`) used by `meet sync --force`. Will be removed once vezir respects schedules. |
| `VEZIR_MAX_UPLOAD_BYTES` | `2147483648` | Maximum accepted upload size (default 2 GiB). Oversized uploads return HTTP 413. |

Runtime directories are created private (`0700`) and sensitive runtime files
are written private (`0600`). The systemd unit also sets `UMask=0077` so
artifacts created by subprocesses inherit private defaults.

## License

MIT — see [LICENSE](LICENSE).
