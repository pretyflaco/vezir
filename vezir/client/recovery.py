"""Crash recovery for local recordings (vezir 0.23.0).

Scans the local recordings roots for sessions that never made it to the
server, classifies them, and drives salvage:

* ``interrupted``    — recorder dead, chunk WAVs on disk (parent crashed
                       mid-recording).  Salvage: stitch + upload.
* ``orphaned``       — recorder process still alive but its controlling
                       vezir process is gone (it keeps capturing whatever
                       meetings come next into the dead session's file).
                       Salvage: SIGINT the recorder, then stitch + upload.
* ``pending_upload`` — recording finished, upload never completed
                       (crash during compress/upload).  Salvage: upload.

The recording-window detection is millet-record 0.6.0's
``find_interrupted_sessions``; the post-stop window is the per-session
upload journal (``upload_journal.py``).  For pre-0.6.0 dirs (no
``.recorder.json`` marker) a live ffmpeg writing into the dir is
detected via a ``/proc`` cmdline scan (Linux) so those orphans are
still found.
"""
from __future__ import annotations

import json
import logging
import os
import re
import signal
import time
from dataclasses import dataclass
from pathlib import Path

from . import upload_journal

log = logging.getLogger("vezir.client.recovery")

# How long to wait for a SIGINTed recorder to finalize its WAV and exit.
_STOP_TIMEOUT = 15.0


@dataclass
class RecoverableSession:
    """One local session that never reached the server."""

    session_dir: Path
    team_id: str  # recordings-root subdir name ("startups", "default", ...)
    kind: str  # "interrupted" | "orphaned" | "pending_upload"
    started_at: str | None
    total_bytes: int
    recorder_pid: int | None  # live recorder to stop (orphaned), else None
    audio_path: Path | None  # uploadable file (pending_upload), else None
    title_hint: str | None
    detail: str  # human-readable state line for the UI


_SESSION_DIR_RE = re.compile(r"^meeting-\d{8}-\d{6}")


def recordings_roots() -> list[tuple[str, Path]]:
    """All (team_id, recordings_root) pairs present on disk.

    The recordings base (``~/vezir-meetings`` or ``$VEZIR_RECORD_DIR``)
    holds one subdir per team; the subdir name IS the team id.
    Enumerating the base directly — rather than resolving teams.json —
    also covers recordings of teams since removed from the client
    config.  Sorted by team id for stable output.

    A folder named like a session (``meeting-YYYYMMDD-HHMMSS…``) sitting
    directly in the base — e.g. an old ``vezir pull`` that couldn't
    resolve the team — is a stray session, not a team (0.26.1: it was
    offered as a "team" in every team picker).
    """
    from .. import config as _config

    try:
        # recordings_dir() appends the team subdir beneath the (possibly
        # env-overridden) root; probing with a placeholder recovers the base.
        base = _config.recordings_dir("__vezir_probe__").parent
    except Exception:
        return []
    try:
        entries = sorted(base.iterdir())
    except OSError:
        return []
    return [
        (d.name, d) for d in entries
        if d.is_dir() and not d.name.startswith(".") and not _SESSION_DIR_RE.match(d.name)
    ]


def _pid_alive(pid: int | None) -> bool:
    """Bare liveness check (no start-ticks identity) for legacy ppids."""
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by another user
    except OSError:
        return False
    return True


def _proc_ppid(pid: int, proc_root: str = "/proc") -> int | None:
    """Parent pid from /proc (field 4 after comm). None if unreadable."""
    try:
        text = Path(proc_root, str(pid), "stat").read_text()
        return int(text[text.rindex(")") + 2:].split()[1])
    except (OSError, ValueError, IndexError):
        return None


