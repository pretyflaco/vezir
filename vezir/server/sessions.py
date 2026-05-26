"""Session metadata + dashboard endpoints.

GET  /                       → HTML dashboard (recent sessions)
GET  /s/<session-id>         → HTML session detail page
GET  /api/sessions           → JSON list (for clients)
GET  /api/sessions/<id>      → JSON session detail
GET  /artifact/<id>/<name>   → download a generated artifact
POST /session/<id>/sync      → retroactive sync of a local-only session
POST /api/exchange-code      → mint a short-lived login code for browser hand-off
"""
from __future__ import annotations

import json
import logging
import threading
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel

from .. import config
from . import auth, queue, ratelimit, web_sessions, worker
from .templating import templates

log = logging.getLogger("vezir.sessions")
router = APIRouter()


class _RetrySummaryBody(BaseModel):
    preset: str | None = None


_VALID_PRESETS = {"high-quality", "confidential", "alternative"}


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
    # Ensure summary_error / sync_error are present (may be absent in
    # old DB rows created before the columns were added).
    row.setdefault("summary_error", None)
    row.setdefault("sync_error", None)
    return row


def _enforce_team_visibility(row: dict, viewer_team_id: str) -> None:
    """Raise 404 if ``row`` belongs to a team the viewer isn't in.

    v0.6.0: every session-detail / session-fetch endpoint must call
    this with the caller's team_id (from the auth dependency).  Cross-
    team requests are returned as 404 (not 403) to avoid leaking the
    existence of a session that belongs to another team.
    """
    if row.get("team_id") != viewer_team_id:
        raise HTTPException(404, "session not found")


@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    auth_triple: tuple = Depends(auth.require_bearer_or_cookie_full),
):
    github, team_id, _admin = auth_triple
    rows = [
        _decorate(r)
        for r in queue.list_recent(
            limit=50, viewer_github=github, viewer_team_id=team_id,
        )
    ]
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"request": request, "rows": rows, "me": github, "team_id": team_id},
    )


@router.get("/s/{session_id}", response_class=HTMLResponse)
def session_detail(
    request: Request,
    session_id: str,
    auth_triple: tuple = Depends(auth.require_bearer_or_cookie_full),
):
    github, team_id, _admin = auth_triple
    row = queue.get(session_id)
    if not row:
        raise HTTPException(404, "session not found")
    _enforce_team_visibility(row, team_id)
    return templates.TemplateResponse(
        request,
        "session.html",
        {"request": request, "row": _decorate(row), "me": github,
         "team_id": team_id},
    )


@router.get("/api/sessions", dependencies=[Depends(ratelimit.limit_api)])
def api_sessions(
    limit: int = 50,
    since: str | None = None,
    auth_triple: tuple = Depends(auth.require_bearer_full),
):
    """Return recent sessions visible to the authenticated user.

    v0.6.0 visibility rule:
      * Team scope: only sessions in the caller's team.
      * Personal: all non-personal sessions in that team PLUS the
        caller's own personal sessions in that team.
      * Sessions in OTHER teams are entirely invisible.

    v0.7.0: optional ``since`` parameter (ISO 8601 date or datetime)
    filters to sessions created at or after that timestamp.  Enables
    efficient incremental ``vezir pull``.
    """
    github, team_id, _admin = auth_triple
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
    auth_triple: tuple = Depends(auth.require_bearer_full),
):
    _github, team_id, _admin = auth_triple
    row = queue.get(session_id)
    if not row:
        raise HTTPException(404, "session not found")
    _enforce_team_visibility(row, team_id)
    return _decorate(row)


@router.get("/artifact/{session_id}/{name}")
def artifact(
    session_id: str,
    name: str,
    auth_triple: tuple = Depends(auth.require_bearer_or_cookie_full),
):
    _github, team_id, _admin = auth_triple
    # Cross-team artifact downloads must be impossible, so check the
    # job row's team BEFORE touching the filesystem.
    row = queue.get(session_id)
    if not row:
        raise HTTPException(404, "session not found")
    _enforce_team_visibility(row, team_id)
    sdir = config.sessions_dir() / session_id
    if not sdir.exists():
        raise HTTPException(404, "session not found")
    # Path traversal protection: name must be a single filename
    if "/" in name or ".." in name:
        raise HTTPException(400, "invalid artifact name")
    p = sdir / name
    if not p.exists():
        raise HTTPException(404, "artifact not found")
    return FileResponse(p, filename=name)


@router.post("/session/{session_id}/sync")
def sync_now(
    session_id: str,
    request: Request,
    auth_triple: tuple = Depends(auth.require_bearer_or_cookie_full),
):
    """Retroactively sync a previously local-only session to git.

    Flow: the user uploaded a meeting with `sync=false` (or via an older
    workflow that never reached the sync step), the session sits at
    `done` with no git push, and the user now wants to publish it.
    Clicking "Sync now" in the dashboard hits this endpoint, which:

      1. Sets the queue row's `sync_enabled = 1`
      2. Runs `millet sync` via the existing worker.finalize_after_labeling
         flow (in a background thread, like the labeling submit handler)
      3. Redirects the browser back to /s/<id> where the page polls
         the status until it transitions through `syncing` -> `done`

    Refuses if the session is in a status that doesn't admit retroactive
    sync (e.g. `error`, `transcribing`, `needs_labeling`).  Re-syncing a
    session that already synced is allowed (force-push semantics inherited
    from `millet sync --force`).
    """
    github, team_id, _admin = auth_triple
    row = queue.get(session_id)
    if not row:
        raise HTTPException(404, "session not found")
    _enforce_team_visibility(row, team_id)
    if row["status"] not in ("done", "syncing"):
        raise HTTPException(
            409,
            f"session status '{row['status']}' does not admit retroactive sync; "
            "wait for transcription/labeling to complete first",
        )

    # Flip the per-job flag so the worker's gates allow sync this round.
    queue.set_sync_enabled(session_id, True)
    log.info("session=%s retroactive sync requested by %s", session_id, github)

    # Hand off to the same finalize path the labeling submit uses.  Runs
    # in a background thread so the HTTP response returns immediately.
    threading.Thread(
        target=worker.finalize_after_labeling,
        args=(session_id,),
        name=f"sync-now-{session_id}",
        daemon=True,
    ).start()

    # Browser POSTs from the dashboard form land here; redirect back to
    # the session page so the user sees the status transition.
    if "text/html" in (request.headers.get("accept") or ""):
        return RedirectResponse(url=f"/s/{session_id}", status_code=303)
    return {"session_id": session_id, "queued": True}


