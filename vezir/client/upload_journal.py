"""Per-session upload journal — a crash-safe record of "this recording
still needs uploading" (vezir 0.23.0).

Motivation (incident 2026-09-28): a TUI crash mid-recording left an
87-minute meeting on disk with no trace anywhere — the Sessions list is
server-side only, and vezir kept zero local state about recordings that
never uploaded.  The millet-record 0.6.0 markers cover the recording
window; this journal covers the post-stop window (compress + upload),
so a crash at ANY point after the user hits record is recoverable on
next launch.

Marker file: ``<session_dir>/.upload.json`` (hidden, alongside
``.pull-manifest.json``).  States:

    recording   (0.25.0) written when the TUI starts recording; carries
                the destination team so a crash mid-recording is
                salvaged to the RIGHT team.  millet-record rewrites its own
                ``<stem>.session.json`` from memory at stop, so vezir's
                destination can't live there while recording.
    pending     recorded (or imported), upload not yet attempted
    uploading   upload in flight when the process died
    failed      last attempt failed (error recorded)
    held        (0.25.0) deliberately kept local ("Keep local" after Stop);
                carries the chosen ``options`` and ``pending_attachments``
    done        upload completed (session_id recorded)

Only sessions that ever entered the upload flow get a marker, so
historical dirs and deliberate local-only recordings are never nagged
about.  All functions are best-effort and never raise — a journaling
bug must not lose a recording.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_JOURNAL_NAME = ".upload.json"

# States that mean "the user wanted this uploaded but it isn't".
PENDING_STATES = frozenset({"pending", "uploading", "failed"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read(session_dir: Path) -> dict:
    """Parse the journal marker; {} on missing/corrupt."""
    try:
        data = json.loads((Path(session_dir) / _JOURNAL_NAME).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(session_dir: Path, state: dict) -> None:
    try:
        (Path(session_dir) / _JOURNAL_NAME).write_text(json.dumps(state, indent=2))
    except OSError:
        pass


def mark_pending(session_dir: Path, *, title: str | None, team_id: str | None) -> None:
    """Record that the audio in *session_dir* is meant to be uploaded."""
    if session_dir is None:
        return
    state = read(session_dir)
    state.update(
        {
            "status": "pending",
            "title": title,
            "team_id": team_id,
            "updated_at": _now(),
        }
    )
    state.setdefault("created_at", state["updated_at"])
    _write(session_dir, state)


def mark_recording(session_dir: Path, *, team_id: str | None) -> None:
    """Record the destination team the moment a recording starts."""
    if session_dir is None:
        return
    try:
        Path(session_dir).mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    now = _now()
    _write(session_dir, {
        "status": "recording", "team_id": team_id, "title": None,
        "created_at": now, "updated_at": now,
    })


def mark_held(
    session_dir: Path,
    *,
    title: str | None,
    team_id: str | None,
    options: dict | None = None,
    pending_attachments: bool = False,
) -> None:
    """Record a deliberate "keep local" — listed in the Outbox, never nagged."""
    if session_dir is None:
        return
    state = read(session_dir)
    state.update({
        "status": "held",
        "title": title,
        "team_id": team_id,
        "options": options or {},
        "pending_attachments": bool(pending_attachments),
        "updated_at": _now(),
    })
    state.setdefault("created_at", state["updated_at"])
    state.pop("error", None)
    _write(session_dir, state)


def clear_pending_attachments(session_dir: Path) -> None:
    state = read(session_dir)
    if state.pop("pending_attachments", None) is None:
        return
    _write(session_dir, state)


def set_team(session_dir: Path, team_id: str) -> None:
    """Re-point an existing journal at another team (``vezir local move``)."""
    state = read(session_dir)
    if not state:
        return
    state["team_id"] = team_id
    state["updated_at"] = _now()
    _write(session_dir, state)


def mark_uploading(session_dir: Path) -> None:
    state = read(session_dir)
    if not state:
        return  # no pending marker — nothing to transition
    state["status"] = "uploading"
    state["updated_at"] = _now()
    _write(session_dir, state)


def mark_done(session_dir: Path, session_id: str) -> None:
    state = read(session_dir)
    if not state:
        return
    state.update({"status": "done", "session_id": session_id, "updated_at": _now()})
    state.pop("error", None)
    _write(session_dir, state)


def mark_failed(session_dir: Path, error: str) -> None:
    state = read(session_dir)
    if not state:
        return
    state.update({"status": "failed", "error": error[:500], "updated_at": _now()})
    _write(session_dir, state)


@dataclass
class PendingUpload:
    """A session dir whose upload never completed."""

    session_dir: Path
    title: str | None
    team_id: str | None
    status: str  # pending | uploading | failed
    error: str | None
    audio_path: Path | None  # best available audio file (ogg preferred)
    updated_at: str | None


_AUDIO_SUFFIXES = (".ogg", ".mp3", ".wav", ".mp4", ".mov")


def find_audio(session_dir: Path) -> Path | None:
    """The uploadable audio/video file in a session dir (compressed first)."""
    session_dir = Path(session_dir)
    for suffix in _AUDIO_SUFFIXES:
        candidates = sorted(
            p for p in session_dir.glob(f"*{suffix}") if ".chunk-" not in p.name
        )
        if candidates:
            return candidates[0]
    return None


def pending_in_root(root: Path) -> list[PendingUpload]:
    """Scan one recordings root for journal-marked unfinished uploads.

    A dir counts when its marker is in a pending-ish state, uploadable
    audio is still on disk, and no ``session.json`` upload stub exists
    (the stub is written on success — belt-and-braces alongside the
    journal's own ``done`` state).  Never raises on filesystem races.
    """
    found: list[PendingUpload] = []
    try:
        entries = list(Path(root).iterdir())
    except OSError:
        return found
    for d in entries:
        if not d.is_dir():
            continue
        state = read(d)
        if state.get("status") not in PENDING_STATES:
            continue
        if (d / "session.json").exists():
            continue  # uploaded despite the marker — stub wins
        audio = find_audio(d)
        if audio is None:
            continue  # nothing left to upload
        found.append(
            PendingUpload(
                session_dir=d,
                title=state.get("title"),
                team_id=state.get("team_id"),
                status=state.get("status", "pending"),
                error=state.get("error"),
                audio_path=audio,
                updated_at=state.get("updated_at"),
            )
        )
    return found
