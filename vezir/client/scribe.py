"""`vezir scribe` — record a meeting locally, then upload to the service.

Records via the ``meet_record.capture`` library directly (gets
pause/resume for free since millet-record 0.3.0), then uploads
to the configured vezir server.  Falls back to subprocess invocation
of ``millet record`` when the library is not importable -- defensive
behavior so older deployments keep working unchanged.

Interactive keystrokes during recording:

  Ctrl+C   stop, drain buffer, upload
  p        toggle pause / resume

The ``p`` key requires the controlling terminal to be a TTY in cbreak
mode; if stdin isn't a TTY (piped, no controlling terminal) the
``p``-keystroke loop is skipped and only Ctrl+C is honored.

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
import threading
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
            "millet `meet` binary not found in PATH. Install millet-pipeline."
        )
    return found


def _check_meet_prerequisites(meet_bin: str) -> None:
    """Run ``millet check`` to verify recording prerequisites.

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
            "vezir: WARNING: `millet check` timed out — a macOS permission"
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
    """Return the default recordings directory (``~/millet-recordings/``).

    PR (vezir 0.4.0 rename): default changed from ``~/meet-recordings`` to
    ``~/millet-recordings``.  If the legacy directory exists but the new
    one doesn't, emit a one-time stderr hint suggesting the user move it.
    No auto-move — pure consent per the rename handoff rule.
    """
    explicit = os.environ.get("VEZIR_RECORD_DIR")
    if explicit:
        return Path(explicit)

    new_dir = Path.home() / "millet-recordings"
    legacy_dir = Path.home() / "meet-recordings"
    if legacy_dir.exists() and not new_dir.exists():
        global _migration_hint_shown
        if not _migration_hint_shown:
            _migration_hint_shown = True
            print(
                f"vezir: legacy recordings directory found at {legacy_dir}.\n"
                f"  millet-record writes to {new_dir} by default.\n"
                f"  To migrate: mv {legacy_dir} {new_dir}",
                file=sys.stderr,
                flush=True,
            )
    return new_dir


_migration_hint_shown = False


