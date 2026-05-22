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

from .. import config
from . import auth, queue, ratelimit, web_sessions, worker
from .templating import templates

log = logging.getLogger("vezir.sessions")
router = APIRouter()


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
    return row


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, github: str = Depends(auth.require_bearer_or_cookie)):
    rows = [_decorate(r) for r in queue.list_recent(limit=50)]
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"request": request, "rows": rows, "me": github},
    )


@router.get("/s/{session_id}", response_class=HTMLResponse)
def session_detail(
    request: Request,
    session_id: str,
    github: str = Depends(auth.require_bearer_or_cookie),
):
    row = queue.get(session_id)
    if not row:
        raise HTTPException(404, "session not found")
    return templates.TemplateResponse(
        request,
        "session.html",
        {"request": request, "row": _decorate(row), "me": github},
    )


@router.get("/api/sessions", dependencies=[Depends(ratelimit.limit_api)])
def api_sessions(
    limit: int = 50,
    github: str = Depends(auth.require_bearer),
):
    return {"sessions": [_decorate(r) for r in queue.list_recent(limit=limit)]}


@router.get(
    "/api/sessions/{session_id}",
    dependencies=[Depends(ratelimit.limit_api)],
)
def api_session(
    session_id: str,
    github: str = Depends(auth.require_bearer),
):
    row = queue.get(session_id)
    if not row:
        raise HTTPException(404, "session not found")
    return _decorate(row)


@router.get("/artifact/{session_id}/{name}")
def artifact(
    session_id: str,
    name: str,
    github: str = Depends(auth.require_bearer_or_cookie),
):
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
    github: str = Depends(auth.require_bearer_or_cookie),
):
    """Retroactively sync a previously local-only session to git.

    Flow: the user uploaded a meeting with `sync=false` (or via an older
    workflow that never reached the sync step), the session sits at
    `done` with no git push, and the user now wants to publish it.
    Clicking "Sync now" in the dashboard hits this endpoint, which:

      1. Sets the queue row's `sync_enabled = 1`
      2. Runs `meet sync` via the existing worker.finalize_after_labeling
         flow (in a background thread, like the labeling submit handler)
      3. Redirects the browser back to /s/<id> where the page polls
         the status until it transitions through `syncing` -> `done`

    Refuses if the session is in a status that doesn't admit retroactive
    sync (e.g. `error`, `transcribing`, `needs_labeling`).  Re-syncing a
    session that already synced is allowed (force-push semantics inherited
    from `meet sync --force`).
    """
    row = queue.get(session_id)
    if not row:
        raise HTTPException(404, "session not found")
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


# ── exchange-code minting ───────────────────────────────────────────────────


@router.post(
    "/api/exchange-code",
    dependencies=[Depends(ratelimit.limit_api)],
)
def mint_exchange_code(
    request: Request,
    next: str | None = None,
    github: str = Depends(auth.require_bearer),
):
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
