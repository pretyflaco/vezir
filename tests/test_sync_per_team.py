"""v0.6.2 per-team sync remote tests (Feature B).

Covers:

* ``_meeting_type_base_for_team`` prefers ``team.sync_meeting_type``
  over the legacy VEZIR_SYNC_MEETING_TYPE env var.
* ``_resolve_team_sync_config`` precedence (0.8.10: real ~/.config/meet
  fallback removed — a remote-less team must not borrow the operator's
  personal placeholder config):
  1. per-team file override (teams/<id>/sync_config.json)
  2. materialized from team.sync_remote
  3. legacy VEZIR_DATA/sync_config.json
  4. None
* ``team_has_sync_target`` mirrors that precedence as a bool gate the worker
  uses to skip sync for remote-less teams.
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
    # No per-team override, no team.sync_remote, no legacy file.
    assert resolved is None


def test_sync_config_ignores_real_user_meet_config(tmp_data, tmp_path):
    """0.8.10: a remote-less team must NOT borrow the operator's personal
    ~/.config/meet/sync_config.json (which often holds a placeholder remote)."""
    from vezir.server import meet_runner, queue
    queue.create_team("twentyone", "Twentyone")  # no sync_remote
    # Simulate the operator's personal millet config (placeholder remote).
    real_meet = tmp_path / "meet"
    real_meet.mkdir()
    (real_meet / "sync_config.json").write_text(
        json.dumps({"repo_url": "https://example.com/global.git"})
    )
    resolved = meet_runner._resolve_team_sync_config("twentyone", real_meet)
    assert resolved is None  # step-4 fallback removed


# ── team_has_sync_target gate ───────────────────────────────────────────────


def test_team_has_sync_target_false_when_no_remote(tmp_data):
    from vezir.server import meet_runner, queue
    queue.create_team("twentyone", "Twentyone")  # no sync_remote
    assert meet_runner.team_has_sync_target("twentyone") is False


def test_team_has_sync_target_true_with_remote(tmp_data):
    from vezir.server import meet_runner, queue
    queue.create_team("blink", "Blink", sync_remote="https://git.example/b.git")
    assert meet_runner.team_has_sync_target("blink") is True


def test_team_has_sync_target_true_with_per_team_override(tmp_data):
    from vezir import config
    from vezir.server import meet_runner, queue
    queue.create_team("twentyone", "Twentyone")  # no sync_remote
    override = config.team_sync_config_path("twentyone")
    config.secure_mkdir(override.parent)
    override.write_text(json.dumps({"repo_url": "https://ops.example/x.git"}))
    assert meet_runner.team_has_sync_target("twentyone") is True


def test_team_has_sync_target_true_with_legacy_global(tmp_data):
    from vezir import config
    from vezir.server import meet_runner, queue
    queue.create_team("twentyone", "Twentyone")  # no sync_remote
    (config.data_dir() / "sync_config.json").write_text(json.dumps({"x": 1}))
    assert meet_runner.team_has_sync_target("twentyone") is True


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


# ── worker skips sync for remote-less teams (0.8.10) ────────────────────────


def test_worker_skips_sync_when_team_has_no_remote(tmp_data, monkeypatch):
    """process_one must NOT call millet sync (and must end `done` with no
    sync_error) when the team has no git remote."""
    from vezir.server import meet_runner, queue, worker

    queue.create_team("twentyone", "Twentyone")  # no sync_remote
    queue.enqueue("01HZ0000000000000NOSYNC01", github="alice", team_id="twentyone")
    job = queue.claim_next()

    # Mock the millet stages so we don't need torch/a real session.
    monkeypatch.setattr(meet_runner, "transcribe", lambda *a, **k: 0)
    monkeypatch.setattr(meet_runner, "label_auto", lambda *a, **k: 0)
    monkeypatch.setattr(meet_runner, "cleanup_home_shim", lambda *a, **k: None)
    monkeypatch.setattr(worker, "_find_artifacts", lambda sd: {"txt": "x.txt"})
    monkeypatch.setattr(worker, "_has_unresolved_speakers", lambda sd: False)

    sync_calls = []
    monkeypatch.setattr(
        meet_runner, "sync",
        lambda *a, **k: sync_calls.append(a) or 0,
    )

    worker.process_one(job)

    assert sync_calls == []  # sync never attempted
    row = queue.get("01HZ0000000000000NOSYNC01")
    assert row["status"] == "done"
    assert not row["sync_error"]


def test_worker_syncs_when_team_has_remote(tmp_data, monkeypatch):
    """Control: a team WITH a remote still invokes millet sync."""
    from vezir.server import meet_runner, queue, worker

    queue.create_team("blink", "Blink", sync_remote="https://git.example/b.git")
    queue.enqueue("01HZ00000000000000SYNC001", github="alice", team_id="blink")
    job = queue.claim_next()

    monkeypatch.setattr(meet_runner, "transcribe", lambda *a, **k: 0)
    monkeypatch.setattr(meet_runner, "label_auto", lambda *a, **k: 0)
    monkeypatch.setattr(meet_runner, "cleanup_home_shim", lambda *a, **k: None)
    monkeypatch.setattr(worker, "_find_artifacts", lambda sd: {"txt": "x.txt"})
    monkeypatch.setattr(worker, "_has_unresolved_speakers", lambda sd: False)
    monkeypatch.setattr(worker, "_sync_log_indicates_failure", lambda lp: None)

    sync_calls = []
    monkeypatch.setattr(
        meet_runner, "sync",
        lambda *a, **k: sync_calls.append(a) or 0,
    )

    worker.process_one(job)

    assert len(sync_calls) == 1  # sync attempted exactly once
    row = queue.get("01HZ00000000000000SYNC001")
    assert row["status"] == "done"


# ── sync_now endpoint refuses remote-less teams (0.8.10) ────────────────────


def _client_and_token(tmp_data):
    from fastapi.testclient import TestClient

    from vezir.server import auth
    from vezir.server.app import create_app

    # Build the app FIRST so startup migrations seed teams with stable UUIDs;
    # issuing/enqueueing before would create a team that migrate_0_7_4 then
    # remaps to a different UUID, orphaning the row.
    client = TestClient(create_app(), follow_redirects=False)
    token = auth.issue("alice", team_id="twentyone")  # shim adds membership
    return client, token


def test_sync_now_409_when_team_has_no_remote(tmp_data, monkeypatch):
    from vezir.server import queue
    client, token = _client_and_token(tmp_data)
    # twentyone exists from the auth shim with no sync_remote.
    queue.enqueue("01HZ0000000000000SYNCNOW1", github="alice", team_id="twentyone")
    queue.update_status("01HZ0000000000000SYNCNOW1", "done")

    r = client.post(
        "/session/01HZ0000000000000SYNCNOW1/sync",
        headers={"Authorization": f"Bearer {token}", "X-Team-Id": "twentyone"},
    )
    assert r.status_code == 409
    assert "no git sync remote" in r.json()["detail"]
    # Status untouched (no failing job queued).
    assert queue.get("01HZ0000000000000SYNCNOW1")["status"] == "done"


def test_sync_now_proceeds_when_team_has_remote(tmp_data, monkeypatch):
    from vezir.server import queue, worker
    client, token = _client_and_token(tmp_data)
    queue.update_team_sync("twentyone", sync_remote="https://git.example/x.git")
    queue.enqueue("01HZ0000000000000SYNCNOW2", github="alice", team_id="twentyone")
    queue.update_status("01HZ0000000000000SYNCNOW2", "done")

    # Don't actually run millet in the background thread.
    monkeypatch.setattr(worker, "finalize_after_labeling", lambda *a, **k: None)

    r = client.post(
        "/session/01HZ0000000000000SYNCNOW2/sync",
        headers={"Authorization": f"Bearer {token}", "X-Team-Id": "twentyone"},
    )
    assert r.status_code == 200
    assert r.json()["queued"] is True
