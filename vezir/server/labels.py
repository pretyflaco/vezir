"""Speaker labeling endpoints: web UI (HTML) and native-client API (JSON).

HTML routes (browser / web dashboard):
  GET  /label/<session-id>                      → HTML labeling page
  GET  /label/<session-id>/clip/<speaker-id>    → audio clip (WAV)
  POST /label/<session-id>                      → apply label_map, regenerate

JSON API routes (Android / programmatic clients):
  GET  /api/label/<session-id>                  → JSON speaker list + team
  POST /api/label/<session-id>                  → apply labels from JSON body

The labeling page is shown when a session's status is `needs_labeling`.
On submit, vezir invokes millet's apply_labels() directly to relabel
the transcript and regenerate artifacts (txt, srt, json, summary, pdf),
then transitions the job to `syncing` → `done`.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import threading
from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from .. import config
from . import auth, meet_runner, queue, ratelimit, worker
from .templating import templates

log = logging.getLogger("vezir.labels")
router = APIRouter()


def _team_handles(team_id: str | None = None) -> list[str]:
    """Read roster of GitHub handles for the labeling dropdown.

    v0.6.0+: reads ``~/vezir-data/teams/<team_id>/roster.json`` (the
    per-team roster).  When called without ``team_id`` (back-compat
    path for legacy callers within the same process), falls back to
    the pre-v0.6.0 global ``team.json``.

    Per-team rosters are populated by the v0.6.0 migration which moves
    the old global ``team.json`` to ``teams/blink/roster.json`` and
    creates ``teams/twentyone/roster.json`` with pretyflaco +
    bettermorning seeded.
    """
    if team_id:
        p = config.team_roster_path(team_id)
    else:
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
    config.secure_mkdir(d)
    return d


def _find_wav(session_dir: Path) -> Path | None:
    """Locate session audio. Prefers WAV, falls back to OGG.

    Key name is `wav` for back-compat with millet's _find_session_files
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
    """Fetch SpeakerInfo list from millet for the given session."""
    from millet.label import get_speakers as meet_get_speakers
    return meet_get_speakers(_session_dir(session_id))


def _enforce_team_visibility(row: dict, viewer_team_id: str) -> None:
    """Reject cross-team access with a 404 (mirror of sessions._enforce_team_visibility).

    Duplicated here rather than imported to keep labels.py independent
    of sessions.py — both have the same security obligation; the
    duplication is intentional belt-and-suspenders.
    """
    if row.get("team_id") != viewer_team_id:
        raise HTTPException(404, "session not found")


@router.get("/label/{session_id}", response_class=HTMLResponse)
def label_page(
    request: Request,
    session_id: str,
    auth_triple: tuple = Depends(auth.require_bearer_or_cookie_full),
):
    github, team_id, _admin = auth_triple
    row = queue.get(session_id)
    if not row:
        raise HTTPException(404, "session not found")
    _enforce_team_visibility(row, team_id)
    if row["status"] not in ("needs_labeling", "done", "error"):
        return templates.TemplateResponse(
            request,
            "label_pending.html",
            {"request": request, "row": row, "me": github},
        )

    speakers = _get_speakers(session_id)
    return templates.TemplateResponse(
        request,
        "label.html",
        {
            "request": request,
            "row": row,
            "me": github,
            "speakers": speakers,
            "team": _team_handles(team_id),
        },
    )


@router.get(
    "/label/{session_id}/clip/{speaker_id}",
    dependencies=[Depends(ratelimit.limit_api)],
)
def label_clip(
    session_id: str,
    speaker_id: str,
    auth_triple: tuple = Depends(auth.require_bearer_or_cookie_full),
):
    """Return an audio clip for a speaker. Generates and caches on first hit."""
    _github, team_id, _admin = auth_triple
    if not re.match(r"^[A-Za-z0-9_]+$", speaker_id):
        raise HTTPException(400, "invalid speaker id")

    row = queue.get(session_id)
    if not row:
        raise HTTPException(404, "session not found")
    _enforce_team_visibility(row, team_id)

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

    from millet.label import extract_speaker_clip
    tmp = extract_speaker_clip(wav, sp)
    shutil.move(str(tmp), str(cached))
    config.secure_chmod_file(cached)
    return FileResponse(cached, media_type="audio/wav")


