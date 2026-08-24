"""Upload endpoint.

POST /upload
    multipart/form-data with:
        audio: the .wav/.ogg/.mp3 file produced by `millet record` or `vezir upload`
        title: optional meeting title

    Returns: { "session_id": "<ulid>", "bytes": <n> }
"""
from __future__ import annotations

import json
import logging
import shutil
import threading
import time
from pathlib import Path

import ulid
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from starlette.concurrency import run_in_threadpool

from .. import config
from . import auth, queue, ratelimit

log = logging.getLogger("vezir.uploads")

router = APIRouter()


CHUNK_BYTES = 4 * 1024 * 1024  # 4 MB

# ── Idempotency cache (v0.12.1) ─────────────────────────────────────────────
#
# The one-shot ``/upload`` retry re-POSTs the whole file, so a
# lost/late response makes the client retry an upload the server already
# committed — creating a SECOND session (double GPU work + duplicate meeting
# in the team repo).  The client sends a stable ``Idempotency-Key`` header
# per logical upload; we remember (github, team, key) -> session_id for a
# short TTL and replay the same session_id on a repeat instead of enqueuing
# again.  In-memory is sufficient: retries happen within seconds-to-minutes
# and this is single-process.
_IDEMPOTENCY: dict[tuple[str, str, str], tuple[float, str]] = {}
_IDEMPOTENCY_TTL_SEC = 15 * 60
_IDEMPOTENCY_LOCK = threading.Lock()


def _idempotency_get(github: str, team_id: str, key: str) -> str | None:
    if not key:
        return None
    now = time.time()
    with _IDEMPOTENCY_LOCK:
        # Opportunistic prune.
        for k in [k for k, (ts, _) in _IDEMPOTENCY.items() if now - ts > _IDEMPOTENCY_TTL_SEC]:
            _IDEMPOTENCY.pop(k, None)
        entry = _IDEMPOTENCY.get((github, team_id, key))
        return entry[1] if entry is not None else None


def _idempotency_put(github: str, team_id: str, key: str, session_id: str) -> None:
    if not key:
        return
    with _IDEMPOTENCY_LOCK:
        _IDEMPOTENCY[(github, team_id, key)] = (time.time(), session_id)


def _reset_idempotency_for_tests() -> None:
    with _IDEMPOTENCY_LOCK:
        _IDEMPOTENCY.clear()


# Resumable-upload tuning.
TUS_VERSION = "1.0.0"
# Abandoned .part sessions older than this are swept (24h, per plan).
RESUMABLE_TTL_SEC = 24 * 60 * 60