def _legacy_live_recorder(
    session_dir: Path, proc_root: str = "/proc"
) -> tuple[int, int | None] | None:
    """Find a live ffmpeg writing into *session_dir* via /proc (Linux).

    Pre-0.6.0 recordings have no ``.recorder.json`` marker, so the only
    way to spot a still-running recorder is matching its argv against the
    chunk path.  Returns ``(pid, ppid)`` — the ppid is the ownership
    proxy: a recorder whose parent is dead (reparented to init) is an
    orphan; one whose parent is a live process belongs to that process
    (e.g. an old-version TUI recording right now) and must be left alone.
    Returns None on non-Linux or when nothing matches.  ``proc_root`` is
    a parameter only so tests can substitute a fixture tree.
    """
    if not os.path.isdir(proc_root):
        return None
    needle = os.fsencode(str(session_dir))
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return None
    for name in entries:
        if not name.isdigit():
            continue
        try:
            with open(os.path.join(proc_root, name, "cmdline"), "rb") as f:
                cmdline = f.read()
        except OSError:
            continue
        parts = cmdline.split(b"\0")
        if parts and b"ffmpeg" in parts[0] and needle in cmdline:
            return int(name), _proc_ppid(int(name), proc_root)
    return None


def _millet_meta_path(session_dir: Path) -> Path | None:
    """millet-record's ``<stem>.session.json`` (not vezir's ``session.json``)."""
    try:
        metas = sorted(Path(session_dir).glob("*.session.json"))
    except OSError:
        return None
    return metas[0] if metas else None


def _read_json(path: Path | None) -> dict:
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def resolve_team(session_dir: Path, folder_team: str | None = None) -> str:
    """The team a local recording belongs to (0.24.0).

    Precedence: the upload journal's ``team_id`` (the destination chosen
    when the upload was attempted) → the ``vezir_team`` key in
    millet-record's ``<stem>.session.json`` → the recordings-root subdir the
    folder lives in.  ``vezir local move`` keeps all three in agreement.
    """
    session_dir = Path(session_dir)
    team = upload_journal.read(session_dir).get("team_id")
    if team:
        return str(team)
    team = _read_json(_millet_meta_path(session_dir)).get("vezir_team")
    if team:
        return str(team)
    return folder_team or session_dir.parent.name


def move_session_dir(session_dir: Path, team: str) -> Path:
    """Re-home a local recording under *team*'s recordings root (0.24.0).

    Renames the folder to ``<base>/<team>/<name>`` and rewrites the
    metadata that names a team or an absolute path, so every reader
    (recovery scan, ``vezir local``, the upload) agrees on the new home:

    * upload journal ``team_id`` (if a journal exists)
    * millet-record meta: ``vezir_team`` + ``output_file`` re-rooted

    Raises ``FileExistsError`` when the destination exists.  The caller
    must ensure no recorder is writing into the folder.
    """
    from .. import config as _config

    session_dir = Path(session_dir)
    dest_root = _config.recordings_dir(team)
    dest = dest_root / session_dir.name
    if dest.resolve() == session_dir.resolve():
        return session_dir
    if dest.exists():
        raise FileExistsError(f"{dest} already exists")
    dest_root.mkdir(parents=True, exist_ok=True)
    session_dir.rename(dest)

    upload_journal.set_team(dest, team)

    meta_path = _millet_meta_path(dest)
    if meta_path is not None:
        meta = _read_json(meta_path)
        meta["vezir_team"] = team
        out = meta.get("output_file")
        if isinstance(out, str) and out:
            meta["output_file"] = str(dest / Path(out).name)
        try:
            meta_path.write_text(json.dumps(meta, indent=2))
        except OSError as exc:
            log.warning("could not update %s: %s", meta_path, exc)
    return dest


