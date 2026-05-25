"""SQLite-backed job queue + team registry.

Single-writer, single-worker model. Serialized job execution per the MVP
plan.

Tables
------

``jobs``: one row per uploaded session.
    id                   ULID, primary key, also the session id
    github               GitHub handle of the scribe who uploaded
    team_id              Slug of the team this session belongs to.  Added
                         in v0.6.0.  All visibility filtering is anchored
                         here.  See :func:`list_recent`.
    title                Optional meeting title
    summary_preset       Optional preset id (high-quality | confidential | alternative)
    auto_label_enabled   0/1.  When 0, worker skips `millet label --auto` and
                         routes the session straight to needs_labeling for
                         human-only labeling.  Default 1.
    sync_enabled         0/1.  When 0, worker skips `millet sync` after the
                         pipeline completes; session goes to `done` with
                         no git push.  Default 1.  Operator-side env var
                         VEZIR_SKIP_SYNC overrides to 0 globally.
    personal             0/1.  When 1, the session is hidden from other team
                         members' session lists (only the uploader sees it).
                         Personal sessions force sync_enabled=0 server-side.
                         Default 0.
    status               one of: queued, transcribing, summarizing,
                         needs_labeling, syncing, done, error
    created_at           ISO timestamp
    updated_at           ISO timestamp
    error                Last error message, if any
    summary_error        Summary-specific failure message. When transcription
                         succeeds but summary generation fails, this field
                         stores the failure message and the job proceeds to
                         done (transcript artifacts are still usable). The
                         user can retry summary generation later.
    sync_error           Sync-specific failure message. When the pipeline
                         completes but `millet sync` fails (e.g. DNS, git auth),
                         this field stores the failure and the job proceeds to
                         done.  The user can retry via "Sync now".
    artifacts            JSON-encoded dict of artifact paths (relative to session
                         dir): txt, srt, json, summary, pdf

``teams``: one row per team.  Added in v0.6.0.
    id                   Slug, primary key.  3-32 chars, [a-z0-9-], immutable.
    name                 Human display name.
    sync_remote          Git URL for this team's sync target.  Reserved schema
                         slot in v0.6.0; per-team sync wiring lands in v0.6.1
                         (worker still uses global VEZIR_SYNC* env vars until
                         then).
    sync_meeting_type    Meeting-type prefix used by ``millet sync --meeting-type``.
                         Defaults to 'sandbox' to match pre-0.6.0 behavior.
    created_at           ISO timestamp.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Iterator

from .. import config

_LOCK = threading.Lock()


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id                  TEXT PRIMARY KEY,
    github              TEXT NOT NULL,
    title               TEXT,
    summary_preset      TEXT,
    auto_label_enabled  INTEGER NOT NULL DEFAULT 1,
    sync_enabled        INTEGER NOT NULL DEFAULT 1,
    status              TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    error               TEXT,
    summary_error       TEXT,
    sync_error          TEXT,
    artifacts           TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC);

CREATE TABLE IF NOT EXISTS teams (
    id                  TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    sync_remote         TEXT,
    sync_meeting_type   TEXT NOT NULL DEFAULT 'sandbox',
    created_at          TEXT NOT NULL
);
"""


VALID_STATUSES = {
    "queued",
    "transcribing",
    "summarizing",
    "needs_labeling",
    "syncing",
    "done",
    "error",
}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    """Get a connection to the queue DB. Thread-safe via a global lock."""
    config.ensure_dirs()
    db_path = config.queue_db_path()
    with _LOCK:
        conn = sqlite3.connect(str(db_path))
        config.secure_chmod_file(db_path)
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript(SCHEMA)
            # Idempotent column-add migrations for existing DBs predating
            # each column.  Each is wrapped in its own try because the
            # second add must still run if the first is a duplicate.
            #
            # NOTE on team_id: the column is added with a placeholder
            # default '' so the ALTER succeeds against any existing row
            # set.  The 0.6.0 data migration (server.migrations) then
            # backfills real team_id values immediately after schema
            # bring-up.  Code that queries jobs without filtering by
            # team_id (e.g. the worker's claim_next loop) is unaffected;
            # only visibility-relevant call sites check team_id.
            for ddl in (
                "ALTER TABLE jobs ADD COLUMN summary_preset TEXT",
                "ALTER TABLE jobs ADD COLUMN auto_label_enabled INTEGER NOT NULL DEFAULT 1",
                "ALTER TABLE jobs ADD COLUMN sync_enabled INTEGER NOT NULL DEFAULT 1",
                "ALTER TABLE jobs ADD COLUMN personal INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE jobs ADD COLUMN summary_error TEXT",
                "ALTER TABLE jobs ADD COLUMN sync_error TEXT",
                "ALTER TABLE jobs ADD COLUMN team_id TEXT NOT NULL DEFAULT ''",
            ):
                try:
                    conn.execute(ddl)
                    conn.commit()
                except sqlite3.OperationalError:
                    pass
            try:
                conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_team ON jobs(team_id)")
                conn.commit()
            except sqlite3.OperationalError:
                pass
            yield conn
            conn.commit()
        finally:
            conn.close()


