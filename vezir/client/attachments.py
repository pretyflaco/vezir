"""Meeting attachments on the client side (issue #16).

Shared by ``vezir scribe`` (CLI) and the TUI record screen, which drive the
same workflow through different surfaces:

  1. a fixed staging folder is created and shown when recording starts,
  2. the user drops slides / agendas / screenshots into it while the meeting
     runs, and gets one last chance to add more when recording stops,
  3. the files upload once the session id is known, then move into that
     recording's own ``attachments/`` so the staging folder is empty for the
     next meeting.

Reporting goes through ``on_info`` / ``on_error`` callbacks rather than
``print``: the CLI prints, while the TUI posts messages into its event loop.
"""
from __future__ import annotations

import filecmp
import logging
import shutil
import time
from collections.abc import Callable
from pathlib import Path

from . import uploader

log = logging.getLogger("vezir.client.attachments")

# Must match millet.sync.ATTACHMENTS_SUBDIR and the server's storage layout:
# the same directory name travels from the staging folder to the session dir
# to the meeting folder in the team's git archive.
ATTACHMENTS_SUBDIR = "attachments"

Notify = Callable[[str], None]


def _noop(_msg: str) -> None:
    return None


def staging_dir() -> Path:
    from .config import attachments_dir

    return attachments_dir()


def staged_attachments() -> list[Path]:
    """Files currently in the staging folder, in a stable order.

    Dotfiles are skipped (``.DS_Store`` and editor droppings are not
    attachments).  Symlinks are followed on purpose — linking a large file
    instead of copying it is a reasonable way to attach it, and unlike the
    server side nothing here is copied into someone else's repository.
    """
    adir = staging_dir()
    if not adir.is_dir():
        return []
    return sorted(
        p for p in adir.iterdir()
        if p.is_file() and not p.name.startswith(".")
    )


def ensure_staging_dir() -> Path:
    """Create the staging folder if needed; never raises."""
    adir = staging_dir()
    try:
        adir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning("could not create attachments folder %s: %s", adir, exc)
    return adir


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


def move_staged_into_recording(
    session_dir: Path,
    staged: list[Path],
    *,
    on_info: Notify = _noop,
    on_error: Notify = _noop,
) -> int:
    """Move uploaded attachments next to the local recording.

    Keeps the files (they are the user's) while emptying the fixed staging
    folder for the next meeting.  Best effort: a failure here must not fail a
    run whose audio and attachments are already on the server.

    Returns the number of files moved.
    """
    dest_dir = session_dir / ATTACHMENTS_SUBDIR
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        on_error(
            f"could not create {dest_dir}: {exc}; attachments left in the "
            f"staging folder"
        )
        return 0
    moved = 0
    for p in staged:
        target = dest_dir / p.name
        try:
            if target.exists() and p.resolve() == target.resolve():
                continue  # already exactly where it belongs
            if target.exists() and filecmp.cmp(p, target, shallow=False):
                # The destination already holds this exact file.  Happens when
                # client and server run on the same host and VEZIR_RECORD_DIR
                # points into VEZIR_DATA/sessions: the copy the server just
                # stored IS this file.  Dropping the staged one keeps the
                # staging folder empty without creating an "_2" duplicate.
                p.unlink()
                continue
            shutil.move(str(p), str(_unique_dest(dest_dir, p.name)))
            moved += 1
        except OSError as exc:
            on_error(f"could not move {p.name} to {dest_dir}: {exc}")
    if moved:
        on_info(f"moved {moved} attachment(s) to {dest_dir}")
    return moved


def send_attachments(
    server_url: str,
    token: str,
    session_id: str,
    session_dir: Path,
    team_id: str | None,
    *,
    on_info: Notify = _noop,
    on_error: Notify = _noop,
) -> list[dict]:
    """Upload staged attachments, then move them next to the recording.

    Never raises: the meeting itself is already uploaded by the time this
    runs, so a failure warns and leaves the staging folder untouched for a
    manual retry.  Returns the server's stored-attachment descriptors.
    """
    staged = staged_attachments()
    if not staged:
        return []
    on_info(f"uploading {len(staged)} attachment(s) ...")
    try:
        stored = uploader.upload_attachments(
            server_url, token, session_id, staged, team_id=team_id,
        )
    except Exception as exc:
        on_error(f"attachment upload failed: {exc}")
        on_error(
            "the files are still in the staging folder; the meeting itself "
            "uploaded fine."
        )
        return []
    for item in stored:
        on_info(f"  attached {item.get('name')}")
    move_staged_into_recording(
        session_dir, staged, on_info=on_info, on_error=on_error,
    )
    return stored
