"""``vezir pull`` — download team meeting artifacts from the server.

Enables meeting artifact sharing within a team without relying on git
sync.  Each team member runs ``vezir pull`` to fetch summaries,
transcripts, and PDFs for meetings they didn't record themselves.

Output layout::

    ~/vezir-meetings/<team>/
        meeting-20260525-223319_BACK_2_BACK/
            20260525_back_2_back.md
            20260525_back_2_back.txt
            20260525_back_2_back.srt
            20260525_back_2_back.pdf
            20260525_back_2_back.frontmatter.json
            session.json       # metadata (session_id, title, github, ...)
        meeting-20260526-014608_BLINK_MEETING/
            meeting-20260526-014608.wav   # raw audio (only local recordings)
            20260526_blink_meeting.pdf    # downloaded artifacts
            ...

Idempotent: sessions already pulled (tracked in ``.pull-manifest.json``)
are skipped on re-runs.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from .. import config
from .api import Session, VezirClient
from .artifacts import _friendly_name, download_session_artifacts

log = logging.getLogger("vezir.client.pull")

_MANIFEST_NAME = ".pull-manifest.json"


def _load_manifest(output_dir: Path) -> dict:
    """Load the pull manifest (session_id -> local dirname mapping)."""
    path = output_dir / _MANIFEST_NAME
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_manifest(output_dir: Path, manifest: dict) -> None:
    path = output_dir / _MANIFEST_NAME
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def record_uploaded_session(
    session_dir: Path,
    session_id: str,
    *,
    title: str | None = None,
    team_id: str | None = None,
) -> None:
    """Bridge a just-uploaded local recording dir to its server session.

    Writes a minimal ``session.json`` into ``session_dir`` (if one isn't
    already there).  This is what lets a later ``find_local_session_dir`` /
    "open folder" reuse the existing recording folder instead of pulling the
    artifacts into a new, differently-timestamped duplicate folder.

    It deliberately does NOT touch ``.pull-manifest.json``: the manifest means
    "artifacts have been downloaded here", which is not yet true at upload
    time (only the stub exists).  Marking it pulled here would make ``vezir
    pull`` skip the session and leave the folder permanently artifact-less.
    The manifest is written by the actual download path (auto-download in the
    Record tab, ``vezir pull``, or the "open folder" self-heal).

    Best-effort and idempotent; failures are swallowed by the caller.
    """
    if not session_id or session_dir is None:
        return
    session_dir = Path(session_dir)
    if not session_dir.is_dir():
        return

    meta_path = session_dir / "session.json"
    if not meta_path.exists():
        meta = {
            "session_id": session_id,
            "title": title,
            "team_id": team_id,
            "created_by": "vezir-upload",
        }
        meta_path.write_text(
            json.dumps(meta, indent=2, default=str), encoding="utf-8",
        )


def _dir_has_artifacts(session_dir: Path) -> bool:
    """True if the folder contains downloaded meeting artifacts.

    Artifacts are saved under friendly names (``YYYYMMDD_<title>.md``,
    ``YYYYMMDD_<title>.{txt,srt,pdf,json}``; legacy pulls may still hold
    ``summary.md`` / ``transcript.*``).  A folder with only the audio +
    ``session.json`` stub (written at upload time) does NOT count — the
    artifacts still need downloading.
    """
    if not session_dir.is_dir():
        return False
    if (session_dir / "summary.md").exists():
        return True
    if any(session_dir.glob("transcript.*")):
        return True
    # v0.14.1+ dated names: <date>_<slug>.<ext>.  Match on the date prefix
    # so we don't need the session to enumerate.
    return any(
        p.name[:8].isdigit() and len(p.name) > 9 and p.name[8] == "_"
        for p in session_dir.iterdir()
        if p.is_file()
    )


def missing_server_artifacts(session: Session, session_dir: Path) -> list[str]:
    """Return the friendly artifact filenames the server has but disk lacks."""
    if not session_dir.is_dir():
        return [_friendly_name(session, n) for n in (session.artifacts or {}).values()]
    missing = []
    for server_name in (session.artifacts or {}).values():
        friendly = _friendly_name(session, server_name)
        if not (session_dir / friendly).exists():
            missing.append(friendly)
    return missing


def _dirname_for_session(session: Session) -> str:
    """Derive a ``meeting-YYYYMMDD-HHMMSS_TITLE`` directory name from session metadata."""
    # Parse created_at (ISO 8601 UTC) -> local-ish timestamp for the dirname.
    ts = session.created_at or ""
    try:
        # Handle common formats: 2026-05-25T22:33:19Z, 2026-05-25T22:33:19
        clean = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean).astimezone()  # convert to local tz
    except (ValueError, TypeError):
        dt = datetime.now()
    date_str = dt.strftime("%Y%m%d-%H%M%S")

    title = session.title or ""
    slug = config.sanitize_title(title)
    if slug:
        return f"meeting-{date_str}_{slug}"
    return f"meeting-{date_str}"


def _find_existing_dir(output_dir: Path, session: Session) -> Path | None:
    """Check if a directory for this session already exists (by session.json match)."""
    for d in output_dir.iterdir():
        if not d.is_dir():
            continue
        meta = d / "session.json"
        if not meta.exists():
            continue
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
            if data.get("session_id") == session.id:
                return d
        except Exception:
            continue
    return None


def _scan_dir_for_session(output_dir: Path, session_id: str) -> Path | None:
    """Manifest + ``session.json`` scan for *session_id* within one team dir."""
    if not output_dir.is_dir():
        return None
    # 1. Check manifest.
    manifest = _load_manifest(output_dir)
    if session_id in manifest:
        candidate = output_dir / manifest[session_id]
        if candidate.is_dir():
            return candidate
    # 2. Directory scan.
    for d in output_dir.iterdir():
        if not d.is_dir():
            continue
        meta = d / "session.json"
        if not meta.exists():
            continue
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
            if data.get("session_id") == session_id:
                return d
        except Exception:
            continue
    return None


def find_local_session_dir(
    session_id: str,
    team_id: str | None = None,
) -> Path | None:
    """Find the local directory for a session, or None if not pulled/recorded.

    Resolution order:
    1. The team-specific recordings dir (manifest, then ``session.json`` scan).
    2. **Global fallback** — scan every team subdirectory under the
       ``~/vezir-meetings/`` root for a ``session.json`` matching the
       session_id.

    The global fallback exists because recordings are written under the
    team **slug** (``~/vezir-meetings/blink/``) at record time, while the
    server identifies teams by **UUID**.  A caller that passes the UUID
    (e.g. the TUI detail screen, whose ``session.team_id`` is the server
    UUID) would otherwise resolve to a nonexistent ``~/vezir-meetings/<uuid>/``
    and report "no artifacts" for a session that is present on disk under
    its slug.  Scanning all sibling team dirs makes lookup robust to the
    slug/UUID split (and to a session filed under an unexpected team dir).

    Used by TUI actions (copy path, open folder) to locate the local
    meeting artifacts directory.
    """
    # 1. Team-specific dir (fast path when team_id matches the on-disk name).
    output_dir = config.recordings_dir(team_id)
    found = _scan_dir_for_session(output_dir, session_id)
    if found is not None:
        return found

    # 2. Global fallback: scan all team subdirs under the recordings root.
    #    ``output_dir`` is ``<root>/<team_id>``; its parent is the root.
    root = output_dir.parent
    if not root.is_dir():
        return None
    for team_dir in sorted(root.iterdir()):
        if not team_dir.is_dir() or team_dir == output_dir:
            continue
        found = _scan_dir_for_session(team_dir, session_id)
        if found is not None:
            return found
    return None


def pull_team_sessions(
    api: VezirClient,
    output_dir: Path | None = None,
    limit: int = 50,
    since: str | None = None,
    session_id: str | None = None,
) -> int:
    """Pull meeting artifacts from the server.

    Returns the number of sessions successfully pulled.
    """
    # Resolve output directory.
    if output_dir is None:
        output_dir = config.recordings_dir()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Fetch sessions.
    if session_id:
        result = api.get_session(session_id)
        if not result.is_ok():
            print(
                f"vezir pull: failed to fetch session {session_id}: "
                f"{result.error_message()}",
                file=sys.stderr,
                flush=True,
            )
            return 0
        sessions = [result.ok]
    else:
        result = api.get_sessions(limit=limit, since=since)
        if not result.is_ok():
            print(
                f"vezir pull: failed to list sessions: {result.error_message()}",
                file=sys.stderr,
                flush=True,
            )
            return 0
        sessions = result.ok

    # Filter to completed sessions with artifacts.  `sync_failed` sessions
    # finished transcription/summary (only the git push failed) and have
    # artifacts, so they're pullable too.
    pullable = [
        s for s in sessions
        if s.status in ("done", "sync_failed", "imported") and s.artifacts
    ]
    if not pullable:
        print("vezir pull: no completed sessions to pull", flush=True)
        return 0

    manifest = _load_manifest(output_dir)
    pulled = 0

    for session in pullable:
        # Already pulled?  Only skip when the mapped folder actually holds
        # artifacts — a manifest entry for an artifact-less folder (or a stale
        # entry) must NOT block re-downloading the missing files.
        if session.id in manifest:
            existing = output_dir / manifest[session.id]
            if existing.is_dir() and _dir_has_artifacts(existing):
                log.debug("skip already-pulled %s", session.id)
                continue

        # Check if a directory for this session already exists on disk
        # (e.g. local recording that already has a session.json stub).
        existing_dir = _find_existing_dir(output_dir, session)
        if existing_dir is not None:
            dest_dir = existing_dir
        else:
            dirname = _dirname_for_session(session)
            dest_dir = output_dir / dirname

        # Download artifacts.
        saved = download_session_artifacts(api, session, dest_dir)
        if saved:
            manifest[session.id] = dest_dir.name
            _save_manifest(output_dir, manifest)
            pulled += 1
            title = session.title or "(untitled)"
            who = session.github or "?"
            print(
                f"  pulled: {dest_dir.name}  "
                f"[{title} by {who}, {len(saved)} files]",
                flush=True,
            )
        else:
            print(
                f"  warning: no artifacts downloaded for {session.id}",
                file=sys.stderr,
                flush=True,
            )

    print(
        f"vezir pull: {pulled} session(s) pulled to {output_dir}",
        flush=True,
    )
    return pulled
