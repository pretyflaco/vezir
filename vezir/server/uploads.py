"""Upload endpoint.

POST /upload
    multipart/form-data with:
        audio: the .wav file produced by `meet record`
        title: optional meeting title

    Returns: { "session_id": "<ulid>", "dashboard_url": "..." }

The uploaded WAV is stored at sessions/<id>/<id>.wav (single-channel or
dual-channel; meetscribe handles both). A new job is enqueued for the
worker to process.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

import ulid
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile

from .. import config
from . import auth, queue

log = logging.getLogger("vezir.uploads")

router = APIRouter()


CHUNK_BYTES = 4 * 1024 * 1024  # 4 MB


@router.post("/upload")
async def upload(
    request: Request,
    audio: UploadFile = File(...),
    title: str | None = Form(default=None),
    github: str = Depends(auth.require_bearer),
):
    config.ensure_dirs()
    session_id = ulid.new().str
    sdir = config.sessions_dir() / session_id
    sdir.mkdir(parents=True, exist_ok=True)
    out = sdir / f"{session_id}.wav"

    bytes_written = 0
    with out.open("wb") as f:
        while True:
            chunk = await audio.read(CHUNK_BYTES)
            if not chunk:
                break
            f.write(chunk)
            bytes_written += len(chunk)

    log.info(
        "upload accepted: session=%s github=%s bytes=%d title=%r",
        session_id, github, bytes_written, title,
    )

    queue.enqueue(session_id, github=github, title=title)

    base = str(request.base_url).rstrip("/")
    return {
        "session_id": session_id,
        "bytes": bytes_written,
        "dashboard_url": f"{base}/s/{session_id}",
    }