def salvage(
    rec: RecoverableSession,
    *,
    server_url: str,
    token: str,
    team: str,
    title: str | None = None,
    auto_label: bool = True,
    sync: bool = True,
    personal: bool = False,
    refresh_cb=None,
    progress=None,
    on_status=None,
) -> str:
    """Stop/stitch/compress/upload one local recording; return session_id.

    The single salvage pipeline behind the TUI Outbox tab and
    ``vezir local upload`` (0.24.0; previously inlined in the dialog with
    auto-label/sync hard-wired on and the team fixed to the folder).  If
    *team* differs from where the recording lives, the folder is moved
    first (after an orphaned recorder is stopped — never move a dir a live
    process is writing into).  The upload journal tracks every step, so a
    failure here is offered again on the next launch.
    """
    from . import uploader

    def status(msg: str) -> None:
        if on_status is not None:
            on_status(msg)

    if rec.kind == "orphaned":
        status(f"stopping recorder (pid {rec.recorder_pid})…")
        if not stop_orphaned(rec):
            raise RuntimeError(
                f"recorder pid {rec.recorder_pid} did not exit; "
                "stop it manually and retry"
            )

    if team != rec.team_id:
        status(f"moving {rec.session_dir.name} → {team}…")
        old_dir = rec.session_dir
        rec.session_dir = move_session_dir(rec.session_dir, team)
        if rec.audio_path is not None:
            rec.audio_path = rec.session_dir / rec.audio_path.relative_to(old_dir)
        rec.team_id = team

    try:
        if rec.kind in ("interrupted", "orphaned"):
            status(f"stitching chunks in {rec.session_dir.name}…")
            recover(rec)
        if rec.audio_path is None:
            rec.audio_path = upload_journal.find_audio(rec.session_dir)
        if rec.audio_path is None:
            raise RuntimeError("no uploadable audio file found")

        upload_journal.mark_pending(rec.session_dir, title=title, team_id=team)
        audio_path = rec.audio_path
        if audio_path.suffix.lower() == ".wav":
            status("compressing…")
            audio_path = uploader.compress_wav_for_upload(audio_path, keep_wav=False)
            rec.audio_path = audio_path

        if personal:
            sync = False  # server enforces this too; keep the request honest
        status(f"uploading to {team}…")
        upload_journal.mark_uploading(rec.session_dir)
        kwargs = dict(
            title=title,
            auto_label=auto_label,
            sync=sync,
            personal=personal,
            team_id=team,
            refresh_cb=refresh_cb,
            progress=progress,
        )
        if uploader.server_supports_resumable(server_url, token, team_id=team):
            result = uploader.upload_resumable(server_url, token, audio_path, **kwargs)
        else:
            result = uploader.upload(server_url, token, audio_path, **kwargs)
    except Exception as exc:
        upload_journal.mark_failed(rec.session_dir, str(exc))
        raise

    session_id = result.get("session_id", "")
    held_attachments = bool(upload_journal.read(rec.session_dir).get("pending_attachments"))
    upload_journal.mark_done(rec.session_dir, session_id)
    if session_id and held_attachments:
        from .attachments import send_held_attachments

        stored = send_held_attachments(
            server_url, token, session_id, rec.session_dir, team,
            on_info=status, on_error=status,
        )
        if stored:
            upload_journal.clear_pending_attachments(rec.session_dir)
    if session_id:
        try:
            from .pull import record_uploaded_session

            record_uploaded_session(
                rec.session_dir, session_id, title=title, team_id=team,
            )
        except Exception as exc:
            log.warning("could not write upload session.json: %s", exc)
    return session_id