def enqueue(
    job_id: str,
    github: str,
    title: str | None = None,
    *,
    team_id: str,
    summary_preset: str | None = None,
    auto_label_enabled: bool = True,
    sync_enabled: bool = True,
    personal: bool = False,
) -> None:
    """Add a new job in `queued` state.

    Booleans are stored as 0/1 (SQLite convention).  Callers may pass
    None implicitly via missing form fields — those land here as True by
    default, preserving the pre-opt-out worker behavior.

    ``personal=True`` forces ``sync_enabled=False`` server-side: personal
    recordings should not be pushed to the shared git repo.

    ``team_id`` was added in v0.6.0 and is required.  It MUST be derived
    server-side from the uploader's bearer token (see
    ``auth._resolve_auth``); clients never supply it directly.
    """
    if not team_id:
        raise ValueError("enqueue requires team_id (added in v0.6.0)")
    if personal:
        sync_enabled = False
    with _conn() as c:
        c.execute(
            "INSERT INTO jobs (id, github, team_id, title, summary_preset, "
            "auto_label_enabled, sync_enabled, personal, status, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)",
            (
                job_id, github, team_id, title, summary_preset,
                1 if auto_label_enabled else 0,
                1 if sync_enabled else 0,
                1 if personal else 0,
                _now(), _now(),
            ),
        )


def claim_next() -> dict | None:
    """Atomically claim the oldest queued job and mark it transcribing.

    Returns the claimed row as a dict, or None if no work.
    """
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM jobs WHERE status = 'queued' "
            "ORDER BY created_at ASC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        c.execute(
            "UPDATE jobs SET status = 'transcribing', updated_at = ? WHERE id = ?",
            (_now(), row["id"]),
        )
        out = dict(row)
        out["status"] = "transcribing"
        return out


def update_status(
    job_id: str,
    status: str,
    error: str | None = None,
    artifacts: dict | None = None,
    summary_error: str | None = ...,
    sync_error: str | None = ...,
) -> None:
    """Update a job's status (and optionally error / artifacts / summary_error / sync_error).

    ``summary_error`` and ``sync_error`` use a sentinel default (``...``)
    so callers can distinguish "don't touch" from "clear it to None".
    Pass ``None`` explicitly to clear a previous error (e.g. after a
    successful retry).
    """
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid status: {status}")
    with _conn() as c:
        # Build the SET clause dynamically so we only touch columns the
        # caller intends to change.
        sets = ["status = ?", "updated_at = ?", "error = ?"]
        params: list = [status, _now(), error]
        if artifacts is not None:
            sets.append("artifacts = ?")
            params.append(json.dumps(artifacts))
        if summary_error is not ...:
            sets.append("summary_error = ?")
            params.append(summary_error)
        if sync_error is not ...:
            sets.append("sync_error = ?")
            params.append(sync_error)
        params.append(job_id)
        c.execute(
            f"UPDATE jobs SET {', '.join(sets)} WHERE id = ?",
            params,
        )


def get(job_id: str) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None


def set_sync_enabled(job_id: str, enabled: bool) -> None:
    """Flip the sync_enabled flag on an existing job.

    Used by the retroactive POST /session/<id>/sync endpoint to opt a
    previously local-only session back in to git sync.
    """
    with _conn() as c:
        c.execute(
            "UPDATE jobs SET sync_enabled = ?, updated_at = ? WHERE id = ?",
            (1 if enabled else 0, _now(), job_id),
        )


