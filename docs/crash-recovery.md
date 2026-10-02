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
| `.upload.json` | vezir upload journal: `recording` (+ destination team, 0.25.0) / `pending` / `uploading` / `failed` / `held` / `done` |
| `session.json` | vezir upload stub — written **only after a successful upload**; its absence means "never reached the server" |
| `recording.lock` (in the team dir) | held by the active recording; stale locks are reclaimed automatically |

## The automatic path: the Outbox tab (0.25.0+)

Open `vezir tui`.  If any local recording never reached the server, the
TUI switches to the **Outbox** tab (`ctrl+o`) and says so.  Each row has
its state:

- **interrupted** — recorder dead, chunks on disk.  Upload stitches
  them (repairing SIGKILL-damaged headers) first.
- **orphaned** — the recorder is *still running* but the vezir process
  that owned it is gone; an error toast flags it at launch.  Upload first
  SIGINTs the recorder (ffmpeg finalizes the WAV cleanly).  Orphans keep
  capturing whatever happens next into the dead session's file, so stop
  them promptly.
- **pending / uploading / failed** — recording finished; the upload
  didn't.  Upload re-sends the existing audio file.
- **held** — you chose *Keep local* after Stop (or `h`).  Never nags at
  launch.

Keys: **`u`** review + upload (pick any team; the folder moves there
first), **`t`** move to a team, **`h`** hold, **`d`** discard to
`~/vezir-meetings/.trash/`, **`o`** open folder, **`a`** show all.
Recordings whose owner process is alive show as *in-progress* and are
left alone.

(0.23–0.24 showed a one-shot "Recovered recordings" dialog instead.)

`vezir doctor` reports the same states non-interactively.

## From the command line (0.24.0+): `vezir local`

```bash
vezir local list                 # the outbox: everything not on the server
vezir local list --all           # + uploaded and never-uploaded historical folders
vezir local upload <ref> [--team X] [--title T] [--personal] [--no-sync] [-y]
vezir local move <ref> --team X  # re-home a not-yet-uploaded recording
vezir local discard <ref>        # → ~/vezir-meetings/.trash/ (--purge deletes)
```

`<ref>` is the folder name, a unique prefix of it (`meeting-20261002-1233`),
or a path.  `local upload` does the whole salvage — stop an orphaned
recorder, stitch, compress, upload with the journal, link the folder to
the new session — and `--team` sends it to a different team than the one
it was recorded in.  Its `--sync`/`--auto-label` flags apply to that upload
only.

**Recorded in the wrong team?**  Since 0.25.0, change the **Team**
selector on the Record tab — it works while recording or paused — or pick
the team in the *Ready to upload* review after Stop.  Not sure yet?
*Keep local* (or Escape) and decide later from the Outbox.

Already uploaded to the wrong team?  Since 0.26.0 the uploader can move
it: `m` on the session detail screen, or
`vezir move <session-id> --to-team <right-team>`.  A copy already synced to
the old team's git repo stays there — remove it from that repo by hand.

On 0.24.x: don't press Stop (it uploads to the team the recording started
in).  Pause, quit the TUI (`ctrl+q`; while paused no recorder is running
and the audio is finalized), then:

```bash
vezir local upload meeting-YYYYMMDD-HHMMSS --team <right-team>
```

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