def _find_latest_session(output_dir: Path, before: float) -> Path | None:
    """Find the session directory created by `millet record` after `before`.

    `millet record` writes to <output_dir>/meeting-<timestamp>/. We pick
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


# ─── library-direct recording with pause/resume ──────────────────────────────


def _record_via_library(
    output_dir: Path,
    extra_record_args: list[str] | None,
) -> Path | None:
    """Record using meet_record.capture directly.

    Gets pause/resume for free (the library exposes them since
    millet-record 0.3.0).  Interactive ``p`` keystroke toggles
    pause; Ctrl+C stops and drains.

    Returns the path of the produced audio file, or None if the library
    is not importable -- caller should fall back to subprocess.

    ``extra_record_args`` is currently NOT honored on the library path;
    if the caller passes any (e.g. ``--virtual-sink``), we return None
    and let the subprocess fallback handle it.  Most users don't pass
    extras, so the common path takes the library route with pause/resume.
    """
    if extra_record_args:
        log.debug(
            "extra_record_args=%r -> skipping library path, "
            "falling back to subprocess",
            extra_record_args,
        )
        return None
    try:
        from millet_record.capture import create_session, check_prerequisites
    except ImportError as exc:
        log.debug("meet_record not importable (%s); using subprocess", exc)
        return None

    issues = check_prerequisites()
    if issues:
        # Surface to stderr just like the subprocess path's `millet check`.
        print("vezir: recording prerequisites not met:", file=sys.stderr, flush=True)
        for issue in issues:
            print(f"  {issue}", file=sys.stderr, flush=True)
        raise SystemExit(1)

    session = create_session(output_dir=str(output_dir))
    print(f"vezir: starting recording (output: {session.output_file})", flush=True)
    print(
        "vezir: press Ctrl+C to stop; press 'p' to pause/resume",
        flush=True,
    )

    session.start()

    stop_pause_thread = threading.Event()
    pause_thread = threading.Thread(
        target=_pause_keystroke_loop,
        args=(session, stop_pause_thread),
        name="vezir-pause-listener",
        daemon=True,
    )
    pause_thread.start()

    try:
        # Block until Ctrl+C.  The pause_thread handles 'p' on stdin.
        # We sleep in short slices so the watchdog can interject if
        # the recording fails fatally (sets session.status().failed).
        while True:
            time.sleep(0.5)
            st = session.status()
            if st.failed:
                print(
                    f"vezir: WARNING: recorder failed: {st.fail_reason}",
                    file=sys.stderr,
                    flush=True,
                )
                break
    except KeyboardInterrupt:
        pass
    finally:
        stop_pause_thread.set()

    print("vezir: stopping recording (draining buffer) ...", flush=True)
    out = session.stop()
    if not out.exists() or out.stat().st_size == 0:
        return None
    return out


def _pause_keystroke_loop(session, stop_event: "threading.Event") -> None:
    """Read single-character keystrokes from stdin and toggle pause.

    Skipped silently when stdin is not a TTY (piped, no controlling
    terminal, or running under a wrapper that ate stdin).  The user
    can still Ctrl+C to stop.

    Implementation note: uses ``termios`` + ``select`` for non-blocking
    cbreak-mode reads.  POSIX only -- the millet-record audio
    backends only support Linux + macOS anyway, so no Windows
    compatibility shim is needed.
    """
    if not sys.stdin.isatty():
        log.debug("stdin is not a TTY; pause keystroke listener disabled")
        return
    try:
        import select
        import termios
        import tty
    except ImportError:
        log.debug("termios/tty not available; pause keystroke listener disabled")
        return

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        while not stop_event.is_set():
            rd, _, _ = select.select([fd], [], [], 0.25)
            if not rd:
                continue
            try:
                ch = os.read(fd, 1).decode("utf-8", errors="ignore")
            except OSError:
                break
            if ch.lower() != "p":
                continue
            try:
                st = session.status()
                if st.paused:
                    session.resume()
                    print(
                        "\nvezir: resumed",
                        flush=True,
                    )
                else:
                    session.pause()
                    print(
                        "\nvezir: paused (press 'p' to resume; Ctrl+C to stop)",
                        flush=True,
                    )
            except Exception as exc:
                log.warning("pause/resume failed: %s", exc)
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        except Exception:
            pass


# ─── subprocess fallback (existing behavior) ─────────────────────────────────


def _record_via_subprocess(
    meet_bin: str,
    output_dir: Path,
    extra_record_args: list[str] | None,
) -> Path | None:
    """Fallback: spawn ``millet record`` as a subprocess.

    Used when:
      * meet_record library is not importable (older deployments)
      * caller passed extra_record_args that the library path can't honor

    No pause/resume -- only Ctrl+C stops.  Same behavior as pre-0.3.0
    ``vezir scribe``.
    """
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
            f"vezir: WARNING: millet record exited with code {proc.returncode}",
            file=sys.stderr,
        )

    sdir = _find_latest_session(output_dir, started)
    if sdir is None:
        return None
    audio_files = sorted(sdir.glob("*.wav")) or sorted(sdir.glob("*.ogg"))
    if not audio_files:
        return None
    return audio_files[0]


def _login_url(server_url: str, token: str, next_path: str) -> str:
    """Get a /login?code=vzx_...&next=... URL via the server exchange-code API.

    Falls back to a code-free /login?next=... URL (user pastes token
    manually) if the server is unreachable or running pre-0.1.12.
    """
    import httpx

    base = server_url.rstrip("/")
    try:
        r = httpx.post(
            f"{base}/api/exchange-code",
            headers={"Authorization": f"Bearer {token}"},
            params={"next": next_path},
            timeout=5,
        )
        if r.status_code == 200:
            return r.json().get("login_url") or f"{base}/login?next={quote(next_path)}"
    except Exception:
        pass
    # Fallback: code-free URL. User lands on the paste-token form.
    return f"{base}/login?next={quote(next_path)}"


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
    summary_preset: str | None = None,
    auto_label: bool = True,
    sync: bool = True,
    personal: bool = False,
) -> dict:
    """Record locally, then upload. Returns the upload response dict.

    ``personal=True`` marks the resulting session as private to the
    uploader (hidden from other team members' session lists).  The
    server forces ``sync_enabled=False`` for personal sessions
    regardless of the ``sync`` argument; we propagate the same intent
    locally by overriding ``sync`` to ``False`` when ``personal`` is
    set, so log lines and prompts read consistently.
    """
    if personal:
        sync = False
    server_url = server_url or config.server_url()
    token = token or config.client_token()
    if not token:
        raise RuntimeError("VEZIR_TOKEN is not set; run `vezir token issue` on the server")
    config.validate_token_format(token)

    output_dir = output_dir or _default_output_dir()
    output_dir.mkdir(parents=True, exist_ok=True)

    # Try the library-direct path first (gets pause/resume).  Falls
    # back to subprocess invocation of `millet record` when:
    #   * meet_record library isn't importable, or
    #   * caller passed extra_record_args we don't translate yet.
    audio = _record_via_library(output_dir, extra_record_args)
    if audio is None:
        meet_bin = _meet_bin()
        _check_meet_prerequisites(meet_bin)
        audio = _record_via_subprocess(meet_bin, output_dir, extra_record_args)
        if audio is None:
            raise RuntimeError(
                f"could not locate a session directory under {output_dir} "
                f"from this run"
            )
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
    try:
        result = uploader.upload(
            server_url,
            token,
            audio,
            title=title,
            summary_preset=summary_preset,
            auto_label=auto_label,
            sync=sync,
            personal=personal,
            progress=_progress_line,
            on_retry=_retry_line,
        )
    except Exception as exc:
        print(flush=True)
        print(
            f"vezir: upload failed after retries: {exc}",
            file=sys.stderr,
            flush=True,
        )
        print(
            f"vezir: check VPN tunnel: curl -sS {server_url.rstrip('/')}/health",
            file=sys.stderr,
            flush=True,
        )
        title_flag = f' --title "{title}"' if title else ""
        print(
            f"vezir: retry when connectivity is restored:\n"
            f"  vezir upload{title_flag} {audio}",
            file=sys.stderr,
            flush=True,
        )
        raise
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