def set_personal(job_id: str, personal: bool) -> None:
    """Flip the personal flag on an existing job.

    Used by ``POST /api/sessions/{id}/share`` to un-personal a session
    ("Share with team").
    """
    with _conn() as c:
        c.execute(
            "UPDATE jobs SET personal = ?, updated_at = ? WHERE id = ?",
            (1 if personal else 0, _now(), job_id),
        )


def list_recent(
    limit: int = 50,
    github: str | None = None,
    viewer_github: str | None = None,
    viewer_team_id: str | None = None,
    team_id: str | None = None,
    since: str | None = None,
) -> list[dict]:
    """Return recent jobs, filtered for visibility.

    Team scoping (v0.6.0+): the visibility filter ALWAYS requires a team
    discriminator when called from a request-handling context.  Cross-team
    leakage is the central security property of multi-team support, so
    every caller must supply one of:

    * ``viewer_team_id`` — return jobs from this team only, AND apply the
      personal-visibility rule (non-personal OR own-personal).  This is
      the filter used by ``/api/sessions``.  When set, ``viewer_github``
      is required too (so the personal-visibility OR-clause has a handle
      to match).
    * ``team_id`` — return all jobs in this team regardless of personal
      flag.  Used by admin-only views and the background worker (e.g.
      ``claim_next`` doesn't go through this function but is independently
      team-blind because the worker processes ALL queued jobs).

    Back-compat shims:

    * ``viewer_github`` (without ``viewer_team_id``): rejected with
      ``ValueError`` in v0.6.0+.  Older calls must add ``viewer_team_id``.
    * ``github`` (without team scoping): rejected with ``ValueError`` in
      v0.6.0+ to prevent accidental cross-team listings.  Admin callers
      that genuinely want a global view across teams can pass
      ``team_id=None, github=None, _allow_all=True`` (not yet plumbed —
      the only current global caller is the worker, which uses
      ``claim_next``, not ``list_recent``).
    """
    # Tight v0.6.0 contract: a caller must say which team they want to
    # see.  Catching ambiguous calls early prevents silent cross-team
    # leakage from a refactor that forgets to pass team_id.
    if viewer_github and not viewer_team_id:
        raise ValueError(
            "list_recent(viewer_github=...) now also requires "
            "viewer_team_id (v0.6.0+ team isolation)"
        )
    if github and not (team_id or viewer_team_id):
        raise ValueError(
            "list_recent(github=...) now also requires team_id "
            "or viewer_team_id (v0.6.0+ team isolation)"
        )

    # v0.7.0: optional ``since`` filter for incremental ``vezir pull``.
    since_clause = ""
    since_params: tuple = ()
    if since:
        since_clause = " AND created_at >= ?"
        since_params = (since,)

    with _conn() as c:
        if viewer_team_id and viewer_github:
            rows = c.execute(
                "SELECT * FROM jobs "
                "WHERE team_id = ? AND (personal = 0 OR github = ?)"
                f"{since_clause} "
                "ORDER BY created_at DESC LIMIT ?",
                (viewer_team_id, viewer_github, *since_params, limit),
            ).fetchall()
        elif team_id and github:
            rows = c.execute(
                "SELECT * FROM jobs WHERE team_id = ? AND github = ?"
                f"{since_clause} "
                "ORDER BY created_at DESC LIMIT ?",
                (team_id, github, *since_params, limit),
            ).fetchall()
        elif team_id:
            rows = c.execute(
                "SELECT * FROM jobs WHERE team_id = ?"
                f"{since_clause} "
                "ORDER BY created_at DESC LIMIT ?",
                (team_id, *since_params, limit),
            ).fetchall()
        else:
            # No team scope at all — only reachable from internal callers
            # (tests, admin tools) that explicitly want a global view.
            where = "WHERE created_at >= ?" if since else ""
            rows = c.execute(
                f"SELECT * FROM jobs {where} "
                "ORDER BY created_at DESC LIMIT ?",
                (*since_params, limit),
            ).fetchall()
        return [dict(r) for r in rows]


