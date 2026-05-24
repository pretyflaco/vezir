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

    Precedence (v0.6.2+):

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
    4. Real ``~/.config/meet/sync_config.json`` — final fallback when
       vezir hasn't been configured for sync at all.

    Returns the chosen source path, or None if no source exists.
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

    # 4. Real user config.
    real = real_meet / "sync_config.json"
    if real.exists():
        return real

    return None


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
    cmd = [config.meet_binary()] + args
    log.info("running: HOME=%s %s", home, " ".join(cmd))

    if log_path:
        config.secure_mkdir(log_path.parent)
        with log_path.open("ab") as f:
            config.secure_chmod_file(log_path)
            f.write(f"\n--- {' '.join(cmd)}\n".encode("utf-8"))
            f.flush()
            proc = subprocess.run(cmd, env=env, stdout=f, stderr=f)
    else:
        proc = subprocess.run(cmd, env=env)
    log.info("meet exited: %s", proc.returncode)
    return proc.returncode


def build_transcribe_args(session_dir: Path, *, summary_preset: str | None = None) -> list[str]:
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
        build_transcribe_args(session_dir, summary_preset=summary_preset),
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


def ensure_session_json(session_dir: Path, session_id: str) -> Path:
    """Inject a `<session_id>.session.json` if one is not present.

    Meetscribe's `_date_from_session` (meet/sync.py:321) checks first the
    directory name (which for vezir is a bare ULID, no date prefix) and
    falls back to reading `*.session.json` for `started_at`. Without an
    injected session.json, millet falls all the way through to
    datetime.now() at sync time, which is wrong (it's the worker's clock,
    not the meeting's start). For a vezir-uploaded session, the closest
    proxy for "meeting started" is the ULID's embedded timestamp.

    Returns the session.json path, creating it from the ULID if needed.
    """
    sj = session_dir / f"{session_id}.session.json"
    if sj.exists():
        return sj
    dt = _ulid_to_utc_datetime(session_id)
    if dt is None:
        from datetime import datetime, timezone
        dt = datetime.now(timezone.utc)
    payload = {
        "started_at": dt.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        "source": "vezir",
        "session_id": session_id,
        "_note": "Injected by vezir to satisfy meet/sync.py:_date_from_session.",
    }
    import json as _json
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


def sync(session_dir: Path, job_id: str, team_id: str, log_path: Path) -> int:
    """Push session to the team's configured millet sync target.

    v0.6.2+: ``team_id`` is required.  The meeting-type prefix comes
    from the team row's ``sync_meeting_type`` (set via
    ``vezir team set-sync --meeting-type ...``), falling back to the
    legacy ``VEZIR_SYNC_MEETING_TYPE`` env var (deprecated; removed in
    v0.7.0) and finally to ``'sandbox'``.

    The sync remote itself is wired through the HOME shim — see
    :func:`_resolve_team_sync_config`.  This call only constructs the
    millet CLI invocation; the shim handles where the bits actually go.

    During the sandbox phase, vezir uses --force with a per-session
    meeting type derived from the session ULID, so each session gets a
    unique folder under `meetings/` regardless of when it was recorded.
    This bypasses the schedule + team-presence gating that the millet
    CLI applies for the personal flow.

    Resulting layout in the sync repo:
        meetings/{date}_{base}-{HHMMSSZ}-{id8}/
            summary.md
            transcript.{txt,srt,json,pdf}
    """
    base = _meeting_type_base_for_team(team_id)
    # Ensure millet can extract the meeting date from the session.
    ensure_session_json(session_dir, job_id)
    meeting_type = _meeting_type_for(job_id, base=base)
    return run_meet(
        [
            "sync",
            "--force",
            "--meeting-type", meeting_type,
            str(session_dir),
        ],
        job_id=job_id,
        team_id=team_id,
        log_path=log_path,
    )


def _meeting_type_base_for_team(team_id: str) -> str:
    """Pick the meeting-type prefix for a team.

    Precedence (v0.6.2+):

    1. ``team.sync_meeting_type`` column (set via
       ``vezir team set-sync --meeting-type ...``).
    2. Legacy ``VEZIR_SYNC_MEETING_TYPE`` env var (deprecated;
       kept as a back-compat fallback so existing muscle installs
       don't need a config edit).  Removed in v0.7.0.
    3. ``'sandbox'``.
    """
    # Local import: queue.py imports config, which is fine, but
    # importing queue from meet_runner top-level would create a cycle
    # via app.py's import graph in some launch configurations.
    from . import queue as _queue
    team = _queue.get_team(team_id)
    if team:
        mtype = (team.get("sync_meeting_type") or "").strip()
        if mtype:
            return mtype
    env_base = os.environ.get("VEZIR_SYNC_MEETING_TYPE", "").strip()
    if env_base:
        log.info(
            "sync: falling back to VEZIR_SYNC_MEETING_TYPE=%r (deprecated; "
            "set team.sync_meeting_type instead)",
            env_base,
        )
        return env_base
    return "sandbox"
