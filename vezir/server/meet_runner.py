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


def build_home_shim(job_id: str) -> Path:
    """Create a per-job HOME shim with voiceprint symlink.

    Returns the path to use as HOME when invoking `meet`.
    """
    shim = config.jobs_dir() / job_id / "HOME"
    meet_cfg = shim / ".config" / "meet"
    meet_cfg.mkdir(parents=True, exist_ok=True)

    # Symlink the central voiceprint DB into the shim.
    central = config.speaker_profiles_path()
    central.parent.mkdir(parents=True, exist_ok=True)
    if not central.exists():
        # Create empty profile file so meetscribe sees a valid (empty) DB.
        central.write_text("{}", encoding="utf-8")

    link = meet_cfg / "speaker_profiles.json"
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(central)

    # Forward HF_TOKEN-related and other meetscribe config files if any.
    # For v0 we rely on env-var passthrough only.
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
    """Push session to the configured meetscribe sync target."""
    return run_meet(
        ["sync", str(session_dir)],
        job_id=job_id,
        log_path=log_path,
    )