# ── teams CRUD ───────────────────────────────────────────────────────────────


_TEAM_ID_RE = None  # lazy-compiled in validate_team_id


def validate_team_id(team_id: str) -> None:
    """Raise ``ValueError`` if ``team_id`` doesn't match the slug shape.

    Constraints (per the v0.6.0 design): 3-32 chars, lowercase
    alphanumeric and hyphen, must start with a letter.
    """
    global _TEAM_ID_RE
    if _TEAM_ID_RE is None:
        import re as _re
        _TEAM_ID_RE = _re.compile(r"^[a-z][a-z0-9-]{2,31}$")
    if not isinstance(team_id, str) or not _TEAM_ID_RE.match(team_id):
        raise ValueError(
            f"invalid team_id {team_id!r}: must be 3-32 chars, lowercase "
            "alphanumeric + hyphen, starting with a letter"
        )


def create_team(
    team_id: str,
    name: str,
    sync_remote: str | None = None,
    sync_meeting_type: str = "sandbox",
) -> None:
    """Insert a new team row.  Idempotent: raises if the slug exists."""
    validate_team_id(team_id)
    with _conn() as c:
        existing = c.execute(
            "SELECT id FROM teams WHERE id = ?", (team_id,)
        ).fetchone()
        if existing:
            raise ValueError(f"team {team_id!r} already exists")
        c.execute(
            "INSERT INTO teams (id, name, sync_remote, sync_meeting_type, "
            "created_at) VALUES (?, ?, ?, ?, ?)",
            (team_id, name, sync_remote, sync_meeting_type, _now()),
        )


def get_team(team_id: str) -> dict | None:
    """Return the team row as a dict, or None if missing."""
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM teams WHERE id = ?", (team_id,)
        ).fetchone()
        return dict(row) if row else None


def list_teams() -> list[dict]:
    """Return all teams ordered by id."""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM teams ORDER BY id ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def update_team_sync(
    team_id: str,
    sync_remote: str | None = ...,
    sync_meeting_type: str | None = ...,
) -> None:
    """Update sync_remote and/or sync_meeting_type for an existing team.

    Sentinel default ``...`` distinguishes "don't touch" from explicit
    ``None`` (which clears sync_remote).
    """
    validate_team_id(team_id)
    with _conn() as c:
        sets: list[str] = []
        params: list = []
        if sync_remote is not ...:
            sets.append("sync_remote = ?")
            params.append(sync_remote)
        if sync_meeting_type is not ...:
            if not sync_meeting_type:
                raise ValueError("sync_meeting_type must be non-empty")
            sets.append("sync_meeting_type = ?")
            params.append(sync_meeting_type)
        if not sets:
            return  # nothing to update
        params.append(team_id)
        c.execute(
            f"UPDATE teams SET {', '.join(sets)} WHERE id = ?",
            params,
        )


def set_job_team(
    job_id: str,
    team_id: str,
    *,
    require_team_exists: bool = True,
) -> None:
    """Reassign a job to a different team.

    v0.6.0 used this only for the migration backfill (with
    ``require_team_exists=False`` so the seed teams could be inserted
    in the same transaction).  v0.6.2's ``vezir session move`` CLI
    relies on the default (existence-check ON) to fail loudly when
    the destination team doesn't exist, instead of silently leaving
    an orphaned job that's invisible to every token.

    Raises ``ValueError`` if the slug shape is invalid, or (when
    ``require_team_exists=True``) if no team with that slug exists.
    """
    validate_team_id(team_id)
    if require_team_exists:
        if get_team(team_id) is None:
            raise ValueError(f"team {team_id!r} does not exist")
    with _conn() as c:
        c.execute(
            "UPDATE jobs SET team_id = ?, updated_at = ? WHERE id = ?",
            (team_id, _now(), job_id),
        )


# ── v0.6.2: team rename (display name only) + team delete ────────────────────