def _apply_and_finalize(
    session_id: str,
    label_map: dict[str, str],
    github: str,
    team_id: str,
) -> None:
    """Shared logic for both the HTML form POST and the JSON API POST.

    Applies labels via millet, updates the team's voiceprint DB, and
    spawns a background thread for sync + cleanup.

    v0.6.2+: ``team_id`` is required so the HOME shim points at the
    correct per-team voiceprint DB, and the
    ``update_profiles_from_confirmed_labels`` call writes to that
    same DB.
    """
    log.info(
        "session=%s team=%s labels=%s by=%s",
        session_id, team_id, label_map, github,
    )

    import os

    home = meet_runner.build_home_shim(session_id, team_id)
    saved = {k: os.environ.get(k) for k in ("HOME", "XDG_CONFIG_HOME")}
    try:
        os.environ["HOME"] = str(home)
        os.environ.pop("XDG_CONFIG_HOME", None)
        def _progress(msg: str) -> None:
            log.info("session=%s apply_labels: %s", session_id, msg)

        from millet.label import apply_labels
        apply_labels(
            _session_dir(session_id),
            label_map=label_map,
            regenerate_summary=False,
            progress_callback=_progress,
        )

        try:
            from millet.label import _detect_speaker_channels, _load_transcript
            from millet.voiceprint import update_profiles_from_confirmed_labels

            sdir = _session_dir(session_id)
            wav_path = _find_wav(sdir)
            tj_path = sdir / f"{sdir.name}.json"
            if wav_path and tj_path.exists():
                transcript = _load_transcript(tj_path)
                channel_map = _detect_speaker_channels(
                    wav_path, transcript.segments, transcript.speakers,
                )
                update_profiles_from_confirmed_labels(
                    wav_path,
                    transcript.segments,
                    label_map,
                    channel_map,
                    profiles_path=config.team_speaker_profiles_path(team_id),
                )
            else:
                log.warning(
                    "session=%s: skipping voiceprint update (wav=%s, transcript=%s)",
                    session_id, wav_path is not None, tj_path.exists(),
                )
        except Exception:
            log.exception(
                "could not update team %s voiceprint DB", team_id,
            )

    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    threading.Thread(
        target=worker.finalize_after_labeling,
        args=(session_id,),
        name=f"finalize-{session_id}",
        daemon=True,
    ).start()


_LABELABLE_STATUSES = ("needs_labeling", "done", "error")


@router.get(
    "/api/team",
    dependencies=[Depends(ratelimit.limit_api)],
)
def api_team(auth_triple: tuple = Depends(auth.require_bearer_full)):
    """Return the team handles list (for autocomplete in native clients).

    v0.6.0+: reads ``~/vezir-data/teams/<caller-team>/roster.json``.
    The label dropdown is scoped to the caller's team so cross-team
    handles don't appear in autocomplete.
    """
    _github, team_id, _admin = auth_triple
    return {"team": _team_handles(team_id)}


@router.post("/label/{session_id}")
async def submit_labels(
    request: Request,
    session_id: str,
    auth_triple: tuple = Depends(auth.require_bearer_or_cookie_full),
):
    """Apply user-assigned labels and trigger sync (HTML form POST)."""
    github, team_id, _admin = auth_triple
    row = queue.get(session_id)
    if not row:
        raise HTTPException(404, "session not found")
    _enforce_team_visibility(row, team_id)

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

    _apply_and_finalize(session_id, label_map, github, team_id)
    return RedirectResponse(url=f"/s/{session_id}", status_code=303)


# ── JSON API (native clients) ───────────────────────────────────────────────


@router.get(
    "/api/label/{session_id}",
    dependencies=[Depends(ratelimit.limit_api)],
)
def api_label_get(
    session_id: str,
    auth_triple: tuple = Depends(auth.require_bearer_full),
):
    _github, team_id, _admin = auth_triple
    """Return the speaker list and team handles for native-client labeling.

    Response:
        {
          "session_id": "01KS...",
          "status": "needs_labeling",
          "speakers": [
            {"id": "SPEAKER_00", "channel": "mic", "sample_text": "Yeah I think..."},
            ...
          ],
          "team": ["kasita", "pretyflaco", ...],
          "audio_available": true
        }

    Note: ``channel`` is a string from millet (e.g. "mic", "system"),
    not an integer.
    """
    row = queue.get(session_id)
    if not row:
        raise HTTPException(404, "session not found")
    _enforce_team_visibility(row, team_id)
    if row["status"] not in _LABELABLE_STATUSES:
        raise HTTPException(
            409,
            f"session status is '{row['status']}'; labeling requires "
            f"one of: {', '.join(_LABELABLE_STATUSES)}",
        )

    speakers = _get_speakers(session_id)
    sdir = _session_dir(session_id)
    audio_available = _find_wav(sdir) is not None

    return {
        "session_id": session_id,
        "status": row["status"],
        "speakers": [
            {
                "id": sp.id,
                "channel": getattr(sp, "channel", None),
                "sample_text": getattr(sp, "sample_text", None),
            }
            for sp in speakers
        ],
        "team": _team_handles(team_id),
        "audio_available": audio_available,
    }


@router.post(
    "/api/label/{session_id}",
    dependencies=[Depends(ratelimit.limit_api)],
)
def api_label_post(
    session_id: str,
    labels: dict = Body(..., example={"labels": {"REMOTE_0": "kasita"}}),
    auth_triple: tuple = Depends(auth.require_bearer_full),
):
    """Apply labels from a JSON body (native clients).

    Expected body:
        {"labels": {"REMOTE_0": "kasita", "REMOTE_1": "alice"}}

    Empty or missing labels for a speaker keep the auto-assigned label.
    """
    github, team_id, _admin = auth_triple
    row = queue.get(session_id)
    if not row:
        raise HTTPException(404, "session not found")
    _enforce_team_visibility(row, team_id)
    if row["status"] not in _LABELABLE_STATUSES:
        raise HTTPException(
            409,
            f"session status is '{row['status']}'; labeling requires "
            f"one of: {', '.join(_LABELABLE_STATUSES)}",
        )

    raw_labels = labels.get("labels")
    if not isinstance(raw_labels, dict):
        raise HTTPException(400, "body must contain a 'labels' dict")

    label_map: dict[str, str] = {}
    for speaker_id, name in raw_labels.items():
        if not isinstance(name, str):
            continue
        name = name.strip()
        if name:
            label_map[str(speaker_id)] = name

    _apply_and_finalize(session_id, label_map, github, team_id)
    return {"ok": True, "session_id": session_id}
