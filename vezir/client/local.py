"""Local recordings: everything on this machine, whatever its state (0.24.0).

Backs ``vezir local list|upload|move|discard``.  Incident 2026-10-02: a
paused recording had to go to a different team than the one it was started
in.  Every piece existed — chunk stitching, the upload journal, the
recovery scan — but only inside the TUI, and the team was implied by the
folder; fixing it took a coding session.  This module gives each local
recording folder ONE classified view and a stable handle (its folder name)
so the CLI can act on it.

States, most actionable first::

    in-progress   owner process alive (recording or paused) — hands off
    orphaned      recorder alive, owner dead — still capturing!
    interrupted   chunks on disk, recorder dead, never stitched
    failed        upload attempted and failed (journal)
    uploading     upload was in flight when the process died (journal)
    pending       upload intended, never attempted (journal)
    held          deliberately kept local (journal; set by the TUI, 0.25.0)
    uploaded      reached the server (``session.json`` stub / journal done)
    local-only    audio, but never entered the upload flow (historical)

``uploaded`` and ``local-only`` are hidden from ``list`` unless ``--all``,
so the default view is an outbox.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

from . import recovery, upload_journal

# Display / sort order.
STATES = (
    "in-progress", "orphaned", "interrupted", "failed", "uploading",
    "pending", "held", "uploaded", "local-only",
)
OUTBOX_STATES = frozenset(STATES[:7])
_JOURNAL_STATES = ("failed", "uploading", "pending", "held")


@dataclass
class LocalRecording:
    session_dir: Path
    team: str  # resolved destination team (recovery.resolve_team)
    state: str
    total_bytes: int
    started_at: str | None
    title: str | None
    session_id: str | None
    recorder_pid: int | None  # live orphaned recorder, else None
    audio_path: Path | None
    detail: str
    needs_stitch: bool = False  # chunks on disk, no final WAV yet
    options: dict | None = None  # upload options saved with a "held" choice

    @property
    def name(self) -> str:
        return self.session_dir.name

    def to_json(self) -> dict:
        d = asdict(self)
        d["session_dir"] = str(self.session_dir)
        d["audio_path"] = str(self.audio_path) if self.audio_path else None
        d["name"] = self.name
        return d

    def to_recoverable(self) -> recovery.RecoverableSession:
        """Adapter for :func:`recovery.salvage`."""
        if self.state == "orphaned":
            kind = "orphaned"
        elif self.needs_stitch:
            kind = "interrupted"
        else:
            kind = "pending_upload"
        return recovery.RecoverableSession(
            session_dir=self.session_dir,
            team_id=self.team,
            kind=kind,
            started_at=self.started_at,
            total_bytes=self.total_bytes,
            recorder_pid=self.recorder_pid,
            audio_path=self.audio_path,
            title_hint=self.title,
            detail=self.detail,
        )


def _dir_bytes(d: Path) -> int:
    total = 0
    try:
        for p in d.iterdir():
            if p.is_file() and p.suffix.lower() in (".wav", ".ogg", ".mp3", ".mp4", ".mov"):
                total += p.stat().st_size
    except OSError:
        pass
    return total


def _stub(d: Path) -> dict:
    try:
        data = json.loads((d / "session.json").read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def scan(*, include_all: bool = False) -> list[LocalRecording]:
    """Classify every recording folder under every recordings root.

    Newest first (by folder mtime).  With ``include_all=False`` only
    outbox states are returned.  Never raises on filesystem races.
    """
    try:
        from millet_record.capture import find_interrupted_sessions
    except ImportError:  # pragma: no cover - millet-record is a base dep
        find_interrupted_sessions = None

    # Orphaned/interrupted classification (incl. the legacy /proc fallback)
    # lives in recovery.scan_interrupted; reuse it rather than re-derive.
    salvageable = {
        r.session_dir: r for r in recovery.scan_interrupted()
        if r.kind in ("orphaned", "interrupted")
    }

    found: list[LocalRecording] = []
    for folder_team, root in recovery.recordings_roots():
        in_progress: dict[Path, object] = {}
        if find_interrupted_sessions is not None:
            try:
                for s in find_interrupted_sessions(root):
                    if s.owner_alive:
                        in_progress[s.session_dir] = s
            except Exception:
                pass
        try:
            entries = sorted(d for d in root.iterdir() if d.is_dir())
        except OSError:
            continue
        for d in entries:
            if d.name.startswith("."):
                continue
            team = recovery.resolve_team(d, folder_team)
            journal = upload_journal.read(d)
            stub = _stub(d)
            started = journal.get("created_at")
            title = journal.get("title") or stub.get("title")
            pid = None
            sid = None
            audio = None
            needs_stitch = False
            if d in in_progress:
                s = in_progress[d]
                state = "in-progress"
                started = getattr(s, "started_at", None)
                total = getattr(s, "total_bytes", 0)
                detail = f"owned by live pid {getattr(s, 'owner_pid', '?')}"
            elif d in salvageable:
                r = salvageable[d]
                state = r.kind
                started = r.started_at
                total = r.total_bytes
                pid = r.recorder_pid
                detail = r.detail
                needs_stitch = True
                if state == "interrupted" and journal.get("status") == "held":
                    state, detail = "held", "kept local (not stitched yet)"
            else:
                audio = upload_journal.find_audio(d)
                total = _dir_bytes(d)
                sid = stub.get("session_id") or (
                    journal.get("session_id") if journal.get("status") == "done" else None
                )
                status = journal.get("status")
                if sid:
                    state, detail = "uploaded", f"session {sid}"
                elif status == "held" and audio is not None:
                    state, detail = "held", "kept local"
                elif status == "recording" and audio is not None:
                    # Clean stop, then nothing: the process died before the
                    # review step journaled it.
                    state, detail = "pending", "recorded; upload never started"
                elif status in _JOURNAL_STATES and audio is not None:
                    state = status
                    detail = journal.get("error") or f"upload {status}"
                elif audio is not None:
                    state, detail = "local-only", "never uploaded"
                else:
                    continue  # no audio, not uploaded: nothing to act on
            if not include_all and state not in OUTBOX_STATES:
                continue
            found.append(LocalRecording(
                session_dir=d, team=team, state=state, total_bytes=total,
                started_at=started, title=title, session_id=sid,
                recorder_pid=pid, audio_path=audio, detail=detail,
                needs_stitch=needs_stitch,
                options=journal.get("options") or None,
            ))

    def _mtime(r: LocalRecording) -> float:
        try:
            return r.session_dir.stat().st_mtime
        except OSError:
            return 0.0

    found.sort(key=_mtime, reverse=True)
    return found


class RefError(LookupError):
    """A ``<ref>`` matched nothing or more than one recording."""


def resolve_ref(ref: str, recordings: list[LocalRecording]) -> LocalRecording:
    """Find one recording by path, exact folder name, or unique prefix."""
    p = Path(ref).expanduser()
    if p.is_dir():
        rp = p.resolve()
        for r in recordings:
            if r.session_dir.resolve() == rp:
                return r
        raise RefError(f"{ref} is not a recording folder under the recordings roots")
    exact = [r for r in recordings if r.name == ref]
    if len(exact) == 1:
        return exact[0]
    matches = exact or [r for r in recordings if r.name.startswith(ref)]
    if not matches:
        raise RefError(f"no local recording matches {ref!r} (see `vezir local list --all`)")
    if len(matches) > 1:
        names = "\n  ".join(f"{r.name} [{r.team}]" for r in matches)
        raise RefError(f"{ref!r} is ambiguous:\n  {names}")
    return matches[0]


def known_teams() -> list[str]:
    """Teams a recording may be moved to: teams.json ids + existing roots."""
    from .config import load_teams_config

    teams = {t["id"] for t in load_teams_config()["teams"]}
    teams.update(name for name, _root in recovery.recordings_roots())
    return sorted(teams)


def check_movable(rec: LocalRecording) -> None:
    """Raise ``ValueError`` with the way out when *rec* can't be moved."""
    if rec.state == "in-progress":
        raise ValueError(
            f"{rec.name} is still recording (or paused) in a running vezir; "
            "stop or quit it first"
        )
    if rec.state == "orphaned":
        raise ValueError(
            f"{rec.name} has a live recorder (pid {rec.recorder_pid}); "
            f"`vezir local upload {rec.name} --team <team>` stops it and moves it"
        )
    if rec.state == "uploaded":
        raise ValueError(
            f"{rec.name} is already on the server as {rec.session_id} in "
            f"{rec.team}; moving the local folder would not move the session"
        )


