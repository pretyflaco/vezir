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
    multi_audio          0/1.  When 1, the session dir holds multiple
                         ``<id>.part-NNN<ext>`` audio files (one meeting split
                         across several uploads, e.g. Telegram voicenotes).
                         The worker concatenates them in filename order into the
                         single canonical ``<id><ext>`` before transcribe.
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
    summary_fallback     "<backend>/<model>" of the fallback summarizer that
                         actually produced the summary when the requested
                         preset's backend failed and millet fell back (opt-in
                         via MILLET_SUMMARY_PRESET_FALLBACK).  NULL when the
                         requested preset ran or no summary was generated.
                         Read from millet's .summary.meta.json sidecar.
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
from collections.abc import Iterator
from contextlib import contextmanager

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
    multi_audio         INTEGER NOT NULL DEFAULT 0,
    status              TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    error               TEXT,
    summary_error       TEXT,
    sync_error          TEXT,
    summary_fallback    TEXT,
    artifacts           TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC);

CREATE TABLE IF NOT EXISTS teams (
    id                  TEXT PRIMARY KEY,   -- stable UUID (v0.7.4+)
    slug                TEXT UNIQUE,        -- mutable display slug (v0.7.4+)
    name                TEXT NOT NULL,
    sync_remote         TEXT,
    sync_meeting_type   TEXT NOT NULL DEFAULT 'sandbox',
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memberships (
    github              TEXT NOT NULL,
    team_id             TEXT NOT NULL REFERENCES teams(id),
    role                TEXT NOT NULL DEFAULT 'scribe',
    added_at            TEXT NOT NULL,
    added_by            TEXT,
    PRIMARY KEY (github, team_id)
);

CREATE TABLE IF NOT EXISTS session_teams (
    session_id          TEXT NOT NULL REFERENCES jobs(id),
    team_id             TEXT NOT NULL REFERENCES teams(id),
    PRIMARY KEY (session_id, team_id)
);

CREATE INDEX IF NOT EXISTS idx_memberships_team ON memberships(team_id);
CREATE INDEX IF NOT EXISTS idx_session_teams_team ON session_teams(team_id);

CREATE TABLE IF NOT EXISTS tokens (
    token_hash    TEXT PRIMARY KEY,
    github        TEXT NOT NULL,
    issued_at     TEXT NOT NULL,
    expires_at    TEXT,
    last_used_at  TEXT,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    label         TEXT
);

CREATE INDEX IF NOT EXISTS idx_tokens_github ON tokens(github);

CREATE TABLE IF NOT EXISTS nostr_members (
    npub          TEXT PRIMARY KEY,   -- 64-char lowercase hex x-only pubkey
    github        TEXT NOT NULL,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    label         TEXT,
    added_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_nostr_members_github ON nostr_members(github);

CREATE TABLE IF NOT EXISTS google_members (
    email         TEXT PRIMARY KEY,   -- lowercased verified Google email
    github        TEXT NOT NULL,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    label         TEXT,
    added_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_google_members_github ON google_members(github);

CREATE TABLE IF NOT EXISTS sessions (
    sid                 TEXT PRIMARY KEY,   -- session/family id (uuid4 hex)
    github              TEXT NOT NULL,
    npub                TEXT,               -- '' for google identities
    is_admin            INTEGER NOT NULL DEFAULT 0,
    auth_method         TEXT NOT NULL,      -- 'nostr' | 'google'
    refresh_hash        TEXT NOT NULL,      -- sha256(current refresh token)
    prev_refresh_hash   TEXT,               -- one-generation grace window
    created_at          TEXT NOT NULL,      -- ISO8601 UTC (absolute-cap anchor)
    refresh_expires_at  TEXT NOT NULL,      -- now + idle TTL (bumped each rotation)
    absolute_max_at     TEXT NOT NULL,      -- created_at + max TTL (never moves)
    last_rotated_at     TEXT,
    revoked             INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_sessions_github ON sessions(github);
CREATE INDEX IF NOT EXISTS idx_sessions_refresh_hash ON sessions(refresh_hash);
CREATE INDEX IF NOT EXISTS idx_sessions_prev_refresh_hash
    ON sessions(prev_refresh_hash);
"""


VALID_STATUSES = {
    "queued",
    "transcribing",
    "summarizing",
    "needs_labeling",
    "syncing",
    "done",
    "sync_failed",
    "error",
    # Terminal: transcription succeeded but produced no speech (0 segments).
    # Such a session must NOT sync empty artifacts to the team repo.
    "empty",
}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    """Apply connection-level PRAGMAs for concurrency + integrity.

    Must run on every fresh connection, before any other statement,
    because several of these PRAGMAs are per-connection in SQLite:

    * ``journal_mode=WAL`` — readers don't block the single writer and
      vice-versa; also the only mode safe for concurrent multi-process
      access (server + a CLI invocation touching the same file).  WAL
      is a *database-level* setting that persists once set, but issuing
      it per-connect is harmless and idempotent.
    * ``busy_timeout=5000`` — wait up to 5s for a competing writer to
      release its lock instead of immediately raising
      ``database is locked``.  Per-connection.
    * ``synchronous=NORMAL`` — safe with WAL (the WAL is fsynced at
      checkpoint, not on every commit); markedly faster than the
      default ``FULL`` with no durability loss that matters for our
      workload.  Per-connection.
    * ``foreign_keys=ON`` — enforce the ``REFERENCES`` clauses in the
      schema (``memberships``/``session_teams`` → ``teams``,
      ``session_teams`` → ``jobs``).  Previously documentary only.
      Per-connection, MUST be set before any statement in a
      transaction.  ``delete_team`` already deletes child rows before
      the parent, so enforcement is order-compatible.
    """
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")


# DB paths whose schema bring-up has already run in this process.
# v0.11.0: the DDL (CREATE TABLE/INDEX script + ~10 idempotent ALTERs)
# used to run on EVERY connection — at least twice per authenticated
# request, all while holding the global lock, and its blanket
# ``except OperationalError`` masked genuine errors (e.g. disk full) as
# ignored duplicates.  Now it runs once per (process, db-path); tests
# that point config at fresh tmp DBs still get their schema.
_SCHEMA_DONE: set[str] = set()


def _ensure_schema(conn: sqlite3.Connection, db_path: str) -> None:
    """Run schema bring-up once per DB path per process (caller holds _LOCK)."""
    if db_path in _SCHEMA_DONE:
        return
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
        "ALTER TABLE jobs ADD COLUMN multi_audio INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE jobs ADD COLUMN summary_error TEXT",
        "ALTER TABLE jobs ADD COLUMN sync_error TEXT",
        "ALTER TABLE jobs ADD COLUMN team_id TEXT NOT NULL DEFAULT ''",
        # v0.11.1: User-Agent of the client that uploaded the session
        # (e.g. "vezir-cli/0.11.1", "okhttp/4.12.0").  Nullable; NULL for
        # pre-0.11.1 rows and any client that sends no User-Agent.
        "ALTER TABLE jobs ADD COLUMN client_agent TEXT",
        # v0.14.0: "<backend>/<model>" of the fallback summarizer that
        # produced the summary when the requested preset failed and millet
        # fell back (MILLET_SUMMARY_PRESET_FALLBACK opt-in).  NULL when the
        # requested preset ran.
        "ALTER TABLE jobs ADD COLUMN summary_fallback TEXT",
        # v0.7.4: teams.slug (mutable display name); teams.id is now a
        # stable UUID.  Added here for DBs predating the column; the
        # 0.7.4 data migration backfills slug=id for legacy rows then
        # rewrites ids to UUIDs.
        "ALTER TABLE teams ADD COLUMN slug TEXT",
    ):
        try:
            conn.execute(ddl)
            conn.commit()
        except sqlite3.OperationalError:
            pass
    for index_ddl in (
        "CREATE INDEX IF NOT EXISTS idx_jobs_team ON jobs(team_id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_teams_slug ON teams(slug)",
    ):
        try:
            conn.execute(index_ddl)
            conn.commit()
        except sqlite3.OperationalError:
            pass
    _SCHEMA_DONE.add(db_path)


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    """Get a connection to the queue DB. Thread-safe via a global lock."""
    config.ensure_dirs()
    db_path = config.queue_db_path()
    with _LOCK:
        conn = sqlite3.connect(str(db_path))
        config.secure_chmod_file(db_path)
        conn.row_factory = sqlite3.Row
        _apply_pragmas(conn)
        try:
            _ensure_schema(conn, str(db_path))
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
    multi_audio: bool = False,
    client_agent: str | None = None,
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

    ``client_agent`` (v0.11.1) is the uploading client's User-Agent header,
    stored verbatim for provenance (e.g. answering "was this an old
    client?").  Nullable; trimmed to 200 chars defensively.
    """
    if not team_id:
        raise ValueError("enqueue requires team_id (added in v0.6.0)")
    if personal:
        sync_enabled = False
    if client_agent is not None:
        client_agent = client_agent.strip()[:200] or None
    if title is not None:
        # Bound the title: it's echoed in every listing and drives millet's
        # sync folder name / schedule matching, so an unbounded value is
        # both a storage and a display hazard (L-10).
        title = title.strip()[:256] or None
    # v0.7.4: jobs store the team's stable uuid.  In production
    # ``team_id`` arrives as the uuid (from require_team_context); accept
    # a slug too and resolve, so the stored value is always the uuid.
    team_id = resolve_team_uuid(team_id) or team_id
    with _conn() as c:
        c.execute(
            "INSERT INTO jobs (id, github, team_id, title, summary_preset, "
            "auto_label_enabled, sync_enabled, personal, multi_audio, "
            "client_agent, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)",
            (
                job_id, github, team_id, title, summary_preset,
                1 if auto_label_enabled else 0,
                1 if sync_enabled else 0,
                1 if personal else 0,
                1 if multi_audio else 0,
                client_agent,
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


# In-progress states that only the single worker ever drives.  If any job is
# left in one of these at worker startup, no worker is running it (the previous
# process died / was restarted mid-pipeline) — it is orphaned and must be
# re-queued so it gets picked up again.
_ORPHANABLE_STATUSES = ("transcribing", "summarizing", "syncing")


def requeue_orphans() -> list[str]:
    """Reset orphaned in-progress jobs back to ``queued``.

    Called once at worker startup (single-writer model: at startup the worker
    is not yet processing anything, so any job still in ``transcribing`` /
    ``summarizing`` / ``syncing`` was interrupted by a previous restart/crash
    and is orphaned).  ``claim_next`` only picks up ``queued`` jobs, so without
    this an interrupted job would stay stuck forever.

    Re-queuing is safe: ``millet transcribe`` is idempotent (it re-runs from the
    audio and overwrites artifacts), and a re-queued job replays the full
    pipeline (transcribe → label → sync) from scratch.

    Returns the list of job ids that were re-queued (empty if none).
    """
    placeholders = ",".join("?" for _ in _ORPHANABLE_STATUSES)
    with _conn() as c:
        rows = c.execute(
            f"SELECT id FROM jobs WHERE status IN ({placeholders})",
            _ORPHANABLE_STATUSES,
        ).fetchall()
        ids = [r["id"] for r in rows]
        if ids:
            c.execute(
                f"UPDATE jobs SET status = 'queued', updated_at = ? "
                f"WHERE status IN ({placeholders})",
                (_now(), *_ORPHANABLE_STATUSES),
            )
    return ids


def update_status(
    job_id: str,
    status: str,
    error: str | None = None,
    artifacts: dict | None = None,
    summary_error: str | None = ...,
    sync_error: str | None = ...,
    summary_fallback: str | None = ...,
) -> None:
    """Update a job's status (and optionally error / artifacts / summary_error / sync_error).

    ``summary_error``, ``sync_error`` and ``summary_fallback`` use a
    sentinel default (``...``) so callers can distinguish "don't touch"
    from "clear it to None".  Pass ``None`` explicitly to clear a
    previous error (e.g. after a successful retry).
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
        if summary_fallback is not ...:
            sets.append("summary_fallback = ?")
            params.append(summary_fallback)
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


def set_title(job_id: str, title: str | None) -> None:
    """Set (or clear) the human title on an existing job.

    Used by ``POST /api/sessions/{id}/title`` so a scribe who forgot to
    name a session at record time can add/change the title afterwards.
    An empty/blank title is normalized to NULL (falls back to the id in
    every display surface).
    """
    normalized = (title or "").strip() or None
    with _conn() as c:
        c.execute(
            "UPDATE jobs SET title = ?, updated_at = ? WHERE id = ?",
            (normalized, _now(), job_id),
        )


def _session_was_synced(row: dict) -> bool:
    """Best-effort guess whether a session's artifacts reached the team git repo.

    There is no stored remote commit/path, so we infer from the row.  A sync
    was attempted (and may have pushed something) when the status is
    ``syncing``/``sync_failed``, when ``sync_error`` is set, or when a
    sync-enabled job reached the terminal ``done`` state (the pipeline runs
    sync as the last step before marking ``done``).  Used only to drive the
    "git copy remains" backwash warning on delete; a false positive just
    shows a harmless extra warning.
    """
    status = (row.get("status") or "").lower()
    if status in ("syncing", "sync_failed"):
        return True
    if row.get("sync_error"):
        return True
    if status == "done" and row.get("sync_enabled"):
        return True
    return False


def was_synced(row: dict) -> bool:
    """Public best-effort "did this session reach the team git repo" check.

    Thin wrapper over the internal heuristic so callers outside this
    module (e.g. the retitle endpoint's warning) don't reach into a
    private name.
    """
    return _session_was_synced(row)


def delete_session(session_id: str) -> dict:
    """Hard-delete a session: DB rows + on-disk artifacts + log.

    Ordered child-then-parent (FKs are enforced, see ``_conn``), mirroring
    ``delete_team``.  Removes:

      * ``session_teams`` rows (cross-team shares),
      * the ``jobs`` row itself,
      * the on-disk ``sessions/<id>/`` directory,
      * the ``logs/<id>.log`` file,
      * any leftover HOME-shim ``jobs/<id>/`` dir (normally already cleaned
        by ``meet_runner.cleanup_home_shim``).

    This is local-only: artifacts already pushed to a team's git remote are
    NOT removed (millet sync is push-only; there is no un-sync).  Callers
    should surface the ``was_synced`` hint as a backwash warning.

    Returns a stats dict; ``db_deleted`` is False if the row did not exist.
    """
    import shutil as _shutil

    row = get(session_id)
    if row is None:
        return {
            "session_id": session_id,
            "db_deleted": False,
            "dir_removed": False,
            "log_removed": False,
            "was_synced": False,
        }

    was_synced = _session_was_synced(row)

    # 1. Remove DB rows (children first; FKs enforced).
    with _conn() as c:
        c.execute("DELETE FROM session_teams WHERE session_id = ?", (session_id,))
        c.execute("DELETE FROM jobs WHERE id = ?", (session_id,))

    # 2. On-disk cleanup (best-effort; the DB is the source of truth).
    session_dir = config.sessions_dir() / session_id
    dir_removed = False
    if session_dir.exists():
        _shutil.rmtree(session_dir, ignore_errors=True)
        dir_removed = not session_dir.exists()

    log_file = config.logs_dir() / f"{session_id}.log"
    log_removed = False
    if log_file.exists():
        try:
            log_file.unlink()
            log_removed = True
        except OSError:
            log_removed = False

    # Leftover HOME-shim dir (normally already removed by cleanup_home_shim).
    shim_dir = config.jobs_dir() / session_id
    if shim_dir.exists():
        _shutil.rmtree(shim_dir, ignore_errors=True)

    return {
        "session_id": session_id,
        "db_deleted": True,
        "dir_removed": dir_removed,
        "log_removed": log_removed,
        "was_synced": was_synced,
    }


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

    # v0.7.4: jobs store the team uuid.  Resolve slug-or-uuid inputs so
    # callers may pass either form; in production these arrive as the
    # uuid already (from require_team_context).
    if viewer_team_id:
        viewer_team_id = resolve_team_uuid(viewer_team_id) or viewer_team_id
    if team_id:
        team_id = resolve_team_uuid(team_id) or team_id

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


_TEAM_SLUG_RE = None  # lazy-compiled in validate_slug


def validate_slug(slug: str) -> None:
    """Raise ``ValueError`` if ``slug`` doesn't match the slug shape.

    Constraints (per the v0.6.0 design): 3-32 chars, lowercase
    alphanumeric and hyphen, must start with a letter.

    v0.7.4: slugs are now mutable *display* identifiers; the stable
    primary key is a UUID (``teams.id``).
    """
    global _TEAM_SLUG_RE
    if _TEAM_SLUG_RE is None:
        import re as _re
        _TEAM_SLUG_RE = _re.compile(r"^[a-z][a-z0-9-]{2,31}$")
    if not isinstance(slug, str) or not _TEAM_SLUG_RE.match(slug):
        raise ValueError(
            f"invalid team slug {slug!r}: must be 3-32 chars, lowercase "
            "alphanumeric + hyphen, starting with a letter"
        )


# Back-compat alias: pre-0.7.4 callers used validate_team_id for slugs.
validate_team_id = validate_slug


def _new_team_uuid() -> str:
    import uuid as _uuid
    return _uuid.uuid4().hex


def resolve_team_uuid(slug_or_uuid: str) -> str | None:
    """Resolve a slug OR a uuid to the team's uuid (``teams.id``), or None.

    Used at API/CLI boundaries: callers may pass either form.  The
    ``X-Team-Id`` header carries the uuid (v0.7.4+), but CLI commands
    accept the human slug.
    """
    if not slug_or_uuid:
        return None
    with _conn() as c:
        row = c.execute(
            "SELECT id FROM teams WHERE id = ? OR slug = ?",
            (slug_or_uuid, slug_or_uuid),
        ).fetchone()
        return row["id"] if row else None


def create_team(
    slug: str,
    name: str,
    sync_remote: str | None = None,
    sync_meeting_type: str = "sandbox",
    *,
    team_uuid: str | None = None,
) -> str:
    """Insert a new team row keyed by a fresh UUID; returns the uuid.

    ``slug`` is the mutable display identifier (must be unique).  Pass
    ``team_uuid`` only from migrations that need a deterministic id.
    """
    validate_slug(slug)
    team_uuid = team_uuid or _new_team_uuid()
    with _conn() as c:
        existing = c.execute(
            "SELECT id FROM teams WHERE slug = ?", (slug,)
        ).fetchone()
        if existing:
            raise ValueError(f"team {slug!r} already exists")
        c.execute(
            "INSERT INTO teams (id, slug, name, sync_remote, "
            "sync_meeting_type, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (team_uuid, slug, name, sync_remote, sync_meeting_type, _now()),
        )
    return team_uuid


def get_team(team_id: str) -> dict | None:
    """Return the team row as a dict (keyed by uuid OR slug), or None."""
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM teams WHERE id = ? OR slug = ?",
            (team_id, team_id),
        ).fetchone()
        return dict(row) if row else None


def list_teams() -> list[dict]:
    """Return all teams ordered by slug."""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM teams ORDER BY slug ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def rename_team_slug(team_id: str, new_slug: str) -> None:
    """Change a team's display slug (uuid PK is unchanged).

    Because the slug is no longer a key (v0.7.4), this is a pure
    single-row UPDATE — no cascade across jobs/memberships/dirs.
    Raises ``ValueError`` on bad slug shape, unknown team, or collision.
    """
    validate_slug(new_slug)
    uuid = resolve_team_uuid(team_id)
    if uuid is None:
        raise ValueError(f"team {team_id!r} does not exist")
    with _conn() as c:
        clash = c.execute(
            "SELECT id FROM teams WHERE slug = ? AND id != ?",
            (new_slug, uuid),
        ).fetchone()
        if clash:
            raise ValueError(f"slug {new_slug!r} is already in use")
        c.execute("UPDATE teams SET slug = ? WHERE id = ?", (new_slug, uuid))


def update_team_sync(
    team_id: str,
    sync_remote: str | None = ...,
    sync_meeting_type: str | None = ...,
) -> None:
    """Update sync_remote and/or sync_meeting_type for an existing team.

    Sentinel default ``...`` distinguishes "don't touch" from explicit
    ``None`` (which clears sync_remote).  ``team_id`` may be a slug or
    a uuid.
    """
    uuid = resolve_team_uuid(team_id)
    if uuid is None:
        raise ValueError(f"team {team_id!r} does not exist")
    with _conn() as c:
        sets: list[str] = []
        params: list = []
        if sync_remote is not ...:
            sets.append("sync_remote = ?")
            params.append(sync_remote)
        if sync_meeting_type is not ...:
            if not sync_meeting_type:
                raise ValueError("sync_meeting_type must be non-empty")
            # Validate/normalize to a filesystem-safe slug at write time so a
            # bad value fails here rather than downstream at `millet sync
            # --meeting-type` (L-12).
            slug = config.sync_slug(sync_meeting_type)
            if not slug:
                raise ValueError(
                    f"sync_meeting_type {sync_meeting_type!r} is not a valid "
                    "folder name"
                )
            sets.append("sync_meeting_type = ?")
            params.append(slug)
        if not sets:
            return  # nothing to update
        params.append(uuid)
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

    ``team_id`` may be a slug or a uuid; the job is stored against the
    resolved uuid.  Raises ``ValueError`` when ``require_team_exists``
    and no such team exists.

    Migration backfill passes ``require_team_exists=False`` and a raw
    uuid (the seed teams are inserted in the same transaction), so in
    that mode the value is used verbatim.
    """
    if require_team_exists:
        uuid = resolve_team_uuid(team_id)
        if uuid is None:
            raise ValueError(f"team {team_id!r} does not exist")
    else:
        uuid = team_id
    with _conn() as c:
        c.execute(
            "UPDATE jobs SET team_id = ?, updated_at = ? WHERE id = ?",
            (uuid, _now(), job_id),
        )


# ── v0.6.2: team rename (display name only) + team delete ────────────────────


def update_team_name(team_id: str, name: str) -> None:
    """Update the human display name of an existing team.

    v0.7.4: slug renames are now first-class (see
    :func:`rename_team_slug`) because the slug is no longer a key —
    the stable PK is a uuid.  This function updates only the freeform
    ``name``.  ``team_id`` may be a slug or a uuid.
    """
    if not name or not name.strip():
        raise ValueError("name must be non-empty")
    uuid = resolve_team_uuid(team_id)
    if uuid is None:
        raise ValueError(f"team {team_id!r} does not exist")
    with _conn() as c:
        c.execute(
            "UPDATE teams SET name = ? WHERE id = ?",
            (name.strip(), uuid),
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
    on_disk_removed}``.  ``team_id`` and ``reassign_to`` may each be a
    slug or a uuid.
    """
    uuid = resolve_team_uuid(team_id)
    if uuid is None:
        raise ValueError(f"team {team_id!r} does not exist")

    reassign_uuid: str | None = None
    if reassign_to is not None:
        reassign_uuid = resolve_team_uuid(reassign_to)
        if reassign_uuid is None:
            raise ValueError(
                f"reassign-to team {reassign_to!r} does not exist"
            )
        if reassign_uuid == uuid:
            raise ValueError(
                "reassign_to must be a different team than the one being deleted"
            )

    # Steps 1-4 run in ONE transaction (v0.11.0).  They used to span
    # four separate ``_conn()`` blocks, so a crash mid-sequence could
    # leave jobs reassigned but the team (and its memberships) still
    # present, or memberships dropped with the team row alive.  Any
    # refusal raise now rolls the whole thing back.
    with _conn() as c:
        # 1. Count and (optionally) reassign jobs.
        n_jobs = c.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE team_id = ?",
            (uuid,),
        ).fetchone()["n"]

        if n_jobs and reassign_uuid is None:
            raise ValueError(
                f"team {team_id!r} has {n_jobs} job(s) assigned; "
                f"pass reassign_to=<slug> to cascade, or move them "
                f"first via `vezir session move`"
            )

        if n_jobs:
            c.execute(
                "UPDATE jobs SET team_id = ?, updated_at = ? "
                "WHERE team_id = ?",
                (reassign_uuid, _now(), uuid),
            )

        # 2. Count and (optionally) drop memberships.  v0.7.0: tokens no
        #    longer have team_id, so "tokens scoped to this team" doesn't
        #    exist as a concept.  Instead we count membership rows; cascade
        #    drops them, refusal mode demands the operator first
        #    `vezir team remove-member` each one.
        n_members = c.execute(
            "SELECT COUNT(*) AS n FROM memberships WHERE team_id = ?",
            (uuid,),
        ).fetchone()["n"]

        if n_members and reassign_uuid is None:
            raise ValueError(
                f"team {team_id!r} has {n_members} member(s); "
                f"pass reassign_to=<slug> to cascade-drop memberships, "
                f"or remove them first via `vezir team remove-member`"
            )

        members_dropped = 0
        if n_members:
            cur = c.execute(
                "DELETE FROM memberships WHERE team_id = ?", (uuid,)
            )
            members_dropped = cur.rowcount

        # 3. Drop session_teams rows targeting this team (cross-team shares).
        c.execute(
            "DELETE FROM session_teams WHERE team_id = ?", (uuid,)
        )

        # 4. Delete the team row itself.
        c.execute("DELETE FROM teams WHERE id = ?", (uuid,))

    # 5. Remove the on-disk per-team dir (keyed by uuid in v0.7.4+).
    #    Filesystem cleanup stays outside the transaction: the DB commit
    #    is the point of no return, and a failed rmtree only leaves a
    #    harmless orphan directory.
    on_disk_removed = False
    import shutil as _shutil

    from .. import config as _config
    team_dir = _config.teams_dir() / uuid
    if team_dir.exists():
        _shutil.rmtree(team_dir, ignore_errors=True)
        on_disk_removed = not team_dir.exists()

    return {
        "jobs_reassigned": n_jobs if reassign_uuid else 0,
        "members_dropped": members_dropped,
        "on_disk_removed": on_disk_removed,
        "reassigned_to": reassign_uuid,
    }


# ── memberships (v0.7.0) ──────────────────────────────────────────────────


def add_membership(
    github: str, team_id: str, role: str = "scribe", added_by: str | None = None,
) -> None:
    """Add or update a user's membership in a team.

    ``team_id`` may be a slug or a uuid; stored against the uuid.
    """
    if role not in ("admin", "scribe"):
        raise ValueError(f"invalid role {role!r}; must be 'admin' or 'scribe'")
    uuid = resolve_team_uuid(team_id)
    if uuid is None:
        raise ValueError(f"team {team_id!r} does not exist")
    now = _now()
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO memberships "
            "(github, team_id, role, added_at, added_by) "
            "VALUES (?, ?, ?, ?, ?)",
            (github, uuid, role, now, added_by),
        )


def remove_membership(github: str, team_id: str) -> bool:
    """Remove a user from a team. Returns True if a row was deleted.

    ``team_id`` may be a slug or a uuid.
    """
    uuid = resolve_team_uuid(team_id) or team_id
    with _conn() as c:
        cur = c.execute(
            "DELETE FROM memberships WHERE github = ? AND team_id = ?",
            (github, uuid),
        )
        return cur.rowcount > 0


def get_memberships(github: str) -> list[dict]:
    """Return all teams a user is a member of, with name, slug, and role.

    Each dict carries ``team_id`` (the uuid — what the client sends back
    in ``X-Team-Id``), ``slug`` (display), ``team_name``, and ``role``.
    """
    with _conn() as c:
        rows = c.execute(
            "SELECT m.team_id, t.slug, m.role, t.name AS team_name "
            "FROM memberships m JOIN teams t ON m.team_id = t.id "
            "WHERE m.github = ? ORDER BY t.slug",
            (github,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_team_members(team_id: str) -> list[dict]:
    """Return all members of a team with their roles (slug or uuid ok)."""
    uuid = resolve_team_uuid(team_id) or team_id
    with _conn() as c:
        rows = c.execute(
            "SELECT github, role, added_at, added_by "
            "FROM memberships WHERE team_id = ? ORDER BY github",
            (uuid,),
        ).fetchall()
        return [dict(r) for r in rows]


def is_member(github: str, team_id: str) -> bool:
    """Check if a user is a member of a team (slug or uuid ok)."""
    uuid = resolve_team_uuid(team_id) or team_id
    with _conn() as c:
        row = c.execute(
            "SELECT 1 FROM memberships WHERE github = ? AND team_id = ?",
            (github, uuid),
        ).fetchone()
        return row is not None


def get_role(github: str, team_id: str) -> str | None:
    """Return the user's role in a team, or None if not a member."""
    uuid = resolve_team_uuid(team_id) or team_id
    with _conn() as c:
        row = c.execute(
            "SELECT role FROM memberships WHERE github = ? AND team_id = ?",
            (github, uuid),
        ).fetchone()
        return row["role"] if row else None


# ── session_teams (v0.7.0) ────────────────────────────────────────────────


def share_session_with_team(session_id: str, team_id: str) -> None:
    """Make a session visible to a team (slug or uuid ok)."""
    uuid = resolve_team_uuid(team_id)
    if uuid is None:
        raise ValueError(f"team {team_id!r} does not exist")
    with _conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO session_teams (session_id, team_id) VALUES (?, ?)",
            (session_id, uuid),
        )


def unshare_session_from_team(session_id: str, team_id: str) -> None:
    """Remove a session's visibility from a team (slug or uuid ok)."""
    uuid = resolve_team_uuid(team_id) or team_id
    with _conn() as c:
        c.execute(
            "DELETE FROM session_teams WHERE session_id = ? AND team_id = ?",
            (session_id, uuid),
        )


def get_session_teams(session_id: str) -> list[str]:
    """Return the list of team IDs a session is shared with."""
    with _conn() as c:
        rows = c.execute(
            "SELECT team_id FROM session_teams WHERE session_id = ? ORDER BY team_id",
            (session_id,),
        ).fetchall()
        return [r["team_id"] for r in rows]


def can_view_session(session_id: str, viewer_team_id: str) -> bool:
    """Check if a session is visible to a team.

    A session is visible if:
    1. The session's own team_id matches, OR
    2. The session is in the session_teams junction table for that team.
    """
    with _conn() as c:
        row = c.execute(
            "SELECT 1 FROM jobs WHERE id = ? AND team_id = ?",
            (session_id, viewer_team_id),
        ).fetchone()
        if row:
            return True
        row = c.execute(
            "SELECT 1 FROM session_teams WHERE session_id = ? AND team_id = ?",
            (session_id, viewer_team_id),
        ).fetchone()
        return row is not None
