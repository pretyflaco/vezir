"""`vezir scribe` — record a meeting locally, then upload to the service.

Wraps unmodified meetscribe (`meet record`) as a subprocess. After
recording stops (Ctrl+C), locates the produced WAV file and uploads
it to the configured vezir server.

Behavior matches the previous-plan recommendation for client v0:
record fully, then upload (option a). Streaming during the call is
out of scope.
"""
from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from .. import config
from . import uploader

log = logging.getLogger("vezir.client.scribe")


def _meet_bin() -> str:
    explicit = os.environ.get("VEZIR_MEET_BIN")
    if explicit:
        return explicit
    found = shutil.which("meet")
    if not found:
        raise RuntimeError(
            "meetscribe `meet` binary not found in PATH. Install meetscribe-offline."
        )
    return found


def _default_output_dir() -> Path:
    return Path(os.environ.get("VEZIR_RECORD_DIR", str(Path.home() / "meet-recordings")))


def _find_latest_session(output_dir: Path, before: float) -> Path | None:
    """Find the session directory created by `meet record` after `before`.

    `meet record` writes to <output_dir>/meeting-<timestamp>/. We pick
    the newest one whose mtime >= before.
    """
    if not output_dir.exists():
        return None
    candidates = []
    for p in output_dir.iterdir():
        if not p.is_dir():
            continue
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if mtime >= before - 1:  # 1s slack
            candidates.append((mtime, p))
    if not candidates:
        return None
    candidates.sort()
    return candidates[-1][1]


def run_scribe(
    server_url: str | None = None,
    token: str | None = None,
    title: str | None = None,
    output_dir: Path | None = None,
    extra_record_args: list[str] | None = None,
) -> dict:
    """Record locally, then upload. Returns the upload response dict."""
    server_url = server_url or config.server_url()
    token = token or config.client_token()
    if not token:
        raise RuntimeError("VEZIR_TOKEN is not set; run `vezir token issue` on the server")

    output_dir = output_dir or _default_output_dir()
    output_dir.mkdir(parents=True, exist_ok=True)

    meet_bin = _meet_bin()
    cmd = [meet_bin, "record", "-o", str(output_dir)]
    if extra_record_args:
        cmd.extend(extra_record_args)

    print(f"vezir: starting recording (output: {output_dir})", flush=True)
    print("vezir: press Ctrl+C to stop the recording", flush=True)

    started = time.time()
    proc = subprocess.Popen(cmd)
    try:
        proc.wait()
    except KeyboardInterrupt:
        # Forward SIGINT to meet record so it does its drain-buffer cleanup.
        try:
            proc.send_signal(signal.SIGINT)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    if proc.returncode not in (0, -signal.SIGINT):
        print(
            f"vezir: WARNING: meet record exited with code {proc.returncode}",
            file=sys.stderr,
        )

    sdir = _find_latest_session(output_dir, started)
    if sdir is None:
        raise RuntimeError(
            f"could not locate a session directory under {output_dir} from this run"
        )
    wavs = sorted(sdir.glob("*.wav"))
    if not wavs:
        raise RuntimeError(f"no .wav file found in {sdir}")
    wav = wavs[0]
    print(f"vezir: recording captured: {wav} ({wav.stat().st_size:,} bytes)", flush=True)

    print(f"vezir: uploading to {server_url} ...", flush=True)
    result = uploader.upload(server_url, token, wav, title=title)
    print(f"vezir: uploaded as session {result['session_id']}", flush=True)
    print(f"vezir: track at {result['dashboard_url']}", flush=True)
    return result
