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
    shim.mkdir(parents=True, exist_ok=True)

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
    shim_config.mkdir(parents=True, exist_ok=True)
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
    shim_meet.mkdir(parents=True, exist_ok=True)
    OVERRIDDEN = {"speaker_profiles.json", "sync_config.json"}
    if real_meet.is_dir():
        for entry in real_meet.iterdir():
            if entry.name in OVERRIDDEN:
                continue
            (shim_meet / entry.name).symlink_to(entry)

    # 4. Override: speaker_profiles.json -> central vezir DB.
    central = config.speaker_profiles_path()
    central.parent.mkdir(parents=True, exist_ok=True)
    if not central.exists():
        central.write_text("{}", encoding="utf-8")
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
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("ab") as f:
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
    # `meet transcribe` accepts either a .wav path or a session dir. We
    # pass the dir to keep the layout compatible with `meet sync` later.
    return run_meet(
        ["transcribe", str(session_dir)],
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


def sync(session_dir: Path, job_id: str, log_path: Path) -> int:
    """Push session to vezir's configured meetscribe sync target.

    During the sandbox phase, vezir uses --force with a fixed meeting type
    so every successfully-processed session lands in `sandbox/` regardless
    of when it was recorded. This bypasses the schedule + team-presence
    gating that the meetscribe CLI applies for the personal flow.

    The meeting type is configurable via VEZIR_SYNC_MEETING_TYPE
    (default 'sandbox').
    """
    meeting_type = os.environ.get("VEZIR_SYNC_MEETING_TYPE", "sandbox")
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
