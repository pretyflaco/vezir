<p align="left">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/logo/vezir-logo-light.svg">
    <img src="assets/logo/vezir-logo.svg" alt="vezir" width="320">
  </picture>
</p>

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

Alpha (**0.12.1**). Built for small teams that want meeting audio to stay
inside their own infrastructure: one GPU server (Linux/CUDA or Apple
Silicon) reachable over ordinary HTTPS. Full history in
[`CHANGELOG.md`](CHANGELOG.md).

**What's new (0.9 → 0.12):**

- **Rotating refresh-token sessions (0.10.0).** A login mints a **pair**: a
  short-lived **access JWT** (default 60 min) used as `Authorization:
  Bearer`, and a long-lived **refresh token** that rotates on each use
  (RFC 9700 reuse detection). Clients refresh transparently on 401 — no more
  forced re-login on expiry. `vezir logout` revokes a session. (0.12.1
  hardened the lost-response grace window against stale-token replay.)
- **Auth hardening + subprocess boundary restored (0.11.0).** Label apply /
  retry-summary go back through `millet label --apply-json`; follow-up work
  is serialized on the single worker thread (no ad-hoc threads racing it).
- **Multi-file meetings (0.9.0).** `vezir upload-multi` / `POST /upload/multi`
  stitch several audio files into one meeting.
- **Session retitle (0.12.0).** `vezir session set-title <id> "…"` /
  `POST /api/sessions/{id}/title`, or `[t]` in the TUI, to name a session
  after the fact.
- **Empty-recording handling (0.11.1).** Silent/no-speech recordings reach a
  terminal `empty` status and are not synced. Client provenance
  (`client_agent`) is recorded per session.
- **Identity sign-in.** Members sign in with **Nostr** (a remote signer like
  [Amber](https://github.com/greenart7c3/Amber) via NIP-46, or the NIP-55
  Android intent flow) or with **Google** (`@workspace-domain` accounts via
  the OAuth device grant). No key or password touches the client. `vzr_`
  bearer tokens are retained for machine/CI use.
- **Public-access front.** A small VPS terminates nothing — it
  WireGuard-forwards TLS to the server, which keeps the cert. Clients reach
  the server over plain outbound HTTPS, so it works from CGNAT / IPv6-only
  links (e.g. Starlink) with no per-client VPN.
- **Multi-team by membership (0.7.0).** A token/identity is a *person*, not
  a team; team scope is supplied per-request via `X-Team-Id` and validated
  against a memberships table. One identity covers every team you're in;
  the TUI/Android auto-discover them. Team keys are stable UUIDs with
  mutable slugs (`vezir team rename`).
- **Hardening.** NIP-98 replay protection, header-injection-resistant
  login-URL pinning (`VEZIR_PUBLIC_URL`), exact Google-domain matching.
- **Resumable uploads** (tus.io subset), **`vezir relabel`**, **`vezir
  pull`**, **per-team voiceprints + sync**, **`vezir doctor`**.

> The JSON-only API (no web dashboard since 0.7.0) is consumed by the TUI,
> the Android app, and the CLI. Speaker labeling happens in the TUI (open
> `vezir tui` → Sessions → press `l` on the row) or in the Android app.

Linux and macOS (Apple Silicon) laptop clients and an
[Android client](https://github.com/pretyflaco/vezir-android) are supported.

Requires **`millet-pipeline >= 0.13.0`** (enforced at runtime; pinned via
the `[server]` extra).

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
| **`vezir tui`** | Day-to-day desktop use — record, browse sessions, read transcripts/summaries, label speakers, all in one terminal UI. `ctrl+e` Teams tab, `ctrl+t` cycles teams. | `pip install 'vezir[tui]'` |
| **`vezir scribe`** | Headless / ssh / scripted recording. Pause-resume with `p`. | `pip install vezir` |
| **`vezir upload <file>`** | An existing WAV/OGG/MP3 (phone, OBS, etc.); resumable. `vezir upload-multi` stitches several files into one meeting. | `pip install vezir` |
| **`vezir pull`** | Download artifacts for meetings others recorded (team sharing without git). | `pip install vezir` |
| **[vezir-android](https://github.com/pretyflaco/vezir-android)** | Recording from a phone; signs in with Nostr (Amber) or Google. | Sideload the release APK |

All desktop clients resolve credentials from a `vezir login` session
(stored per-team in `~/.config/vezir/teams.json`), or from
`VEZIR_URL`+`VEZIR_TOKEN` for machine/CI. The TUI/Android auto-discover
every team you belong to from `/api/me`.

## Summarization presets

The client sends a preset id as the `summary_preset` form field; the worker
passes it to `millet transcribe --summary-preset <id>`.

| Preset | Backend | Model | Use case |
|---|---|---|---|
| `high-quality` | claudemax | Claude (Sonnet) | Default on desktop; highest quality (Claude Max on the server). |
| `confidential` | tinfoil | TEE-hosted model | Hardware-attested enclave — prompts not visible to the provider. Default on Android; PDF gets a CONFIDENTIAL watermark. |
| `alternative` | openrouter | Kimi | Cheapest cloud option. |

When a preset is explicitly chosen the server **does not silently fall
back** to another backend on failure — a silent tinfoil→cloud fallback
would defeat the Confidential preset. Set via `vezir scribe --preset …` or
the TUI/Android dropdown.

Since v0.14.0 (with millet-pipeline ≥ 0.16.0) the operator may opt in to
fallback for the **non-confidential** presets — e.g. Claude Max quota
exhausted → Kimi K3 — by setting on the vezir service:

```
MILLET_SUMMARY_PRESET_FALLBACK=1
MILLET_SUMMARY_FALLBACK_ORDER=openai
MILLET_OPENAI_BASE_URL=https://api.kimi.com/coding/v1   # sk-kimi… keys
# (pay-per-token platform keys use https://api.moonshot.ai/v1 instead)
MILLET_OPENAI_API_KEY=<kimi key>
MILLET_OPENAI_MODEL=kimi-k3
```

The `confidential` preset always stays fail-loud.  A fallback is never
silent: the session records `summary_fallback` (e.g. `openai/kimi-k3`),
shown as a `· fallback` badge in the TUI.

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
`.wav`/`.ogg`/`.mp3`.

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
fetched by `vezir pull` into `<meeting>/attachments/`, and — with
`millet-pipeline >= 0.15.0` — sync into the team's git archive under the
meeting folder, names intact. They are *not* fed to summarization.

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
| `VEZIR_SUMMARY_PRESET` | unset | Default preset (`high-quality`\|`confidential`\|`alternative`). |
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
