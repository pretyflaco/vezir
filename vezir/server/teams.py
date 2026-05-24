"""Admin-only team CRUD endpoints.

Routes
------

GET    /admin/teams                          list teams (JSON)
POST   /admin/teams                          create a team
PATCH  /admin/teams/{team_id}                update name and/or sync settings
GET    /admin/teams/{team_id}                get one team
DELETE /admin/teams/{team_id}[?reassign_to=] delete a team (v0.6.2+)

All routes use ``auth.require_admin`` which checks that the bearer
token has ``is_admin=true``.  Listing teams is gated to admin to keep
the team roster from leaking to scribe-tier tokens (which already
know their own team_id via /api/me, added in v0.6.1).

v0.6.2 additions:

* PATCH accepts a ``name`` field for display-name renames.  Slug
  renames remain forbidden — they would cascade across
  ``jobs.team_id``, the token store, and on-disk team dirs, and
  break in-flight web sessions.  Deferred to v0.7.0.
* DELETE cascades by default refuses if the team has any jobs or
  tokens.  Pass ``?reassign_to=<other_slug>`` to move jobs and
  revoke tokens in one call.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Body, Depends, HTTPException, Query
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
    name: str | None = None
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
    """Update name and/or sync settings for an existing team.

    v0.6.2+: ``name`` is accepted alongside ``sync_remote`` and
    ``sync_meeting_type``.  Pass ``sync_remote: null`` to clear (in
    JSON, ``null``).  Omitted fields are left untouched (uses
    ``model_fields_set`` so absent keys don't overwrite stored
    values with their default).
    """
    existing = queue.get_team(team_id)
    if not existing:
        raise HTTPException(404, f"team {team_id!r} not found")

    fields_set = body.model_fields_set if hasattr(body, "model_fields_set") else set(
        body.__fields_set__  # pydantic v1 fallback
    )

    try:
        if "name" in fields_set and body.name is not None:
            queue.update_team_name(team_id, body.name)

        # Build kwargs for update_team_sync from explicitly-set fields
        # so an omitted field doesn't accidentally clear a value.
        sync_kwargs: dict = {}
        if "sync_remote" in fields_set:
            sync_kwargs["sync_remote"] = body.sync_remote  # may be None to clear
        if "sync_meeting_type" in fields_set and body.sync_meeting_type:
            sync_kwargs["sync_meeting_type"] = body.sync_meeting_type
        if sync_kwargs:
            queue.update_team_sync(team_id, **sync_kwargs)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    log.info("updated team %r (fields: %s)", team_id, sorted(fields_set))
    return queue.get_team(team_id)


@router.delete("/admin/teams/{team_id}")
def admin_delete_team(
    team_id: str,
    reassign_to: str | None = Query(
        default=None,
        description=(
            "If set, jobs are reassigned to this team and tokens are "
            "revoked.  If omitted, the call refuses when the team has "
            "any jobs or tokens (operator must clean up first)."
        ),
    ),
    _github: str = Depends(auth.require_admin),
):
    """Delete a team.  Refuses if non-empty unless ``reassign_to`` is given.

    v0.6.2+.  Cascade policy: see ``queue.delete_team`` for details.
    Tokens scoped to the deleted team are REVOKED (not migrated) —
    the destination team's members are probably different humans, so
    moving tokens across would be a privilege escalation.

    Returns ``{"deleted": true, ...stats}``.
    """
    if queue.get_team(team_id) is None:
        raise HTTPException(404, f"team {team_id!r} not found")
    try:
        stats = queue.delete_team(team_id, reassign_to=reassign_to)
    except ValueError as exc:
        # 409: the team exists but the operation can't proceed without
        # explicit operator intent (jobs or tokens still scoped to it).
        raise HTTPException(409, str(exc))
    log.info(
        "deleted team %r (reassigned_to=%s, jobs=%s, tokens_revoked=%s)",
        team_id,
        stats.get("reassigned_to"),
        stats.get("jobs_reassigned"),
        stats.get("tokens_revoked"),
    )
    return {"deleted": True, **stats}
