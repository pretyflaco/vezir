# vezir

Internal scribe service for team-scale meeting capture. Vezir wraps
[meetscribe](https://github.com/pretyflaco/meetscribe) and turns it into a
multi-user, Tailscale-hosted service: a designated scribe records a meeting
on their laptop, the audio uploads to a central GPU-equipped box, and the
team gets back a diarized transcript, AI summary, and PDF — with speaker
labels resolved to GitHub handles via a shared web UI.

## Status

Alpha. Local-only, single-tenant (Blink team), Linux + macOS clients,
no remote git push. See `docs/PLAN.md` for the active roadmap.

## Architecture

```
[Scribe laptop]                       [kasita / GPU server]
  vezir scribe        ──upload──▶     vezir serve (FastAPI)
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
job queue, voiceprint database, and team roster.

## Repo layout

```
vezir/
  vezir/                    # python package
    cli.py                  # serve, scribe, token issue
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

## Quick start (server, on kasita)

```bash
cd /home/kasita/models/vezir
pip install --user -e . --no-deps   # vezir uses /usr/bin/python3 on kasita
                                    # (deps already present from meetscribe)

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
pip install -e /path/to/vezir   # or pip install vezir once published
export VEZIR_URL=http://kasita.<tailnet>.ts.net:8000
export VEZIR_TOKEN=<token-from-server>

vezir scribe                # records, uploads on Ctrl+C
```

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

## License

MIT — see [LICENSE](LICENSE).
