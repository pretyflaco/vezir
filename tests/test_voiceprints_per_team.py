"""v0.6.2 per-team voiceprint DB tests.

Covers:

* The v0.6.2 migration moves the legacy central DB into
  ``teams/blink/`` and seeds an empty ``teams/twentyone/`` DB
  (per the locked-in user decision: twentyone starts clean).
* ``voiceprints.ensure_db_exists/list_known_names/seed_from`` all
  require an explicit team_id (v0.6.2 signature change).
* The per-job HOME shim symlinks the per-team DB, not a single
  central one — blink and twentyone get different symlink targets.
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
    """Insert blink + twentyone via the queue helper (bypasses migration).

    Tests that need the migration explicitly construct the app via
    TestClient.
    """
    from vezir.server import queue
    queue.create_team("blink", "Blink")
    queue.create_team("twentyone", "Twentyone")


# ── migration (A6) ──────────────────────────────────────────────────────────


def test_migration_moves_legacy_db_into_blink(tmp_data, monkeypatch):
    """Legacy ~/vezir-data/speaker_profiles.json -> teams/blink/."""
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
    from vezir.server import queue
    blink_db = config.team_speaker_profiles_path(queue.get_team("blink")["id"])
    twentyone_db = config.team_speaker_profiles_path(
        queue.get_team("twentyone")["id"]
    )

    # Blink got the legacy content (renamed in place).
    assert blink_db.exists(), "blink per-team voiceprint DB not created"
    data = json.loads(blink_db.read_text(encoding="utf-8"))
    assert "alice" in data
    assert data["alice"]["n_sessions"] == 3

    # Twentyone got seeded empty.
    assert twentyone_db.exists(), "twentyone per-team voiceprint DB not created"
    assert json.loads(twentyone_db.read_text(encoding="utf-8")) == {}

    # Legacy file is gone (moved, not copied).
    assert not legacy.exists()


def test_migration_seeds_empty_db_when_no_legacy(tmp_data, monkeypatch):
    """No legacy DB: blink starts empty, twentyone starts empty."""
    from vezir import config
    config.ensure_dirs()
    # No legacy file pre-seeded.

    from fastapi.testclient import TestClient

    from vezir.server.app import create_app
    TestClient(create_app(), follow_redirects=False)

    from vezir.server import queue
    blink_db = config.team_speaker_profiles_path(queue.get_team("blink")["id"])
    twentyone_db = config.team_speaker_profiles_path(
        queue.get_team("twentyone")["id"]
    )
    assert blink_db.exists() and json.loads(blink_db.read_text(encoding="utf-8")) == {}
    assert twentyone_db.exists() and json.loads(twentyone_db.read_text(encoding="utf-8")) == {}


def test_migration_idempotent(tmp_data, monkeypatch):
    """Running create_app twice does not corrupt blink's DB."""
    from vezir import config
    config.ensure_dirs()
    legacy = config.speaker_profiles_path()
    legacy.write_text(json.dumps({"alice": {"n_sessions": 5}}))

    from fastapi.testclient import TestClient

    from vezir.server.app import create_app
    TestClient(create_app(), follow_redirects=False)
    # Run a second time — should NOT clobber blink's contents.
    TestClient(create_app(), follow_redirects=False)

    from vezir.server import queue
    blink_db = config.team_speaker_profiles_path(queue.get_team("blink")["id"])
    data = json.loads(blink_db.read_text(encoding="utf-8"))
    assert data == {"alice": {"n_sessions": 5}}


# ── voiceprints module signature (A2) ───────────────────────────────────────


def test_ensure_db_exists_requires_team_id(tmp_data):
    from vezir.server import voiceprints
    with pytest.raises(ValueError, match="team_id"):
        voiceprints.ensure_db_exists("")


def test_ensure_db_exists_creates_per_team_file(tmp_data):
    from vezir import config
    from vezir.server import voiceprints
    p = voiceprints.ensure_db_exists("blink")
    assert p == config.team_speaker_profiles_path("blink")
    assert p.exists()
    assert json.loads(p.read_text(encoding="utf-8")) == {}


def test_list_known_names_is_per_team(tmp_data):
    from vezir import config
    from vezir.server import voiceprints

    voiceprints.ensure_db_exists("blink")
    voiceprints.ensure_db_exists("twentyone")
    config.team_speaker_profiles_path("blink").write_text(
        json.dumps({"alice": {}, "bob": {}})
    )
    config.team_speaker_profiles_path("twentyone").write_text(
        json.dumps({"carol": {}})
    )

    assert voiceprints.list_known_names("blink") == ["alice", "bob"]
    assert voiceprints.list_known_names("twentyone") == ["carol"]


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

    stats = voiceprints.seed_from(src, "twentyone")
    assert stats["added"] == 1
    assert stats["total"] == 1
    db = config.team_speaker_profiles_path("twentyone")
    assert json.loads(db.read_text(encoding="utf-8")) == {
        "alice": {"n_sessions": 4}
    }
    # Blink's DB was NOT touched.
    assert not config.team_speaker_profiles_path("blink").exists()


def test_seed_from_refuses_when_populated_without_merge(tmp_data, tmp_path):
    from vezir import config
    from vezir.server import voiceprints

    voiceprints.ensure_db_exists("blink")
    config.team_speaker_profiles_path("blink").write_text(
        json.dumps({"alice": {"n_sessions": 2}})
    )

    src = tmp_path / "src.json"
    src.write_text(json.dumps({"bob": {"n_sessions": 1}}))

    with pytest.raises(FileExistsError):
        voiceprints.seed_from(src, "blink")

    # merge=True works
    stats = voiceprints.seed_from(src, "blink", merge=True)
    assert stats["added"] == 1
    assert stats["total"] == 2


