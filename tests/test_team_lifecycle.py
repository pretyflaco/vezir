"""v0.6.2 team lifecycle tests: rename (display name) + delete (Feature D).

Covers:

* ``queue.update_team_name`` happy + validation paths.
* ``queue.delete_team`` policy:
  - refuses when jobs exist and ``reassign_to`` is None
  - refuses when tokens exist and ``reassign_to`` is None
  - cascade with ``reassign_to``: jobs moved, tokens REVOKED (not migrated)
  - removes the on-disk teams/<id>/ dir
  - rejects reassign_to=<self>
  - rejects unknown reassign_to slug
* ``auth.count_tokens_for_team`` + ``auth.revoke_all_for_team``.
* CLI ``vezir team set-name`` + ``vezir team delete``.
* Admin HTTP DELETE /admin/teams/{id} endpoint.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from click.testing import CliRunner


@pytest.fixture
def tmp_data(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("VEZIR_DATA", d)
        from vezir.server import web_sessions
        web_sessions._reset_for_tests()
        yield Path(d)


def _issue_raw(github, team_id, **kwargs):
    from vezir.server import auth
    return auth._issue_raw(github, team_id=team_id, **kwargs)


# ── update_team_name (D1) ───────────────────────────────────────────────────


def test_update_team_name_happy(tmp_data):
    from vezir.server import queue
    queue.create_team("blink", "Blink")
    queue.update_team_name("blink", "Blink Inc.")
    assert queue.get_team("blink")["name"] == "Blink Inc."


def test_update_team_name_rejects_empty(tmp_data):
    from vezir.server import queue
    queue.create_team("blink", "Blink")
    with pytest.raises(ValueError, match="non-empty"):
        queue.update_team_name("blink", "")
    with pytest.raises(ValueError, match="non-empty"):
        queue.update_team_name("blink", "   ")


def test_update_team_name_rejects_unknown_team(tmp_data):
    from vezir.server import queue
    with pytest.raises(ValueError, match="does not exist"):
        queue.update_team_name("ghost", "Ghost Co.")


def test_update_team_name_strips_whitespace(tmp_data):
    from vezir.server import queue
    queue.create_team("blink", "Blink")
    queue.update_team_name("blink", "  Padded Name  ")
    assert queue.get_team("blink")["name"] == "Padded Name"


# ── auth helpers (D1b) ──────────────────────────────────────────────────────


def test_count_tokens_for_team(tmp_data):
    from vezir.server import auth
    _issue_raw("alice", team_id="blink")
    _issue_raw("bob", team_id="blink")
    _issue_raw("carol", team_id="twentyone")
    assert auth.count_tokens_for_team("blink") == 2
    assert auth.count_tokens_for_team("twentyone") == 1
    assert auth.count_tokens_for_team("ghost") == 0
    assert auth.count_tokens_for_team("") == 0


def test_revoke_all_for_team(tmp_data):
    from vezir.server import auth
    _issue_raw("alice", team_id="blink")
    _issue_raw("bob", team_id="blink")
    _issue_raw("carol", team_id="twentyone")

    n = auth.revoke_all_for_team("blink")
    assert n == 2
    assert auth.count_tokens_for_team("blink") == 0
    # twentyone untouched.
    assert auth.count_tokens_for_team("twentyone") == 1


# ── delete_team (D1a) ───────────────────────────────────────────────────────


def test_delete_team_refuses_when_jobs_exist(tmp_data):
    from vezir.server import queue
    queue.create_team("blink", "Blink")
    queue.enqueue("01J", github="alice", title="t", team_id="blink")
    with pytest.raises(ValueError, match="has 1 job"):
        queue.delete_team("blink")
    # Row still exists.
    assert queue.get_team("blink") is not None


def test_delete_team_refuses_when_tokens_exist(tmp_data):
    from vezir.server import queue
    queue.create_team("blink", "Blink")
    _issue_raw("alice", team_id="blink")
    with pytest.raises(ValueError, match="token"):
        queue.delete_team("blink")
    assert queue.get_team("blink") is not None


def test_delete_team_cascade_with_reassign(tmp_data):
    from vezir import config
    from vezir.server import auth, queue

    queue.create_team("blink", "Blink")
    queue.create_team("twentyone", "Twentyone")

    queue.enqueue("01J1", github="alice", title="t", team_id="blink")
    queue.enqueue("01J2", github="bob", title="t", team_id="blink")
    _issue_raw("alice", team_id="blink")
    _issue_raw("bob", team_id="blink")

    # Drop a sentinel file in blink's on-disk dir so we can confirm
    # it's removed.
    blink_dir = config.teams_dir() / "blink"
    blink_dir.mkdir(parents=True, exist_ok=True)
    (blink_dir / "roster.json").write_text("[]")

    stats = queue.delete_team("blink", reassign_to="twentyone")
    assert stats["jobs_reassigned"] == 2
    assert stats["tokens_revoked"] == 2
    assert stats["reassigned_to"] == "twentyone"
    assert stats["on_disk_removed"] is True

    # Team row gone.
    assert queue.get_team("blink") is None
    # Jobs migrated.
    assert queue.get("01J1")["team_id"] == "twentyone"
    assert queue.get("01J2")["team_id"] == "twentyone"
    # Tokens revoked (NOT migrated — security-conscious default).
    assert auth.count_tokens_for_team("twentyone") == 0
    # On-disk dir removed.
    assert not blink_dir.exists()


def test_delete_team_empty_does_not_require_cascade(tmp_data):
    """A team with no jobs and no tokens deletes cleanly without cascade."""
    from vezir.server import queue
    queue.create_team("blink", "Blink")
    stats = queue.delete_team("blink")
    assert stats["jobs_reassigned"] == 0
    assert stats["tokens_revoked"] == 0
    assert queue.get_team("blink") is None


def test_delete_team_rejects_reassign_to_self(tmp_data):
    from vezir.server import queue
    queue.create_team("blink", "Blink")
    with pytest.raises(ValueError, match="different team"):
        queue.delete_team("blink", reassign_to="blink")


def test_delete_team_rejects_unknown_reassign_to(tmp_data):
    from vezir.server import queue
    queue.create_team("blink", "Blink")
    queue.enqueue("01J", github="alice", title="t", team_id="blink")
    with pytest.raises(ValueError, match="reassign-to team"):
        queue.delete_team("blink", reassign_to="ghost")


def test_delete_team_rejects_unknown_slug(tmp_data):
    from vezir.server import queue
    with pytest.raises(ValueError, match="does not exist"):
        queue.delete_team("ghost")


# ── CLI: team set-name ──────────────────────────────────────────────────────


def test_cli_team_set_name(tmp_data):
    from vezir.server import queue
    queue.create_team("blink", "Blink")

    from vezir.cli import main
    runner = CliRunner()
    result = runner.invoke(
        main, ["team", "set-name", "--id", "blink", "--name", "Blink Inc."],
    )
    assert result.exit_code == 0, result.output
    assert queue.get_team("blink")["name"] == "Blink Inc."


def test_cli_team_set_name_rejects_unknown(tmp_data):
    from vezir.cli import main
    runner = CliRunner()
    result = runner.invoke(
        main, ["team", "set-name", "--id", "ghost", "--name", "X"],
    )
    assert result.exit_code == 2
    assert "does not exist" in result.output


# ── CLI: team delete ────────────────────────────────────────────────────────


def test_cli_team_delete_refuses_without_cascade(tmp_data):
    from vezir.server import queue
    queue.create_team("blink", "Blink")
    queue.enqueue("01J", github="alice", title="t", team_id="blink")

    from vezir.cli import main
    runner = CliRunner()
    result = runner.invoke(
        main, ["team", "delete", "--id", "blink", "--yes"],
    )
    assert result.exit_code == 2
    assert "has 1 job" in result.output
    assert queue.get_team("blink") is not None


def test_cli_team_delete_with_reassign(tmp_data):
    from vezir.server import auth, queue
    queue.create_team("blink", "Blink")
    queue.create_team("twentyone", "Twentyone")
    queue.enqueue("01J", github="alice", title="t", team_id="blink")
    _issue_raw("alice", team_id="blink")

    from vezir.cli import main
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["team", "delete", "--id", "blink",
         "--reassign-to", "twentyone", "--yes"],
    )
    assert result.exit_code == 0, result.output
    assert queue.get_team("blink") is None
    assert queue.get("01J")["team_id"] == "twentyone"
    assert auth.count_tokens_for_team("blink") == 0


# ── Admin HTTP endpoints (D1c) ──────────────────────────────────────────────


@pytest.fixture
def client(tmp_data):
    from fastapi.testclient import TestClient

    from vezir.server.app import create_app
    return TestClient(create_app(), follow_redirects=False)


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_admin_patch_renames_team(client):
    from vezir.server import queue
    admin_tok = _issue_raw("admin", team_id="blink", is_admin=True)

    r = client.patch(
        "/admin/teams/blink",
        headers=_bearer(admin_tok),
        json={"name": "Blink Inc."},
    )
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Blink Inc."
    assert queue.get_team("blink")["name"] == "Blink Inc."


def test_admin_patch_combined_name_and_sync(client):
    from vezir.server import queue
    admin_tok = _issue_raw("admin", team_id="blink", is_admin=True)

    r = client.patch(
        "/admin/teams/blink",
        headers=_bearer(admin_tok),
        json={
            "name": "Blink Inc.",
            "sync_remote": "https://git.example/blink.git",
            "sync_meeting_type": "prod",
        },
    )
    assert r.status_code == 200, r.text
    row = queue.get_team("blink")
    assert row["name"] == "Blink Inc."
    assert row["sync_remote"] == "https://git.example/blink.git"
    assert row["sync_meeting_type"] == "prod"


def test_admin_delete_team_empty(client):
    from vezir.server import queue
    admin_tok = _issue_raw("admin", team_id="blink", is_admin=True)
    queue.create_team("temp", "Temporary")

    r = client.delete("/admin/teams/temp", headers=_bearer(admin_tok))
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] is True
    assert queue.get_team("temp") is None


def test_admin_delete_team_refuses_non_empty(client):
    from vezir.server import queue
    admin_tok = _issue_raw("admin", team_id="blink", is_admin=True)
    queue.enqueue("01J", github="alice", title="t", team_id="twentyone")

    r = client.delete("/admin/teams/twentyone", headers=_bearer(admin_tok))
    assert r.status_code == 409
    assert "job" in r.text.lower()
    assert queue.get_team("twentyone") is not None


def test_admin_delete_team_cascade(client):
    from vezir.server import queue
    admin_tok = _issue_raw("admin", team_id="blink", is_admin=True)
    queue.enqueue("01J", github="alice", title="t", team_id="twentyone")

    r = client.delete(
        "/admin/teams/twentyone?reassign_to=blink",
        headers=_bearer(admin_tok),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deleted"] is True
    assert body["jobs_reassigned"] == 1
    assert body["reassigned_to"] == "blink"
    assert queue.get_team("twentyone") is None
    assert queue.get("01J")["team_id"] == "blink"


def test_admin_delete_team_requires_admin(client):
    from vezir.server import queue
    scribe_tok = _issue_raw("alice", team_id="blink")
    queue.create_team("temp", "Temporary")
    r = client.delete("/admin/teams/temp", headers=_bearer(scribe_tok))
    assert r.status_code == 403
    assert queue.get_team("temp") is not None
