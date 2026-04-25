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
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# Seed voiceprints from existing meetscribe profile DB
mkdir -p ~/vezir-data
cp ~/.config/meet/speaker_profiles.json ~/vezir-data/speaker_profiles.json

# Initialize team roster
cp data/team.json.example ~/vezir-data/team.json
$EDITOR ~/vezir-data/team.json

# Issue a token for yourself
vezir token issue --github kasita

# Start the service
vezir serve
```

## Quick start (scribe client)

```bash
pip install -e /path/to/vezir   # or pip install vezir once published
export VEZIR_URL=http://kasita.<tailnet>.ts.net:8000
export VEZIR_TOKEN=<token-from-server>

vezir scribe                # records, uploads on Ctrl+C
```

## License

MIT — see [LICENSE](LICENSE).
