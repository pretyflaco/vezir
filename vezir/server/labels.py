"""Speaker labeling endpoints (the U2 web UI).

GET  /label/<session-id>                      → HTML labeling page
GET  /label/<session-id>/clip/<speaker-id>    → audio clip (WAV)
POST /label/<session-id>                      → apply label_map, regenerate

The labeling page is shown when a session's status is `needs_labeling`.
On submit, vezir invokes meetscribe's apply_labels() directly to relabel
the transcript and regenerate artifacts (txt, srt, json, summary, pdf),
then transitions the job to `syncing` → `done`.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from .. import config
from . import auth, meet_runner, queue, worker
from .templating import templates

log = logging.getLogger("vezir.labels")
router = APIRouter()


def _team_handles() -> list[str]:
    """Read team.json roster of GitHub handles."""
    p = config.team_json_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    handles = []
    for entry in data:
        h = entry.get("github") if isinstance(entry, dict) else None
        if h:
            handles.append(h)
    return sorted(handles)


def _session_dir(session_id: str) -> Path:
    return config.sessions_dir() / session_id


def _ensure_clips_dir(session_id: str) -> Path:
    d = _session_dir(session_id) / "clips"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _find_wav(session_dir: Path) -> Path | None:
    """Locate session audio. Prefers WAV, falls back to OGG.

    Key name is `wav` for back-compat with meetscribe's _find_session_files
    (which uses the same convention). Meetscribe's extract_speaker_clip
    handles both formats via its ffmpeg fallback.
    """
    wavs = sorted(session_dir.glob("*.wav"))
    if wavs:
        return wavs[0]
    oggs = sorted(session_dir.glob("*.ogg"))
    if oggs:
        return oggs[0]
    return None


def _get_speakers(session_id: str):
    """Fetch SpeakerInfo list from meetscribe for the given session."""
    from meet.label import get_speakers as meet_get_speakers
    return meet_get_speakers(_session_dir(session_id))


@router.get("/label/{session_id}", response_class=HTMLResponse)
def label_page(
    request: Request,
    session_id: str,
    github: str = Depends(auth.require_bearer),
):
    row = queue.get(session_id)
    if not row:
        raise HTTPException(404, "session not found")
    if row["status"] not in ("needs_labeling", "done", "error"):
        return templates.TemplateResponse(
            "label_pending.html",
            {"request": request, "row": row, "me": github},
        )

    speakers = _get_speakers(session_id)
    return templates.TemplateResponse(
        "label.html",
        {
            "request": request,
            "row": row,
            "me": github,
            "speakers": speakers,
            "team": _team_handles(),
        },
    )


@router.get("/label/{session_id}/clip/{speaker_id}")
def label_clip(
    session_id: str,
    speaker_id: str,
    github: str = Depends(auth.require_bearer),
):
    """Return an audio clip for a speaker. Generates and caches on first hit."""
    if not re.match(r"^[A-Za-z0-9_]+$", speaker_id):
        raise HTTPException(400, "invalid speaker id")

    sdir = _session_dir(session_id)
    if not sdir.exists():
        raise HTTPException(404, "session not found")

    cache_dir = _ensure_clips_dir(session_id)
    cached = cache_dir / f"{speaker_id}.wav"
    if cached.exists():
        return FileResponse(cached, media_type="audio/wav")

    wav = _find_wav(sdir)
    if wav is None:
        raise HTTPException(404, "audio file not available (already deleted?)")

    speakers = _get_speakers(session_id)
    sp = next((s for s in speakers if s.id == speaker_id), None)
    if sp is None:
        raise HTTPException(404, f"speaker {speaker_id} not found in transcript")

    from meet.label import extract_speaker_clip
    tmp = extract_speaker_clip(wav, sp)
    shutil.move(str(tmp), str(cached))
    return FileResponse(cached, media_type="audio/wav")


@router.post("/label/{session_id}")
async def submit_labels(
    request: Request,
    session_id: str,
    github: str = Depends(auth.require_bearer),
):
    """Apply user-assigned labels and trigger sync.

    Form fields are dynamic: one per speaker, named `label_<speaker_id>`.
    Empty values are skipped (speaker stays as-is).
    """
    row = queue.get(session_id)
    if not row:
        raise HTTPException(404, "session not found")

    form = await request.form()
    label_map: dict[str, str] = {}
    for key, value in form.items():
        if not key.startswith("label_"):
            continue
        if not isinstance(value, str):
            continue
        name = value.strip()
        if not name:
            continue
        speaker_id = key[len("label_"):]
        label_map[speaker_id] = name

    log.info("session=%s labels=%s by=%s", session_id, label_map, github)

    # Apply via meetscribe (regenerates txt/srt/json/summary/pdf in-place).
    # Run inside the per-job HOME shim so the central voiceprint DB is
    # updated by meetscribe's update_profiles_from_confirmed_labels()
    # if/when meet/label.py routes through it.
    home = meet_runner.build_home_shim(session_id)
    import os
    saved = {k: os.environ.get(k) for k in ("HOME", "XDG_CONFIG_HOME")}
    try:
        os.environ["HOME"] = str(home)
        os.environ.pop("XDG_CONFIG_HOME", None)
        from meet.label import apply_labels
        apply_labels(
            _session_dir(session_id),
            label_map=label_map,
            regenerate_summary=False,  # avoid LLM re-run; cheap find/replace path
        )

        # Update the central voiceprint DB with confirmed labels.
        try:
            from meet.voiceprint import update_profiles_from_confirmed_labels
            update_profiles_from_confirmed_labels(
                _session_dir(session_id),
                label_map,
            )
        except Exception:
            log.exception("could not update central voiceprint DB")

    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # Hand off to worker for sync + cleanup. Run in a background thread so
    # the HTTP request returns promptly.
    import threading
    threading.Thread(
        target=worker.finalize_after_labeling,
        args=(session_id,),
        name=f"finalize-{session_id}",
        daemon=True,
    ).start()

    return RedirectResponse(url=f"/s/{session_id}", status_code=303)
