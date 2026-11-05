"""v0.6.0 / v0.6.2 migrations: fresh install vs. single-team data vs. done.

* A fresh data dir seeds nothing: no teams, rosters or voiceprint files.
* Pre-0.6.0 single-team data (team-less jobs and tokens, ``team.json``, a
  central ``speaker_profiles.json``) all moves into one team,
  ``migrations.LEGACY_TEAM_SLUG``; smoke-test jobs are deleted.
* A data dir where the migrations already ran is left byte-for-byte alone.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def tmp_data(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("VEZIR_DATA", d)
        yield Path(d)


def _db(tmp_data: Path) -> sqlite3.Connection:
    from vezir import config

    c = sqlite3.connect(str(config.queue_db_path()))
    c.row_factory = sqlite3.Row
    return c


def _applied(tmp_data: Path) -> set[str]:
    with _db(tmp_data) as c:
        return {r["version"] for r in c.execute("SELECT version FROM schema_migrations")}


def test_fresh_install_seeds_nothing(tmp_data):
    from vezir import config
    from vezir.server import migrations, queue

    config.ensure_dirs()
    migrations.run_pending_migrations()

    assert queue.list_teams() == []
    assert not any(config.teams_dir().iterdir())
    assert {"0.6.0-multi-team", "0.6.2-per-team-voiceprints"} <= _applied(tmp_data)


def _legacy_data_dir(tmp_data: Path) -> None:
    """Lay out a pre-0.6.0 (single-team) data dir."""
    from vezir import config
    from vezir.server import queue

    config.ensure_dirs()
    with _db(tmp_data) as c:
        c.executescript(queue.SCHEMA)
        for ddl in ("ALTER TABLE jobs ADD COLUMN team_id TEXT NOT NULL DEFAULT ''",):
            try:
                c.execute(ddl)
            except sqlite3.OperationalError:
                pass
        for jid, gh in (("01OLD1", "alice"), ("01OLD2", "bob"),
                        ("01SMOKE", "smoke-upload-test-1")):
            c.execute(
                "INSERT INTO jobs (id, github, status, created_at, updated_at, team_id) "
                "VALUES (?, ?, 'done', 'x', 'x', '')",
                (jid, gh),
            )
        c.commit()
    tokens = [
        {"token_hash": hashlib.sha256(b"a").hexdigest(), "github": "alice",
         "is_admin": True, "label": "laptop", "issued_at": "x"},
        {"token_hash": hashlib.sha256(b"b").hexdigest(), "github": "bob",
         "is_admin": False, "label": None, "issued_at": "x"},
    ]
    config.secure_write_text(config.tokens_json_path(), json.dumps({"tokens": tokens}))
    config.secure_write_text(config.team_json_path(), json.dumps([{"github": "alice"}]))
    config.secure_write_text(
        config.speaker_profiles_path(), json.dumps({"Alice": {"n_sessions": 2}})
    )


def test_single_team_data_moves_into_one_team(tmp_data):
    from vezir import config
    from vezir.server import migrations, queue

    _legacy_data_dir(tmp_data)
    migrations.run_pending_migrations()

    teams = queue.list_teams()
    assert [t["slug"] for t in teams] == [migrations.LEGACY_TEAM_SLUG]
    team_id = teams[0]["id"]

    with _db(tmp_data) as c:
        jobs = {r["id"]: r["team_id"] for r in c.execute("SELECT id, team_id FROM jobs")}
        members = {
            (r["github"], r["role"])
            for r in c.execute("SELECT github, role FROM memberships WHERE team_id = ?",
                               (team_id,))
        }
    assert jobs == {"01OLD1": team_id, "01OLD2": team_id}  # smoke job deleted
    assert members == {("alice", "admin"), ("bob", "scribe")}

    roster = json.loads(config.team_roster_path(team_id).read_text())
    assert roster == [{"github": "alice"}]
    voiceprints = json.loads(config.team_speaker_profiles_path(team_id).read_text())
    assert voiceprints == {"Alice": {"n_sessions": 2}}
    assert not config.team_json_path().exists()
    assert not config.speaker_profiles_path().exists()


def test_only_a_central_voiceprint_db_still_gets_a_team(tmp_data):
    from vezir import config
    from vezir.server import migrations, queue

    config.ensure_dirs()
    config.secure_write_text(config.speaker_profiles_path(), json.dumps({"Bob": {}}))
    migrations.run_pending_migrations()

    team = queue.get_team(migrations.LEGACY_TEAM_SLUG)
    assert team is not None
    assert json.loads(config.team_speaker_profiles_path(team["id"]).read_text()) == {"Bob": {}}


def _snapshot(tmp_data: Path) -> dict:
    """Every row of every table + every file under the data dir."""
    out: dict = {}
    with _db(tmp_data) as c:
        for (name,) in c.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            out[f"table:{name}"] = sorted(
                tuple(r) for r in c.execute(f"SELECT * FROM {name}")  # noqa: S608
            )
    for p in sorted(tmp_data.rglob("*")):
        if p.is_file() and not p.name.startswith("vezir.sqlite") and "logs" not in p.parts:
            out[f"file:{p.relative_to(tmp_data)}"] = p.read_bytes()
    return out


def test_already_migrated_data_dir_is_not_touched(tmp_data):
    """A data dir where the migrations already ran (like any live server):
    its teams, jobs and files survive another startup unchanged."""
    from vezir import config
    from vezir.server import migrations, queue

    config.ensure_dirs()
    migrations.run_pending_migrations()
    queue.create_team("team-a", "Team A")
    queue.add_membership("alice", "team-a", role="admin")
    queue.enqueue("01KEEP", github="alice", title="t", team_id="team-a")
    # Legacy-looking leftovers must not be picked up by a migration that
    # already ran.
    config.secure_write_text(config.speaker_profiles_path(), json.dumps({"X": {}}))
    config.secure_write_text(config.team_json_path(), json.dumps([{"github": "zed"}]))

    before = _snapshot(tmp_data)
    migrations.run_pending_migrations()
    assert _snapshot(tmp_data) == before
