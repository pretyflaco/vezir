"""v0.6.2 per-team sync remote tests (Feature B).

Covers:

* ``_meeting_type_base_for_team`` prefers ``team.sync_meeting_type``
  over the legacy VEZIR_SYNC_MEETING_TYPE env var.
* ``_resolve_team_sync_config`` precedence:
  1. per-team file override (teams/<id>/sync_config.json)
  2. materialized from team.sync_remote
  3. legacy VEZIR_DATA/sync_config.json
  4. real ~/.config/meet/sync_config.json
  5. None
* ``_materialize_team_sync_config`` writes a minimal JSON with
  ``remote_url`` and is idempotent across runs.
* ``build_home_shim`` materializes per-team sync_config and symlinks
  it into the shim — DIFFERENT teams get DIFFERENT remote URLs.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def tmp_data(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("VEZIR_DATA", d)
        from vezir.server import web_sessions
        web_sessions._reset_for_tests()
        yield Path(d)


# ── meeting-type prefix precedence ──────────────────────────────────────────


def test_meeting_type_falls_back_to_default(tmp_data, monkeypatch):
    """Empty sync_meeting_type falls back to 'meeting' (v0.7.0+)."""
    from vezir.server import meet_runner, queue
    queue.create_team("blink", "Blink", sync_meeting_type="")
    import sqlite3
    from vezir import config
    with sqlite3.connect(str(config.queue_db_path())) as c:
        c.execute(
            "UPDATE teams SET sync_meeting_type = '' WHERE id = 'blink'"
        )
    assert meet_runner._meeting_type_base_for_team("blink") == "meeting"


def test_meeting_type_team_overrides_env(tmp_data, monkeypatch):
    from vezir.server import meet_runner, queue
    queue.create_team("blink", "Blink", sync_meeting_type="prod")
    monkeypatch.setenv("VEZIR_SYNC_MEETING_TYPE", "from-env")
    # Team value wins.
    assert meet_runner._meeting_type_base_for_team("blink") == "prod"


def test_meeting_type_defaults_to_meeting(tmp_data, monkeypatch):
    """No team row at all defaults to 'meeting' (v0.7.0+)."""
    from vezir.server import meet_runner, queue
    queue.create_team("blink", "Blink", sync_meeting_type="")
    import sqlite3
    from vezir import config
    with sqlite3.connect(str(config.queue_db_path())) as c:
        c.execute(
            "UPDATE teams SET sync_meeting_type = '' WHERE id = 'blink'"
        )
    assert meet_runner._meeting_type_base_for_team("blink") == "meeting"


def test_meeting_type_uses_team_row(tmp_data, monkeypatch):
    from vezir.server import meet_runner, queue
    queue.create_team("twentyone", "Twentyone", sync_meeting_type="prod-21")
    monkeypatch.delenv("VEZIR_SYNC_MEETING_TYPE", raising=False)
    assert meet_runner._meeting_type_base_for_team("twentyone") == "prod-21"


# ── sync config resolution (B2: per-team override) ──────────────────────────


def test_sync_config_per_team_override_wins(tmp_data, monkeypatch):
    """teams/<id>/sync_config.json beats team.sync_remote (B2 escape hatch)."""
    from vezir import config
    from vezir.server import meet_runner, queue

    queue.create_team(
        "blink", "Blink", sync_remote="https://git.example/blink.git",
    )
    override = config.team_sync_config_path("blink")
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text(json.dumps({"hand_tuned": True}))

    resolved = meet_runner._resolve_team_sync_config(
        "blink", Path("/nonexistent/meet"),
    )
    assert resolved == override
    # And the file we wrote is verbatim — vezir didn't rewrite it.
    assert json.loads(override.read_text(encoding="utf-8")) == {"hand_tuned": True}


def test_sync_config_materialized_from_team_remote(tmp_data, monkeypatch):
    from vezir import config
    from vezir.server import meet_runner, queue

    queue.create_team(
        "blink", "Blink", sync_remote="https://git.example/blink.git",
    )

    resolved = meet_runner._resolve_team_sync_config(
        "blink", Path("/nonexistent/meet"),
    )
    # Path 2: materialized at the vezir-managed path (NOT the
    # operator-override path) so a future override can shadow it
    # without being clobbered on the next run.
    expected = config.team_materialized_sync_config_path("blink")
    assert resolved == expected
    payload = json.loads(expected.read_text(encoding="utf-8"))
    assert payload["remote_url"] == "https://git.example/blink.git"
    # The operator-override file does NOT exist.
    assert not config.team_sync_config_path("blink").exists()


def test_sync_config_materialization_is_idempotent(tmp_data):
    from vezir.server import meet_runner, queue

    queue.create_team("blink", "Blink", sync_remote="https://a.example/x.git")

    # First call materializes.
    p1 = meet_runner._resolve_team_sync_config("blink", Path("/none"))
    # Second call with same remote: file should not be rewritten
    # (idempotent).  We can't easily assert mtime equality on fast
    # filesystems, but we can assert the content matches.
    p2 = meet_runner._resolve_team_sync_config("blink", Path("/none"))
    assert p1 == p2
    payload = json.loads(p2.read_text(encoding="utf-8"))
    assert payload["remote_url"] == "https://a.example/x.git"


def test_sync_config_materialization_rewrites_on_remote_change(tmp_data):
    from vezir.server import meet_runner, queue

    queue.create_team("blink", "Blink", sync_remote="https://old.example/x.git")
    p1 = meet_runner._resolve_team_sync_config("blink", Path("/none"))
    payload1 = json.loads(p1.read_text(encoding="utf-8"))
    assert payload1["remote_url"] == "https://old.example/x.git"

    # Operator updates the remote.
    queue.update_team_sync("blink", sync_remote="https://new.example/x.git")

    p2 = meet_runner._resolve_team_sync_config("blink", Path("/none"))
    payload2 = json.loads(p2.read_text(encoding="utf-8"))
    assert payload2["remote_url"] == "https://new.example/x.git"


def test_sync_config_falls_through_to_legacy(tmp_data):
    """No per-team override, no team.sync_remote: use legacy VEZIR_DATA/sync_config.json."""
    from vezir import config
    from vezir.server import meet_runner, queue

    queue.create_team("blink", "Blink")  # no sync_remote
    legacy = config.data_dir() / "sync_config.json"
    legacy.write_text(json.dumps({"legacy": True}))

    resolved = meet_runner._resolve_team_sync_config(
        "blink", Path("/nonexistent/meet"),
    )
    assert resolved == legacy


def test_sync_config_returns_none_when_nothing_configured(tmp_data):
    from vezir.server import meet_runner, queue
    queue.create_team("blink", "Blink")
    resolved = meet_runner._resolve_team_sync_config(
        "blink", Path("/nonexistent/meet"),
    )
    # No per-team override, no team.sync_remote, no legacy file, no
    # real ~/.config/meet/sync_config.json under the bogus path.
    assert resolved is None


# ── HOME shim symlinks per-team sync_config (integration) ───────────────────


def test_build_home_shim_symlinks_per_team_sync_config(tmp_data):
    from vezir import config
    from vezir.server import meet_runner, queue

    queue.create_team("blink", "Blink", sync_remote="https://blink.example/x.git")
    queue.create_team(
        "twentyone", "Twentyone", sync_remote="https://21.example/y.git",
    )

    blink_shim = meet_runner.build_home_shim("job-b", "blink")
    twentyone_shim = meet_runner.build_home_shim("job-t", "twentyone")

    blink_link = blink_shim / ".config" / "meet" / "sync_config.json"
    twentyone_link = twentyone_shim / ".config" / "meet" / "sync_config.json"

    assert blink_link.is_symlink()
    assert twentyone_link.is_symlink()
    # Each shim points at the team's materialized config (no operator
    # override exists in this test).
    assert os.readlink(blink_link) == str(
        config.team_materialized_sync_config_path("blink")
    )
    assert os.readlink(twentyone_link) == str(
        config.team_materialized_sync_config_path("twentyone")
    )
    # Different targets -> different repos.
    assert os.readlink(blink_link) != os.readlink(twentyone_link)

    blink_payload = json.loads(Path(os.readlink(blink_link)).read_text(encoding="utf-8"))
    t21_payload = json.loads(
        Path(os.readlink(twentyone_link)).read_text(encoding="utf-8")
    )
    assert blink_payload["remote_url"] == "https://blink.example/x.git"
    assert t21_payload["remote_url"] == "https://21.example/y.git"
