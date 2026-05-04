"""Session metadata + dashboard endpoints.

GET /                      → HTML dashboard (recent sessions)
GET /s/<session-id>        → HTML session detail page
GET /api/sessions          → JSON list (for clients)
GET /api/sessions/<id>     → JSON session detail
GET /artifact/<id>/<name>  → download a generated artifact
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse

from .. import config
from . import auth, queue
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


@router.get("/api/sessions")
def api_sessions(
    limit: int = 50,
    github: str = Depends(auth.require_bearer),
):
    return {"sessions": [_decorate(r) for r in queue.list_recent(limit=limit)]}


@router.get("/api/sessions/{session_id}")
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
