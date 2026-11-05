"""v0.6.2 per-team voiceprint DB tests.

Covers:

* The v0.6.2 migration moves a legacy central DB into the single-team
  data's team and seeds nothing on a fresh install.
* ``voiceprints.ensure_db_exists/list_known_names/seed_from`` all
  require an explicit team_id (v0.6.2 signature change).
* The per-job HOME shim symlinks the per-team DB, not a single
  central one — team-a and team-b get different symlink targets.
* ``meet_runner._env_for_meet`` sets ``MEET_PROFILES_PATH`` to the
  per-team DB path.
* CLI ``voiceprints seed/list --team`` round-trip works.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest
from click.testing import CliRunner


@pytest.fixture
def tmp_data(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("VEZIR_DATA", d)
        yield Path(d)


# ── Helpers (per-team CRUD without going through the app factory) ───────────

def _seed_two_teams():
    """Insert team-a + team-b via the queue helper (bypasses migration).

    Tests that need the migration explicitly construct the app via
    TestClient.
    """
    from vezir.server import queue
    queue.create_team("team-a", "Team A")
    queue.create_team("team-b", "Team B")


# ── migration (A6) ──────────────────────────────────────────────────────────


def test_migration_moves_legacy_db_into_legacy_team(tmp_data, monkeypatch):
    """Legacy ~/vezir-data/speaker_profiles.json -> the 'default' team."""
    # Seed the legacy file BEFORE the app starts.
    from vezir import config
    config.ensure_dirs()
    legacy = config.speaker_profiles_path()
    legacy.write_text(json.dumps({"alice": {"n_sessions": 3}}))

    # Trigger migrations via create_app().
    from fastapi.testclient import TestClient

    from vezir.server.app import create_app
    TestClient(create_app(), follow_redirects=False)

    # v0.7.4: per-team dirs keyed by uuid; resolve from slug.
    from vezir.server import migrations, queue
    team = queue.get_team(migrations.LEGACY_TEAM_SLUG)
    assert team is not None, "single-team data needs a team to live in"
    db = config.team_speaker_profiles_path(team["id"])
    assert db.exists(), "legacy voiceprint DB not moved into the team"
    data = json.loads(db.read_text(encoding="utf-8"))
    assert data["alice"]["n_sessions"] == 3

    # Legacy file is gone (moved, not copied); no other team was created.
    assert not legacy.exists()
    assert [t["slug"] for t in queue.list_teams()] == [migrations.LEGACY_TEAM_SLUG]


def test_migration_seeds_nothing_when_no_legacy(tmp_data, monkeypatch):
    """No legacy DB: no team and no voiceprint file is created."""
    from vezir import config
    config.ensure_dirs()

    from fastapi.testclient import TestClient

    from vezir.server.app import create_app
    TestClient(create_app(), follow_redirects=False)

    from vezir.server import queue
    assert queue.list_teams() == []
    assert list(config.teams_dir().glob("*/speaker_profiles.json")) == []


def test_migration_idempotent(tmp_data, monkeypatch):
    """Running create_app twice does not corrupt the moved DB."""
    from vezir import config
    config.ensure_dirs()
    legacy = config.speaker_profiles_path()
    legacy.write_text(json.dumps({"alice": {"n_sessions": 5}}))

    from fastapi.testclient import TestClient

    from vezir.server.app import create_app
    TestClient(create_app(), follow_redirects=False)
    # Run a second time — should NOT clobber the team's contents.
    TestClient(create_app(), follow_redirects=False)

    from vezir.server import migrations, queue
    team = queue.get_team(migrations.LEGACY_TEAM_SLUG)
    data = json.loads(config.team_speaker_profiles_path(team["id"]).read_text(encoding="utf-8"))
    assert data == {"alice": {"n_sessions": 5}}


# ── voiceprints module signature (A2) ───────────────────────────────────────


def test_ensure_db_exists_requires_team_id(tmp_data):
    from vezir.server import voiceprints
    with pytest.raises(ValueError, match="team_id"):
        voiceprints.ensure_db_exists("")


def test_ensure_db_exists_creates_per_team_file(tmp_data):
    from vezir import config
    from vezir.server import voiceprints
    p = voiceprints.ensure_db_exists("team-a")
    assert p == config.team_speaker_profiles_path("team-a")
    assert p.exists()
    assert json.loads(p.read_text(encoding="utf-8")) == {}


def test_list_known_names_is_per_team(tmp_data):
    from vezir import config
    from vezir.server import voiceprints

    voiceprints.ensure_db_exists("team-a")
    voiceprints.ensure_db_exists("team-b")
    config.team_speaker_profiles_path("team-a").write_text(
        json.dumps({"alice": {}, "bob": {}})
    )
    config.team_speaker_profiles_path("team-b").write_text(
        json.dumps({"carol": {}})
    )

    assert voiceprints.list_known_names("team-a") == ["alice", "bob"]
    assert voiceprints.list_known_names("team-b") == ["carol"]


def test_seed_from_requires_team_id(tmp_data, tmp_path):
    src = tmp_path / "src.json"
    src.write_text(json.dumps({"alice": {}}))
    from vezir.server import voiceprints
    with pytest.raises(ValueError, match="team_id"):
        voiceprints.seed_from(src, "")


def test_seed_from_writes_to_team_db(tmp_data, tmp_path):
    src = tmp_path / "src.json"
    src.write_text(json.dumps({"alice": {"n_sessions": 4}}))
    from vezir import config
    from vezir.server import voiceprints

    stats = voiceprints.seed_from(src, "team-b")
    assert stats["added"] == 1
    assert stats["total"] == 1
    db = config.team_speaker_profiles_path("team-b")
    assert json.loads(db.read_text(encoding="utf-8")) == {
        "alice": {"n_sessions": 4}
    }
    # Team A's DB was NOT touched.
    assert not config.team_speaker_profiles_path("team-a").exists()


def test_seed_from_refuses_when_populated_without_merge(tmp_data, tmp_path):
    from vezir import config
    from vezir.server import voiceprints

    voiceprints.ensure_db_exists("team-a")
    config.team_speaker_profiles_path("team-a").write_text(
        json.dumps({"alice": {"n_sessions": 2}})
    )

    src = tmp_path / "src.json"
    src.write_text(json.dumps({"bob": {"n_sessions": 1}}))

    with pytest.raises(FileExistsError):
        voiceprints.seed_from(src, "team-a")

    # merge=True works
    stats = voiceprints.seed_from(src, "team-a", merge=True)
    assert stats["added"] == 1
    assert stats["total"] == 2


# ── HOME shim symlinks per-team DB (A3) ─────────────────────────────────────


def test_build_home_shim_symlinks_per_team_voiceprints(tmp_data):
    from vezir import config
    from vezir.server import meet_runner

    _seed_two_teams()

    team_a_shim = meet_runner.build_home_shim("job-team-a", "team-a")
    team_b_shim = meet_runner.build_home_shim("job-21", "team-b")

    team_a_link = team_a_shim / ".config" / "meet" / "speaker_profiles.json"
    team_b_link = team_b_shim / ".config" / "meet" / "speaker_profiles.json"

    assert team_a_link.is_symlink()
    assert team_b_link.is_symlink()
    assert os.readlink(team_a_link) == str(config.team_speaker_profiles_path("team-a"))
    assert os.readlink(team_b_link) == str(
        config.team_speaker_profiles_path("team-b")
    )
    # Crucially: they point at DIFFERENT files.
    assert os.readlink(team_a_link) != os.readlink(team_b_link)


def test_build_home_shim_requires_team_id(tmp_data):
    from vezir.server import meet_runner
    with pytest.raises(ValueError, match="team_id"):
        meet_runner.build_home_shim("job-1", "")


def test_env_for_meet_uses_per_team_profiles_path(tmp_data):
    from vezir import config
    from vezir.server import meet_runner

    _seed_two_teams()
    home = meet_runner.build_home_shim("job-1", "team-b")
    env = meet_runner._env_for_meet(home, "team-b")
    assert env["MEET_PROFILES_PATH"] == str(
        config.team_speaker_profiles_path("team-b")
    )
    assert env["HOME"] == str(home)


# ── CLI --team flag (A5) ────────────────────────────────────────────────────


def test_cli_voiceprints_list_requires_team_when_multiple(tmp_data, tmp_path):
    _seed_two_teams()
    from vezir.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["voiceprints", "list"])
    assert result.exit_code == 2
    assert "--team is required" in result.output


def test_cli_voiceprints_list_defaults_to_only_team(tmp_data):
    from vezir.server import queue, voiceprints
    team_a_uuid = queue.create_team("team-a", "Team A")
    voiceprints.ensure_db_exists(team_a_uuid)
    from vezir import config
    config.team_speaker_profiles_path(team_a_uuid).write_text(
        json.dumps({"alice": {}})
    )

    from vezir.cli import main
    runner = CliRunner()
    result = runner.invoke(main, ["voiceprints", "list"])
    assert result.exit_code == 0
    assert "alice" in result.output


def test_cli_voiceprints_seed_writes_to_team(tmp_data, tmp_path):
    _seed_two_teams()
    src = tmp_path / "src.json"
    src.write_text(json.dumps({"carol": {"n_sessions": 2}}))

    from vezir.cli import main
    runner = CliRunner()
    result = runner.invoke(
        main, ["voiceprints", "seed", "--from", str(src), "--team", "team-b"],
    )
    assert result.exit_code == 0, result.output

    from vezir.server import queue, voiceprints
    t21_uuid = queue.get_team("team-b")["id"]
    assert voiceprints.list_known_names(t21_uuid) == ["carol"]
    # Team A untouched.
    assert voiceprints.list_known_names("team-a") == []


def test_cli_voiceprints_seed_rejects_unknown_team(tmp_data, tmp_path):
    _seed_two_teams()
    src = tmp_path / "src.json"
    src.write_text(json.dumps({"x": {}}))

    from vezir.cli import main
    runner = CliRunner()
    result = runner.invoke(
        main, ["voiceprints", "seed", "--from", str(src), "--team", "ghost"],
    )
    assert result.exit_code == 2
    assert "not found" in result.output


# ── remove / merge (v0.23.2) ────────────────────────────────────────────────
# The "Peggy" incident: a profile built from filler + the scribe's echo had
# to be deleted, and one person enrolled under two names merged — by hand in
# JSON.  These commands do it with an automatic backup.


def _team_with(profiles: dict) -> str:
    from vezir import config
    from vezir.server import queue, voiceprints
    uuid = queue.create_team("team-a", "Team A")
    voiceprints.ensure_db_exists(uuid)
    config.team_speaker_profiles_path(uuid).write_text(json.dumps(profiles))
    return uuid


def _db(uuid: str) -> dict:
    from vezir import config
    return json.loads(config.team_speaker_profiles_path(uuid).read_text())


def test_cli_voiceprints_remove_backs_up_and_deletes(tmp_data):
    uuid = _team_with({
        "Peggy": {"embedding": [1.0, 0.0], "n_sessions": 5},
        "Rosa": {"embedding": [0.0, 1.0], "n_sessions": 40},
    })
    from vezir.cli import main
    result = CliRunner().invoke(main, ["voiceprints", "remove", "Peggy"])
    assert result.exit_code == 0, result.output
    assert set(_db(uuid)) == {"Rosa"}
    bak = Path(result.output.split("Backup: ")[1].strip())
    assert set(json.loads(bak.read_text())) == {"Peggy", "Rosa"}
    assert oct(bak.stat().st_mode & 0o777) == "0o600"


def test_cli_voiceprints_remove_unknown_name_fails_cleanly(tmp_data):
    uuid = _team_with({"Rosa": {"embedding": [0.0, 1.0], "n_sessions": 40}})
    from vezir.cli import main
    result = CliRunner().invoke(main, ["voiceprints", "remove", "Nobody"])
    assert result.exit_code == 2
    assert "no profile named 'Nobody'" in result.output
    assert set(_db(uuid)) == {"Rosa"}


def test_cli_voiceprints_merge_weights_by_sessions(tmp_data):
    uuid = _team_with({
        "Oscar": {"embedding": [1.0, 0.0], "n_sessions": 3},
        "Oscar van Dam": {"embedding": [0.0, 1.0], "n_sessions": 1},
    })
    from vezir.cli import main
    result = CliRunner().invoke(
        main, ["voiceprints", "merge", "Oscar van Dam", "Oscar"],
    )
    assert result.exit_code == 0, result.output
    db = _db(uuid)
    assert set(db) == {"Oscar"}
    assert db["Oscar"]["n_sessions"] == 4
    x, y = db["Oscar"]["embedding"]
    # (3*[1,0] + 1*[0,1]) / 4, re-normalized → direction (3, 1)
    assert abs(x / y - 3.0) < 1e-9
    assert abs(x * x + y * y - 1.0) < 1e-9
    assert "Backup:" in result.output


def test_cli_voiceprints_merge_rejects_same_or_missing(tmp_data):
    _team_with({"Oscar": {"embedding": [1.0, 0.0], "n_sessions": 3}})
    from vezir.cli import main
    runner = CliRunner()
    assert runner.invoke(main, ["voiceprints", "merge", "Oscar", "Oscar"]).exit_code == 2
    assert runner.invoke(main, ["voiceprints", "merge", "Nobody", "Oscar"]).exit_code == 2
