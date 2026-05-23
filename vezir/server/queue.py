"""SQLite-backed job queue.

Single-writer, single-worker model. Serialized job execution per the MVP
plan. The queue stores one row per uploaded session.

Schema:
    id                   ULID, primary key, also the session id
    github               GitHub handle of the scribe who uploaded
    title                Optional meeting title
    summary_preset       Optional preset id (high-quality | confidential | alternative)
    auto_label_enabled   0/1.  When 0, worker skips `meet label --auto` and
                         routes the session straight to needs_labeling for
                         human-only labeling.  Default 1.
    sync_enabled         0/1.  When 0, worker skips `meet sync` after the
                         pipeline completes; session goes to `done` with
                         no git push.  Default 1.  Operator-side env var
                         VEZIR_SKIP_SYNC overrides to 0 globally.
    personal             0/1.  When 1, the session is hidden from other team
                         members' session lists (only the uploader sees it).
                         Personal sessions force sync_enabled=0 server-side.
                         Default 0.
    status               one of: queued, transcribing, needs_labeling, syncing,
                         done, error
    created_at           ISO timestamp
    updated_at           ISO timestamp
    error                Last error message, if any
    summary_error        Summary-specific failure message. When transcription
                         succeeds but summary generation fails, this field
                         stores the failure message and the job proceeds to
                         done (transcript artifacts are still usable). The
                         user can retry summary generation later.
    artifacts            JSON-encoded dict of artifact paths (relative to session
                         dir): txt, srt, json, summary, pdf
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
    artifacts           TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC);
"""


VALID_STATUSES = {
    "queued",
    "transcribing",
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
            for ddl in (
                "ALTER TABLE jobs ADD COLUMN summary_preset TEXT",
                "ALTER TABLE jobs ADD COLUMN auto_label_enabled INTEGER NOT NULL DEFAULT 1",
                "ALTER TABLE jobs ADD COLUMN sync_enabled INTEGER NOT NULL DEFAULT 1",
                "ALTER TABLE jobs ADD COLUMN personal INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE jobs ADD COLUMN summary_error TEXT",
            ):
                try:
                    conn.execute(ddl)
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
    """
    if personal:
        sync_enabled = False
    with _conn() as c:
        c.execute(
            "INSERT INTO jobs (id, github, title, summary_preset, "
            "auto_label_enabled, sync_enabled, personal, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)",
            (
                job_id, github, title, summary_preset,
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
) -> None:
    """Update a job's status (and optionally error / artifacts / summary_error).

    ``summary_error`` uses a sentinel default (``...``) so callers can
    distinguish "don't touch summary_error" from "clear it to None".
    Pass ``None`` explicitly to clear a previous summary_error (e.g. after
    a successful retry).
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
) -> list[dict]:
    """Return recent jobs, filtered for visibility.

    ``viewer_github``: when set, applies the personal-visibility rule:
    return all non-personal sessions PLUS personal sessions owned by
    ``viewer_github``. This is the filter used by ``/api/sessions``.

    ``github``: when set (without ``viewer_github``), returns only
    sessions uploaded by that handle (admin / per-user view). This is
    the legacy filter used by ``vezir status``.
    """
    with _conn() as c:
        if viewer_github:
            rows = c.execute(
                "SELECT * FROM jobs WHERE personal = 0 OR github = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (viewer_github, limit),
            ).fetchall()
        elif github:
            rows = c.execute(
                "SELECT * FROM jobs WHERE github = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (github, limit),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
