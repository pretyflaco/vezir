"""Upload endpoint.

POST /upload
    multipart/form-data with:
        audio: the .wav/.ogg file produced by `meet record` or `vezir upload`
        title: optional meeting title

    Returns: { "session_id": "<ulid>", "dashboard_url": "..." }

The uploaded WAV is stored at sessions/<id>/<id>.wav (single-channel or
dual-channel; meetscribe handles both). A new job is enqueued for the
worker to process.
"""
from __future__ import annotations

import logging
from pathlib import Path

import ulid
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile

from .. import config
from . import auth, queue, web_sessions

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


@router.post("/upload")
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
    audio_bytes: int | None = Form(default=None),
    github: str = Depends(auth.require_bearer),
):
    auto_label_enabled = _parse_bool_form(auto_label, default=True)
    sync_enabled = _parse_bool_form(sync, default=True)
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
        "upload accepted: session=%s github=%s bytes=%d ext=%s title=%r "
        "summary_preset=%r auto_label=%s sync=%s",
        session_id, github, bytes_written, ext, title, summary_preset,
        auto_label_enabled, sync_enabled,
    )

    queue.enqueue(
        session_id,
        github=github,
        title=title,
        summary_preset=summary_preset,
        auto_label_enabled=auto_label_enabled,
        sync_enabled=sync_enabled,
    )

    base = str(request.base_url).rstrip("/")
    # `dashboard_url` is the canonical session detail path; clients with a
    # bearer-token-aware HTTP client can fetch it directly.
    # `dashboard_login_url` is the same destination wrapped through /login
    # so a browser opened to it picks up a session cookie before being
    # redirected.
    #
    # 0.1.12: this URL now embeds a one-time, 60-second exchange code
    # instead of the plaintext bearer token. The code resolves to the
    # uploader's bearer on /login consume (single-use) and is then
    # swapped for an opaque session id stored in the cookie. The bearer
    # never enters the URL, browser history, or Caddy access log.
    from urllib.parse import quote
    auth_token = request.headers.get("authorization", "")
    if auth_token.lower().startswith("bearer "):
        plaintext = auth_token.split(None, 1)[1].strip()
        code = web_sessions.mint_exchange_code(plaintext)
        login_url = (
            f"{base}/login?code={quote(code, safe='')}"
            f"&next=%2Fs%2F{session_id}"
        )
    else:
        # No bearer header (shouldn't happen given require_bearer above,
        # but stay defensive): emit a code-free URL that lands on the
        # paste-token form.
        login_url = f"{base}/login?next=%2Fs%2F{session_id}"
    return {
        "session_id": session_id,
        "bytes": bytes_written,
        "dashboard_url": f"{base}/s/{session_id}",
        "dashboard_login_url": login_url,
    }