# Audio extensions vezir accepts. millet decodes all of these via ffmpeg
# (whisperx.load_audio is ffmpeg-backed); MP3 is supported end-to-end since 0.8.11.
ACCEPTED_EXTS = {".wav", ".ogg", ".mp3"}
CONTENT_TYPE_EXTS = {
    "audio/wav": ".wav",
    "audio/wave": ".wav",
    "audio/x-wav": ".wav",
    "audio/vnd.wave": ".wav",
    "audio/ogg": ".ogg",
    "application/ogg": ".ogg",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
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
    """Reject obvious filename/MIME spoofing for WAV, OGG, and MP3 uploads."""
    if not chunk:
        return
    ok = False
    if ext == ".wav":
        ok = len(chunk) >= 12 and chunk[:4] == b"RIFF" and chunk[8:12] == b"WAVE"
    elif ext == ".ogg":
        ok = chunk.startswith(b"OggS")
    elif ext == ".mp3":
        # MP3 has no single fixed prefix: either an ID3v2 tag ("ID3") or a raw
        # MPEG audio frame sync (11 set bits: 0xFF followed by 0xE0-mask high bits).
        ok = chunk.startswith(b"ID3") or (
            len(chunk) >= 2 and chunk[0] == 0xFF and (chunk[1] & 0xE0) == 0xE0
        )
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
    idempotency_key: str | None = Header(default=None),
    auth_triple: tuple = Depends(auth.require_team_context),
):
    # v0.6.0: team_id is derived server-side from the bearer token;
    # clients never supply it.  This is the cornerstone of the team-
    # isolation invariant — see vezir_plan.md v0.6.0 design notes.
    github, team_id, _admin = auth_triple
    # Idempotent retry: if this exact upload already committed, return the
    # existing session instead of creating a duplicate (M2).
    key = (idempotency_key or "").strip()[:128]
    existing = _idempotency_get(github, team_id, key)
    if existing is not None:
        log.info(
            "upload idempotency hit: key=%s -> session=%s (github=%s)",
            key, existing, github,
        )
        return {
            "session_id": existing,
            "idempotent": True,
        }
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
    except BaseException:
        # BaseException, not just HTTPException: a mid-upload client
        # disconnect raises starlette's ClientDisconnect (a plain
        # Exception), which previously slipped past the cleanup and
        # left a partial audio file + orphan session dir on disk
        # forever (nothing sweeps never-enqueued session dirs).
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

    # enqueue takes the process-global DB lock (busy_timeout 5s); run it off
    # the event loop so DB contention with the worker/CLI can't freeze every
    # other request (health included).  See M-6.
    await run_in_threadpool(
        queue.enqueue,
        session_id,
        github=github,
        team_id=team_id,
        title=title,
        summary_preset=summary_preset,
        auto_label_enabled=auto_label_enabled,
        sync_enabled=sync_enabled,
        personal=is_personal,
        client_agent=request.headers.get("user-agent"),
    )
    _idempotency_put(github, team_id, key, session_id)

    return {
        "session_id": session_id,
        "bytes": bytes_written,
    }


# ─── Multi-audio upload (v0.9.0) ─────────────────────────────────────────────
#
# A single meeting split across several audio files (e.g. a batch of Telegram
# voicenotes).  All parts are uploaded in one multipart request, stored as
# ``<session_id>.part-NNN<ext>`` in filename/upload order, and stitched into the
# canonical ``<session_id><ext>`` by the worker (filename order) before
# transcribe.  One upload -> one session -> one job (multi_audio=1).


@router.post("/upload/multi", dependencies=[Depends(ratelimit.limit_upload)])
async def upload_multi(
    request: Request,
    audio: list[UploadFile] = File(...),
    title: str | None = Form(default=None),
    summary_preset: str | None = Form(default=None),
    auto_label: str | None = Form(default=None),
    sync: str | None = Form(default=None),
    personal: str | None = Form(default=None),
    audio_bytes: int | None = Form(default=None),
    auth_triple: tuple = Depends(auth.require_team_context),
):
    """Accept multiple audio files as a single meeting.

    The client is responsible for ordering the files (it sends them in the
    desired order); the server preserves that order via a zero-padded
    ``.part-NNN`` suffix.  The worker concatenates them before transcribe.
    """
    github, team_id, _admin = auth_triple
    auto_label_enabled = _parse_bool_form(auto_label, default=True)
    sync_enabled = _parse_bool_form(sync, default=True)
    is_personal = _parse_bool_form(personal, default=False)

    if not audio:
        raise HTTPException(status_code=400, detail="no audio files provided")

    config.ensure_dirs()
    max_bytes = config.max_upload_bytes()
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > max_bytes:
                raise HTTPException(status_code=413, detail="upload too large")
        except ValueError:
            pass

    session_id = ulid.new().str
    sdir = config.sessions_dir() / session_id
    config.secure_mkdir(sdir)

    # All parts must share one extension so the concatenated output has a
    # single, unambiguous container.  We pick the extension from the first
    # part and require the rest to agree.
    first_ext = _pick_extension(audio[0].filename, audio[0].content_type)

    total_written = 0
    try:
        for idx, part_file in enumerate(audio):
            ext = _pick_extension(part_file.filename, part_file.content_type)
            if ext != first_ext:
                raise HTTPException(
                    status_code=415,
                    detail=(
                        f"all parts must share one audio type; part {idx} is "
                        f"{ext}, expected {first_ext}"
                    ),
                )
            out = sdir / f"{session_id}.part-{idx:03d}{ext}"
            with out.open("wb") as f:
                config.secure_chmod_file(out)
                first_chunk = True
                while True:
                    chunk = await part_file.read(CHUNK_BYTES)
                    if not chunk:
                        break
                    if first_chunk:
                        _validate_magic(ext, chunk)
                        first_chunk = False
                    total_written += len(chunk)
                    if total_written > max_bytes:
                        raise HTTPException(
                            status_code=413, detail="upload too large"
                        )
                    f.write(chunk)
            config.secure_chmod_file(out)
        if audio_bytes is not None and total_written != audio_bytes:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"upload incomplete: received {total_written} bytes, "
                    f"expected {audio_bytes}"
                ),
            )
    except BaseException:
        # See the single-file endpoint: ClientDisconnect must clean up too.
        for stale in sdir.glob(f"{session_id}.part-*"):
            stale.unlink(missing_ok=True)
        try:
            sdir.rmdir()
        except OSError:
            pass
        raise

    log.info(
        "multi upload accepted: session=%s github=%s team=%s parts=%d bytes=%d "
        "ext=%s title=%r summary_preset=%r auto_label=%s sync=%s personal=%s",
        session_id, github, team_id, len(audio), total_written, first_ext,
        title, summary_preset, auto_label_enabled, sync_enabled, is_personal,
    )

    await run_in_threadpool(
        queue.enqueue,
        session_id,
        github=github,
        team_id=team_id,
        title=title,
        summary_preset=summary_preset,
        auto_label_enabled=auto_label_enabled,
        sync_enabled=sync_enabled,
        personal=is_personal,
        multi_audio=True,
        client_agent=request.headers.get("user-agent"),
    )

    return {
        "session_id": session_id,
        "bytes": total_written,
        "parts": len(audio),
    }