# ── retry summary ────────────────────────────────────────────────────────────


@router.post(
    "/api/sessions/{session_id}/retry-summary",
    dependencies=[Depends(ratelimit.limit_api)],
)
def retry_summary(
    session_id: str,
    body: _RetrySummaryBody | None = None,
    auth_triple: tuple = Depends(auth.require_bearer_full),
):
    github, team_id, _admin = auth_triple
    """Retry summary generation for a session whose summary previously failed.

    The session must be in ``done`` status with a non-empty ``summary_error``
    field (i.e. transcription succeeded but the summary step failed, typically
    due to a transient network/DNS error reaching the LLM backend).

    Runs the summary step in a background thread and returns immediately.
    The client can poll ``GET /api/sessions/{id}`` to observe the status
    transition: ``done`` -> ``transcribing`` -> ``done`` (with ``summary_error``
    cleared on success, or updated on repeat failure).
    """
    row = queue.get(session_id)
    if not row:
        raise HTTPException(404, "session not found")
    _enforce_team_visibility(row, team_id)
    if row["status"] != "done":
        raise HTTPException(
            409,
            f"session status '{row['status']}' does not admit summary retry; "
            "session must be in 'done' status",
        )
    if not row.get("summary_error"):
        raise HTTPException(
            409,
            "session has no summary_error; summary already succeeded",
        )

    preset_override = None
    if body and body.preset:
        if body.preset not in _VALID_PRESETS:
            raise HTTPException(400, f"invalid preset: {body.preset}")
        preset_override = body.preset

    log.info(
        "session=%s summary retry requested by %s", session_id, github,
    )

    threading.Thread(
        target=worker.retry_summary_for_session,
        args=(session_id,),
        kwargs={"preset_override": preset_override},
        name=f"retry-summary-{session_id}",
        daemon=True,
    ).start()

    return {"session_id": session_id, "queued": True}


# ── personal → team sharing ──────────────────────────────────────────────────


@router.post(
    "/api/sessions/{session_id}/share",
    dependencies=[Depends(ratelimit.limit_api)],
)
def share_with_team(
    session_id: str,
    auth_triple: tuple = Depends(auth.require_bearer_full),
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
    _enforce_team_visibility(row, team_id)
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


# ── /api/me (v0.6.1) ────────────────────────────────────────────────────────


@router.get("/api/me", dependencies=[Depends(ratelimit.limit_api)])
def api_me(auth_triple: tuple = Depends(auth.require_bearer_full)):
    """Return identity + team info for the calling token.

    Used by:
      * TUI title-bar display ("vezir — blink — sessions (N)").
      * TUI ^t team-switcher after switching tokens to confirm the
        new identity + display name.
      * Future ``vezir whoami`` CLI (not in v0.6.1).

    Response shape:

        {
          "github": "pretyflaco",
          "team_id": "blink",
          "team_name": "Blink",
          "is_admin": false
        }

    ``team_name`` is the human-friendly name from the ``teams`` table;
    falls back to the slug if the team row is somehow missing (shouldn't
    happen post-migration but defensive in case the row was deleted).
    """
    github, team_id, is_admin = auth_triple
    row = queue.get_team(team_id)
    team_name = row.get("name") if row else team_id
    return {
        "github": github,
        "team_id": team_id,
        "team_name": team_name or team_id,
        "is_admin": is_admin,
        "alternate_urls": config.alternate_urls(),
    }


# ── exchange-code minting ───────────────────────────────────────────────────


@router.post(
    "/api/exchange-code",
    dependencies=[Depends(ratelimit.limit_api)],
)
def mint_exchange_code(
    request: Request,
    next: str | None = None,
    auth_triple: tuple = Depends(auth.require_bearer_full),
):
    _github, _team, _admin = auth_triple  # team carried by bearer itself
    """Mint a one-time, 60-second exchange code for browser hand-off.

    The client calls this when it needs a login URL (e.g. to print a
    "Label speakers" link in the terminal). The returned ``login_url``
    contains ``?code=vzx_...`` — the bearer never enters the URL.

    Query parameter ``next`` is the page the browser should land on
    after consuming the code (e.g. ``/label/<session_id>``).
    """
    auth_header = request.headers.get("authorization", "")
    bearer = auth_header.split(None, 1)[1].strip() if " " in auth_header else ""
    code = web_sessions.mint_exchange_code(bearer)
    base = str(request.base_url).rstrip("/")
    safe_next = quote(next or "/", safe="")
    login_url = f"{base}/login?code={quote(code, safe='')}&next={safe_next}"
    return {"login_url": login_url}
