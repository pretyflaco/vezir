"""Admin-only team CRUD endpoints.

Routes
------

GET    /admin/teams                 list teams (JSON)
POST   /admin/teams                 create a team
PATCH  /admin/teams/{team_id}       update name and/or sync settings
GET    /admin/teams/{team_id}       get one team

DELETE is intentionally not implemented in v0.6.0.  Deleting a team
with jobs assigned is a destructive operation that needs careful
handling (cascade rules, audit trail, cross-references).  The
``vezir session move`` admin command lands in v0.6.1 alongside team
deletion.

All routes use ``auth.require_admin`` which checks that the bearer
token has ``is_admin=true``.  Listing teams is gated to admin to keep
the team roster from leaking to scribe-tier tokens (which already
know their own team_id via /api/me, planned for v0.6.1).
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel

from . import auth, queue

log = logging.getLogger("vezir.teams")
router = APIRouter()


class _CreateTeamBody(BaseModel):
    id: str
    name: str
    sync_remote: str | None = None
    sync_meeting_type: str = "sandbox"


class _UpdateTeamBody(BaseModel):
    sync_remote: str | None = None
    sync_meeting_type: str | None = None


@router.get("/admin/teams")
def admin_list_teams(_github: str = Depends(auth.require_admin)):
    """List every team (admin only)."""
    return {"teams": queue.list_teams()}


@router.post("/admin/teams")
def admin_create_team(
    body: _CreateTeamBody = Body(...),
    _github: str = Depends(auth.require_admin),
):
    """Create a team (admin only).

    Returns 409 if the slug is taken; 400 if the slug doesn't match the
    naming rules (3-32 chars, lowercase + hyphen, starts with a letter).
    """
    try:
        queue.validate_team_id(body.id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    try:
        queue.create_team(
            body.id,
            body.name,
            sync_remote=body.sync_remote,
            sync_meeting_type=body.sync_meeting_type,
        )
    except ValueError as exc:
        # already exists
        raise HTTPException(409, str(exc))
    log.info("created team %r (%s)", body.id, body.name)
    return queue.get_team(body.id)


@router.get("/admin/teams/{team_id}")
def admin_get_team(
    team_id: str,
    _github: str = Depends(auth.require_admin),
):
    row = queue.get_team(team_id)
    if not row:
        raise HTTPException(404, f"team {team_id!r} not found")
    return row


@router.patch("/admin/teams/{team_id}")
def admin_update_team(
    team_id: str,
    body: _UpdateTeamBody = Body(...),
    _github: str = Depends(auth.require_admin),
):
    """Update sync remote and/or meeting type for an existing team.

    Pass ``sync_remote: null`` to clear (in JSON, ``null``).
    Omitted fields are left untouched.  Note: per Pydantic's defaults
    we can't distinguish "field omitted" from "field explicitly null"
    in this minimal model; v0.6.0 treats ``null`` as clear and absent
    keys (use ``exclude_unset``) as touch-not.  For now both behaviors
    write the value.
    """
    existing = queue.get_team(team_id)
    if not existing:
        raise HTTPException(404, f"team {team_id!r} not found")
    try:
        queue.update_team_sync(
            team_id,
            sync_remote=body.sync_remote,
            sync_meeting_type=(
                body.sync_meeting_type if body.sync_meeting_type
                else ...
            ),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    log.info("updated team %r sync settings", team_id)
    return queue.get_team(team_id)
