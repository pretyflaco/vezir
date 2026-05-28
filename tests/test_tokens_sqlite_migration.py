"""Tests for the v0.7.2 tokens.json -> sqlite migration."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
from pathlib import Path

import pytest


@pytest.fixture
def tmp_data(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("VEZIR_DATA", d)
        yield Path(d)


def _token_rows() -> list[dict]:
    from vezir import config
    conn = sqlite3.connect(str(config.queue_db_path()))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM tokens")]
    finally:
        conn.close()


def test_migration_imports_json_rows(tmp_data):
    from vezir import config
    from vezir.server import migrations

    # Seed a legacy tokens.json (post-0.7.0 shape: no team_id).
    config.secure_write_text(
        config.tokens_json_path(),
        json.dumps({"tokens": [
            {"github": "alice", "token_hash": "hash_alice",
             "issued_at": "2025-01-01T00:00:00Z", "expires_at": None,
             "last_used_at": None, "is_admin": True, "label": "laptop"},
            {"github": "bob", "token_hash": "hash_bob",
             "issued_at": "2025-01-02T00:00:00Z", "expires_at": None,
             "last_used_at": None, "is_admin": False, "label": None},
        ]}),
    )

    summary = migrations.migrate_0_7_2()
    assert summary["tokens"]["tokens_imported"] == 2

    rows = {r["github"]: r for r in _token_rows()}
    assert set(rows) == {"alice", "bob"}
    assert rows["alice"]["token_hash"] == "hash_alice"
    assert bool(rows["alice"]["is_admin"]) is True
    assert rows["alice"]["label"] == "laptop"
    assert bool(rows["bob"]["is_admin"]) is False

    # Source file renamed to .migrated backstop.
    assert not config.tokens_json_path().exists()
    assert config.tokens_json_path().with_suffix(".json.migrated").exists()


def test_migration_idempotent(tmp_data):
    from vezir import config
    from vezir.server import migrations

    config.secure_write_text(
        config.tokens_json_path(),
        json.dumps({"tokens": [
            {"github": "alice", "token_hash": "hash_alice",
             "issued_at": "2025-01-01T00:00:00Z", "is_admin": False},
        ]}),
    )

    migrations.migrate_0_7_2()
    second = migrations.migrate_0_7_2()
    assert second == {"already_applied": True}
    # Still exactly one row.
    assert len(_token_rows()) == 1


def test_migration_no_file_is_noop(tmp_data):
    from vezir.server import migrations
    summary = migrations.migrate_0_7_2()
    assert summary["tokens"]["tokens_imported"] == 0
    assert summary["tokens"]["file_renamed"] is False


def test_issue_then_lookup_through_sqlite(tmp_data):
    """End-to-end: issue writes to sqlite, lookup reads from sqlite."""
    from vezir.server import auth
    tok = auth.issue("alice", label="phone")
    assert auth.lookup(tok) == "alice"
    rows = _token_rows()
    assert len(rows) == 1
    assert rows[0]["github"] == "alice"


def test_concurrent_issue_no_lost_update(tmp_data):
    """The race the migration fixes: concurrent issue() must not lose rows."""
    from vezir.server import auth

    # Bypass the conftest auth.issue shim (which races on team creation,
    # unrelated to the token write path under test).
    raw_issue = getattr(auth, "_issue_raw", auth.issue)

    errors: list[Exception] = []

    def worker(n: int) -> None:
        try:
            raw_issue(f"user{n}", label=f"dev{n}")
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent issue raised: {errors}"
    # All 20 tokens persisted — no lost update.
    assert len(_token_rows()) == 20