def scan_interrupted() -> list[RecoverableSession]:
    """Scan all recordings roots for salvageable sessions, newest first.

    In-progress recordings (owner process alive) are deliberately
    excluded — another live vezir owns them.  Never raises.
    """
    try:
        from millet_record.capture import find_interrupted_sessions
    except ImportError:
        find_interrupted_sessions = None

    found: list[RecoverableSession] = []
    for team_id, root in recordings_roots():
        if find_interrupted_sessions is not None:
            try:
                interrupted = find_interrupted_sessions(root)
            except Exception as exc:
                log.warning("interrupted scan of %s failed: %s", root, exc)
                interrupted = []
            for s in interrupted:
                if s.owner_alive:
                    continue  # in progress under a live process
                pid = s.recorder_pid if s.recorder_alive else None
                if pid is None:
                    # Legacy (pre-marker) dir: is an old ffmpeg still
                    # writing?  Ownership is inferred from the ppid —
                    # reparented-to-init means the owner is gone.
                    legacy = _legacy_live_recorder(s.session_dir)
                    if legacy is not None:
                        legacy_pid, ppid = legacy
                        if ppid is not None and ppid != 1 and _pid_alive(ppid):
                            continue  # live legacy owner (old TUI/scribe)
                        pid = legacy_pid
                if pid is not None:
                    kind = "orphaned"
                    detail = f"recorder still running (pid {pid})"
                else:
                    kind = "interrupted"
                    detail = "recorder stopped; audio on disk"
                found.append(
                    RecoverableSession(
                        session_dir=s.session_dir,
                        team_id=resolve_team(s.session_dir, team_id),
                        kind=kind,
                        started_at=s.started_at,
                        total_bytes=s.total_bytes,
                        recorder_pid=pid,
                        audio_path=None,
                        title_hint=None,
                        detail=detail,
                    )
                )

        try:
            pending = upload_journal.pending_in_root(root)
        except Exception as exc:
            log.warning("pending-upload scan of %s failed: %s", root, exc)
            pending = []
        for p in pending:
            found.append(
                RecoverableSession(
                    session_dir=p.session_dir,
                    team_id=resolve_team(p.session_dir, team_id),
                    kind="pending_upload",
                    started_at=p.updated_at,
                    total_bytes=p.audio_path.stat().st_size if p.audio_path else 0,
                    recorder_pid=None,
                    audio_path=p.audio_path,
                    title_hint=p.title,
                    detail=(
                        f"upload never finished ({p.status})"
                        + (f": {p.error}" if p.error else "")
                    ),
                )
            )

    def _mtime(rec: RecoverableSession) -> float:
        try:
            return rec.session_dir.stat().st_mtime
        except OSError:
            return 0.0

    found.sort(key=_mtime, reverse=True)
    return found


def _pid_gone(pid: int) -> bool:
    """True when *pid* is dead — including zombies.

    A zombie still accepts signal 0, which would make ``stop_orphaned``
    wait forever when the orphan was (in tests or odd double-fork cases)
    our own unreaped child.  Linux /proc exposes the zombie state; on
    other platforms real orphans are reaped by the init system anyway.
    """
    try:
        os.kill(pid, 0)
    except OSError:
        return True
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
        # field 3 (state) follows the parenthesised comm — see
        # _proc_start_ticks for why rindex is required.
        return text[text.rindex(")") + 2:].split()[0] == "Z"
    except (OSError, ValueError, IndexError):
        return False


def stop_orphaned(rec: RecoverableSession, timeout: float = _STOP_TIMEOUT) -> bool:
    """SIGINT an orphaned recorder and wait for it to finalize + exit.

    SIGINT (not SIGKILL) so ffmpeg writes a proper WAV trailer — same
    signal millet-record's own stop ladder uses.  Returns True when the
    process is gone afterwards.
    """
    pid = rec.recorder_pid
    if pid is None:
        return True
    try:
        os.kill(pid, signal.SIGINT)
    except ProcessLookupError:
        return True
    except OSError as exc:
        log.warning("could not SIGINT orphan recorder %s: %s", pid, exc)
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _pid_gone(pid):
            rec.recorder_pid = None
            rec.kind = "interrupted"
            rec.detail = "recorder stopped; audio on disk"
            return True
        time.sleep(0.25)
    log.warning("orphan recorder %s did not exit within %.0fs", pid, timeout)
    return False


def recover(rec: RecoverableSession) -> Path:
    """Stitch an interrupted session's chunks into the final WAV.

    Thin wrapper over millet_record.capture.recover_session that also
    refreshes the RecoverableSession's audio_path.
    """
    from millet_record.capture import recover_session

    out = recover_session(rec.session_dir)
    rec.audio_path = out
    return out
