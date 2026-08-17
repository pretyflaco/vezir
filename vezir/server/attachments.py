"""User-supplied meeting attachments (issue #16).

POST   /api/sessions/<id>/attachments         → store one or more files
GET    /api/sessions/<id>/attachments         → list [{name, size, content_type}]
GET    /api/sessions/<id>/attachments/<name>  → download one

Storage is the filesystem — no DB column, no migration: files live in
``<sessions_dir>/<session_id>/attachments/``, which is exactly the directory
the worker hands to ``millet sync``.  millet-pipeline >= 0.15.0 pushes that
subdirectory into the team's git archive verbatim (bypassing its
suffix-allowlist and descriptive-rename map), so attachments reach the repo
with no further work on this side.

Attachments deliberately do NOT go through ``/artifact/<id>/<name>``: that
route rejects ``/`` outright, and user-chosen filenames would collide with
millet's canonical artifact names (``summary.md``, ``transcript.pdf``, …) in
that flat namespace.

Caps mirror millet's (``MAX_ATTACHMENTS`` / ``MAX_ATTACHMENTS_BYTES``): a file
accepted here but dropped at sync time would be silently missing from the team
repo, so the two ends agree on the limit.
"""
from __future__ import annotations

import logging
import mimetypes
import re
from pathlib import Path, PurePath

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from .. import config
from . import auth, queue, ratelimit
from .sessions import enforce_team_visibility

log = logging.getLogger("vezir.attachments")
router = APIRouter()

# Must match millet.sync.ATTACHMENTS_SUBDIR — this is the directory name
# millet looks for and reproduces in the meeting folder.
ATTACHMENTS_SUBDIR = "attachments"

CHUNK_BYTES = 1024 * 1024
_MAX_NAME_LEN = 128
_MAX_SUFFIX_LEN = 16
# Control characters and separators can't survive in a filename that later
# becomes a git path on someone else's checkout.
_UNSAFE_NAME_RE = re.compile(r"[\x00-\x1f\x7f]")
_FALLBACK_NAME = "attachment"


def attachments_dir(session_id: str) -> Path:
    """Path of a session's attachments directory (not created)."""
    return config.sessions_dir() / session_id / ATTACHMENTS_SUBDIR


def safe_attachment_name(raw: str | None) -> str:
    """Reduce a client-supplied filename to one safe path component.

    Drops any directory prefix (``../../etc/passwd`` and
    ``C:\\Users\\x\\slides.pdf`` both collapse to their last segment), strips
    control characters, and caps the length while keeping the extension —
    which is what the TUI and the OS opener key off.  Never returns an empty
    string, ``.`` or ``..``.
    """
    name = (raw or "").replace("\\", "/")
    name = PurePath(name).name
    name = _UNSAFE_NAME_RE.sub("", name).strip()
    # A name that is only dots would resolve to the directory itself.
    if not name.strip("."):
        return _FALLBACK_NAME
    if len(name) > _MAX_NAME_LEN:
        stem, dot, suffix = name.rpartition(".")
        if dot and 0 < len(suffix) <= _MAX_SUFFIX_LEN:
            keep = _MAX_NAME_LEN - len(suffix) - 1
            name = f"{stem[:keep]}.{suffix}"
        else:
            name = name[:_MAX_NAME_LEN]
    return name


def _unique_path(adir: Path, name: str) -> Path:
    """Resolve ``name`` inside ``adir``, suffixing on collision.

    Two meetings' worth of ``Screenshot.png`` must both survive; the second
    becomes ``Screenshot_2.png``.
    """
    candidate = adir / name
    if not candidate.exists():
        return candidate
    stem, dot, suffix = name.rpartition(".")
    if not dot:
        stem, suffix = name, ""
    for n in range(2, 1000):
        alt = f"{stem}_{n}.{suffix}" if suffix else f"{stem}_{n}"
        candidate = adir / alt
        if not candidate.exists():
            return candidate
    raise HTTPException(409, "too many attachments with the same name")


def _stored(adir: Path) -> list[Path]:
    """Currently stored attachments, sorted, regular files only."""
    if not adir.is_dir():
        return []
    return sorted(p for p in adir.iterdir() if p.is_file() and not p.is_symlink())


def _describe(p: Path) -> dict:
    ctype, _ = mimetypes.guess_type(p.name)
    return {
        "name": p.name,
        "size": p.stat().st_size,
        "content_type": ctype or "application/octet-stream",
    }


