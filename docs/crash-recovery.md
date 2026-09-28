# Recovering from a crash

What to do when vezir (TUI or `scribe`) dies while recording or
uploading.  Short version: **the audio is almost certainly fine on
disk**, and since vezir 0.23.0 / millet-record 0.6.0 the TUI finds it
for you on next launch.

## Where the audio lives

```
~/vezir-meetings/<team>/meeting-YYYYMMDD-HHMMSS[_TITLE]/
```

A session dir contains some of:

| file | meaning |
|---|---|
| `*.chunk-NNN.wav` | raw recorder output, one per recorder run; valid WAV, possibly with a damaged (oversized) header if the recorder was killed |
| `meeting-….wav` | stitched final WAV (exists only after a clean stop or a recovery) |
| `*.ogg` | compressed upload artifact + local archive |
| `meeting-….session.json` | millet-record metadata; `status` is `recording` / `stopped` / `failed` / `recovered` |
| `meeting-….recorder.json` | live-recorder identity (pid + owner pid); present only while a recorder runs |
| `.upload.json` | vezir upload journal: `pending` / `uploading` / `failed` / `done` |
| `session.json` | vezir upload stub — written **only after a successful upload**; its absence means "never reached the server" |
| `recording.lock` (in the team dir) | held by the active recording; stale locks are reclaimed automatically |

## The automatic path (0.23.0+)

Open `vezir tui`.  If any local session never reached the server, a
**Recovered recordings** dialog appears listing each with its state:

- **interrupted** — recorder dead, chunks on disk.  Salvage stitches
  them (repairing SIGKILL-damaged headers) and uploads.
- **orphaned** — the recorder is *still running* but the vezir process
  that owned it is gone.  Salvage first SIGINTs the recorder (ffmpeg
  finalizes the WAV cleanly), then stitches and uploads.  Orphans keep
  capturing whatever happens next into the dead session's file, so stop
  them promptly.
- **pending upload** — recording finished; the upload didn't.  Salvage
  re-uploads the existing audio file.

Recordings whose owner process is alive are never listed (another live
vezir owns them).

`vezir doctor` reports the same three states non-interactively.

## The manual path (any version)

```bash
# 1. Find the session dir
ls -lt ~/vezir-meetings/<team>/ | head

# 2. If an ffmpeg is still writing into it, stop it gracefully (this
#    finalizes the WAV header):
kill -INT <pid>

# 3. Stitch chunks into the final WAV (millet-record 0.6.0+):
python3 -c "from millet_record.capture import recover_session; \
            print(recover_session('$HOME/vezir-meetings/<team>/<dir>'))"
#    On older millet-record, a chunk file is already a playable WAV —
#    a remux fixes the header:  ffmpeg -i chunk.wav -c copy fixed.wav

# 4. Upload
vezir upload --team <team> --title "Meeting title" --compress <file>
```

## If the TUI crashes again

The traceback is now persisted: `~/.local/state/vezir/tui.log`
(rotating, 3 × 1 MB).  Include it when reporting.
