"""Subprocess wrapper around unmodified meetscribe.

Vezir does not patch meetscribe. To redirect meetscribe's hardcoded
voiceprint path (~/.config/meet/speaker_profiles.json), each job runs
`meet` with HOME pointed at a per-job shim directory whose
.config/meet/speaker_profiles.json is a symlink to the central vezir
profile DB. After the job, profile updates flow back automatically
because the symlink is followed for writes too.
"""
from __future__ import annotations

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


def build_home_shim(job_id: str) -> Path:
    """Create a per-job HOME shim used as $HOME when invoking meetscribe.

    The shim is a directory whose top-level entries are symlinks back to
    the real user's home. Only meetscribe's voiceprint database is
    redirected to vezir's central DB. Everything else (.local site
    packages, .cache model downloads, .bashrc, etc.) is transparently
    available to the subprocess, so `meet` and its transitive deps work
    exactly as if invoked normally.

    The shim layout:
        <shim>/<entry>                                 -> ~/<entry>     (for every top-level entry)
        <shim>/.config/                                MATERIALIZED dir
        <shim>/.config/<entry>                         -> ~/.config/<entry>  (for every entry except 'meet')
        <shim>/.config/meet/                           MATERIALIZED dir
        <shim>/.config/meet/<file>                     -> ~/.config/meet/<file>  (for every file except 'speaker_profiles.json')
        <shim>/.config/meet/speaker_profiles.json      -> central vezir DB

    This avoids forwarding-list creep -- new files in real HOME or
    ~/.config/meet/ become visible automatically without code changes,
    and only the single file we explicitly override is replaced.

    Returns the path to use as HOME when invoking `meet`.
    """
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
    #    EXCEPT speaker_profiles.json (redirected to vezir's central DB)
    #    and sync_config.json (redirected to vezir's sandbox config when
    #    one exists in VEZIR_DATA, else falls back to the real one).
    real_meet = real_config / "meet"
    shim_meet = shim_config / "meet"
    config.secure_mkdir(shim_meet)
    OVERRIDDEN = {"speaker_profiles.json", "sync_config.json"}
    if real_meet.is_dir():
        for entry in real_meet.iterdir():
            if entry.name in OVERRIDDEN:
                continue
            (shim_meet / entry.name).symlink_to(entry)

    # 4. Override: speaker_profiles.json -> central vezir DB.
    central = config.speaker_profiles_path()
    config.secure_mkdir(central.parent)
    if not central.exists():
        config.secure_write_text(central, "{}")
    else:
        config.secure_chmod_file(central)
    (shim_meet / "speaker_profiles.json").symlink_to(central)

    # 5. Override: sync_config.json. If vezir has its own at
    #    VEZIR_DATA/sync_config.json, use that. Else fall back to the
    #    real ~/.config/meet/sync_config.json (preserves prior behavior
    #    when vezir hasn't been configured for sync yet).
    vezir_sync = config.data_dir() / "sync_config.json"
    real_sync = real_meet / "sync_config.json"
    if vezir_sync.exists():
        (shim_meet / "sync_config.json").symlink_to(vezir_sync)
    elif real_sync.exists():
        (shim_meet / "sync_config.json").symlink_to(real_sync)

    return shim


def cleanup_home_shim(job_id: str) -> None:
    shim_root = config.jobs_dir() / job_id
    if shim_root.exists():
        shutil.rmtree(shim_root, ignore_errors=True)


def _env_for_meet(home: Path) -> dict:
    env = os.environ.copy()
    env["HOME"] = str(home)
    # Make sure XDG_CONFIG_HOME doesn't override our shim.
    env.pop("XDG_CONFIG_HOME", None)
    return env


def run_meet(args: list[str], job_id: str, log_path: Path | None = None) -> int:
    """Invoke `meet <args>` with the per-job HOME shim.

    Streams stdout/stderr to log_path if provided. Returns the meet
    process exit code.
    """
    home = build_home_shim(job_id)
    env = _env_for_meet(home)
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


def transcribe(session_dir: Path, job_id: str, log_path: Path) -> int:
    """Run `meet transcribe` on a session directory with --auto labeling.

    The session_dir must contain the .wav file produced by `meet record`
    (or by vezir's upload handler unpacking the upload).
    """
    device = config.meet_device()
    compute_type = config.meet_compute_type(device)
    # `meet transcribe` accepts either a .wav path or a session dir. We
    # pass the dir to keep the layout compatible with `meet sync` later.
    return run_meet(
        [
            "transcribe",
            "--device",
            device,
            "--compute-type",
            compute_type,
            str(session_dir),
        ],
        job_id=job_id,
        log_path=log_path,
    )


def label_auto(session_dir: Path, job_id: str, log_path: Path) -> int:
    """Run `meet label --auto` against the central voiceprint DB.

    Confident matches are applied; unknowns remain as REMOTE_N.
    `--no-audio` keeps it non-interactive (no ffplay).
    `--no-summary` keeps it cheap (find-and-replace, no LLM re-run).
    """
    return run_meet(
        ["label", "--auto", "--no-audio", "--no-summary", str(session_dir)],
        job_id=job_id,
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
    injected session.json, meetscribe falls all the way through to
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


def sync(session_dir: Path, job_id: str, log_path: Path) -> int:
    """Push session to vezir's configured meetscribe sync target.

    During the sandbox phase, vezir uses --force with a per-session
    meeting type derived from the session ULID, so each session gets a
    unique folder under `meetings/` regardless of when it was recorded.
    This bypasses the schedule + team-presence gating that the meetscribe
    CLI applies for the personal flow.

    Resulting layout in the sync repo:
        meetings/{date}_{base}-{HHMMSSZ}-{id8}/
            summary.md
            transcript.{txt,srt,json,pdf}

    The base is configurable via VEZIR_SYNC_MEETING_TYPE (default 'sandbox').
    """
    base = os.environ.get("VEZIR_SYNC_MEETING_TYPE", "sandbox")
    # Ensure meetscribe can extract the meeting date from the session.
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
        log_path=log_path,
    )