# ── HOME shim symlinks per-team DB (A3) ─────────────────────────────────────


def test_build_home_shim_symlinks_per_team_voiceprints(tmp_data):
    from vezir import config
    from vezir.server import meet_runner

    _seed_two_teams()

    blink_shim = meet_runner.build_home_shim("job-blink", "blink")
    twentyone_shim = meet_runner.build_home_shim("job-21", "twentyone")

    blink_link = blink_shim / ".config" / "meet" / "speaker_profiles.json"
    twentyone_link = twentyone_shim / ".config" / "meet" / "speaker_profiles.json"

    assert blink_link.is_symlink()
    assert twentyone_link.is_symlink()
    assert os.readlink(blink_link) == str(config.team_speaker_profiles_path("blink"))
    assert os.readlink(twentyone_link) == str(
        config.team_speaker_profiles_path("twentyone")
    )
    # Crucially: they point at DIFFERENT files.
    assert os.readlink(blink_link) != os.readlink(twentyone_link)


def test_build_home_shim_requires_team_id(tmp_data):
    from vezir.server import meet_runner
    with pytest.raises(ValueError, match="team_id"):
        meet_runner.build_home_shim("job-1", "")


def test_env_for_meet_uses_per_team_profiles_path(tmp_data):
    from vezir import config
    from vezir.server import meet_runner

    _seed_two_teams()
    home = meet_runner.build_home_shim("job-1", "twentyone")
    env = meet_runner._env_for_meet(home, "twentyone")
    assert env["MEET_PROFILES_PATH"] == str(
        config.team_speaker_profiles_path("twentyone")
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
    blink_uuid = queue.create_team("blink", "Blink")
    voiceprints.ensure_db_exists(blink_uuid)
    from vezir import config
    config.team_speaker_profiles_path(blink_uuid).write_text(
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
        main, ["voiceprints", "seed", "--from", str(src), "--team", "twentyone"],
    )
    assert result.exit_code == 0, result.output

    from vezir.server import queue, voiceprints
    t21_uuid = queue.get_team("twentyone")["id"]
    assert voiceprints.list_known_names(t21_uuid) == ["carol"]
    # Blink untouched.
    assert voiceprints.list_known_names("blink") == []


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
# The "Pattern" incident: a profile built from filler + the scribe's echo had
# to be deleted, and one person enrolled under two names merged — by hand in
# JSON.  These commands do it with an automatic backup.


def _team_with(profiles: dict) -> str:
    from vezir import config
    from vezir.server import queue, voiceprints
    uuid = queue.create_team("blink", "Blink")
    voiceprints.ensure_db_exists(uuid)
    config.team_speaker_profiles_path(uuid).write_text(json.dumps(profiles))
    return uuid


def _db(uuid: str) -> dict:
    from vezir import config
    return json.loads(config.team_speaker_profiles_path(uuid).read_text())


def test_cli_voiceprints_remove_backs_up_and_deletes(tmp_data):
    uuid = _team_with({
        "Pattern": {"embedding": [1.0, 0.0], "n_sessions": 5},
        "Gustavo": {"embedding": [0.0, 1.0], "n_sessions": 40},
    })
    from vezir.cli import main
    result = CliRunner().invoke(main, ["voiceprints", "remove", "Pattern"])
    assert result.exit_code == 0, result.output
    assert set(_db(uuid)) == {"Gustavo"}
    bak = Path(result.output.split("Backup: ")[1].strip())
    assert set(json.loads(bak.read_text())) == {"Pattern", "Gustavo"}
    assert oct(bak.stat().st_mode & 0o777) == "0o600"


def test_cli_voiceprints_remove_unknown_name_fails_cleanly(tmp_data):
    uuid = _team_with({"Gustavo": {"embedding": [0.0, 1.0], "n_sessions": 40}})
    from vezir.cli import main
    result = CliRunner().invoke(main, ["voiceprints", "remove", "Nobody"])
    assert result.exit_code == 2
    assert "no profile named 'Nobody'" in result.output
    assert set(_db(uuid)) == {"Gustavo"}


def test_cli_voiceprints_merge_weights_by_sessions(tmp_data):
    uuid = _team_with({
        "Patrick": {"embedding": [1.0, 0.0], "n_sessions": 3},
        "Patrick van der Meijde": {"embedding": [0.0, 1.0], "n_sessions": 1},
    })
    from vezir.cli import main
    result = CliRunner().invoke(
        main, ["voiceprints", "merge", "Patrick van der Meijde", "Patrick"],
    )
    assert result.exit_code == 0, result.output
    db = _db(uuid)
    assert set(db) == {"Patrick"}
    assert db["Patrick"]["n_sessions"] == 4
    x, y = db["Patrick"]["embedding"]
    # (3*[1,0] + 1*[0,1]) / 4, re-normalized → direction (3, 1)
    assert abs(x / y - 3.0) < 1e-9
    assert abs(x * x + y * y - 1.0) < 1e-9
    assert "Backup:" in result.output


def test_cli_voiceprints_merge_rejects_same_or_missing(tmp_data):
    _team_with({"Patrick": {"embedding": [1.0, 0.0], "n_sessions": 3}})
    from vezir.cli import main
    runner = CliRunner()
    assert runner.invoke(main, ["voiceprints", "merge", "Patrick", "Patrick"]).exit_code == 2
    assert runner.invoke(main, ["voiceprints", "merge", "Nobody", "Patrick"]).exit_code == 2