# ─── Resumable uploads (tus.io 1.0 subset, v0.7.3+) ──────────────────────────
#
# Protocol:
#   POST   /upload/resumable           → create session; returns upload_id +
#                                         Location; stores metadata sidecar.
#   HEAD   /upload/resumable/{id}       → returns Upload-Offset (bytes on disk).
#   PATCH  /upload/resumable/{id}       → append at Upload-Offset; on reaching
#                                         Upload-Length, assemble + enqueue.
#
# A client that drops mid-transfer HEADs the id to learn the server's
# offset, then resumes the PATCH from there.  The on-disk staging lives
# in config.uploads_tmp_dir() as <id>.part + <id>.meta.json.


# Per-upload-id append locks (v0.11.0).  Two concurrent PATCHes with the
# same (valid) offset both passed the 409 check and interleaved writes
# into the same .part file — corrupting the audio while the offset
# bookkeeping still "added up".  Single-instance deployment makes an
# in-process lock sufficient.  Entries are dropped on finalize/sweep.
_PATCH_LOCKS: dict[str, threading.Lock] = {}
_PATCH_LOCKS_GUARD = threading.Lock()


def _patch_lock(upload_id: str) -> threading.Lock:
    with _PATCH_LOCKS_GUARD:
        return _PATCH_LOCKS.setdefault(upload_id, threading.Lock())


def _drop_patch_lock(upload_id: str) -> None:
    with _PATCH_LOCKS_GUARD:
        _PATCH_LOCKS.pop(upload_id, None)


def _part_path(upload_id: str) -> Path:
    return config.uploads_tmp_dir() / f"{upload_id}.part"


def _meta_path(upload_id: str) -> Path:
    return config.uploads_tmp_dir() / f"{upload_id}.meta.json"


