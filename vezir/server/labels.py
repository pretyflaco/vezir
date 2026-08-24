"""Speaker labeling endpoints: JSON API and audio clips.

JSON API routes (Android / programmatic clients):
  GET  /api/team                                → team handles list
  GET  /api/label/<session-id>                  → JSON speaker list + team
  POST /api/label/<session-id>                  → apply labels from JSON body

Audio clip route:
  GET  /label/<session-id>/clip/<speaker-id>    → audio clip (WAV)

On submit, vezir runs `millet label --apply-json` as a subprocess (per-job
HOME shim, millet-pipeline >= 0.13.0) to relabel the transcript and
regenerate artifacts (txt, srt, json, summary, pdf), then queues the
voiceprint update + sync follow-up onto the background worker, which
transitions the job to `syncing` → `done`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import FileResponse

from .. import config
from . import auth, meet_runner, queue, ratelimit, worker

log = logging.getLogger("vezir.labels")
router = APIRouter()

# Speaker ids may now be real names (e.g. "Juan Pablo") once voiceprint
# auto-labeling persists matches into the transcript.  Reject only ids that
# could escape the clips cache directory or contain control characters; allow
# spaces and the common punctuation found in names.
_UNSAFE_CLIP_ID_RE = re.compile(r"[\x00-\x1f/\\]")


def _is_safe_clip_id(speaker_id: str) -> bool:
    """True if ``speaker_id`` is safe to resolve against the transcript.

    Blocks path separators, ``..`` traversal and control characters; allows
    letters, numbers, spaces and name punctuation (apostrophes, hyphens, dots).
    The on-disk cache filename is derived via ``_safe_clip_filename`` so the
    raw id is never used directly as a path component.
    """
    if not speaker_id or len(speaker_id) > 128:
        return False
    if ".." in speaker_id:
        return False
    return not _UNSAFE_CLIP_ID_RE.search(speaker_id)


def _safe_clip_filename(speaker_id: str) -> str:
    """Derive a path-safe ``.wav`` filename from a speaker id.

    Slugifies the id (alnum runs joined by underscores) and appends a short
    sha1 suffix so distinct ids never collide even after slugification.  Used
    for both the server-side cache and the client temp clip so playback maps
    back to the right speaker regardless of name punctuation.
    """
    slug = re.sub(r"[^A-Za-z0-9]+", "_", speaker_id).strip("_") or "speaker"
    digest = hashlib.sha1(speaker_id.encode("utf-8")).hexdigest()[:8]
    return f"{slug}-{digest}.wav"


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
    """Locate session audio. Prefers WAV, falls back to OGG then MP3.

    Key name is `wav` for back-compat with millet's _find_session_files
    (which uses the same convention). millet's extract_speaker_clip
    handles all of these formats via its ffmpeg fallback.
    """
    wavs = sorted(session_dir.glob("*.wav"))
    if wavs:
        return wavs[0]
    oggs = sorted(session_dir.glob("*.ogg"))
    if oggs:
        return oggs[0]
    mp3s = sorted(session_dir.glob("*.mp3"))
    if mp3s:
        return mp3s[0]
    return None


def _get_speakers(session_id: str):
    """Fetch SpeakerInfo list from millet for the given session."""
    from millet.label import get_speakers as meet_get_speakers
    return meet_get_speakers(_session_dir(session_id))


def _load_autoid_suggestions(session_dir: Path) -> dict[str, dict]:
    """Load voiceprint auto-id suggestions from the ``*.autoid.json`` sidecar.

    Returns a dict mapping speaker id -> {"name": str, "confidence": float},
    or an empty dict if no sidecar exists.  Written by millet's
    ``label --auto`` (keyed by the final transcript speaker id).
    """
    import json as _json

    for sidecar in sorted(session_dir.glob("*.autoid.json")):
        try:
            data = _json.loads(sidecar.read_text(encoding="utf-8"))
            sugg = data.get("suggestions")
            if isinstance(sugg, dict):
                return sugg
        except Exception:
            log.debug("could not read auto-id sidecar %s", sidecar, exc_info=True)
    return {}


def _enforce_team_visibility(
    row: dict,
    viewer_team_id: str,
    viewer_github: str | None = None,
    is_admin: bool = False,
) -> None:
    """Reject cross-team / other-user-personal access with a 404.

    Mirror of ``sessions.enforce_team_visibility``.  Duplicated here
    rather than imported to keep labels.py independent of sessions.py —
    both have the same security obligation; the duplication is
    intentional belt-and-suspenders.  v0.12.1: also hides other users'
    personal sessions (their audio clips / speaker data) from same-team
    members, exposing them only to the owner and global admins.
    """
    if row.get("team_id") != viewer_team_id:
        raise HTTPException(404, "session not found")
    if viewer_github is not None and row.get("personal") and not is_admin:
        if row.get("github") != viewer_github:
            raise HTTPException(404, "session not found")


@router.get(
    "/label/{session_id}/clip/{speaker_id}",
    dependencies=[Depends(ratelimit.limit_api)],
)
def label_clip(
    session_id: str,
    speaker_id: str,
    auth_triple: tuple = Depends(auth.require_team_context),
):
    """Return an audio clip for a speaker. Generates and caches on first hit."""
    github, team_id, is_admin = auth_triple
    if not _is_safe_clip_id(speaker_id):
        raise HTTPException(400, "invalid speaker id")

    row = queue.get(session_id)
    if not row:
        raise HTTPException(404, "session not found")
    _enforce_team_visibility(row, team_id, github, is_admin)

    sdir = _session_dir(session_id)
    if not sdir.exists():
        raise HTTPException(404, "session not found")

    cache_dir = _ensure_clips_dir(session_id)
    cached = cache_dir / _safe_clip_filename(speaker_id)
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
    """Apply labels for the JSON API POST, then queue the follow-up.

    v0.11.0: labels are applied via the ``millet label --apply-json``
    SUBPROCESS through the per-job HOME shim (millet-pipeline >= 0.13.0).
    Pre-0.11.0 this imported ``millet.label.apply_labels`` in-process and
    mutated ``os.environ["HOME"]`` around the call — racing every other
    thread in the server — and spawned an ad-hoc daemon thread for the
    follow-up, which could process a session concurrently with the
    background worker.

    The fast apply (find-and-replace relabel, no summary regen) runs
    synchronously so the HTTP response reflects the applied labels; the
    slow follow-up (voiceprint model inference 30-60 s + git sync) is
    queued onto the single worker thread (``finalize_labels`` task).

    v0.6.2+: ``team_id`` is required so the HOME shim points at the
    correct per-team voiceprint DB.
    """
    log.info(
        "session=%s team=%s labels=%s by=%s",
        session_id, team_id, label_map, github,
    )

    log_path = config.logs_dir() / f"{session_id}.log"
    # Serialize against the worker (finalize/sync/retry) and any other
    # concurrent label apply for THIS session — they share the per-session
    # HOME shim and label-map file (M-3).
    with meet_runner.session_shim_lock(session_id):
        rc = meet_runner.apply_labels_json(
            _session_dir(session_id), session_id, team_id, log_path,
            label_map=label_map,
            regenerate_summary=False,
        )
    if rc != 0:
        log.error(
            "session=%s: millet label --apply-json exited %s", session_id, rc,
        )
        raise HTTPException(
            502, f"applying labels failed (millet exited {rc}); see server log",
        )

    worker.enqueue_task(
        "finalize_labels", session_id, label_map=dict(label_map),
    )


_LABELABLE_STATUSES = ("needs_labeling", "done", "error", "sync_failed", "imported")


# ── JSON API (native clients) ───────────────────────────────────────────────


@router.get(
    "/api/team",
    dependencies=[Depends(ratelimit.limit_api)],
)
def api_team(auth_triple: tuple = Depends(auth.require_team_context)):
    """Return the team handles list (for autocomplete in native clients).

    v0.6.0+: reads ``~/vezir-data/teams/<caller-team>/roster.json``.
    The label dropdown is scoped to the caller's team so cross-team
    handles don't appear in autocomplete.
    """
    _github, team_id, _admin = auth_triple
    return {"team": _team_handles(team_id)}


# ── speaker segments (labeling aid) ─────────────────────────────────────────

_MAX_SEGMENTS = 200
_MAX_SEGMENT_CHARS = 240


@router.get(
    "/label/{session_id}/segments/{speaker_id}",
    dependencies=[Depends(ratelimit.limit_api)],
)
def label_segments(
    session_id: str,
    speaker_id: str,
    auth_triple: tuple = Depends(auth.require_team_context),
):
    """Return a speaker's transcript segments for labeling decisions.

    The main ``GET /api/label/<id>`` payload carries only one 120-char
    sample per speaker; this endpoint serves the full segment list on
    demand (TUI "More" button / Android label screen), so labelers can
    read everything a speaker said when the sample is not enough to
    identify them.
    """
    github, team_id, is_admin = auth_triple
    if not _is_safe_clip_id(speaker_id):
        raise HTTPException(400, "invalid speaker id")

    row = queue.get(session_id)
    if not row:
        raise HTTPException(404, "session not found")
    _enforce_team_visibility(row, team_id, github, is_admin)

    sdir = _session_dir(session_id)
    if not sdir.exists():
        raise HTTPException(404, "session not found")

    from millet.label import _find_session_files, _load_transcript

    files = _find_session_files(sdir)
    if "json" not in files:
        raise HTTPException(404, "transcript not found")
    transcript = _load_transcript(files["json"])

    segs = [
        {"start": s.start, "end": s.end,
         "text": (s.text or "").strip()[:_MAX_SEGMENT_CHARS]}
        for s in transcript.segments
        if s.speaker == speaker_id
    ]
    if not segs:
        raise HTTPException(404, f"speaker {speaker_id} not found in transcript")

    return {
        "speaker_id": speaker_id,
        "total": len(segs),
        "segments": segs[:_MAX_SEGMENTS],
    }



@router.get(
    "/api/label/{session_id}",
    dependencies=[Depends(ratelimit.limit_api)],
)
def api_label_get(
    session_id: str,
    auth_triple: tuple = Depends(auth.require_team_context),
):
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
    github, team_id, is_admin = auth_triple
    row = queue.get(session_id)
    if not row:
        raise HTTPException(404, "session not found")
    _enforce_team_visibility(row, team_id, github, is_admin)
    if row["status"] not in _LABELABLE_STATUSES:
        raise HTTPException(
            409,
            f"session status is '{row['status']}'; labeling requires "
            f"one of: {', '.join(_LABELABLE_STATUSES)}",
        )

    speakers = _get_speakers(session_id)
    sdir = _session_dir(session_id)
    audio_available = _find_wav(sdir) is not None
    suggestions = _load_autoid_suggestions(sdir)

    return {
        "session_id": session_id,
        "status": row["status"],
        "speakers": [
            {
                "id": sp.id,
                "channel": getattr(sp, "channel", None),
                "sample_text": getattr(sp, "sample_text", None),
                # Voiceprint auto-id suggestion (from *.autoid.json sidecar).
                # Lets native clients pre-fill recognized names + show
                # confidence.  None when no confident match was found.
                "suggested_name": (suggestions.get(sp.id) or {}).get("name"),
                "confidence": (suggestions.get(sp.id) or {}).get("confidence"),
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
    auth_triple: tuple = Depends(auth.require_team_context),
):
    """Apply labels from a JSON body (native clients).

    Expected body:
        {"labels": {"REMOTE_0": "kasita", "REMOTE_1": "alice"}}

    Empty or missing labels for a speaker keep the auto-assigned label.
    """
    github, team_id, is_admin = auth_triple
    row = queue.get(session_id)
    if not row:
        raise HTTPException(404, "session not found")
    _enforce_team_visibility(row, team_id, github, is_admin)
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
