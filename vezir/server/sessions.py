"""Session metadata + API endpoints.

GET    /api/sessions           → JSON list (for clients)
GET    /api/sessions/<id>      → JSON session detail
DELETE /api/sessions/<id>      → remove a session (admin or original uploader)
GET    /artifact/<id>/<name>   → download a generated artifact
POST   /session/<id>/sync      → retroactive sync of a local-only session
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .. import config
from . import auth, meet_runner, queue, ratelimit, worker

log = logging.getLogger("vezir.sessions")
router = APIRouter()


class _RetrySummaryBody(BaseModel):
    preset: str | None = None
    # Optional summary-language override.  When set, the summary is regenerated
    # in this language and saved as an ADDITIONAL artifact (the primary
    # auto-detected summary is preserved).  "auto"/None = use the transcript's
    # own language (rewrites the primary summary).
    language: str | None = None


_VALID_PRESETS = {"high-quality", "confidential", "alternative"}
# Languages with localized section headers in millet (millet.languages).
# "auto" means "use the transcript's detected language".
_VALID_SUMMARY_LANGUAGES = {"auto", "en", "de", "fr", "es", "tr", "fa"}


def _decorate(row: dict) -> dict:
    """Add convenience fields used by the dashboard template."""
    artifacts = row.get("artifacts")
    if artifacts:
        try:
            row["artifacts_dict"] = json.loads(artifacts)
        except Exception:
            row["artifacts_dict"] = {}
    else:
        row["artifacts_dict"] = {}
    # Ensure summary_error / sync_error / summary_fallback are present
    # (may be absent in old DB rows created before the columns were added).
    row.setdefault("summary_error", None)
    row.setdefault("sync_error", None)
    row.setdefault("summary_fallback", None)
    return row


def enforce_team_visibility(
    row: dict,
    viewer_team_id: str,
    viewer_github: str | None = None,
    is_admin: bool = False,
) -> None:
    """Raise 404 if ``row`` is not visible to the viewer.

    v0.6.0: every session-detail / session-fetch endpoint must call
    this with the caller's team_id (from the auth dependency).  Cross-
    team requests are returned as 404 (not 403) to avoid leaking the
    existence of a session that belongs to another team.

    v0.12.1: also enforces the ``personal`` flag WHEN ``viewer_github`` is
    supplied.  ``queue.list_recent`` hides other users' personal sessions
    from the listing, but the per-session endpoints only checked
    ``team_id`` — so any same-team member who learned a personal session's
    ULID could read its transcript/audio and even force-sync it to the
    shared repo.  A personal session is now visible only to its owner
    (``github``) and to global admins; everyone else gets a 404
    (existence-hiding, same as the cross-team case).  Callers that legit-
    imately operate on personal sessions regardless of owner (e.g. the
    ``share`` transition, which gates on the uploader itself) omit
    ``viewer_github`` to opt out of the personal check.
    """
    if row.get("team_id") != viewer_team_id:
        raise HTTPException(404, "session not found")
    if viewer_github is not None and row.get("personal") and not is_admin:
        if row.get("github") != viewer_github:
            raise HTTPException(404, "session not found")


@router.get("/api/sessions", dependencies=[Depends(ratelimit.limit_api)])
def api_sessions(
    limit: int = 50,
    since: str | None = None,
    auth_triple: tuple = Depends(auth.require_team_context),
):
    """Return recent sessions visible to the authenticated user.

    v0.7.0 visibility rule:
      * Team scope: only sessions in the team named by ``X-Team-Id``,
        which the caller must be a member of (validated by the auth
        dependency).
      * Personal: all non-personal sessions in that team PLUS the
        caller's own personal sessions in that team.
      * Sessions in OTHER teams are entirely invisible.

    v0.7.0: optional ``since`` parameter (ISO 8601 date or datetime)
    filters to sessions created at or after that timestamp.  Enables
    efficient incremental ``vezir pull``.
    """
    github, team_id, _admin = auth_triple
    # Clamp limit: a negative value becomes SQLite ``LIMIT -1`` (= no limit,
    # returns every row); an absurd value is a cheap DoS.  Bound to 1..500
    # (L-2).
    limit = max(1, min(int(limit), 500))
    return {
        "sessions": [
            _decorate(r)
            for r in queue.list_recent(
                limit=limit,
                viewer_github=github,
                viewer_team_id=team_id,
                since=since,
            )
        ],
    }


@router.get(
    "/api/sessions/{session_id}",
    dependencies=[Depends(ratelimit.limit_api)],
)
def api_session(
    session_id: str,
    auth_triple: tuple = Depends(auth.require_team_context),
):
    github, team_id, is_admin = auth_triple
    row = queue.get(session_id)
    if not row:
        raise HTTPException(404, "session not found")
    enforce_team_visibility(row, team_id, github, is_admin)
    return _decorate(row)


@router.get(
    "/artifact/{session_id}/{name}",
    dependencies=[Depends(ratelimit.limit_api)],
)
def artifact(
    session_id: str,
    name: str,
    auth_triple: tuple = Depends(auth.require_team_context),
):
    github, team_id, is_admin = auth_triple
    # Cross-team artifact downloads must be impossible, so check the
    # job row's team BEFORE touching the filesystem.
    row = queue.get(session_id)
    if not row:
        raise HTTPException(404, "session not found")
    enforce_team_visibility(row, team_id, github, is_admin)
    sdir = config.sessions_dir() / session_id
    if not sdir.exists():
        raise HTTPException(404, "session not found")
    # Path traversal protection: name must be a single filename
    if "/" in name or ".." in name:
        raise HTTPException(400, "invalid artifact name")
    p = sdir / name
    if not p.exists():
        raise HTTPException(404, "artifact not found")
    # Friendly download name (v0.14.1): YYYYMMDD_<title_slug>.<ext> so
    # browser/Android downloads don't get the ULID-based stored name.
    # Best effort: fall back to today's date when created_at is unusable.
    try:
        clean = str(row.get("created_at") or "").replace("Z", "+00:00")
        date_str = datetime.fromisoformat(clean).astimezone().strftime("%Y%m%d")
    except (ValueError, TypeError):
        date_str = datetime.now().strftime("%Y%m%d")
    friendly = config.artifact_friendly_name(name, date_str, row.get("title"))
    return FileResponse(p, filename=friendly)


class _SyncBody(BaseModel):
    # Optional explicit folder override ("sync as").  When set, schedule/title
    # auto-detection is skipped and the session is force-synced into
    # meetings/<date>_<slug>/.  Slugified server-side for path safety.
    meeting_type: str | None = None


@router.post(
    "/session/{session_id}/sync",
    dependencies=[Depends(ratelimit.limit_api)],
)
def sync_now(
    session_id: str,
    body: _SyncBody | None = Body(default=None),
    auth_triple: tuple = Depends(auth.require_team_context),
):
    """Retroactively sync a previously local-only session to git.

    Flow: the user uploaded a meeting with `sync=false` (or via an older
    workflow that never reached the sync step), the session sits at
    `done` with no git push, and the user now wants to publish it.

      1. Sets the queue row's `sync_enabled = 1`
      2. Queues a `sync` task onto the single background worker (v0.11.0:
         serialized with the pipeline — no more ad-hoc thread racing the
         worker; a duplicate request while one is pending is dropped)

    Optional JSON body ``{"meeting_type": "<slug>"}`` forces the target
    folder (the "sync as" override).  It is validated/slugified server-side;
    a value that slugifies to empty is rejected (422).

    Refuses if the session is in a status that doesn't admit retroactive
    sync (e.g. `error`, `transcribing`, `needs_labeling`).  Re-syncing a
    session that already synced is allowed (force-push semantics inherited
    from `millet sync --force`).
    """
    github, team_id, is_admin = auth_triple
    row = queue.get(session_id)
    if not row:
        raise HTTPException(404, "session not found")
    enforce_team_visibility(row, team_id, github, is_admin)
    if row["status"] not in ("done", "syncing", "sync_failed"):
        raise HTTPException(
            409,
            f"session status '{row['status']}' does not admit retroactive sync; "
            "wait for transcription/labeling to complete first",
        )
    # No team-scoped git remote → there is nothing to sync to.  Fail fast with
    # a clear message instead of queueing a job that would clone millet's
    # placeholder remote and land in sync_failed (0.8.10).
    if not meet_runner.team_has_sync_target(row.get("team_id") or team_id):
        raise HTTPException(
            409,
            "this team has no git sync remote configured; an admin can set one "
            "with `vezir team set-sync --team <slug> --remote <git-url>`",
        )

    meeting_type: str | None = None
    if body is not None and body.meeting_type is not None:
        raw = body.meeting_type.strip()
        if raw:
            meeting_type = config.sync_slug(raw)
            if not meeting_type:
                raise HTTPException(
                    422,
                    f"meeting_type '{raw}' is not a valid folder name",
                )

    queue.set_sync_enabled(session_id, True)
    log.info(
        "session=%s retroactive sync requested by %s (meeting_type=%s)",
        session_id, github, meeting_type,
    )

    queued = worker.enqueue_task("sync", session_id, meeting_type=meeting_type)
    if not queued:
        log.info("session=%s sync already pending; duplicate dropped", session_id)

    return {
        "session_id": session_id,
        # Report the ACTUAL enqueue result: a duplicate request while one is
        # already pending is dropped (deduped), not queued again (L-7).
        "queued": queued,
        "meeting_type": meeting_type,
    }


# ── retry summary ────────────────────────────────────────────────────────────


@router.post(
    "/api/sessions/{session_id}/retry-summary",
    dependencies=[Depends(ratelimit.limit_api)],
)
def retry_summary(
    session_id: str,
    body: _RetrySummaryBody | None = None,
    auth_triple: tuple = Depends(auth.require_team_context),
):
    """Retry summary generation for a session whose summary previously failed.

    The session must be in ``done`` status with a non-empty ``summary_error``
    field (i.e. transcription succeeded but the summary step failed, typically
    due to a transient network/DNS error reaching the LLM backend).

    Queues the summary step onto the background worker and returns
    immediately.  The client can poll ``GET /api/sessions/{id}`` to observe
    the status transition: ``done`` -> ``summarizing`` -> ``done`` (with
    ``summary_error`` cleared on success, or updated on repeat failure).
    """
    github, team_id, is_admin = auth_triple
    row = queue.get(session_id)
    if not row:
        raise HTTPException(404, "session not found")
    enforce_team_visibility(row, team_id, github, is_admin)
    if row["status"] not in ("done", "sync_failed"):
        raise HTTPException(
            409,
            f"session status '{row['status']}' does not admit summary retry; "
            "session must be in 'done' or 'sync_failed' status",
        )

    # Validate optional language override.
    language_override: str | None = None
    if body and body.language:
        if body.language not in _VALID_SUMMARY_LANGUAGES:
            raise HTTPException(400, f"invalid language: {body.language}")
        if body.language != "auto":
            language_override = body.language

    # The summary_error guard only applies to a plain retry (fixing a failed
    # summary).  A language override is an intentional re-summary of a
    # *successful* session in another language, so we allow it even when the
    # summary already succeeded.
    if not language_override and not row.get("summary_error"):
        raise HTTPException(
            409,
            "session has no summary_error; summary already succeeded "
            "(pass a 'language' to generate an additional-language summary)",
        )

    preset_override = None
    if body and body.preset:
        if body.preset not in _VALID_PRESETS:
            raise HTTPException(400, f"invalid preset: {body.preset}")
        preset_override = body.preset

    log.info(
        "session=%s summary retry requested by %s (language=%s)",
        session_id, github, language_override or "auto",
    )

    queued = worker.enqueue_task(
        "retry_summary", session_id,
        preset_override=preset_override,
        language_override=language_override,
    )
    if not queued:
        log.info(
            "session=%s retry-summary already pending; duplicate dropped",
            session_id,
        )

    return {"session_id": session_id, "queued": True}


# ── auto-label (voiceprint re-run on a transcribed session) ─────────────────


class _AutoLabelBody(BaseModel):
    # When True and auto-labeling fully resolves every speaker, sync the
    # session to the team git repo exactly like the main pipeline.
    sync: bool = False


@router.post(
    "/api/sessions/{session_id}/auto-label",
    dependencies=[Depends(ratelimit.limit_api)],
)
def auto_label(
    session_id: str,
    body: _AutoLabelBody | None = Body(default=None),
    auth_triple: tuple = Depends(auth.require_team_context),
):
    """Re-run voiceprint auto-labeling for an already-transcribed session.

    Use case: the session was processed while its team's voiceprint DB was
    empty/sparse (or auto-label was off at upload), so speakers landed as raw
    placeholders.  Re-runs ``millet label --auto`` against the per-team DB on
    the background worker; confident matches are applied and artifacts are
    updated.  Speakers the DB can't recognize stay raw.

    Explicit user consent: this endpoint always runs with ``force=True``,
    overriding an upload-time ``auto_label_enabled=0`` opt-out — clicking
    the button IS the consent.

    Returns immediately; poll ``GET /api/sessions/{id}`` to observe the
    status transition (``needs_labeling`` -> ``needs_labeling`` partial /
    ``syncing`` / ``done``).
    """
    github, team_id, is_admin = auth_triple
    row = queue.get(session_id)
    if not row:
        raise HTTPException(404, "session not found")
    enforce_team_visibility(row, team_id, github, is_admin)
    if row["status"] not in ("needs_labeling", "done", "error", "sync_failed"):
        raise HTTPException(
            409,
            f"session status '{row['status']}' does not admit auto-labeling; "
            "session must be in 'needs_labeling', 'done', 'error' or "
            "'sync_failed' status",
        )

    log.info(
        "session=%s auto-label requested by %s (sync=%s)",
        session_id, github, bool(body and body.sync),
    )

    queued = worker.enqueue_task(
        "auto_label", session_id,
        sync=bool(body and body.sync),
        force=True,
    )
    if not queued:
        log.info(
            "session=%s auto-label already pending; duplicate dropped",
            session_id,
        )

    return {"session_id": session_id, "queued": queued}


# ── personal → team sharing ──────────────────────────────────────────────────


@router.post(
    "/api/sessions/{session_id}/share",
    dependencies=[Depends(ratelimit.limit_api)],
)
def share_with_team(
    session_id: str,
    auth_triple: tuple = Depends(auth.require_team_context),
):
    """Un-personal a session so it becomes visible to the whole team.

    Only the original uploader can share their own personal sessions.
    Sets ``personal=0``; does NOT automatically enable sync — the user
    can then click "Sync now" separately if they also want the artifacts
    pushed to git.

    v0.6.0: the team semantic of "share" is unchanged — sessions remain
    in their own team after sharing; "share" only flips the in-team
    personal flag.  Cross-team sharing is intentionally not supported
    (decision recorded in vezir_plan.md v0.6.0 design).
    """
    github, team_id, _admin = auth_triple
    row = queue.get(session_id)
    if not row:
        raise HTTPException(404, "session not found")
    # NB: no personal-visibility enforcement here — `share` IS the
    # owner-controlled transition out of personal, and it already gates on
    # the uploader below (403).  Only the cross-team check applies.
    enforce_team_visibility(row, team_id)
    if row.get("github") != github:
        raise HTTPException(
            403,
            "only the original uploader can share a personal session",
        )
    if not row.get("personal"):
        return {"ok": True, "session_id": session_id, "already_shared": True}

    queue.set_personal(session_id, False)
    log.info("session=%s shared with team by %s", session_id, github)
    return {"ok": True, "session_id": session_id}


# ── POST /api/sessions/{id}/title (v0.12.0) ──────────────────────────────────


class _SetTitleBody(BaseModel):
    # None / empty / blank clears the title (falls back to the id in
    # every display surface).
    title: str | None = None


@router.post(
    "/api/sessions/{session_id}/title",
    dependencies=[Depends(ratelimit.limit_api)],
)
def set_session_title(
    session_id: str,
    body: _SetTitleBody | None = None,
    auth_triple: tuple = Depends(auth.require_team_context),
):
    """Add or change a session's title after it was created.

    Scribes sometimes forget to name a session at record time; this lets
    them fix it afterwards.  Authorization mirrors delete: the server-wide
    admin OR the original uploader (cross-team → 404, other member → 403).

    The title is NOT baked into the transcript/summary/PDF, so no artifact
    regeneration is needed.  It DOES drive millet's sync folder name /
    schedule matching, which read the title fresh at sync time — so a new
    title takes effect on the next sync.  If the session was already
    synced, the pushed git folder is not renamed retroactively; the
    response carries a ``warning`` to that effect.
    """
    github, team_id, is_admin = auth_triple
    row = queue.get(session_id)
    if not row:
        raise HTTPException(404, "session not found")
    enforce_team_visibility(row, team_id, github, is_admin)
    if not is_admin and row.get("github") != github:
        raise HTTPException(
            403,
            "only an admin or the original uploader can retitle this session",
        )

    new_title = ((body.title if body else None) or "").strip() or None
    queue.set_title(session_id, new_title)
    log.info(
        "session=%s retitled by %s (admin=%s, team=%s)",
        session_id, github, is_admin, team_id,
    )

    warning = None
    if queue.was_synced(row):
        warning = (
            "this session was already synced to the team git repo; the "
            "pushed folder is not renamed automatically — run 'Sync now' "
            "again to repropagate the new title."
        )
    return {"ok": True, "session_id": session_id, "title": new_title, "warning": warning}


# ── DELETE /api/sessions/{id} (v0.8.12) ──────────────────────────────────────


@router.delete(
    "/api/sessions/{session_id}",
    dependencies=[Depends(ratelimit.limit_api)],
)
def delete_session(
    session_id: str,
    auth_triple: tuple = Depends(auth.require_team_context),
):
    """Remove a session from a team. Hard delete: DB row + on-disk artifacts.

    Authorization: the server-wide admin (token ``is_admin`` bit) OR the
    original uploader of the session.  A non-admin, non-uploader member of
    the same team gets 403; a caller from a different team gets 404 (the
    same existence-hiding convention as ``enforce_team_visibility``).

    This is local-only.  If the session was already synced to the team's git
    repo, that pushed copy is NOT removed (millet sync is push-only); the
    response carries a ``warning`` to that effect.
    """
    github, team_id, is_admin = auth_triple
    row = queue.get(session_id)
    if not row:
        raise HTTPException(404, "session not found")
    enforce_team_visibility(row, team_id, github, is_admin)
    if not is_admin and row.get("github") != github:
        raise HTTPException(
            403,
            "only an admin or the original uploader can delete this session",
        )

    stats = queue.delete_session(session_id)
    log.info(
        "session=%s deleted by %s (admin=%s, team=%s)",
        session_id, github, is_admin, team_id,
    )

    warning = None
    if stats.get("was_synced"):
        warning = (
            "this session was synced to the team git repo; the local copy "
            "is removed but the pushed copy remains — remove it from the "
            "repo manually if needed."
        )
    return {"ok": True, "session_id": session_id, "warning": warning}


# ── /api/me (v0.6.1) ────────────────────────────────────────────────────────


@router.get("/api/me", dependencies=[Depends(ratelimit.limit_api)])
def api_me(auth_pair: tuple = Depends(auth.require_bearer)):
    """Return identity + team memberships for the calling token.

    v0.7.0: the response now lists every team the user is a member of
    (each with role + team_name) instead of a single team_id baked
    into the token.  Clients use this to populate their team-picker
    UI and to remember which team to send in ``X-Team-Id``.

    v0.7.4: ``team_id`` is the team's stable uuid (what the client
    sends back in ``X-Team-Id``); ``slug`` is the mutable display
    identifier.  A slug rename does not change ``team_id``, so clients
    keyed on it survive renames transparently.

    Response shape:

        {
          "github": "pretyflaco",
          "is_admin": false,
          "memberships": [
            {"team_id": "<uuid>", "slug": "blink", "team_name": "Blink", "role": "scribe"},
            {"team_id": "<uuid>", "slug": "twentyone", "team_name": "Twentyone", "role": "admin"}
          ],
          "alternate_urls": [...]
        }

    Used by:
      * TUI title-bar display.
      * TUI team-switcher after switching tokens to confirm identity.
      * ``vezir doctor`` end-to-end token validation.
      * ``vezir whoami`` CLI.
    """
    github, is_admin = auth_pair
    return {
        "github": github,
        "is_admin": is_admin,
        "memberships": queue.get_memberships(github),
        "alternate_urls": config.alternate_urls(),
    }