def _load_meta(upload_id: str) -> dict | None:
    p = _meta_path(upload_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_meta(upload_id: str, meta: dict) -> None:
    config.secure_write_text(_meta_path(upload_id), json.dumps(meta, indent=2))


def sweep_abandoned_uploads(now: float | None = None) -> int:
    """Delete resumable staging files older than RESUMABLE_TTL_SEC.

    Called at startup and periodically by the worker loop.  Returns the
    number of upload sessions removed.  Best-effort; never raises.
    """
    now = now if now is not None else time.time()
    removed = 0
    tmp = config.uploads_tmp_dir()
    if not tmp.is_dir():
        return 0
    for meta_file in list(tmp.glob("*.meta.json")):
        try:
            created = json.loads(meta_file.read_text())["created_at_epoch"]
        except Exception:
            created = meta_file.stat().st_mtime
        if now - created < RESUMABLE_TTL_SEC:
            continue
        upload_id = meta_file.name[: -len(".meta.json")]
        _part_path(upload_id).unlink(missing_ok=True)
        meta_file.unlink(missing_ok=True)
        _drop_patch_lock(upload_id)
        removed += 1
    # Second pass (v0.11.0): orphan .part files whose meta sidecar is gone
    # (crash between replace() and unlink in _finalize_resumable, manual
    # cleanup, corrupt meta write) were never reclaimed by the meta-driven
    # loop above.
    for part_file in list(tmp.glob("*.part")):
        upload_id = part_file.name[: -len(".part")]
        if _meta_path(upload_id).exists():
            continue
        try:
            if now - part_file.stat().st_mtime < RESUMABLE_TTL_SEC:
                continue
        except OSError:
            continue
        part_file.unlink(missing_ok=True)
        _drop_patch_lock(upload_id)
        removed += 1
    if removed:
        log.info("swept %d abandoned resumable upload(s)", removed)
    return removed


@router.post(
    "/upload/resumable",
    dependencies=[Depends(ratelimit.limit_upload)],
    status_code=201,
)
async def create_resumable_upload(
    request: Request,
    response: Response,
    upload_length: int | None = Header(default=None),
    upload_filename: str | None = Header(default=None),
    upload_content_type: str | None = Header(default=None),
    title: str | None = Form(default=None),
    summary_preset: str | None = Form(default=None),
    auto_label: str | None = Form(default=None),
    sync: str | None = Form(default=None),
    personal: str | None = Form(default=None),
    auth_triple: tuple = Depends(auth.require_team_context),
):
    """Create a resumable upload session (tus creation extension).

    The client sends ``Upload-Length`` (total bytes) and the original
    filename / content-type as headers (used to pick the on-disk
    extension and validate the magic bytes once the first chunk lands).
    """
    github, team_id, _admin = auth_triple
    if upload_length is None or upload_length <= 0:
        raise HTTPException(status_code=400, detail="missing/invalid Upload-Length")
    max_bytes = config.max_upload_bytes()
    if upload_length > max_bytes:
        raise HTTPException(status_code=413, detail="upload too large")

    ext = _pick_extension(upload_filename, upload_content_type)
    upload_id = ulid.new().str
    config.secure_mkdir(config.uploads_tmp_dir())
    # Touch an empty part file.
    part = _part_path(upload_id)
    part.touch()
    config.secure_chmod_file(part)

    _save_meta(upload_id, {
        "upload_id": upload_id,
        "github": github,
        "team_id": team_id,
        "ext": ext,
        "upload_length": upload_length,
        "title": title,
        "summary_preset": summary_preset,
        "auto_label": _parse_bool_form(auto_label, default=True),
        "sync": _parse_bool_form(sync, default=True),
        "personal": _parse_bool_form(personal, default=False),
        # Capture the creating client's User-Agent now; the same client
        # streams the chunks, so this is the authoritative provenance.
        "client_agent": request.headers.get("user-agent"),
        "created_at_epoch": time.time(),
    })

    log.info(
        "resumable upload created: id=%s github=%s team=%s length=%d ext=%s",
        upload_id, github, team_id, upload_length, ext,
    )
    response.headers["Location"] = f"/upload/resumable/{upload_id}"
    response.headers["Tus-Resumable"] = TUS_VERSION
    response.headers["Upload-Offset"] = "0"
    return {"upload_id": upload_id, "offset": 0}


def _owned_meta_or_404(upload_id: str, github: str, team_id: str) -> dict:
    """Load the meta for an upload, enforcing the caller owns it.

    Returns 404 for both "doesn't exist" and "not yours" so we never
    leak the existence of another user's/team's upload session.
    """
    meta = _load_meta(upload_id)
    if meta is None or meta.get("github") != github or meta.get("team_id") != team_id:
        raise HTTPException(status_code=404, detail="upload session not found")
    return meta


@router.head("/upload/resumable/{upload_id}")
async def resumable_offset(
    upload_id: str,
    auth_triple: tuple = Depends(auth.require_team_context),
):
    """Return the current ``Upload-Offset`` so a client can resume."""
    github, team_id, _admin = auth_triple
    meta = _owned_meta_or_404(upload_id, github, team_id)
    part = _part_path(upload_id)
    offset = part.stat().st_size if part.exists() else 0
    return Response(
        status_code=200,
        headers={
            "Upload-Offset": str(offset),
            "Upload-Length": str(meta["upload_length"]),
            "Tus-Resumable": TUS_VERSION,
        },
    )


@router.patch(
    "/upload/resumable/{upload_id}",
    # v0.7.8: do NOT rate-limit the chunk-append endpoint.  The resumable
    # protocol sends one PATCH per ``chunk_bytes`` (4 MB) slice, so a
    # single ~40 MB meeting issues 10+ PATCHes in seconds and drained the
    # old 10/min "upload" bucket, hard-failing the upload with 429.  The
    # bucket is meant to limit *uploads started*, not *chunks*, so it
    # stays on the creation endpoints (POST /upload, POST /upload/resumable)
    # below/above.  PATCH is already authenticated, offset-validated, and
    # total-size-capped (Upload-Length checked at create time), so a
    # runaway client can't write unbounded data here.
)
async def resumable_append(
    upload_id: str,
    request: Request,
    response: Response,
    upload_offset: int | None = Header(default=None),
    auth_triple: tuple = Depends(auth.require_team_context),
):
    """Append a chunk at ``Upload-Offset`` (tus core PATCH).

    On reaching ``Upload-Length`` the staged file is magic-validated,
    moved into ``sessions/<session_id>/`` and enqueued — returning the
    ``session_id``.
    """
    github, team_id, _admin = auth_triple
    meta = _owned_meta_or_404(upload_id, github, team_id)

    if upload_offset is None:
        raise HTTPException(status_code=400, detail="missing Upload-Offset")

    # One in-flight PATCH per upload id.  Non-blocking acquire: a second
    # concurrent chunk is a protocol violation (tus is sequential per
    # upload), and blocking here would stall the event loop's thread.
    lock = _patch_lock(upload_id)
    if not lock.acquire(blocking=False):
        raise HTTPException(
            status_code=409,
            detail="another chunk for this upload is already in flight",
        )
    try:
        part = _part_path(upload_id)
        current = part.stat().st_size if part.exists() else 0
        if upload_offset != current:
            # tus mandates 409 on offset mismatch so the client re-syncs.
            raise HTTPException(
                status_code=409,
                detail=f"offset mismatch: server at {current}, client sent {upload_offset}",
            )

        total = meta["upload_length"]
        ext = meta["ext"]
        max_bytes = config.max_upload_bytes()

        written = current
        validated = current > 0  # first chunk already validated on a prior PATCH
        with part.open("ab") as f:
            async for chunk in request.stream():
                if not chunk:
                    continue
                if not validated:
                    _validate_magic(ext, chunk)
                    validated = True
                written += len(chunk)
                if written > total or written > max_bytes:
                    # Roll back this PATCH's bytes; client must not overshoot.
                    f.flush()
                    raise HTTPException(status_code=413, detail="upload exceeds declared length")
                f.write(chunk)
        config.secure_chmod_file(part)

        response.headers["Upload-Offset"] = str(written)
        response.headers["Tus-Resumable"] = TUS_VERSION

        if written < total:
            # More chunks to come.
            return Response(
                status_code=204,
                headers={
                    "Upload-Offset": str(written),
                    "Tus-Resumable": TUS_VERSION,
                },
            )

        # Complete — assemble into a session dir and enqueue.  Runs file
        # move + chmod + DB enqueue (global lock), so keep it off the event
        # loop (M-6).
        session_id = await run_in_threadpool(
            _finalize_resumable, upload_id, meta
        )
        return {"session_id": session_id, "bytes": written}
    finally:
        lock.release()


def _finalize_resumable(upload_id: str, meta: dict) -> str:
    """Move a completed .part into sessions/<id>/ and enqueue the job."""
    ext = meta["ext"]
    session_id = ulid.new().str
    sdir = config.sessions_dir() / session_id
    config.secure_mkdir(sdir)
    out = sdir / f"{session_id}{ext}"
    _part_path(upload_id).replace(out)
    config.secure_chmod_file(out)
    _meta_path(upload_id).unlink(missing_ok=True)
    _drop_patch_lock(upload_id)

    log.info(
        "resumable upload complete: upload_id=%s session=%s github=%s team=%s",
        upload_id, session_id, meta["github"], meta["team_id"],
    )
    queue.enqueue(
        session_id,
        github=meta["github"],
        team_id=meta["team_id"],
        title=meta.get("title"),
        summary_preset=meta.get("summary_preset"),
        auto_label_enabled=meta.get("auto_label", True),
        sync_enabled=meta.get("sync", True),
        personal=meta.get("personal", False),
        client_agent=meta.get("client_agent"),
    )
    return session_id


# ─── Artifact-bundle import (v0.16.0) ───────────────────────────────────────
#
# Sessions that were recorded AND processed locally by millet before the
# team existed on the vezir server (or never uploaded) can't be listed by
# the TUI/MCP — the DB only knows uploaded sessions.  This endpoint imports
# the existing artifact bundle as-is: no re-transcription, original content
# and labels preserved verbatim.  The job lands with status "imported";
# auto-label and sync stay off until the user triggers them on demand.


# Uploaded filename suffix -> on-disk suffix under <session_id>.  Order
# matters: longest/most-specific suffixes first so "x.summary.md" isn't
# misread as a plain ".txt"/".md"-class file and the transcript json is
# only claimed after the sidecar jsons are ruled out.
_IMPORT_SUFFIXES: list[tuple[str, str]] = [
    (".summary.meta.json", ".summary.meta.json"),
    (".frontmatter.json", ".frontmatter.json"),
    (".session.json", ".session.json"),
    (".autoid.json", ".autoid.json"),
    (".summary.md", ".summary.md"),
    (".json", ".json"),        # the transcript (mandatory)
    (".txt", ".txt"),
    (".srt", ".srt"),
    (".pdf", ".pdf"),
    (".ogg", ".ogg"),
    (".wav", ".wav"),
    (".mp3", ".mp3"),
]

_IMPORT_AUDIO_EXTS = {".ogg", ".wav", ".mp3"}

# Bundles carry several artifacts plus possibly the audio; the audio alone
# already approaches max_upload_bytes for long meetings, so the bundle cap
# is the audio cap plus headroom for text artifacts.
_IMPORT_TEXT_CAP = 32 * 1024 * 1024  # per non-audio artifact


def _classify_import_file(filename: str) -> str | None:
    """Map an uploaded bundle filename to its canonical on-disk suffix."""
    if not filename or "/" in filename or "\\" in filename or ".." in filename:
        return None
    lower = filename.lower()
    for suffix, out_suffix in _IMPORT_SUFFIXES:
        if lower.endswith(suffix):
            return out_suffix
    return None


def _frontmatter_meta(sdir: Path, session_id: str) -> dict:
    """Best-effort title/date extraction from an imported frontmatter json."""
    p = sdir / f"{session_id}.frontmatter.json"
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    title = data.get("title")
    if isinstance(title, str) and title.strip():
        out["title"] = title
    date = data.get("date")
    if isinstance(date, str) and date.strip():
        # millet frontmatter dates are ISO 8601; store as-is (workers and
        # clients parse both naive and Z-suffixed forms).
        out["created_at"] = date.strip()
    return out


@router.post(
    "/api/sessions/import",
    dependencies=[Depends(ratelimit.limit_upload)],
)
async def import_session_endpoint(
    request: Request,
    files: list[UploadFile] = File(...),
    title: str | None = Form(default=None),
    auth_triple: tuple = Depends(auth.require_team_context),
):
    """Import a pre-processed session's artifact bundle, verbatim.

    Multipart fields:
        files: one or more artifact files (millet session-dir contents).
               The transcript .json is mandatory; txt/srt/summary.md/pdf/
               frontmatter/session/autoid jsons and the audio (.ogg/.wav/
               .mp3) are optional but recommended (audio enables clips and
               on-demand auto-label later).
        title: optional meeting title override (frontmatter title wins).

    The job is registered with status ``imported`` — no transcription,
    no auto-label, no sync (both off until explicitly triggered).
    """
    github, team_id, _admin = auth_triple
    config.ensure_dirs()
    max_bytes = config.max_upload_bytes()

    session_id = ulid.new().str
    sdir = config.sessions_dir() / session_id
    config.secure_mkdir(sdir)

    transcript_path: Path | None = None
    total_written = 0
    total_cap = max_bytes + (8 * _IMPORT_TEXT_CAP)
    try:
        for uf in files:
            out_suffix = _classify_import_file(uf.filename or "")
            if out_suffix is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"unsupported or unsafe file name: {uf.filename!r}",
                )
            out = sdir / f"{session_id}{out_suffix}"
            if out.exists():
                raise HTTPException(
                    status_code=400,
                    detail=f"duplicate artifact type in bundle: {uf.filename!r}",
                )
            cap = max_bytes if out_suffix in _IMPORT_AUDIO_EXTS else _IMPORT_TEXT_CAP
            written = 0
            with out.open("wb") as f:
                config.secure_chmod_file(out)
                first_chunk = True
                while True:
                    chunk = await uf.read(CHUNK_BYTES)
                    if not chunk:
                        break
                    if first_chunk:
                        if out_suffix in _IMPORT_AUDIO_EXTS:
                            _validate_magic(out_suffix, chunk)
                        first_chunk = False
                    written += len(chunk)
                    total_written += len(chunk)
                    if written > cap or total_written > total_cap:
                        raise HTTPException(
                            status_code=413, detail="import bundle too large",
                        )
                    f.write(chunk)
            config.secure_chmod_file(out)
            if written == 0:
                raise HTTPException(
                    status_code=400, detail=f"empty file: {uf.filename!r}",
                )
            if out_suffix == ".json":
                transcript_path = out

        if transcript_path is None:
            raise HTTPException(
                status_code=400,
                detail="bundle must include the transcript .json",
            )
        # The transcript must be a real millet transcript (segments list),
        # not some other json that happened to end in .json.
        try:
            tdata = json.loads(transcript_path.read_text(encoding="utf-8"))
        except Exception:
            raise HTTPException(
                status_code=400, detail="transcript .json is not valid JSON",
            ) from None
        if not isinstance(tdata, dict) or not isinstance(tdata.get("segments"), list):
            raise HTTPException(
                status_code=400,
                detail="transcript .json has no 'segments' list",
            )
    except BaseException:
        shutil.rmtree(sdir, ignore_errors=True)
        raise

    meta = _frontmatter_meta(sdir, session_id)
    from .worker import _find_artifacts

    artifacts = _find_artifacts(sdir)
    await run_in_threadpool(
        queue.import_session,
        session_id,
        github,
        team_id=team_id,
        title=title or meta.get("title"),
        created_at=meta.get("created_at"),
        artifacts=artifacts,
    )

    log.info(
        "import accepted: session=%s github=%s team=%s files=%d bytes=%d "
        "title=%r created_at=%r",
        session_id, github, team_id, len(files), total_written,
        title or meta.get("title"), meta.get("created_at"),
    )
    return {
        "session_id": session_id,
        "status": "imported",
        "artifacts": artifacts,
        "title": title or meta.get("title"),
    }