def update_team_name(team_id: str, name: str) -> None:
    """Update the human display name of an existing team.

    Slug (``id``) renames are deferred to v0.7.0 — they require cascading
    updates across ``jobs.team_id``, the ``tokens.json`` token rows, and
    the on-disk ``teams/<id>/`` directory, plus invalidate every
    in-memory web-session that was minted against the old slug.  The
    immutable-slug contract is part of v0.6.0's design.

    Display-name updates are pure-DB and trivially safe.
    """
    validate_team_id(team_id)
    if not name or not name.strip():
        raise ValueError("name must be non-empty")
    if get_team(team_id) is None:
        raise ValueError(f"team {team_id!r} does not exist")
    with _conn() as c:
        c.execute(
            "UPDATE teams SET name = ? WHERE id = ?",
            (name.strip(), team_id),
        )


def delete_team(team_id: str, *, reassign_to: str | None = None) -> dict:
    """Delete a team, optionally cascading its jobs + tokens to another team.

    Policy (v0.6.2):

    * ``reassign_to=None`` (default): refuse-if-not-empty.  Raises
      ``ValueError`` if any jobs reference the team OR any tokens are
      scoped to it.  Forces the operator to do the cleanup explicitly
      (``vezir session move`` per session, ``vezir token revoke`` per
      token).  Safest default; zero data loss possible.
    * ``reassign_to=<other_slug>``: cascade.  All jobs are moved to
      the destination team, all tokens are revoked (token rotation is
      a security-sensitive operation and the destination team's
      members are probably different people; we don't carry tokens
      across teams).  The on-disk ``teams/<id>/`` directory (roster,
      voiceprints, sync_config) is removed last.

    There is intentionally NO ``--force-purge`` that deletes the jobs
    themselves; operator can ``vezir session move`` first or write SQL.

    Returns a stats dict: ``{jobs_reassigned, tokens_revoked,
    on_disk_removed}``.
    """
    validate_team_id(team_id)
    if get_team(team_id) is None:
        raise ValueError(f"team {team_id!r} does not exist")

    if reassign_to is not None:
        validate_team_id(reassign_to)
        if reassign_to == team_id:
            raise ValueError(
                "reassign_to must be a different team than the one being deleted"
            )
        if get_team(reassign_to) is None:
            raise ValueError(
                f"reassign-to team {reassign_to!r} does not exist"
            )

    # 1. Count and (optionally) reassign jobs.
    with _conn() as c:
        n_jobs = c.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE team_id = ?",
            (team_id,),
        ).fetchone()["n"]

        if n_jobs and reassign_to is None:
            raise ValueError(
                f"team {team_id!r} has {n_jobs} job(s) assigned; "
                f"pass reassign_to=<slug> to cascade, or move them "
                f"first via `vezir session move`"
            )

        if n_jobs:
            c.execute(
                "UPDATE jobs SET team_id = ?, updated_at = ? "
                "WHERE team_id = ?",
                (reassign_to, _now(), team_id),
            )

    # 2. Count and (optionally) revoke tokens.
    #    Local import to keep queue.py independent of auth (auth depends
    #    on tokens.json, not on queue).
    from . import auth as _auth
    n_tokens = _auth.count_tokens_for_team(team_id)

    if n_tokens and reassign_to is None:
        # Should be unreachable because we already raised on jobs;
        # but a team with zero jobs and N tokens still needs the same
        # refusal.
        raise ValueError(
            f"team {team_id!r} has {n_tokens} token(s) scoped to it; "
            f"pass reassign_to=<slug> to cascade-revoke, or revoke them "
            f"first via `vezir token revoke`"
        )

    tokens_revoked = 0
    if n_tokens:
        tokens_revoked = _auth.revoke_all_for_team(team_id)

    # 3. Delete the team row itself.
    with _conn() as c:
        c.execute("DELETE FROM teams WHERE id = ?", (team_id,))

    # 4. Remove the on-disk per-team dir (roster, voiceprints, sync_config).
    on_disk_removed = False
    import shutil as _shutil
    from .. import config as _config
    team_dir = _config.teams_dir() / team_id
    if team_dir.exists():
        _shutil.rmtree(team_dir, ignore_errors=True)
        on_disk_removed = not team_dir.exists()

    return {
        "jobs_reassigned": n_jobs if reassign_to else 0,
        "tokens_revoked": tokens_revoked,
        "on_disk_removed": on_disk_removed,
        "reassigned_to": reassign_to,
    }
