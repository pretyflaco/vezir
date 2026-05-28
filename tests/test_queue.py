"""Basic unit tests for the sqlite queue."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def tmp_data(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("VEZIR_DATA", d)
        yield Path(d)


def test_enqueue_and_claim(tmp_data):
    from vezir.server import queue
    queue.enqueue("01HZ000000000000000000ABCD", github="alice", title="t1", team_id="blink")
    queue.enqueue("01HZ000000000000000000ABCE", github="bob", title="t2", team_id="blink")

    job = queue.claim_next()
    assert job is not None
    assert job["id"] == "01HZ000000000000000000ABCD"
    assert job["status"] == "transcribing"

    # Second claim returns the next queued job (since the first is now transcribing).
    job2 = queue.claim_next()
    assert job2 is not None
    assert job2["id"] == "01HZ000000000000000000ABCE"


def test_status_transitions(tmp_data):
    from vezir.server import queue
    queue.enqueue("01HZ0000000000000000000XYZ", github="alice", team_id="blink")
    queue.update_status("01HZ0000000000000000000XYZ", "needs_labeling",
                        artifacts={"pdf": "x.pdf"})
    row = queue.get("01HZ0000000000000000000XYZ")
    assert row["status"] == "needs_labeling"
    assert "pdf" in row["artifacts"]


def test_invalid_status(tmp_data):
    from vezir.server import queue
    queue.enqueue("01HZ000000000000000000NOPE", github="alice", team_id="blink")
    with pytest.raises(ValueError):
        queue.update_status("01HZ000000000000000000NOPE", "bogus")


def test_connection_pragmas(tmp_data):
    """WAL + busy_timeout + foreign_keys are applied on every connection."""
    from vezir.server import queue
    # Force the DB into existence + a connection through the public path.
    queue.enqueue("01HZ00000000000000000PRAG", github="alice", team_id="blink")
    with queue._conn() as c:
        assert c.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        # busy_timeout is reported in milliseconds.
        assert c.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        # synchronous=NORMAL == 1
        assert c.execute("PRAGMA synchronous").fetchone()[0] == 1


def test_foreign_keys_enforced(tmp_data):
    """session_teams.team_id REFERENCES teams(id) is actually enforced."""
    import sqlite3

    from vezir.server import queue
    queue.create_team("blink", "Blink")
    queue.enqueue("01HZ0000000000000000FKJOB", github="alice", team_id="blink")
    # Sharing a session with a non-existent team must violate the FK.
    with pytest.raises(sqlite3.IntegrityError):
        with queue._conn() as c:
            c.execute(
                "INSERT INTO session_teams (session_id, team_id) VALUES (?, ?)",
                ("01HZ0000000000000000FKJOB", "ghost-team"),
            )


def test_concurrent_writers_no_lost_update(tmp_data):
    """Concurrent enqueue from many threads: every row survives (WAL + lock)."""
    import threading

    from vezir.server import queue
    queue.create_team("blink", "Blink")

    errors: list[Exception] = []

    def worker(n: int) -> None:
        try:
            queue.enqueue(f"01HZ00000000000000CONC{n:04d}", github="alice", team_id="blink")
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(25)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent enqueue raised: {errors}"
    rows = queue.list_recent(team_id="blink", limit=100)
    assert len([r for r in rows if r["id"].startswith("01HZ00000000000000CONC")]) == 25
