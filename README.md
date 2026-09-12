<p align="left">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/logo/vezir-logo-light.svg">
    <img src="assets/logo/vezir-logo.svg" alt="vezir" width="320">
  </picture>
</p>

[![CI](https://github.com/pretyflaco/vezir/actions/workflows/ci.yml/badge.svg)](https://github.com/pretyflaco/vezir/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/vezir.svg)](https://pypi.org/project/vezir/)
[![PyPI Downloads](https://static.pepy.tech/personalized-badge/vezir?period=total&units=INTERNATIONAL_SYSTEM&left_color=BLACK&right_color=GREEN&left_text=downloads)](https://pepy.tech/projects/vezir)

**Self-hosted team intelligence.** Record a meeting on any device; Vezir
gives you back a diarized transcript, an AI summary, and a PDF — processed
on your own GPU server and synced into a private team archive you control.
Sign in with Nostr or Google.

Vezir wraps [millet](https://github.com/pretyflaco/millet) (the
transcription/diarization/summarization pipeline) and turns it into a
multi-user, multi-team service: a scribe records on their laptop or phone,
the audio uploads to a central GPU box, and the team gets back labeled
transcripts and summaries — with speakers resolved to GitHub handles.

## Status

Alpha (**0.21.0**). Built for small teams that want meeting audio to stay
inside their own infrastructure: one GPU server (Linux/CUDA or Apple
Silicon) reachable over ordinary HTTPS.

**Recent highlights** (full history in [`CHANGELOG.md`](CHANGELOG.md)):

- **Screen recordings are summarized from the screen (0.21.0).** For a
  `video` session using the `iteration-plan` template, the summary is
  deferred until after cue frames are extracted, then generated once with
  the frames attached — so the plan reports what is visibly wrong, not just
  what the narrator said. Needs `millet-pipeline >= 0.20.1`.
- **Summary attestation (0.20.0).** Every job records `<backend>/<model>` in
  `summary_provenance`; the TUI states it in the session detail and badges
  only the exception. The preset axis is deprecated.
- **Video upload + templates (0.18.0/0.19.0).** `vezir upload demo.mp4
  --template iteration-plan`, TUI video import (`ctrl+u`), and cue frames
  extracted from the recording.
- **AI harness integration (0.15.0+).** `vezir mcp` — a read-only
  [MCP](https://modelcontextprotocol.io) server (optional `[mcp]` extra) —
  plus `vezir ctx <id-or-title>` for a one-shot context doc on stdout. See
  [AI harness integration (MCP)](#ai-harness-integration-mcp).
- **Identity sign-in.** Members sign in with **Nostr** (a remote signer like
  [Amber](https://github.com/greenart7c3/Amber) via NIP-46, or the NIP-55
  Android intent flow) or with **Google** (`@workspace-domain` accounts via
  the OAuth device grant). No key or password touches the client. `vzr_`
  bearer tokens are retained for machine/CI use.
- **Multi-team by membership (0.7.0).** A token/identity is a *person*, not
  a team; scope is supplied per-request via `X-Team-Id` and validated
  against a memberships table. The TUI/Android auto-discover your teams.
- **Public-access front.** A small VPS terminates nothing — it
  WireGuard-forwards TLS to the server, which keeps the cert. Works from
  CGNAT / IPv6-only links with no per-client VPN.

> The JSON-only API (no web dashboard since 0.7.0) is consumed by the TUI,
> the Android app, and the CLI. Speaker labeling happens in the TUI (open
> `vezir tui` → Sessions → press `l` on the row) or in the Android app.

Linux and macOS (Apple Silicon) laptop clients and an
[Android client](https://github.com/pretyflaco/vezir-android) are supported.

Requires **`millet-pipeline >= 0.20.1`** (pinned by the `[server]` extra;
also probed at runtime).

## Sign-in & access

Members authenticate with their own identity; an admin authorizes it once.

```bash
# Admin, on the server — authorize an identity (one of):
vezir npub add   --npub npub1…             --github <handle> --label "<who>"
vezir google add --email them@blinkbtc.com --github <handle> --label "<who>"
# …and grant team scope (one handle covers every team they join):
vezir team add-member --team <slug> --role scribe --github <handle>
```

```bash
# Member, on their laptop:
export VEZIR_URL=https://your-vezir-host
vezir login --team <slug>                  # Nostr (remote signer / Amber)
vezir login --method google --team <slug>  # Google (@workspace-domain)
```

`vezir login` stores a rotating session (a short access JWT + a refresh
token) in `~/.config/vezir/teams.json`; the client uses the access JWT as
`Authorization: Bearer` and **refreshes transparently** on expiry — no
re-login needed. `vezir logout` revokes the session.

**Production note:** set `VEZIR_PUBLIC_URL=https://your-host` on the server
so NIP-98 login-URL verification is pinned to a fixed base (not
reconstructed from request headers).

## Architecture

```
[Scribe laptop / phone]                 [GPU server]
  vezir tui / scribe   ──HTTPS──▶   vezir serve (FastAPI, 127.0.0.1)
   (record, list,                     │  fronted by Caddy (TLS terminates here)
    label, view,                      │
    pull artifacts)                   │   identity sign-in:
                                      │     NIP-46 / NIP-55 (nostr)  ─┐
  vezir-android        ──HTTPS──▶     │     Google device grant      ─┴─▶ session JWT
   (record + sign in)                 │     vzr_ tokens (machine/CI)
                                      │
                            ┌─ public VPS front (optional) ─┐
   any network ────────────┤  WireGuard + nftables TLS-     │
   (incl. CGNAT/IPv6)       │  passthrough → server :443    │
                            └───────────────────────────────┘
                                      │
                                      ├── sqlite job queue (per-team)
                                      ▼
                                    worker  ── HOME-shim ──▶ millet
                                      │   (per-team voiceprints + sync)
                                      ▼
                                    millet transcribe / label --auto / sync
                                      └──▶ private git repo (per-team)
```

Millet runs as an unmodified subprocess via a per-job HOME shim that exposes
per-team voiceprints + sync config. Vezir owns the job queue, per-team
voiceprint DBs, team roster/memberships, and auth.

## Clients

| Client | Best for | Install |
|---|---|---|
| **`vezir tui`** | Day-to-day desktop use — record, browse sessions, read transcripts/summaries, label speakers, import a video (`ctrl+u`), all in one terminal UI. `ctrl+e` Teams tab, `ctrl+t` cycles teams. | `pip install 'vezir[tui]'` |
| **`vezir scribe`** | Headless / ssh / scripted recording. Pause-resume with `p`. | `pip install vezir` |
| **`vezir upload <file>`** | An existing WAV/OGG/MP3, or an MP4/MOV screen recording; resumable. `vezir upload-multi` stitches several files into one meeting. | `pip install vezir` |
| **`vezir pull`** | Download artifacts for meetings others recorded (team sharing without git). | `pip install vezir` |
| **[vezir-android](https://github.com/pretyflaco/vezir-android)** | Recording from a phone; signs in with Nostr (Amber) or Google. | Sideload the release APK |

All desktop clients resolve credentials from a `vezir login` session
(stored per-team in `~/.config/vezir/teams.json`), or from
`VEZIR_URL`+`VEZIR_TOKEN` for machine/CI. The TUI/Android auto-discover
every team you belong to from `/api/me`.

## AI harness integration (MCP)

Pull meeting context — session lists, summaries, transcripts — straight
into an AI coding harness (opencode, Claude Code, …) instead of manually
running `vezir pull` and pasting paths. Both entry points are **read-only**,
reuse the same credentials as `vezir pull` (`teams.json` / `VEZIR_URL` /
`VEZIR_TOKEN` / `VEZIR_TEAM_ID`), and need no server-side changes.

**`vezir mcp`** — a stdio [Model Context Protocol](https://modelcontextprotocol.io)
server (optional `[mcp]` extra):

```bash
pip install 'vezir[mcp]'
```

Wire it into opencode (`~/.config/opencode/opencode.json`):

```json
{ "mcp": { "vezir": { "type": "local", "command": ["vezir", "mcp"] } } }
```

It exposes six read-only tools:

| Tool | Returns |
|---|---|
| `list_sessions(limit, status?)` | Recent sessions (id / title / status / date / github), up to 500. |
| `search_sessions(query, limit)` | Sessions whose title matches a substring. |
| `get_summary(session_id)` | The AI summary (markdown). |
| `get_transcript(session_id, max_chars?)` | The full diarized transcript (pass `max_chars` only for a preview). |
| `list_artifacts(session_id)` | Every downloadable file: artifacts by type, plus attachments and cue frames. |
| `get_artifact(session_id, name, save_path?)` | One artifact by type key or filename; binary (PNG/PDF/MP4) needs `save_path`. |

**`vezir ctx <id-or-title>`** — a one-shot alternative for any harness (no
extra needed; base install). It pulls the session (unless `--no-pull`) and
prints a single context document (header + summary + transcript) to stdout:

```bash
vezir ctx "brainstorm phoenix" | opencode run "summarize the decisions"
opencode run "$(vezir ctx 01M0TS2SWWD15JT0VQHNDREFKH)"   # inline as prompt
vezir ctx 01M0TS2SWWD15JT0VQHNDREFKH --path              # just the artifacts dir
```

## Summarization: private by default

Every summary is produced by a private backend — a hardware-attested
Tinfoil TEE, or fully local Ollama.  millet-pipeline 0.19.0 removed the
cloud backends (Claude Max, OpenRouter, generic OpenAI) after a blind
evaluation found the TEE model beat Sonnet 4.6 on both precision and
recall in every language tested, so routing meeting content through a
provider that can read it bought nothing.  Method and numbers:
[the evaluation](https://github.com/pretyflaco/millet/blob/main/docs/tee-summarization-evaluation.md)
(10 meetings, blind, two independent in-TEE judges, 80 verdicts).

### Attestation

The worker records `<backend>/<model>` on every job in
`jobs.summary_provenance`, read from millet's `.summary.meta.json`
sidecar.  The TUI shows it in the session detail:

```
  summary: tinfoil/glm-5-3-flash (hardware-attested TEE)
```

The session list badges only the **exception** — a yellow `· unattested`
when a summary demonstrably did *not* come from a TEE (a pre-0.19.0
session, or a local Ollama fallback).  A positive badge on every row would
appear everywhere and stop being read.  Sessions with unknown provenance
(predating the column) are not badged: absence of evidence isn't evidence
of absence.

### Screen recordings are summarized from the screen

Upload an MP4/MOV with the `iteration-plan` template and Vezir samples one
cue frame per transcript timestamp, then summarizes **with the frames
attached** — so the plan can report a misrendered value or a
mislabeled control, not only what the narrator said aloud.

```bash
vezir upload ./walkthrough.mp4 --title "glow 1.1 walkthrough" \
  --template iteration-plan
```

Ordering matters and is why this is not a single pass: the frames are
sampled *from* transcript timestamps, so they cannot exist while
transcription is running — which is when the summary used to be produced.
For these sessions the worker passes `--no-summarize`, extracts frames,
then generates the summary once. Every other session keeps the single-pass
flow.

Only `glm-5-3-flash` is vision-capable in millet's allowlist; a summary
that falls back to a sibling model degrades to text-only rather than
failing. See the [case study](https://github.com/pretyflaco/millet/blob/main/docs/vision-summarization-case-study.md)
for what this does and does not buy — it is an n=1 case study, not an
evaluation.

### Presets (deprecated)

Presets used to select between backends with different privacy/quality
tradeoffs.  With only private backends left there is nothing to choose, so
`high-quality`, `confidential` and `alternative` are now **aliases for the
same default** and will be removed in 0.22.0.  They are still accepted —
stored jobs and shipped Android builds send them.

A requested preset still pins the backend: the server does **not** silently
fall back, so it either succeeds or fails loudly.  When millet does serve a
summary from a fallback, the session records `summary_fallback` and the TUI
shows a `· fallback` badge.

## Privacy toggles (per upload)

| Toggle | Default | When set | Sticky? |
|---|---|---|---|
| `auto_label` | ON | OFF skips voiceprint matching; routes to manual labeling. | Yes |
| `sync` | ON | OFF keeps the session on the server (`local-only`), not pushed to the team git repo. Retroactively syncable. | Yes |
| `personal` | OFF | ON marks it private to you and forces `sync` off for this recording. | **No** (per-recording) |

CLI: `--auto-label/--no-auto-label`, `--sync/--no-sync`, `--personal` on
`scribe` and `upload`. Server-side kill switches: `VEZIR_SKIP_SYNC=1`
(global sync off), `VEZIR_DELETE_AUDIO=1` (drop audio after artifacts).

## Install profiles

| Role | Install | Footprint |
|---|---|---|
| **Scribe client (CLI)** | `pip install --user vezir` | ~30 MB |
| **Scribe client + TUI** *(recommended desktop)* | `pip install --user 'vezir[tui]'` | ~35 MB |
| **MCP add-on** (AI harness integration) | `pip install --user 'vezir[mcp]'` | +~2 MB (the `mcp` SDK; combine as `vezir[tui,mcp]`) |
| **Server** (FastAPI + worker + pipeline) | `pip install --user 'vezir[server]'` | ~3 GB (Linux/CUDA: whisperx+torch+pyannote); +`mlx-whisper` on Apple Silicon |

The base install uses
[millet-record](https://github.com/pretyflaco/millet-record) (capture only);
`[server]` adds [millet-pipeline](https://github.com/pretyflaco/millet) for
transcription/diarization/summarization (plus `mlx-whisper` on Apple
Silicon via a PEP 508 marker for the MLX ASR backend).

## Quick start — server

```bash
git clone https://github.com/pretyflaco/vezir.git && cd vezir
pip install --user -e '.[server]'

mkdir -p ~/vezir-data
vezir voiceprints seed --from ~/.config/meet/speaker_profiles.json   # optional

vezir team create --id myteam --name "My Team"
vezir team set-sync --id myteam --remote https://github.com/yourorg/meetings.git  # optional

# Authorize yourself + grant scope (identity sign-in):
vezir npub add --npub npub1… --github you --admin --label "laptop"
vezir team add-member --team myteam --role admin --github you

export VEZIR_PUBLIC_URL=https://your-vezir-host   # recommended in prod
vezir serve                                       # binds 127.0.0.1:8000; front with Caddy
```

### TLS via Caddy

```bash
cd infra/caddy && ./install-caddy.sh
# edit the Caddyfile for your hostnames, then:
sudo systemctl enable --now caddy
```

For a public-access deployment (clients on any network, incl. CGNAT), see
[`infra/vps/`](infra/vps/) — a VPS WireGuard-forwards :443 to the server,
which terminates TLS (the VPS sees only ciphertext).

## Quick start — scribe client

```bash
pip install --user 'vezir[tui]'
export VEZIR_URL=https://your-vezir-host

vezir login --team myteam                 # Nostr / Amber
# or: vezir login --method google --team myteam

vezir tui                                 # record + browse + label
vezir scribe --title "team sync"          # CLI record (p = pause; Ctrl+C = stop)
vezir upload ./recording.ogg --title "…"  # existing file (resumable)
vezir pull                                # artifacts for meetings others recorded
vezir doctor                              # diagnose creds / connectivity / certs
```

After upload, artifacts (summary, transcript, PDF) auto-download into
`~/vezir-meetings/<team>/meeting-…/`. Standalone uploads accept
`.wav`/`.ogg`/`.mp3` and `.mp4`/`.mov`.

### Meeting attachments (0.13.0)

`vezir scribe` prints a fixed staging folder — `~/vezir-attachments/` — when
recording starts. Drop slides, agendas, screenshots or PDFs in there while the
meeting runs; when recording stops, scribe lists what it found and waits for
Enter as a last chance to add more (skipped without a TTY, or with
`--no-pause`). The files upload with the meeting, then move into that
recording's own `attachments/` folder so the staging folder is empty for the
next meeting.

`vezir tui` does the same: the record screen shows the folder and how many
files are staged, and prompts with the list when recording stops.

Attachments show up in the TUI detail screen alongside the artifacts, are
fetched by `vezir pull` into `<meeting>/attachments/`, and sync into the
team's git archive under the meeting folder, names intact. They are *not* fed to summarization.

### macOS (Apple Silicon) scribe

`pip install vezir` pulls `millet-record`, whose macOS wheel ships a Swift
sidecar that captures mic + system audio via native APIs (no virtual
drivers). Grant **both** Microphone and System Audio Recording to your
terminal app; verify with `millet check`. The server does the heavy lifting.

## Environment variables

| Variable | Default | Effect |
|---|---|---|
| `VEZIR_DATA` | `~/vezir-data` | All server runtime state. |
| `VEZIR_HOST` / `VEZIR_PORT` | `127.0.0.1` / `8000` | Bind for `vezir serve` (front with Caddy). |
| `VEZIR_PUBLIC_URL` | unset | Canonical public base URL; pins NIP-98 login-URL verification (recommended in prod). |
| `VEZIR_URL` | `http://localhost:8000` | Server URL for clients. |
| `VEZIR_TOKEN` | — | `vzr_` bearer for machine/CI clients (interactive members use `vezir login`). |
| `VEZIR_ACCESS_TTL` | `3600` | Access-JWT lifetime, seconds (rotating sessions, 0.10.0). |
| `VEZIR_REFRESH_IDLE_TTL` | `604800` | Refresh-token idle TTL (7 d); reset each rotation. |
| `VEZIR_SESSION_MAX_TTL` | `2592000` | Absolute session lifetime cap (30 d) before full re-login. |
| `VEZIR_REFRESH_GRACE` | `60` | Lost-response grace window, seconds (0.11.0; hardened 0.12.1). |
| `VEZIR_GOOGLE_CLIENT_ID` / `…_SECRET[_FILE]` / `…_ALLOWED_DOMAIN` | unset | Enable Google sign-in (server holds the secret). |
| `SSL_CERT_FILE` / `VEZIR_CADDY_ROOT_CERT_PATH` | unset | Extra internal CA to trust; the client *appends* it to the public store (0.8.0+), so public + internal hosts both validate. |
| `VEZIR_COOKIE_SECURE` | unset | `1` adds `Secure` to the session cookie (HTTPS). |
| `VEZIR_SUMMARY_PRESET` | unset | Deprecated; presets no longer select anything. |
| `VEZIR_RECORD_DIR` | `~/vezir-meetings` | Local recordings root. |
| `VEZIR_ATTACHMENTS_DIR` | `~/vezir-attachments` | Staging folder scribe watches for meeting attachments (0.13.0). |
| `VEZIR_MILLET_*` | auto | Pass-throughs to `millet transcribe` (device, compute type, ASR backend, MLX model). |
| `VEZIR_MILLET_TIMEOUT` | `14400` | Per-millet-step timeout, seconds (4 h; 0.11.0). |
| `VEZIR_SKIP_SYNC` / `VEZIR_DELETE_AUDIO` | unset | Server-side sync kill switch / audio retention. |
| `VEZIR_MAX_UPLOAD_BYTES` | `2147483648` | Max upload (2 GiB → 413); also the per-attachment cap. |
| `VEZIR_MAX_ATTACHMENTS` | `50` | Attachments stored per session (matches millet's sync cap). |
| `VEZIR_MAX_ATTACHMENT_BYTES` | `104857600` | Total attachment bytes per session (100 MiB; matches millet). |
| `VEZIR_LOG_LEVEL` | `INFO` | Logging level. |
| `VEZIR_TUI_DISABLE_UPDATE_CHECK` | unset | `1` disables the TUI's background "newer vezir on PyPI" check (0.12.0). |
| `VEZIR_DISABLE_RATELIMIT` | unset | Disable the in-process rate limiter. **Test/CI only** (logs a loud warning if set). |

## Performance (rough, 1h audio)

| Runtime | Path | Time |
|---|---|---|
| NVIDIA CUDA | CUDA float16 | ~5–20 min |
| Apple Silicon (MLX) | MLX Whisper + MPS | ~10–30 min |
| Apple Silicon (split) | CPU CTranslate2 + MPS | ~20–45 min |
| CPU only | CPU int8 | ~1.5–10 h |

Runtime dirs are created `0700`, sensitive files `0600`; the systemd unit
sets `UMask=0077`.

## License

MIT — see [LICENSE](LICENSE).
