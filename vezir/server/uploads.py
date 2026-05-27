"""Upload endpoint.

POST /upload
    multipart/form-data with:
        audio: the .wav/.ogg file produced by `millet record` or `vezir upload`
        title: optional meeting title

    Returns: { "session_id": "<ulid>", "bytes": <n> }
"""
from __future__ import annotations

import logging
from pathlib import Path

import ulid
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile

from .. import config
from . import auth, queue, ratelimit

log = logging.getLogger("vezir.uploads")

router = APIRouter()


CHUNK_BYTES = 4 * 1024 * 1024  # 4 MB

# Audio extensions vezir accepts. Meetscribe handles both WAV and OGG natively
# (see meet/cli.py:389-390 and meet/label.py:66-70).
ACCEPTED_EXTS = {".wav", ".ogg"}
CONTENT_TYPE_EXTS = {
    "audio/wav": ".wav",
    "audio/wave": ".wav",
    "audio/x-wav": ".wav",
    "audio/vnd.wave": ".wav",
    "audio/ogg": ".ogg",
    "application/ogg": ".ogg",
}


def _pick_extension(upload_filename: str | None, content_type: str | None) -> str:
    """Choose the on-disk extension based on filename/MIME or reject."""
    if upload_filename:
        ext = Path(upload_filename).suffix.lower()
        if ext in ACCEPTED_EXTS:
            return ext
    if content_type:
        ct = content_type.split(";", 1)[0].strip().lower()
        if ct in CONTENT_TYPE_EXTS:
            return CONTENT_TYPE_EXTS[ct]
    allowed = ", ".join(sorted(ACCEPTED_EXTS))
    raise HTTPException(
        status_code=415,
        detail=f"unsupported audio type; expected {allowed}",
    )


def _validate_magic(ext: str, chunk: bytes) -> None:
    """Reject obvious filename/MIME spoofing for WAV and OGG uploads."""
    if not chunk:
        return
    ok = False
    if ext == ".wav":
        ok = len(chunk) >= 12 and chunk[:4] == b"RIFF" and chunk[8:12] == b"WAVE"
    elif ext == ".ogg":
        ok = chunk.startswith(b"OggS")
    if not ok:
        raise HTTPException(status_code=415, detail=f"invalid {ext} audio header")


def _parse_bool_form(v: str | None, default: bool) -> bool:
    """Parse a string-encoded bool from multipart form data.

    Multipart bools are tricky in FastAPI — bare bool params coerce
    inconsistently across clients (httpx, OkHttp, curl).  Standardize on
    string "true"/"false" / "1"/"0" / "yes"/"no".  Missing or unparseable
    -> default.
    """
    if v is None:
        return default
    s = v.strip().lower()
    if s in ("true", "1", "yes", "on"):
        return True
    if s in ("false", "0", "no", "off"):
        return False
    return default


@router.post("/upload", dependencies=[Depends(ratelimit.limit_upload)])
async def upload(
    request: Request,
    audio: UploadFile = File(...),
    title: str | None = Form(default=None),
    summary_preset: str | None = Form(default=None),
    # Per-upload privacy toggles.  Default True preserves pre-opt-out
    # behavior for older clients (vezir < 0.1.11, vezir-android < 0.1.4)
    # that don't send these fields.
    auto_label: str | None = Form(default=None),
    sync: str | None = Form(default=None),
    personal: str | None = Form(default=None),
    audio_bytes: int | None = Form(default=None),
    auth_triple: tuple = Depends(auth.require_team_context),
):
    # v0.6.0: team_id is derived server-side from the bearer token;
    # clients never supply it.  This is the cornerstone of the team-
    # isolation invariant — see vezir_plan.md v0.6.0 design notes.
    github, team_id, _admin = auth_triple
    auto_label_enabled = _parse_bool_form(auto_label, default=True)
    sync_enabled = _parse_bool_form(sync, default=True)
    is_personal = _parse_bool_form(personal, default=False)
    config.ensure_dirs()
    max_bytes = config.max_upload_bytes()
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > max_bytes:
                raise HTTPException(status_code=413, detail="upload too large")
        except ValueError:
            pass

    ext = _pick_extension(audio.filename, audio.content_type)
    session_id = ulid.new().str
    sdir = config.sessions_dir() / session_id
    config.secure_mkdir(sdir)
    out = sdir / f"{session_id}{ext}"

    bytes_written = 0
    try:
        with out.open("wb") as f:
            config.secure_chmod_file(out)
            first_chunk = True
            while True:
                chunk = await audio.read(CHUNK_BYTES)
                if not chunk:
                    break
                if first_chunk:
                    _validate_magic(ext, chunk)
                    first_chunk = False
                bytes_written += len(chunk)
                if bytes_written > max_bytes:
                    raise HTTPException(status_code=413, detail="upload too large")
                f.write(chunk)
        if audio_bytes is not None and bytes_written != audio_bytes:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"upload incomplete: received {bytes_written} bytes, "
                    f"expected {audio_bytes}"
                ),
            )
        config.secure_chmod_file(out)
    except HTTPException:
        out.unlink(missing_ok=True)
        try:
            sdir.rmdir()
        except OSError:
            pass
        raise

    log.info(
        "upload accepted: session=%s github=%s team=%s bytes=%d ext=%s "
        "title=%r summary_preset=%r auto_label=%s sync=%s personal=%s",
        session_id, github, team_id, bytes_written, ext, title,
        summary_preset, auto_label_enabled, sync_enabled, is_personal,
    )

    queue.enqueue(
        session_id,
        github=github,
        team_id=team_id,
        title=title,
        summary_preset=summary_preset,
        auto_label_enabled=auto_label_enabled,
        sync_enabled=sync_enabled,
        personal=is_personal,
    )

    return {
        "session_id": session_id,
        "bytes": bytes_written,
    }
