"""Shared artifact download logic.

Used by:
  * TUI/GUI auto-download after processing reaches ``done``
  * ``vezir pull`` CLI command for team meeting sharing

Downloads the tracked artifacts (summary, transcript, PDF, etc.) from
the server into a local directory alongside the raw audio recording.

Artifact files are renamed from ULID-based server names to human-friendly
names so ``ls`` output is immediately useful:

    ~/vezir-meetings/blink/meeting-20260526-143041_ABBOARD/
        summary.md             <- 01KSGN2X...summary.md
        transcript.txt         <- 01KSGN2X...txt
        transcript.srt         <- 01KSGN2X...srt
        transcript.pdf         <- 01KSGN2X...pdf
        transcript.json        <- 01KSGN2X...json
        frontmatter.json       <- 01KSGN2X...frontmatter.json
        session.json           <- metadata written by this module
        meeting-20260526-143041.wav   (raw audio, only for local recordings)
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from .api import Session, VezirClient

log = logging.getLogger("vezir.client.artifacts")

# Map artifact server filenames to human-friendly local names.
# The key suffix is matched against the server filename; first match wins.
_FRIENDLY_NAMES: list[tuple[str, str]] = [
    (".summary.md", "summary.md"),
    (".frontmatter.json", "frontmatter.json"),
    (".srt", "transcript.srt"),
    (".txt", "transcript.txt"),
    (".pdf", "transcript.pdf"),
    # The structured JSON transcript (full segments + speaker data).
    # Must come AFTER .frontmatter.json to avoid shadowing.
    (".json", "transcript.json"),
]


def _friendly_name(server_filename: str) -> str:
    """Convert a ULID-prefixed server filename to a human-friendly name."""
    for suffix, friendly in _FRIENDLY_NAMES:
        if server_filename.endswith(suffix):
            return friendly
    # Unknown artifact type -- keep the original name.
    return server_filename


def download_session_artifacts(
    api: VezirClient,
    session: Session,
    dest_dir: Path,
    *,
    overwrite: bool = False,
) -> list[Path]:
    """Download all tracked artifacts for a session into *dest_dir*.

    Returns the list of paths successfully saved.  Idempotent: files that
    already exist (and *overwrite* is False) are skipped.

    Also writes a ``session.json`` metadata file with the session's
    identity info so the directory is self-describing.
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []

    for _key, server_name in session.artifacts.items():
        friendly = _friendly_name(server_name)
        dest = dest_dir / friendly
        if dest.exists() and not overwrite:
            log.debug("skip existing %s", dest)
            saved.append(dest)
            continue
        result = api.save_artifact(session.id, server_name, dest)
        if result.is_ok():
            saved.append(dest)
            log.debug("saved %s -> %s", server_name, dest)
        else:
            log.warning(
                "failed to download %s/%s: %s",
                session.id, server_name, result.error_message(),
            )

    # Write session metadata so the directory is self-describing.  Upgrade a
    # minimal upload-time stub ("created_by": "vezir-upload") to the full
    # record, since auto-download has the richer server-side fields.
    meta_path = dest_dir / "session.json"
    is_upload_stub = False
    if meta_path.exists():
        try:
            _existing = json.loads(meta_path.read_text(encoding="utf-8"))
            is_upload_stub = (
                isinstance(_existing, dict)
                and _existing.get("created_by") == "vezir-upload"
            )
        except Exception:
            is_upload_stub = False
    if not meta_path.exists() or overwrite or is_upload_stub:
        meta = {
            "session_id": session.id,
            "title": session.title,
            "status": session.status,
            "github": session.github,
            "created_at": session.created_at,
            "team_id": getattr(session, "team_id", None),
            "artifacts": session.artifacts,
            "pulled_by": "vezir",
        }
        try:
            meta_path.write_text(
                json.dumps(meta, indent=2, default=str),
                encoding="utf-8",
            )
            saved.append(meta_path)
        except OSError as exc:
            log.warning("could not write session.json: %s", exc)

    return saved
