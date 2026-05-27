"""Team lifecycle tests: rename + delete + admin HTTP endpoints.

v0.7.0 changes from v0.6.2:

* ``auth.count_tokens_for_team`` / ``auth.revoke_all_for_team`` removed
  (tokens aren't team-scoped); replaced with membership-based cascade.
* ``queue.delete_team`` cascade now drops memberships + session_teams
  rows instead of revoking tokens.  Tokens survive a team deletion
  (the human may still be on other teams).
* Refusal mode triggers on jobs OR memberships, not jobs OR tokens.
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
        yield Path(d)


def _issue_raw(github, **kwargs):
    """Bypass the shim; raw auth.issue without team_id baked in."""
    from vezir.server import auth
    return auth._issue_raw(github, **kwargs)


# ── update_team_name ────────────────────────────────────────────────────────


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


# ── delete_team ─────────────────────────────────────────────────────────────


def test_delete_team_refuses_when_jobs_exist(tmp_data):
    from vezir.server import queue
    queue.create_team("blink", "Blink")
    queue.enqueue("01J", github="alice", title="t", team_id="blink")
    with pytest.raises(ValueError, match="has 1 job"):
        queue.delete_team("blink")
    assert queue.get_team("blink") is not None


def test_delete_team_refuses_when_members_exist(tmp_data):
    """v0.7.0: refusal mode triggers on memberships (not tokens)."""
    from vezir.server import queue
    queue.create_team("blink", "Blink")
    queue.add_membership("alice", "blink")
    with pytest.raises(ValueError, match="member"):
        queue.delete_team("blink")
    assert queue.get_team("blink") is not None


def test_delete_team_cascade_with_reassign(tmp_data):
    from vezir import config
    from vezir.server import queue

    queue.create_team("blink", "Blink")
    queue.create_team("twentyone", "Twentyone")

    queue.enqueue("01J1", github="alice", title="t", team_id="blink")
    queue.enqueue("01J2", github="bob", title="t", team_id="blink")
    queue.add_membership("alice", "blink")
    queue.add_membership("bob", "blink")

    # Drop a sentinel file in blink's on-disk dir so we can confirm
    # it's removed.
    blink_dir = config.teams_dir() / "blink"
    blink_dir.mkdir(parents=True, exist_ok=True)
    (blink_dir / "roster.json").write_text("[]")

    stats = queue.delete_team("blink", reassign_to="twentyone")
    assert stats["jobs_reassigned"] == 2
    assert stats["members_dropped"] == 2
    assert stats["reassigned_to"] == "twentyone"
    assert stats["on_disk_removed"] is True

    # Team row gone.
    assert queue.get_team("blink") is None
    # Jobs migrated.
    assert queue.get("01J1")["team_id"] == "twentyone"
    assert queue.get("01J2")["team_id"] == "twentyone"
    # Memberships gone for the deleted team; destination team is
    # untouched (its own memberships are owned by its own table).
    assert queue.get_team_members("twentyone") == []
    # On-disk dir removed.
    assert not blink_dir.exists()


def test_delete_team_empty_does_not_require_cascade(tmp_data):
    """A team with no jobs and no members deletes cleanly without cascade."""
    from vezir.server import queue
    queue.create_team("blink", "Blink")
    stats = queue.delete_team("blink")
    assert stats["jobs_reassigned"] == 0
    assert stats["members_dropped"] == 0
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


# ── memberships CRUD (v0.7.0) ───────────────────────────────────────────────


def test_add_membership_creates_row(tmp_data):
    from vezir.server import queue
    queue.create_team("blink", "Blink")
    queue.add_membership("alice", "blink", role="scribe", added_by="test")
    members = queue.get_team_members("blink")
    assert len(members) == 1
    assert members[0]["github"] == "alice"
    assert members[0]["role"] == "scribe"
    assert members[0]["added_by"] == "test"


def test_add_membership_rejects_invalid_role(tmp_data):
    from vezir.server import queue
    queue.create_team("blink", "Blink")
    with pytest.raises(ValueError, match="role"):
        queue.add_membership("alice", "blink", role="owner")


def test_remove_membership_returns_true_when_deleted(tmp_data):
    from vezir.server import queue
    queue.create_team("blink", "Blink")
    queue.add_membership("alice", "blink")
    assert queue.remove_membership("alice", "blink") is True
    assert queue.is_member("alice", "blink") is False


def test_remove_membership_returns_false_when_absent(tmp_data):
    from vezir.server import queue
    queue.create_team("blink", "Blink")
    assert queue.remove_membership("alice", "blink") is False


def test_get_memberships_lists_user_teams(tmp_data):
    from vezir.server import queue
    queue.create_team("blink", "Blink")
    queue.create_team("twentyone", "Twentyone")
    queue.add_membership("alice", "blink", role="scribe")
    queue.add_membership("alice", "twentyone", role="admin")
    mems = queue.get_memberships("alice")
    assert {m["team_id"] for m in mems} == {"blink", "twentyone"}
    by_team = {m["team_id"]: m for m in mems}
    assert by_team["blink"]["role"] == "scribe"
    assert by_team["blink"]["team_name"] == "Blink"
    assert by_team["twentyone"]["role"] == "admin"


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
    from vezir.server import queue
    queue.create_team("blink", "Blink")
    queue.create_team("twentyone", "Twentyone")
    queue.enqueue("01J", github="alice", title="t", team_id="blink")
    queue.add_membership("alice", "blink")

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
    # Memberships on the deleted team are dropped.
    assert queue.get_team_members("twentyone") == []


# ── CLI: team add-member / remove-member / members (v0.7.0) ────────────────


def test_cli_team_add_member(tmp_data):
    from vezir.server import queue
    queue.create_team("blink", "Blink")

    from vezir.cli import main
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["team", "add-member", "--team", "blink",
         "--github", "alice", "--role", "scribe"],
    )
    assert result.exit_code == 0, result.output
    assert queue.is_member("alice", "blink") is True


def test_cli_team_add_member_admin_role(tmp_data):
    from vezir.server import queue
    queue.create_team("blink", "Blink")

    from vezir.cli import main
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["team", "add-member", "--team", "blink",
         "--github", "alice", "--role", "admin"],
    )
    assert result.exit_code == 0, result.output
    assert queue.get_role("alice", "blink") == "admin"


def test_cli_team_remove_member(tmp_data):
    from vezir.server import queue
    queue.create_team("blink", "Blink")
    queue.add_membership("alice", "blink")

    from vezir.cli import main
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["team", "remove-member", "--team", "blink", "--github", "alice"],
    )
    assert result.exit_code == 0, result.output
    assert queue.is_member("alice", "blink") is False


def test_cli_team_members_lists(tmp_data):
    from vezir.server import queue
    queue.create_team("blink", "Blink")
    queue.add_membership("alice", "blink", role="scribe")
    queue.add_membership("bob", "blink", role="admin")

    from vezir.cli import main
    runner = CliRunner()
    result = runner.invoke(main, ["team", "members", "blink"])
    assert result.exit_code == 0, result.output
    assert "alice" in result.output
    assert "bob" in result.output
    assert "admin" in result.output
    assert "scribe" in result.output


# ── Admin HTTP endpoints ────────────────────────────────────────────────────


@pytest.fixture
def client(tmp_data):
    from fastapi.testclient import TestClient

    from vezir.server.app import create_app
    return TestClient(create_app(), follow_redirects=False)


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_admin_patch_renames_team(client):
    from vezir.server import queue
    admin_tok = _issue_raw("admin", is_admin=True)

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
    admin_tok = _issue_raw("admin", is_admin=True)

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
    admin_tok = _issue_raw("admin", is_admin=True)
    queue.create_team("temp", "Temporary")

    r = client.delete("/admin/teams/temp", headers=_bearer(admin_tok))
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] is True
    assert queue.get_team("temp") is None


def test_admin_delete_team_refuses_non_empty(client):
    from vezir.server import queue
    admin_tok = _issue_raw("admin", is_admin=True)
    queue.enqueue("01J", github="alice", title="t", team_id="twentyone")

    r = client.delete("/admin/teams/twentyone", headers=_bearer(admin_tok))
    assert r.status_code == 409
    assert "job" in r.text.lower()
    assert queue.get_team("twentyone") is not None


def test_admin_delete_team_cascade(client):
    from vezir.server import queue
    admin_tok = _issue_raw("admin", is_admin=True)
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
    scribe_tok = _issue_raw("alice")
    queue.create_team("temp", "Temporary")
    r = client.delete("/admin/teams/temp", headers=_bearer(scribe_tok))
    assert r.status_code == 403
    assert queue.get_team("temp") is not None


# ── Admin HTTP endpoints: memberships (v0.7.0) ─────────────────────────────


def test_admin_add_member_endpoint(client):
    from vezir.server import queue
    admin_tok = _issue_raw("admin", is_admin=True)

    r = client.post(
        "/admin/teams/blink/members",
        headers=_bearer(admin_tok),
        json={"github": "alice", "role": "scribe"},
    )
    assert r.status_code == 200, r.text
    assert queue.is_member("alice", "blink") is True


def test_admin_add_member_rejects_invalid_role(client):
    admin_tok = _issue_raw("admin", is_admin=True)
    r = client.post(
        "/admin/teams/blink/members",
        headers=_bearer(admin_tok),
        json={"github": "alice", "role": "owner"},
    )
    assert r.status_code == 400


def test_admin_add_member_unknown_team_404(client):
    admin_tok = _issue_raw("admin", is_admin=True)
    r = client.post(
        "/admin/teams/ghost/members",
        headers=_bearer(admin_tok),
        json={"github": "alice"},
    )
    assert r.status_code == 404


def test_admin_remove_member_endpoint(client):
    from vezir.server import queue
    admin_tok = _issue_raw("admin", is_admin=True)
    queue.add_membership("alice", "blink")

    r = client.delete(
        "/admin/teams/blink/members/alice",
        headers=_bearer(admin_tok),
    )
    assert r.status_code == 200, r.text
    assert queue.is_member("alice", "blink") is False


def test_admin_remove_member_idempotent_404(client):
    """Removing a non-member returns 404 (idempotent from operator POV)."""
    admin_tok = _issue_raw("admin", is_admin=True)
    r = client.delete(
        "/admin/teams/blink/members/ghost",
        headers=_bearer(admin_tok),
    )
    assert r.status_code == 404


def test_admin_list_members_endpoint(client):
    from vezir.server import queue
    admin_tok = _issue_raw("admin", is_admin=True)
    queue.add_membership("alice", "blink", role="scribe")
    queue.add_membership("bob", "blink", role="admin")

    r = client.get(
        "/admin/teams/blink/members",
        headers=_bearer(admin_tok),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["team_id"] == "blink"
    by_github = {m["github"]: m for m in body["members"]}
    assert by_github["alice"]["role"] == "scribe"
    assert by_github["bob"]["role"] == "admin"


def test_admin_membership_endpoints_require_admin(client):
    scribe_tok = _issue_raw("alice")
    r = client.post(
        "/admin/teams/blink/members",
        headers=_bearer(scribe_tok),
        json={"github": "alice"},
    )
    assert r.status_code == 403
