"""Subprocess wrapper around unmodified millet.

Vezir does not patch millet. To redirect millet's hardcoded
voiceprint path (~/.config/meet/speaker_profiles.json), each job runs
`meet` with HOME pointed at a per-job shim directory whose
.config/meet/speaker_profiles.json is a symlink to vezir's PER-TEAM
profile DB (v0.6.2+).  After the job, profile updates flow back
automatically because the symlink is followed for writes too.

The same shim layer also redirects ~/.config/meet/sync_config.json to
a per-team override file (or one materialized from the team's
``sync_remote`` column), so different teams sync to different git
repos without operator intervention.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

from .. import config

log = logging.getLogger("vezir.meet_runner")


def _real_home() -> Path:
    """Resolve the real $HOME, ignoring any HOME override applied to vezir."""
    # pwd is more authoritative than $HOME (which we may have overridden).
    import pwd
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def build_home_shim(job_id: str, team_id: str) -> Path:
    """Create a per-job HOME shim used as $HOME when invoking millet.

    The shim is a directory whose top-level entries are symlinks back to
    the real user's home. Only millet's voiceprint database and sync
    config are redirected; everything else (.local site packages,
    .cache model downloads, .bashrc, etc.) is transparently available
    to the subprocess, so `meet` and its transitive deps work exactly
    as if invoked normally.

    The shim layout:
        <shim>/<entry>                                 -> ~/<entry>     (for every top-level entry)
        <shim>/.config/                                MATERIALIZED dir
        <shim>/.config/<entry>                         -> ~/.config/<entry>  (for every entry except 'meet')
        <shim>/.config/meet/                           MATERIALIZED dir
        <shim>/.config/meet/<file>                     -> ~/.config/meet/<file>  (for every file except overrides)
        <shim>/.config/meet/speaker_profiles.json      -> per-team vezir DB
        <shim>/.config/meet/sync_config.json           -> per-team sync config (if any)

    This avoids forwarding-list creep -- new files in real HOME or
    ~/.config/meet/ become visible automatically without code changes,
    and only the files we explicitly override are replaced.

    v0.6.2+: ``team_id`` is required.  The voiceprint DB symlink target
    is per-team, and the sync_config override (if any) is resolved per
    team — see :func:`_resolve_team_sync_config`.

    Returns the path to use as HOME when invoking `meet`.
    """
    if not team_id:
        raise ValueError("build_home_shim requires team_id (added in v0.6.2)")
    shim = config.jobs_dir() / job_id / "HOME"
    if shim.exists():
        # Stale shim from a prior crashed job: nuke it.
        shutil.rmtree(shim, ignore_errors=True)
    config.secure_mkdir(shim.parent)
    config.secure_mkdir(shim)

    real_home = _real_home()

    # 1. Top-level: symlink every entry in real home into the shim,
    #    EXCEPT '.config' which we materialize so we can override one
    #    file inside it.
    if real_home.is_dir():
        for entry in real_home.iterdir():
            if entry.name == ".config":
                continue
            (shim / entry.name).symlink_to(entry)

    # 2. .config: materialize as a real dir; symlink every child entry,
    #    EXCEPT 'meet' which we materialize so we can override one file
    #    inside it.
    real_config = real_home / ".config"
    shim_config = shim / ".config"
    config.secure_mkdir(shim_config)
    if real_config.is_dir():
        for entry in real_config.iterdir():
            if entry.name == "meet":
                continue
            (shim_config / entry.name).symlink_to(entry)

    # 3. .config/meet: materialize as a real dir; symlink every file
    #    EXCEPT speaker_profiles.json and sync_config.json (both
    #    overridden below).
    real_meet = real_config / "meet"
    shim_meet = shim_config / "meet"
    config.secure_mkdir(shim_meet)
    OVERRIDDEN = {"speaker_profiles.json", "sync_config.json"}
    if real_meet.is_dir():
        for entry in real_meet.iterdir():
            if entry.name in OVERRIDDEN:
                continue
            (shim_meet / entry.name).symlink_to(entry)

    # 4. Override: speaker_profiles.json -> per-team vezir DB.
    team_db = config.team_speaker_profiles_path(team_id)
    config.secure_mkdir(team_db.parent)
    if not team_db.exists():
        config.secure_write_text(team_db, "{}")
    else:
        config.secure_chmod_file(team_db)
    (shim_meet / "speaker_profiles.json").symlink_to(team_db)

    # 5. Override: sync_config.json — per-team resolution (v0.6.2+).
    #    Order: per-team file (B2 escape hatch) > materialized from
    #    team.sync_remote > legacy VEZIR_DATA/sync_config.json > real
    #    ~/.config/meet/sync_config.json.
    sync_source = _resolve_team_sync_config(team_id, real_meet)
    if sync_source is not None:
        (shim_meet / "sync_config.json").symlink_to(sync_source)

    return shim


def _resolve_team_sync_config(team_id: str, real_meet: Path) -> Path | None:
    """Pick a sync_config.json source for this team's HOME shim.

    Precedence (v0.6.2+; step 4 removed in 0.8.10):

    1. ``~/vezir-data/teams/<team_id>/sync_config.json`` — the B2
       escape hatch.  If present, used verbatim; ``team.sync_remote``
       is ignored.  This lets ops hand-tune millet's full sync config
       (branch, ssh key, etc.) per team.
    2. Materialize from ``team.sync_remote``: if the team has a
       non-empty ``sync_remote`` and no per-team override exists,
       write a minimal sync_config.json that points millet at that
       URL, then symlink it.
    3. Legacy global ``~/vezir-data/sync_config.json`` — preserves
       pre-v0.6.2 behavior for installs that never set per-team
       sync.

    Returns the chosen source path, or None if no team-scoped sync target
    exists.

    **0.8.10:** the real user-level ``~/.config/meet/sync_config.json`` is NO
    LONGER a fallback.  That file is the operator's personal millet config and
    on a typical install holds millet's placeholder ``example.com/global.git``;
    using it for a team job made every remote-less team attempt a doomed clone
    and land in ``sync_failed``/``sync_error``.  A team syncs only when it has a
    real, team-scoped target (steps 1-3).  ``real_meet`` is retained for
    signature compatibility but intentionally unused.
    """
    # 1. Per-team override file (escape hatch).
    override = config.team_sync_config_path(team_id)
    if override.exists():
        return override

    # 2. Materialize from team.sync_remote column.
    # Local import to avoid an import cycle (queue.py imports config).
    from . import queue as _queue
    team = _queue.get_team(team_id)
    if team and (team.get("sync_remote") or "").strip():
        materialized = _materialize_team_sync_config(team_id, team)
        if materialized is not None:
            return materialized

    # 3. Legacy global.
    legacy = config.data_dir() / "sync_config.json"
    if legacy.exists():
        return legacy

    return None


def team_has_sync_target(team_id: str) -> bool:
    """True if this team has a real, team-scoped git sync target configured.

    Mirrors :func:`_resolve_team_sync_config`'s team-scoped precedence
    (per-team override file > ``team.sync_remote`` > legacy global), WITHOUT
    materializing anything or touching the operator's personal
    ``~/.config/meet/sync_config.json``.  Callers (the worker) use this to skip
    sync entirely for remote-less teams instead of invoking millet, which would
    otherwise fall back to its placeholder remote and fail.
    """
    override = config.team_sync_config_path(team_id)
    if override.exists():
        return True

    from . import queue as _queue
    team = _queue.get_team(team_id)
    if team and (team.get("sync_remote") or "").strip():
        return True

    legacy = config.data_dir() / "sync_config.json"
    return legacy.exists()


def _materialize_team_sync_config(team_id: str, team: dict) -> Path | None:
    """Write a per-team sync_config.materialized.json from team.sync_remote.

    Uses the vezir-managed materialized-path (NOT the operator-override
    path) so a future operator-provided sync_config.json can shadow
    this without us trampling it on the next worker pass.

    Idempotent: if the file already exists and its ``remote_url``
    matches the team's current ``sync_remote``, returns the existing
    path untouched.  When the remote changes (e.g. operator runs
    ``vezir team set-sync --remote ...``), the file is rewritten so
    the next job picks up the new URL.

    Schema is intentionally minimal (B1 / B2 split): vezir writes only
    the field millet absolutely needs (``remote_url``) and leaves
    everything else at millet's defaults.  Ops who need a richer
    config use the per-team override file at
    :func:`config.team_sync_config_path` instead.
    """
    target = config.team_materialized_sync_config_path(team_id)
    remote = (team.get("sync_remote") or "").strip()
    if not remote:
        return None

    payload = {"remote_url": remote}
    desired = json.dumps(payload, indent=2)

    # Idempotency check: only rewrite when content drifts.
    if target.exists():
        try:
            current = target.read_text(encoding="utf-8")
            if current.strip() == desired.strip():
                config.secure_chmod_file(target)
                return target
        except Exception:
            pass  # fall through to rewrite

    config.secure_mkdir(target.parent)
    config.secure_write_text(target, desired)
    return target


def cleanup_home_shim(job_id: str) -> None:
    shim_root = config.jobs_dir() / job_id
    if shim_root.exists():
        shutil.rmtree(shim_root, ignore_errors=True)


def _env_for_meet(home: Path, team_id: str) -> dict:
    env = os.environ.copy()
    env["HOME"] = str(home)
    # Make sure XDG_CONFIG_HOME doesn't override our shim.
    env.pop("XDG_CONFIG_HOME", None)
    # Belt-and-suspenders: explicitly tell millet where the profile DB
    # lives, in addition to the symlink in the HOME shim.  The env var is
    # respected by millet >=0.8.2's _default_profiles_path().
    env["MEET_PROFILES_PATH"] = str(config.team_speaker_profiles_path(team_id))
    # Force offline use of the locally-cached HuggingFace models (the pyannote
    # diarization model is downloaded once and cached under ~/.cache).  Without
    # this, every diarization load makes a network HEAD request to
    # huggingface.co to check freshness; on a host with flaky DNS that adds
    # latency and noisy retries (and could stall).  The model is always cached
    # by the time transcription runs, so offline mode is safe and faster.
    env["HF_HUB_OFFLINE"] = "1"
    return env


def run_meet(
    args: list[str],
    job_id: str,
    team_id: str,
    log_path: Path | None = None,
) -> int:
    """Invoke `meet <args>` with the per-job HOME shim.

    Streams stdout/stderr to log_path if provided. Returns the meet
    process exit code.

    v0.6.2+: ``team_id`` is required so the HOME shim symlinks the
    correct per-team voiceprint DB and sync_config.
    """
    home = build_home_shim(job_id, team_id)
    env = _env_for_meet(home, team_id)
    cmd = [config.meet_binary(), *args]
    log.info("running: HOME=%s %s", home, " ".join(cmd))

    if log_path:
        config.secure_mkdir(log_path.parent)
        with log_path.open("ab") as f:
            config.secure_chmod_file(log_path)
            f.write(f"\n--- {' '.join(cmd)}\n".encode())
            f.flush()
            proc = subprocess.run(cmd, env=env, stdout=f, stderr=f)
    else:
        proc = subprocess.run(cmd, env=env)
    log.info("meet exited: %s", proc.returncode)
    return proc.returncode


def _team_default_language(team_id: str | None) -> str | None:
    """Per-team soft default-language for transcription.

    Reads ``default_language`` from the team's
    ``~/vezir-data/teams/<team_id>/sync_config.json`` (per-team), falling back
    to the global ``VEZIR_MILLET_DEFAULT_LANGUAGE`` env.  Returns None when
    unset.
    """
    if team_id:
        try:
            path = config.team_sync_config_path(team_id)
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                lang = (data.get("default_language") or "").strip()
                if lang:
                    return lang
        except Exception:
            pass
    return config.meet_default_language()


def build_transcribe_args(
    session_dir: Path,
    *,
    summary_preset: str | None = None,
    team_id: str | None = None,
) -> list[str]:
    """Build the `millet transcribe` argument list for a session directory."""
    device = config.meet_device()
    compute_type = config.meet_compute_type(device)
    torch_device = config.meet_torch_device(device)
    asr_backend = config.meet_asr_backend()
    mlx_model = config.meet_mlx_model(asr_backend)
    args = [
        "transcribe",
        "--device",
        device,
        "--compute-type",
        compute_type,
    ]
    if asr_backend:
        args.extend(["--asr-backend", asr_backend])
    if mlx_model:
        args.extend(["--mlx-model", mlx_model])
    if torch_device:
        args.extend(["--torch-device", torch_device])
    if summary_preset:
        args.extend(["--summary-preset", summary_preset])
    default_language = _team_default_language(team_id)
    if default_language and config.meet_supports_option("--default-language"):
        args.extend(["--default-language", default_language])
    args.append(str(session_dir))
    return args


def transcribe(session_dir: Path, job_id: str, team_id: str, log_path: Path,
               *, summary_preset: str | None = None) -> int:
    """Run `millet transcribe` on a session directory with --auto labeling.

    The session_dir must contain the .wav file produced by `millet record`
    (or by vezir's upload handler unpacking the upload).

    v0.6.2+: ``team_id`` is required so the HOME shim points at the
    correct per-team voiceprint DB.
    """
    # `millet transcribe` accepts either a .wav path or a session dir. We
    # pass the dir to keep the layout compatible with `millet sync` later.
    return run_meet(
        build_transcribe_args(
            session_dir, summary_preset=summary_preset, team_id=team_id,
        ),
        job_id=job_id,
        team_id=team_id,
        log_path=log_path,
    )


def label_auto(session_dir: Path, job_id: str, team_id: str, log_path: Path) -> int:
    """Run `millet label --auto` against the team's voiceprint DB.

    Confident matches are applied; unknowns remain as REMOTE_N.
    `--no-audio` keeps it non-interactive (no ffplay).
    `--no-summary` keeps it cheap (find-and-replace, no LLM re-run).

    v0.6.2+: ``team_id`` is required so the HOME shim points at the
    correct per-team voiceprint DB.
    """
    return run_meet(
        ["label", "--auto", "--no-audio", "--no-summary", str(session_dir)],
        job_id=job_id,
        team_id=team_id,
        log_path=log_path,
    )



def _ulid_to_utc_datetime(ulid_str: str):
    """Decode a ULID's embedded timestamp to a UTC datetime, or None."""
    try:
        import ulid as _ulid
        u = _ulid.from_str(ulid_str)
        return u.timestamp().datetime  # tz-aware UTC
    except Exception:
        return None


def _read_session_duration_seconds(session_dir: Path) -> float | None:
    """Read the meeting duration (seconds) from frontmatter or transcript JSON.

    Returns None if the duration cannot be determined.  Tries
    ``*.frontmatter.json`` first (ISO-8601 duration), then the main
    transcript ``*.json`` (numeric ``duration`` field).
    """
    import json as _json
    import re

    # 1. frontmatter.json: "duration": "PT1H1M54S" (ISO 8601)
    for fm in sorted(session_dir.glob("*.frontmatter.json")):
        try:
            data = _json.loads(fm.read_text(encoding="utf-8"))
            iso = data.get("duration", "")
            if iso:
                m = re.match(
                    r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?", iso
                )
                if m:
                    h = int(m.group(1) or 0)
                    mn = int(m.group(2) or 0)
                    s = float(m.group(3) or 0)
                    total = h * 3600 + mn * 60 + s
                    if total > 0:
                        return total
        except Exception:
            pass

    # 2. transcript *.json: "duration": 3713.712
    for tj in sorted(session_dir.glob("*.json")):
        if tj.name.endswith((".session.json", ".frontmatter.json",
                             ".summary.meta.json")):
            continue
        try:
            data = _json.loads(tj.read_text(encoding="utf-8"))
            dur = data.get("duration")
            if isinstance(dur, (int, float)) and dur > 0:
                return float(dur)
        except Exception:
            pass

    return None


def ensure_session_json(
    session_dir: Path, session_id: str, title: str | None = None
) -> Path:
    """Inject a `<session_id>.session.json` if one is not present.

    Meetscribe's `_date_from_session` (meet/sync.py:321) checks first the
    directory name (which for vezir is a bare ULID, no date prefix) and
    falls back to reading `*.session.json` for `started_at`. Without an
    injected session.json, millet falls all the way through to
    datetime.now() at sync time, which is wrong (it's the worker's clock,
    not the meeting's start).

    The ULID's embedded timestamp approximates session *creation* (≈ meeting
    end / upload), not meeting start.  For a 1-hour meeting, the difference
    is large enough to push the start outside the schedule-match window.
    We recover the true start as ``ULID_time - duration`` when the meeting's
    duration is available (frontmatter or transcript JSON, both present by
    the time sync runs).  Falls back to the ULID time when duration is
    unavailable.

    When ``title`` is given it is injected as ``title`` so millet's
    title-aware schedule matching (v0.12.5+) can decide whether an ad-hoc
    titled meeting belongs in a scheduled folder or its own.  If the file
    already exists but lacks a title, the title is merged in.

    Returns the session.json path, creating it from the ULID if needed.
    """
    import json as _json

    sj = session_dir / f"{session_id}.session.json"
    title = (title or "").strip() or None
    if sj.exists():
        # Back-fill the title into a previously-injected file so the
        # title-aware matching can engage on re-sync.
        if title:
            try:
                existing = _json.loads(sj.read_text(encoding="utf-8"))
                if isinstance(existing, dict) and not (
                    existing.get("title") or ""
                ).strip():
                    existing["title"] = title
                    config.secure_write_text(
                        sj, _json.dumps(existing, indent=2)
                    )
            except Exception as exc:  # non-fatal back-fill
                log.warning(
                    "could not back-fill title into %s: %s", sj, exc
                )
        return sj

    from datetime import datetime, timedelta, timezone

    dt = _ulid_to_utc_datetime(session_id)
    if dt is None:
        dt = datetime.now(timezone.utc)

    # Recover the true meeting start: ULID ≈ upload/creation time ≈ meeting
    # end.  Subtract the meeting duration to approximate the real start.
    dur = _read_session_duration_seconds(session_dir)
    if dur is not None and dur > 0:
        dt = dt - timedelta(seconds=dur)
        note = (
            "Injected by vezir; started_at = ULID_time - duration "
            f"({dur:.0f}s) to approximate true recording start."
        )
    else:
        note = (
            "Injected by vezir; started_at = ULID_time (duration unavailable, "
            "may reflect upload time rather than recording start)."
        )

    payload = {
        "started_at": dt.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        "source": "vezir",
        "session_id": session_id,
        "_note": note,
    }
    if title:
        payload["title"] = title
    config.secure_write_text(sj, _json.dumps(payload, indent=2))
    return sj


def _meeting_type_for(session_id: str, base: str = "sandbox") -> str:
    """Build a unique meeting-type string per session.

    Format: `{base}-HHMMSSZ-<rand>` where HHMMSS is UTC time from the
    ULID timestamp and `rand` is 6 chars from the ULID's random suffix
    (positions 20-26 — the trailing portion that's pure entropy, not
    timestamp).

    A naive prefix (`session_id[:8]`) collides for multiple sessions
    minted in the same millisecond, e.g. four back-to-back uploads from
    one client process all share the same timestamp prefix. The random
    suffix avoids that.
    """
    dt = _ulid_to_utc_datetime(session_id)
    if dt is None:
        from datetime import datetime, timezone
        dt = datetime.now(timezone.utc)
    hms = dt.strftime("%H%M%S")
    # ULID is 26 chars: positions 0-9 = 48 bits of timestamp,
    # 10-25 = 80 bits of randomness. Take 6 random-region chars.
    rand = session_id[-6:] if len(session_id) >= 26 else "noulid"
    return f"{base}-{hms}Z-{rand}"


def sync(
    session_dir: Path,
    job_id: str,
    team_id: str,
    log_path: Path,
    meeting_type: str | None = None,
) -> int:
    """Push session to the team's configured millet sync target.

    v0.7.0 hybrid approach (Option C):

    1. **Try schedule-matched sync** — invoke ``millet sync`` without
       ``--force`` and without ``--meeting-type``.  If the session time
       matches a configured meeting schedule in the team's
       ``sync_config.json``, millet produces a clean folder name that
       matches the established repo convention (e.g.
       ``2026-05-26_dev-standup-daily``).

    2. **If no schedule match** — retry with ``--force`` using a
       title-based folder name derived from the session title (e.g.
       ``board-meeting-160000Z-GVXGJ0``).  Falls back to the team's
       ``sync_meeting_type`` column (default ``meeting``) for untitled
       sessions.

    **Explicit override (v0.7.16):** when ``meeting_type`` is given (e.g.
    from the "sync as" dialog or the ``/session/{id}/sync`` body), schedule
    detection is skipped entirely and the session is force-synced straight
    into ``meetings/<date>_<slug>/``.  The override is slugified for path
    safety.

    The sync remote is wired through the HOME shim — see
    :func:`_resolve_team_sync_config`.
    """
    title = _get_job_title(job_id)
    ensure_session_json(session_dir, job_id, title=title)

    # Explicit override: skip schedule detection, force the chosen folder.
    if meeting_type:
        slug = config.sync_slug(meeting_type) or meeting_type
        log.info(
            "job %s: explicit sync override; forcing --meeting-type %s",
            job_id, slug,
        )
        return run_meet(
            ["sync", "--force", "--meeting-type", slug, str(session_dir)],
            job_id=job_id,
            team_id=team_id,
            log_path=log_path,
        )

    # Step 1: try schedule-matched sync.
    rc1 = run_meet(
        ["sync", str(session_dir)],
        job_id=job_id,
        team_id=team_id,
        log_path=log_path,
    )
    if rc1 == 0 and _sync_log_shows_push(log_path):
        log.info("job %s: schedule-matched sync succeeded", job_id)
        return 0

    # Distinguish "no schedule match" (legitimately force a title-based folder)
    # from "schedule matched but the git op failed" (a transient/real error).
    # Only the former should fall through to --force --meeting-type; forcing
    # after a *matched* sync that merely failed to push created a DUPLICATE
    # folder on the remote (e.g. blink-sync-151630Z next to blink-sync-weekly).
    if not _sync_log_shows_skipped(log_path):
        log.warning(
            "job %s: schedule-matched sync did not push and was not skipped "
            "(likely a git error); not force-creating a duplicate folder",
            job_id,
        )
        return rc1 if rc1 != 0 else 1

    # Step 2: no schedule match.  Retry with a title-based folder name.
    # (``title`` was already fetched above for session.json injection.)
    base = _title_slug_for_sync(title) or _meeting_type_base_for_team(team_id)
    fallback_type = _meeting_type_for(job_id, base=base)
    log.info(
        "job %s: no schedule match; retrying with --force "
        "--meeting-type %s",
        job_id, fallback_type,
    )
    return run_meet(
        [
            "sync",
            "--force",
            "--meeting-type", fallback_type,
            str(session_dir),
        ],
        job_id=job_id,
        team_id=team_id,
        log_path=log_path,
    )


def _sync_log_shows_push(log_path: Path) -> bool:
    """Return True if the last ``millet sync`` block shows files were pushed.

    Scans for positive confirmation markers ("Pushed", "Done:") after the
    last ``millet sync`` line.  Returns False if the log contains "Skipped"
    or no push markers are found.
    """
    if not log_path or not log_path.exists():
        return False
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return False
    # Find last sync block.
    idx = text.rfind("millet sync")
    tail = text[idx:] if idx >= 0 else ""
    if not tail:
        return False
    # "Skipped" means no schedule match — not a push.
    if "Skipped:" in tail:
        return False
    # Positive markers from millet's sync CLI output.
    return "Pushed" in tail or "Done:" in tail


def _sync_log_shows_skipped(log_path: Path) -> bool:
    """True if the last ``millet sync`` block was SKIPPED (no schedule match).

    millet prints "Skipped: not a scheduled meeting" when the session time
    doesn't match any configured meeting and ``--force`` wasn't given.  This
    is the ONLY case where retrying with ``--force --meeting-type`` is correct;
    a schedule match that failed to push must not be force-retried (it would
    create a duplicate folder under a different name).
    """
    if not log_path or not log_path.exists():
        return False
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return False
    idx = text.rfind("millet sync")
    tail = text[idx:] if idx >= 0 else ""
    return "Skipped:" in tail


def _get_job_title(job_id: str) -> str | None:
    """Fetch the session title from the job queue."""
    from . import queue as _queue
    job = _queue.get(job_id)
    if not job:
        return None
    return (job.get("title") or "").strip() or None


def _title_slug_for_sync(title: str | None) -> str | None:
    """Convert a session title to a sync-folder slug, or None if empty."""
    if not title:
        return None
    slug = config.sync_slug(title)
    return slug or None


def _meeting_type_base_for_team(team_id: str) -> str:
    """Fallback meeting-type prefix for untitled, unscheduled sessions.

    Precedence:
    1. ``team.sync_meeting_type`` column.
    2. ``'meeting'`` (default).
    """
    from . import queue as _queue
    team = _queue.get_team(team_id)
    if team:
        mtype = (team.get("sync_meeting_type") or "").strip()
        if mtype:
            return mtype
    return "meeting"