def _require_visible_session(
    session_id: str, auth_triple: tuple
) -> tuple[dict, Path]:
    """Return ``(row, session_dir)`` or raise 404 for an invisible session.

    Team membership is checked against the job row BEFORE touching the
    filesystem, so a cross-team caller can't probe for session existence by
    timing or error shape (same rule as ``/artifact/<id>/<name>``).
    """
    github, team_id, is_admin = auth_triple
    row = queue.get(session_id)
    if not row:
        raise HTTPException(404, "session not found")
    enforce_team_visibility(row, team_id, github, is_admin)
    sdir = config.sessions_dir() / session_id
    if not sdir.is_dir():
        raise HTTPException(404, "session not found")
    return row, sdir


@router.post(
    "/api/sessions/{session_id}/attachments",
    dependencies=[Depends(ratelimit.limit_upload)],
)
async def upload_attachments(
    session_id: str,
    request: Request,
    files: list[UploadFile] = File(...),
    auth_triple: tuple = Depends(auth.require_team_context),
):
    """Store one or more attachments for an already-uploaded session.

    Separate from ``/upload`` on purpose: three upload paths exist (``/upload``,
    ``/upload/multi`` and the resumable tus subset), and threading optional
    files through all three multiplies the work for no gain — the client knows
    the session id as soon as the audio lands.

    All-or-nothing: if any file trips a cap (or the client disconnects
    mid-request) everything written by this request is removed, so a retry
    starts from the state the caller last saw.
    """
    _row, sdir = _require_visible_session(session_id, auth_triple)

    max_file_bytes = config.max_upload_bytes()
    max_total = config.max_attachment_bytes_total()
    max_count = config.max_attachments_per_session()

    adir = sdir / ATTACHMENTS_SUBDIR
    existing = _stored(adir)
    used_bytes = sum(p.stat().st_size for p in existing)

    if len(existing) + len(files) > max_count:
        raise HTTPException(
            413,
            f"too many attachments: {len(existing)} stored, {len(files)} sent, "
            f"limit {max_count}",
        )
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if used_bytes + int(content_length) > max_total:
                raise HTTPException(413, "attachments too large")
        except ValueError:
            pass

    config.secure_mkdir(adir)
    written: list[Path] = []
    total = used_bytes
    try:
        for upload in files:
            dest = _unique_path(adir, safe_attachment_name(upload.filename))
            written.append(dest)
            file_bytes = 0
            with dest.open("wb") as fh:
                config.secure_chmod_file(dest)
                while True:
                    chunk = await upload.read(CHUNK_BYTES)
                    if not chunk:
                        break
                    file_bytes += len(chunk)
                    total += len(chunk)
                    if file_bytes > max_file_bytes:
                        raise HTTPException(413, "attachment too large")
                    if total > max_total:
                        raise HTTPException(413, "attachments too large")
                    fh.write(chunk)
            config.secure_chmod_file(dest)
    except BaseException:
        # BaseException, not just HTTPException: a mid-upload disconnect
        # raises starlette's ClientDisconnect, which would otherwise leave
        # half-written attachments behind (see uploads.py).
        for p in written:
            p.unlink(missing_ok=True)
        raise

    stored = [_describe(p) for p in written]
    log.info(
        "attachments stored: session=%s count=%d bytes=%d names=%s",
        session_id, len(stored), total - used_bytes,
        [d["name"] for d in stored],
    )
    return {"session_id": session_id, "attachments": stored}


@router.get(
    "/api/sessions/{session_id}/attachments",
    dependencies=[Depends(ratelimit.limit_api)],
)
def list_attachments(
    session_id: str,
    auth_triple: tuple = Depends(auth.require_team_context),
):
    """List a session's attachments.

    The directory is the source of truth — an attachment that arrived after
    the job row was last written still shows up.
    """
    _row, sdir = _require_visible_session(session_id, auth_triple)
    return {
        "session_id": session_id,
        "attachments": [_describe(p) for p in _stored(sdir / ATTACHMENTS_SUBDIR)],
    }


@router.get(
    "/api/sessions/{session_id}/attachments/{name}",
    dependencies=[Depends(ratelimit.limit_api)],
)
def download_attachment(
    session_id: str,
    name: str,
    auth_triple: tuple = Depends(auth.require_team_context),
):
    _row, sdir = _require_visible_session(session_id, auth_triple)
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(400, "invalid attachment name")
    adir = sdir / ATTACHMENTS_SUBDIR
    p = adir / name
    # Defense in depth: even with the checks above, only a regular file that
    # really sits in this directory may be served (a symlink planted in the
    # session dir could otherwise point anywhere on the host).
    if not p.is_file() or p.is_symlink() or p.parent.resolve() != adir.resolve():
        raise HTTPException(404, "attachment not found")
    return FileResponse(p, filename=name)
