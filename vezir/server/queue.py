"""SQLite-backed job queue.

Single-writer, single-worker model. Serialized job execution per the MVP
plan. The queue stores one row per uploaded session.

Schema:
    id              ULID, primary key, also the session id
    github          GitHub handle of the scribe who uploaded
    title           Optional meeting title
    status          one of: queued, transcribing, needs_labeling, syncing,
                    done, error
    created_at      ISO timestamp
    updated_at      ISO timestamp
    error           Last error message, if any
    artifacts       JSON-encoded dict of artifact paths (relative to session
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
    id              TEXT PRIMARY KEY,
    github          TEXT NOT NULL,
    title           TEXT,
    status          TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    error           TEXT,
    artifacts       TEXT
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
            yield conn
            conn.commit()
        finally:
            conn.close()


def enqueue(job_id: str, github: str, title: str | None = None) -> None:
    """Add a new job in `queued` state."""
    with _conn() as c:
        c.execute(
            "INSERT INTO jobs (id, github, title, status, created_at, updated_at) "
            "VALUES (?, ?, ?, 'queued', ?, ?)",
            (job_id, github, title, _now(), _now()),
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
) -> None:
    """Update a job's status (and optionally error / artifacts)."""
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid status: {status}")
    with _conn() as c:
        if artifacts is not None:
            c.execute(
                "UPDATE jobs SET status = ?, updated_at = ?, error = ?, artifacts = ? "
                "WHERE id = ?",
                (status, _now(), error, json.dumps(artifacts), job_id),
            )
        else:
            c.execute(
                "UPDATE jobs SET status = ?, updated_at = ?, error = ? WHERE id = ?",
                (status, _now(), error, job_id),
            )


def get(job_id: str) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None


def list_recent(limit: int = 50, github: str | None = None) -> list[dict]:
    with _conn() as c:
        if github:
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