def move(rec: LocalRecording, team: str) -> Path:
    """Re-home *rec* under *team* (see :func:`recovery.move_session_dir`)."""
    check_movable(rec)
    return Path(recovery.move_session_dir(rec.session_dir, team))


def rehome_uploaded(session_id: str, team: str) -> list[Path]:
    """After a server-side move (0.26.0), follow with the local copies.

    Every local folder linked to *session_id* (the recording folder and/or a
    pulled copy) moves under *team*'s root and its ``session.json`` stub is
    re-pointed.  Best effort: a folder that can't move is left in place
    (lookups fall back to scanning every team root).
    """
    moved: list[Path] = []
    for rec in scan(include_all=True):
        if rec.session_id != session_id:
            continue
        try:
            new_dir = Path(recovery.move_session_dir(rec.session_dir, team))
        except OSError:
            continue
        stub = new_dir / "session.json"
        try:
            meta = json.loads(stub.read_text())
            meta["team_id"] = team
            stub.write_text(json.dumps(meta, indent=2))
        except (OSError, ValueError):
            pass
        moved.append(new_dir)
    return moved


def trash_dir() -> Path:
    from .. import config as _config

    return _config.recordings_dir("__vezir_probe__").parent / ".trash"


def discard(rec: LocalRecording, *, purge: bool = False) -> Path | None:
    """Move *rec* to ``<base>/.trash/<team>/`` (or delete with *purge*).

    Returns the trash location, or None when purged.  The recordings scan
    skips dot-dirs, so trashed folders disappear from every view.
    """
    if rec.state in ("in-progress", "orphaned"):
        raise ValueError(f"{rec.name} has a live recorder; stop it before discarding")
    if purge:
        shutil.rmtree(rec.session_dir)
        return None
    dest_root = trash_dir() / rec.session_dir.parent.name
    dest_root.mkdir(parents=True, exist_ok=True)
    dest = dest_root / rec.name
    if dest.exists():
        raise FileExistsError(f"{dest} already exists; use --purge or clear the trash")
    rec.session_dir.rename(dest)
    return dest
