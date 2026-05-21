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
from urllib.parse import quote

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


def _check_meet_prerequisites(meet_bin: str) -> None:
    """Run ``meet check`` to verify recording prerequisites.

    On macOS this triggers the TCC permission dialog on first use so the
    user can grant mic + system-audio access interactively. If any
    prerequisite is missing, prints the issues and raises ``SystemExit``.
    """
    try:
        result = subprocess.run(
            [meet_bin, "check"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except FileNotFoundError:
        # _meet_bin() already validated this, but guard anyway.
        return
    except subprocess.TimeoutExpired:
        print(
            "vezir: WARNING: `meet check` timed out — a macOS permission"
            " dialog may be waiting behind other windows",
            file=sys.stderr,
            flush=True,
        )
        return

    if result.returncode != 0:
        print("vezir: recording prerequisites not met:", file=sys.stderr, flush=True)
        for line in result.stderr.strip().splitlines():
            print(f"  {line}", file=sys.stderr, flush=True)
        raise SystemExit(1)


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


def _fmt_bytes(nbytes: int) -> str:
    if nbytes < 1024:
        return f"{nbytes} B"
    if nbytes < 1024 * 1024:
        return f"{nbytes / 1024:.1f} KiB"
    if nbytes < 1024 * 1024 * 1024:
        return f"{nbytes / (1024 * 1024):.1f} MiB"
    return f"{nbytes / (1024 * 1024 * 1024):.1f} GiB"


def _progress_line(sent: int, total: int, elapsed: float) -> None:
    pct = (sent / total * 100) if total else 0.0
    rate = sent / elapsed if elapsed > 0 else 0.0
    remaining = max(total - sent, 0)
    eta = remaining / rate if rate > 0 else 0.0
    print(
        f"\rupload: {pct:5.1f}%  {_fmt_bytes(sent)}/{_fmt_bytes(total)}  "
        f"{_fmt_bytes(int(rate))}/s  ETA {int(eta)}s",
        end="",
        flush=True,
    )


def _retry_line(attempt: int, retries: int, exc: Exception) -> None:
    print(
        f"\nvezir: upload attempt {attempt}/{retries} failed; "
        f"retrying from byte 0: {exc}",
        flush=True,
    )


_TERMINAL_STATUSES = {"done", "error"}
_POLL_INTERVAL = 5.0  # seconds, matches GUI


def _login_url(server_url: str, token: str, next_path: str) -> str:
    """Build a /login?token=...&next=... URL for browser hand-off."""
    base = server_url.rstrip("/")
    return f"{base}/login?token={quote(token)}&next={quote(next_path)}"


def poll_status(
    server_url: str,
    token: str,
    session_id: str,
    timeout: float = 600.0,
    open_labeling: bool = False,
) -> str | None:
    """Poll server until done or error. Returns final status.

    Prints status transitions to stdout. On needs_labeling, prints a
    prominent call-to-action with the labeling URL and continues polling
    until the session reaches done or error. Returns None on timeout.
    """
    import httpx

    base = server_url.rstrip("/")
    url = f"{base}/api/sessions/{session_id}"
    headers = {"Authorization": f"Bearer {token}"}
    deadline = time.time() + timeout
    last_status = ""
    labeling_prompted = False

    while time.time() < deadline:
        try:
            r = httpx.get(url, headers=headers, timeout=10)
            if r.status_code != 200:
                time.sleep(_POLL_INTERVAL)
                continue
            data = r.json()
            status = data.get("status", "?")

            if status != last_status:
                last_status = status
                if status == "needs_labeling" and not labeling_prompted:
                    labeling_prompted = True
                    label_url = _login_url(
                        server_url, token, f"/label/{session_id}",
                    )
                    print(flush=True)
                    print(
                        "------------------------------------------------------------",
                        flush=True,
                    )
                    print(
                        f"  Label speakers to finish: {label_url}",
                        flush=True,
                    )
                    print(
                        "------------------------------------------------------------",
                        flush=True,
                    )
                    if open_labeling:
                        import webbrowser
                        webbrowser.open_new_tab(label_url)
                    print("vezir: waiting for labeling ...", flush=True)
                elif status == "error":
                    err = data.get("error", "unknown error")
                    first_line = str(err).splitlines()[0][:200]
                    print(
                        f"vezir: status: error -- {first_line}",
                        flush=True,
                    )
                    return status
                elif status == "done":
                    print("vezir: status: done -- transcript ready", flush=True)
                    return status
                else:
                    print(f"vezir: status: {status}", flush=True)

            if status in _TERMINAL_STATUSES:
                return status
        except KeyboardInterrupt:
            print("\nvezir: polling interrupted", flush=True)
            return last_status or None
        except Exception as exc:
            log.debug("poll error: %s", exc)

        time.sleep(_POLL_INTERVAL)

    print("vezir: timed out waiting for processing", flush=True)
    return None


def run_scribe(
    server_url: str | None = None,
    token: str | None = None,
    title: str | None = None,
    output_dir: Path | None = None,
    extra_record_args: list[str] | None = None,
    compress: bool = True,
    wait: bool = True,
    wait_timeout: float = 600.0,
    open_labeling: bool = False,
) -> dict:
    """Record locally, then upload. Returns the upload response dict."""
    server_url = server_url or config.server_url()
    token = token or config.client_token()
    if not token:
        raise RuntimeError("VEZIR_TOKEN is not set; run `vezir token issue` on the server")
    config.validate_token_format(token)

    output_dir = output_dir or _default_output_dir()
    output_dir.mkdir(parents=True, exist_ok=True)

    meet_bin = _meet_bin()
    _check_meet_prerequisites(meet_bin)

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
    # Prefer WAV (what `meet record` writes), fall back to OGG (post-archive).
    audio_files = sorted(sdir.glob("*.wav")) or sorted(sdir.glob("*.ogg"))
    if not audio_files:
        raise RuntimeError(f"no .wav or .ogg file found in {sdir}")
    audio = audio_files[0]
    print(
        f"vezir: recording captured: {audio} ({_fmt_bytes(audio.stat().st_size)})",
        flush=True,
    )

    if compress and audio.suffix.lower() == ".wav":
        before = audio.stat().st_size
        print("vezir: compressing WAV to OGG/Opus before upload ...", flush=True)
        audio = uploader.compress_wav_for_upload(audio, keep_wav=True)
        after = audio.stat().st_size
        ratio = before / after if after else 0
        print(
            f"vezir: compressed {_fmt_bytes(before)} -> {_fmt_bytes(after)} "
            f"({ratio:.1f}x smaller)",
            flush=True,
        )

    print(f"vezir: uploading to {server_url} ...", flush=True)
    result = uploader.upload(
        server_url,
        token,
        audio,
        title=title,
        progress=_progress_line,
        on_retry=_retry_line,
    )
    print(flush=True)
    print(f"vezir: uploaded as session {result['session_id']}", flush=True)
    track_url = result.get("dashboard_login_url") or result["dashboard_url"]
    print(f"vezir: track at {track_url}", flush=True)

    if wait:
        print("vezir: waiting for processing ...", flush=True)
        poll_status(
            server_url, token, result["session_id"],
            timeout=wait_timeout, open_labeling=open_labeling,
        )

    return result
