"""Tests for v0.7.4 UUID team keys + slug rename."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def tmp_data(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("VEZIR_DATA", d)
        yield Path(d)


def test_create_team_returns_uuid_not_slug(tmp_data):
    from vezir.server import queue
    uuid = queue.create_team("team-a", "Team A")
    assert uuid != "team-a"
    assert len(uuid) == 32  # uuid4().hex
    row = queue.get_team("team-a")
    assert row["id"] == uuid
    assert row["slug"] == "team-a"


def test_resolve_team_uuid_accepts_slug_and_uuid(tmp_data):
    from vezir.server import queue
    uuid = queue.create_team("team-a", "Team A")
    assert queue.resolve_team_uuid("team-a") == uuid
    assert queue.resolve_team_uuid(uuid) == uuid
    assert queue.resolve_team_uuid("ghost") is None


def test_rename_slug_preserves_uuid_and_data(tmp_data):
    from vezir.server import queue
    uuid = queue.create_team("team-a", "Team A")
    queue.enqueue("01J", github="alice", title="t", team_id="team-a")
    queue.add_membership("alice", "team-a", role="scribe")

    queue.rename_team_slug("team-a", "renamed-team")

    # uuid unchanged; slug updated.
    row = queue.get_team("renamed-team")
    assert row["id"] == uuid
    assert row["slug"] == "renamed-team"
    # Old slug no longer resolves.
    assert queue.get_team("team-a") is None
    # Job + membership still attached (keyed by uuid, untouched).
    assert queue.get("01J")["team_id"] == uuid
    assert queue.is_member("alice", uuid)
    assert queue.is_member("alice", "renamed-team")  # resolves new slug


def test_rename_rejects_collision(tmp_data):
    from vezir.server import queue
    queue.create_team("team-a", "Team A")
    queue.create_team("team-b", "Team B")
    with pytest.raises(ValueError, match="already in use"):
        queue.rename_team_slug("team-a", "team-b")


def test_rename_rejects_bad_slug(tmp_data):
    from vezir.server import queue
    queue.create_team("team-a", "Team A")
    with pytest.raises(ValueError):
        queue.rename_team_slug("team-a", "Bad Slug")


def test_rename_unknown_team(tmp_data):
    from vezir.server import queue
    with pytest.raises(ValueError, match="does not exist"):
        queue.rename_team_slug("ghost", "newslug")


def test_migration_assigns_uuid_and_rewrites_fks(tmp_data):
    """The 0.7.4 migration converts a legacy slug-keyed team to uuid."""
    import sqlite3

    from vezir import config
    from vezir.server import migrations

    # Simulate a pre-0.7.4 DB: team keyed by slug, jobs/memberships too.
    config.ensure_dirs()
    conn = sqlite3.connect(str(config.queue_db_path()))
    try:
        conn.executescript(
            "CREATE TABLE teams (id TEXT PRIMARY KEY, slug TEXT, name TEXT NOT NULL,"
            " sync_remote TEXT, sync_meeting_type TEXT DEFAULT 'sandbox',"
            " created_at TEXT NOT NULL);"
            "CREATE TABLE jobs (id TEXT PRIMARY KEY, github TEXT, team_id TEXT,"
            " status TEXT, created_at TEXT, updated_at TEXT);"
            "CREATE TABLE memberships (github TEXT, team_id TEXT, role TEXT,"
            " added_at TEXT, added_by TEXT, PRIMARY KEY (github, team_id));"
            "CREATE TABLE session_teams (session_id TEXT, team_id TEXT,"
            " PRIMARY KEY (session_id, team_id));"
        )
        conn.execute(
            "INSERT INTO teams (id, name, created_at) VALUES ('team-a','Team A','x')"
        )
        conn.execute(
            "INSERT INTO jobs (id, github, team_id, status, created_at, updated_at)"
            " VALUES ('01J','alice','team-a','queued','x','x')"
        )
        conn.execute(
            "INSERT INTO memberships (github, team_id, role, added_at)"
            " VALUES ('alice','team-a','scribe','x')"
        )
        conn.commit()
    finally:
        conn.close()

    # Place a legacy on-disk team dir at the slug path.
    (config.teams_dir() / "team-a").mkdir(parents=True, exist_ok=True)
    (config.teams_dir() / "team-a" / "roster.json").write_text("[]")

    summary = migrations.migrate_0_7_4()
    assert summary["teams"]["teams_remapped"] == 1
    assert summary["teams"]["dirs_renamed"] == 1

    from vezir.server import queue
    row = queue.get_team("team-a")  # resolves by slug
    assert row is not None
    uuid = row["id"]
    assert uuid != "team-a"
    # FKs rewritten to uuid.
    assert queue.get("01J")["team_id"] == uuid
    assert queue.is_member("alice", uuid)
    # On-disk dir renamed.
    assert (config.teams_dir() / uuid).is_dir()
    assert not (config.teams_dir() / "team-a").exists()


def test_rename_via_cli(tmp_data):
    from click.testing import CliRunner

    from vezir.cli import main
    from vezir.server import queue
    uuid = queue.create_team("team-a", "Team A")

    runner = CliRunner()
    result = runner.invoke(
        main, ["team", "rename", "--id", "team-a", "--new-slug", "renamed-team"],
    )
    assert result.exit_code == 0, result.output
    assert queue.get_team("renamed-team")["id"] == uuid
