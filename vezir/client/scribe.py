"""`vezir scribe` — record a meeting locally, then upload to the service.

Records via the ``meet_record.capture`` library directly (gets
pause/resume for free since millet-record 0.3.0), then uploads
to the configured vezir server.  Falls back to subprocess invocation
of ``millet record`` when the library is not importable -- defensive
behavior so older deployments keep working unchanged.

Interactive keystrokes during recording:

  Ctrl+C   stop, drain buffer, upload
  p        toggle pause / resume

Meeting attachments (0.13.0): the fixed staging folder printed at record
start collects supporting material while the meeting runs; after recording
stops the user gets one last chance to add files, and they upload with the
meeting.  See ``_announce_attachments_folder`` / ``_send_attachments``.

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

from .. import config
from . import uploader

log = logging.getLogger("vezir.client.scribe")


def _meet_bin() -> str:
    # Prefer the explicit override (new VEZIR_MILLET_BIN, legacy
    # VEZIR_MEET_BIN), then the renamed `millet` console script, then
    # the legacy `meet` name.
    explicit = os.environ.get("VEZIR_MILLET_BIN") or os.environ.get("VEZIR_MEET_BIN")
    if explicit:
        return explicit
    found = shutil.which("millet") or shutil.which("meet")
    if not found:
        raise RuntimeError(
            "millet binary not found in PATH (looked for `millet` and "
            "legacy `meet`). Install millet-pipeline / millet-record."
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
    """Return the default recordings directory: ~/vezir-meetings/<team>/.

    v0.7.0: standardized on per-team subdirectory under ~/vezir-meetings/.
    Respects VEZIR_RECORD_DIR override via config.recordings_dir().
    """
    return config.recordings_dir()


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


_TERMINAL_STATUSES = {"done", "error", "empty"}
_POLL_INTERVAL = 5.0  # seconds, matches GUI


# ─── meeting attachments (issue #16) ─────────────────────────────────────────
#
# Users drop slides/agendas/screenshots into one fixed folder while the
# meeting runs; after recording stops they get a last chance to add more,
# then the files ride along to the server and move into the recording's own
# ``attachments/`` so the staging folder is empty for the next meeting.

ATTACHMENTS_SUBDIR = "attachments"


def _staged_attachments() -> list[Path]:
    """Files currently in the staging folder, in a stable order.

    Dotfiles are skipped (``.DS_Store`` and editor droppings are not
    attachments).  Symlinks are followed on purpose — linking a large file
    instead of copying it is a reasonable way to attach it, and unlike the
    server side nothing here is copied into someone else's repository.
    """
    from .config import attachments_dir

    adir = attachments_dir()
    if not adir.is_dir():
        return []
    return sorted(
        p for p in adir.iterdir()
        if p.is_file() and not p.name.startswith(".")
    )


def _announce_attachments_folder() -> Path:
    """Create and print the staging folder before recording starts."""
    from .config import attachments_dir

    adir = attachments_dir()
    try:
        adir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning("could not create attachments folder %s: %s", adir, exc)
        return adir
    print(f"vezir: attachments folder: {adir}", flush=True)
    print(
        "vezir: copy slides, PDFs, images, or docs there while recording.",
        flush=True,
    )
    return adir


def _attachment_pause(no_pause: bool) -> None:
    """Last chance to drop files in before the upload goes out.

    Skipped without a TTY (scribe is documented for headless/ssh/scripted
    use) or with ``--no-pause``.  A Ctrl-C or EOF at the prompt continues
    with the upload rather than aborting: the meeting is already recorded,
    and losing that to a stray keystroke is far worse than an unwanted
    upload the user can delete.
    """
    from .config import attachments_dir

    staged = _staged_attachments()
    print(f"vezir: attachments folder: {attachments_dir()}", flush=True)
    if staged:
        print(f"vezir: found {len(staged)} attachment(s):", flush=True)
        for p in staged:
            print(f"  - {p.name} ({_fmt_bytes(p.stat().st_size)})", flush=True)
    else:
        print("vezir: no attachments staged.", flush=True)
    if no_pause or not sys.stdin.isatty():
        return
    print("vezir: copy any last-minute documents there now.", flush=True)
    try:
        input("vezir: press Enter to upload when ready ... ")
    except (EOFError, KeyboardInterrupt):
        print(flush=True)


def _unique_dest(dest_dir: Path, name: str) -> Path:
    """``dest_dir/name``, suffixed so an existing file is never clobbered."""
    candidate = dest_dir / name
    if not candidate.exists():
        return candidate
    stem, dot, suffix = name.rpartition(".")
    if not dot:
        stem, suffix = name, ""
    for n in range(2, 1000):
        alt = f"{stem}_{n}.{suffix}" if suffix else f"{stem}_{n}"
        candidate = dest_dir / alt
        if not candidate.exists():
            return candidate
    return dest_dir / f"{stem}_{int(time.time())}{('.' + suffix) if suffix else ''}"


def _move_staged_into_recording(session_dir: Path, staged: list[Path]) -> None:
    """Move uploaded attachments next to the local recording.

    Keeps the files (they are the user's) while emptying the fixed staging
    folder for the next meeting.  Best effort: a failure here must not fail
    a run whose audio and attachments are already on the server.
    """
    dest_dir = session_dir / ATTACHMENTS_SUBDIR
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(
            f"vezir: could not create {dest_dir}: {exc}; attachments left in "
            f"the staging folder",
            file=sys.stderr, flush=True,
        )
        return
    moved = 0
    for p in staged:
        try:
            shutil.move(str(p), str(_unique_dest(dest_dir, p.name)))
            moved += 1
        except OSError as exc:
            print(
                f"vezir: could not move {p.name} to {dest_dir}: {exc}",
                file=sys.stderr, flush=True,
            )
    if moved:
        print(f"vezir: moved {moved} attachment(s) to {dest_dir}", flush=True)


def _send_attachments(
    server_url: str,
    token: str,
    session_id: str,
    session_dir: Path,
    team_id: str | None,
) -> None:
    """Upload staged attachments, then move them next to the recording.

    A failure warns and leaves the staging folder untouched — the meeting
    itself is already uploaded, so this must never raise.
    """
    staged = _staged_attachments()
    if not staged:
        return
    print(f"vezir: uploading {len(staged)} attachment(s) ...", flush=True)
    try:
        stored = uploader.upload_attachments(
            server_url, token, session_id, staged, team_id=team_id,
        )
    except Exception as exc:
        print(
            f"vezir: attachment upload failed: {exc}",
            file=sys.stderr, flush=True,
        )
        print(
            "vezir: the files are still in the staging folder; the meeting "
            "itself uploaded fine.",
            file=sys.stderr, flush=True,
        )
        return
    for item in stored:
        print(f"vezir:   attached {item.get('name')}", flush=True)
    _move_staged_into_recording(session_dir, staged)


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
        from millet_record.capture import check_prerequisites, create_session
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


def _pause_keystroke_loop(session, stop_event: threading.Event) -> None:
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


def poll_status(
    server_url: str,
    token: str,
    session_id: str,
    timeout: float = 600.0,
    open_labeling: bool = False,
    team_id: str | None = None,
) -> str | None:
    """Poll server until done or error. Returns final status.

    Prints status transitions to stdout. On needs_labeling, prints a
    prominent call-to-action and continues polling until the session
    reaches done or error. Returns None on timeout.

    v0.12.1: send the ``X-Team-Id`` header (required by v0.7.0+ servers —
    without it every ``GET /api/sessions/{id}`` is a hard 400 and the
    poll silently times out) and resolve TLS trust the same way the rest
    of the client does (internal Caddy CA support).  Persistent non-200s
    are surfaced instead of being swallowed until the deadline.
    """
    import httpx

    from .trust import resolve_verify

    base = server_url.rstrip("/")
    url = f"{base}/api/sessions/{session_id}"
    headers = {"Authorization": f"Bearer {token}"}
    if team_id:
        headers["X-Team-Id"] = team_id
    verify = resolve_verify()
    deadline = time.time() + timeout
    last_status = ""
    labeling_prompted = False
    consecutive_errors = 0
    _MAX_CONSECUTIVE_ERRORS = 6

    while time.time() < deadline:
        try:
            r = httpx.get(url, headers=headers, timeout=10, verify=verify)
            if r.status_code != 200:
                consecutive_errors += 1
                if consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                    detail = r.text.strip().splitlines()[0][:200] if r.text else ""
                    print(
                        f"vezir: polling gave up after {consecutive_errors} "
                        f"failed requests (HTTP {r.status_code}"
                        f"{': ' + detail if detail else ''})",
                        file=sys.stderr,
                        flush=True,
                    )
                    return None
                time.sleep(_POLL_INTERVAL)
                continue
            consecutive_errors = 0
            data = r.json()
            status = data.get("status", "?")

            if status != last_status:
                last_status = status
                if status == "needs_labeling" and not labeling_prompted:
                    labeling_prompted = True
                    print(flush=True)
                    print(
                        "------------------------------------------------------------",
                        flush=True,
                    )
                    print(
                        f"  Session {session_id} needs speaker labeling.",
                        flush=True,
                    )
                    print(
                        "  Open `vezir tui` -> Sessions -> press 'l' on this row,",
                        flush=True,
                    )
                    print(
                        "  or use the Vezir Android app to apply labels.",
                        flush=True,
                    )
                    print(
                        "------------------------------------------------------------",
                        flush=True,
                    )
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
                elif status == "empty":
                    print(
                        "vezir: status: empty -- no speech detected; "
                        "nothing to sync",
                        flush=True,
                    )
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
            consecutive_errors += 1
            if consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                print(
                    f"vezir: polling gave up after {consecutive_errors} "
                    f"connection errors (last: {exc})",
                    file=sys.stderr,
                    flush=True,
                )
                return None

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
    no_pause: bool = False,
) -> dict:
    """Record locally, then upload. Returns the upload response dict.

    ``no_pause=True`` skips the post-recording attachment prompt (it is
    skipped automatically when stdin is not a TTY).

    ``personal=True`` marks the resulting session as private to the
    uploader (hidden from other team members' session lists).  The
    server forces ``sync_enabled=False`` for personal sessions
    regardless of the ``sync`` argument; we propagate the same intent
    locally by overriding ``sync`` to ``False`` when ``personal`` is
    set, so log lines and prompts read consistently.
    """
    if personal:
        sync = False
    # Resolve credentials + the active team so the upload carries
    # X-Team-Id (required by v0.7.0+ servers).
    team_id: str | None = None
    if not server_url or not token:
        from .config import resolve_credentials
        r_url, r_token, r_team, _src = resolve_credentials()
        server_url = server_url or r_url
        token = token or r_token
        team_id = r_team
    server_url = server_url or config.server_url()
    token = token or config.client_token()
    if not token:
        raise RuntimeError("VEZIR_TOKEN is not set; run `vezir token issue` on the server")
    config.validate_token_format(token)

    output_dir = output_dir or _default_output_dir()
    output_dir.mkdir(parents=True, exist_ok=True)

    _announce_attachments_folder()

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
    # v0.7.0: rename session dir with title suffix for discoverability.
    session_dir = audio.parent
    session_dir = config.rename_session_dir_with_title(session_dir, title)
    audio = next(
        iter(sorted(session_dir.glob("*.ogg")) or sorted(session_dir.glob("*.wav"))),
        audio,
    )
    print(
        f"vezir: recording captured: {audio} ({_fmt_bytes(audio.stat().st_size)})",
        flush=True,
    )

    if compress and audio.suffix.lower() == ".wav":
        before = audio.stat().st_size
        print("vezir: compressing WAV to OGG/Opus before upload ...", flush=True)
        # keep_wav=False: the OGG (opus 48k) is the local audio archive and
        # the upload artifact; the raw WAV is never reused, so don't persist
        # it (it is ~10x larger than the OGG).
        audio = uploader.compress_wav_for_upload(audio, keep_wav=False)
        after = audio.stat().st_size
        ratio = before / after if after else 0
        print(
            f"vezir: compressed {_fmt_bytes(before)} -> {_fmt_bytes(after)} "
            f"({ratio:.1f}x smaller)",
            flush=True,
        )

    _attachment_pause(no_pause)

    print(f"vezir: uploading to {server_url} ...", flush=True)
    try:
        # Prefer the resumable protocol; fall back to the one-shot
        # endpoint when the server is too old to expose it.
        upload_kwargs = dict(
            title=title,
            summary_preset=summary_preset,
            auto_label=auto_label,
            sync=sync,
            personal=personal,
            progress=_progress_line,
            on_retry=_retry_line,
            team_id=team_id,
        )
        if uploader.server_supports_resumable(
            server_url, token, team_id=team_id
        ):
            result = uploader.upload_resumable(
                server_url, token, audio, **upload_kwargs
            )
        else:
            result = uploader.upload(
                server_url, token, audio, **upload_kwargs
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
    # Bridge the local recording dir to the server session immediately so a
    # later `vezir pull` / "open folder" reuses THIS folder rather than
    # creating a differently-timestamped duplicate.
    try:
        from .pull import record_uploaded_session
        record_uploaded_session(
            session_dir, result["session_id"], title=title, team_id=team_id,
        )
    except Exception as exc:
        log.warning("could not write upload session.json: %s", exc)

    _send_attachments(
        server_url, token, result["session_id"], session_dir, team_id,
    )

    # v0.7.0: no dashboard URL.  Print a TUI hint instead so users
    # know how to track the session.
    print(
        "vezir: track with `vezir tui` (Sessions tab) or "
        f"`vezir sessions {result['session_id']}`",
        flush=True,
    )

    if wait:
        print("vezir: waiting for processing ...", flush=True)
        poll_status(
            server_url, token, result["session_id"],
            timeout=wait_timeout, open_labeling=open_labeling,
            team_id=team_id,
        )

    return result
