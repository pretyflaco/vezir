"""v0.6.2 ``vezir session move`` tests (Feature C).

Covers:

* ``queue.set_job_team`` with the new ``require_team_exists=True``
  default refuses unknown destination slugs (closes the v0.6.0
  silent-orphan hole).
* Backfill path (``require_team_exists=False``) still works for the
  v0.6.0 migration shape.
* End-to-end: a session moved from team-a -> team-b becomes
  invisible to a team-a token (404) and visible to a team-b token.
* CLI ``vezir session move`` happy path + unknown-dest error.
* Voiceprint backwash: moving a session does NOT touch either team's
  voiceprint DB (documented limitation).
"""
from __future__ import annotations

import io
import json
import tempfile
import wave
from pathlib import Path

import pytest
from click.testing import CliRunner


@pytest.fixture
def tmp_data(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("VEZIR_DATA", d)
        yield Path(d)


@pytest.fixture
def client(tmp_data):
    from fastapi.testclient import TestClient

    from vezir.server.app import create_app
    return TestClient(create_app(), follow_redirects=False)


def _bearer(token: str, team: str = "team-a") -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "X-Team-Id": team,
    }


def _tiny_wav() -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 16000)
    return buf.getvalue()


def _issue_raw(github, team_id, **kwargs):
    """Issue a token AND add a membership for ``github`` in ``team_id``.

    v0.7.0: tokens are no longer team-scoped, but the test still wants
    a token whose handle is a member of the named team so X-Team-Id
    based requests validate.
    """
    from vezir.server import auth, queue
    if queue.get_team(team_id) is None:
        queue.create_team(team_id, team_id.capitalize())
    role = "admin" if kwargs.get("is_admin") else "scribe"
    queue.add_membership(github, team_id, role=role, added_by="test")
    return auth._issue_raw(github, **kwargs)


# ── queue.set_job_team (C1) ─────────────────────────────────────────────────


def test_set_job_team_refuses_unknown_destination(tmp_data):
    from vezir.server import queue
    queue.create_team("team-a", "Team A")
    queue.enqueue("01TEST", github="alice", title="t", team_id="team-a")

    with pytest.raises(ValueError, match="does not exist"):
        queue.set_job_team("01TEST", "ghost")

    # Original team_id preserved (stored as team-a's uuid).
    assert queue.get("01TEST")["team_id"] == queue.get_team("team-a")["id"]


def test_set_job_team_happy_path(tmp_data):
    from vezir.server import queue
    queue.create_team("team-a", "Team A")
    queue.create_team("team-b", "Team B")
    queue.enqueue("01TEST", github="alice", title="t", team_id="team-a")

    queue.set_job_team("01TEST", "team-b")
    # v0.7.4: jobs store the team's uuid, not the slug.
    assert queue.get("01TEST")["team_id"] == queue.get_team("team-b")["id"]


def test_set_job_team_backfill_mode_skips_existence_check(tmp_data):
    """The v0.6.0 migration uses require_team_exists=False to insert
    rows for teams that are being created in the same transaction."""
    from vezir.server import queue
    queue.enqueue("01TEST", github="alice", title="t", team_id="team-a")
    # 'newteam' does NOT exist; backfill mode allows it anyway.
    queue.set_job_team("01TEST", "newteam", require_team_exists=False)
    assert queue.get("01TEST")["team_id"] == "newteam"


# ── End-to-end: visibility flips after move ─────────────────────────────────


def test_moved_session_invisible_to_old_team(client):
    """team-a uploads, then session moves to team-b -- team-a scope
    can no longer see it; team-b scope can.
    """
    from vezir.server import queue

    team_a_tok = _issue_raw("alice", team_id="team-a")
    t21_tok = _issue_raw("bob", team_id="team-b")

    # Upload as team-a (X-Team-Id: team-a).
    resp = client.post(
        "/upload",
        headers=_bearer(team_a_tok, team="team-a"),
        files={"audio": ("x.wav", _tiny_wav(), "audio/wav")},
    )
    assert resp.status_code == 200
    sid = resp.json()["session_id"]
    assert queue.get(sid)["team_id"] == queue.get_team("team-a")["id"]

    # team-a scope can see it.
    r1 = client.get(
        f"/api/sessions/{sid}", headers=_bearer(team_a_tok, team="team-a"),
    )
    assert r1.status_code == 200
    # team-b scope cannot.
    r2 = client.get(
        f"/api/sessions/{sid}", headers=_bearer(t21_tok, team="team-b"),
    )
    assert r2.status_code == 404

    # Move it.
    queue.set_job_team(sid, "team-b")

    # Visibility flips.
    r3 = client.get(
        f"/api/sessions/{sid}", headers=_bearer(team_a_tok, team="team-a"),
    )
    assert r3.status_code == 404, (
        "team-a should NOT see a session that has been moved to team-b"
    )
    r4 = client.get(
        f"/api/sessions/{sid}", headers=_bearer(t21_tok, team="team-b"),
    )
    assert r4.status_code == 200


# ── CLI: session move ───────────────────────────────────────────────────────


def test_cli_session_move_happy_path(tmp_data):
    from vezir.server import queue
    queue.create_team("team-a", "Team A")
    queue.create_team("team-b", "Team B")
    queue.enqueue("01SID", github="alice", title="t", team_id="team-a")

    from vezir.cli import main
    runner = CliRunner()
    result = runner.invoke(
        main, ["session", "move", "01SID", "--to-team", "team-b", "--yes"],
    )
    assert result.exit_code == 0, result.output
    assert queue.get("01SID")["team_id"] == queue.get_team("team-b")["id"]


def test_cli_session_move_unknown_dest_team(tmp_data):
    from vezir.server import queue
    queue.create_team("team-a", "Team A")
    queue.enqueue("01SID", github="alice", title="t", team_id="team-a")

    from vezir.cli import main
    runner = CliRunner()
    result = runner.invoke(
        main, ["session", "move", "01SID", "--to-team", "ghost", "--yes"],
    )
    assert result.exit_code == 2
    assert "does not exist" in result.output


def test_cli_session_move_unknown_session(tmp_data):
    from vezir.server import queue
    queue.create_team("team-a", "Team A")
    queue.create_team("team-b", "Team B")

    from vezir.cli import main
    runner = CliRunner()
    result = runner.invoke(
        main, ["session", "move", "NOPE", "--to-team", "team-b", "--yes"],
    )
    assert result.exit_code == 2
    assert "not found" in result.output


def test_cli_session_move_same_team_noop(tmp_data):
    from vezir.server import queue
    queue.create_team("team-a", "Team A")
    queue.enqueue("01SID", github="alice", title="t", team_id="team-a")

    from vezir.cli import main
    runner = CliRunner()
    result = runner.invoke(
        main, ["session", "move", "01SID", "--to-team", "team-a", "--yes"],
    )
    assert result.exit_code == 0
    assert "already in team" in result.output


# ── Documented limitation: voiceprint backwash ──────────────────────────────


def test_move_does_not_touch_voiceprint_dbs(tmp_data):
    """Moving a session leaves both teams' voiceprint DBs untouched
    (locked-in policy decision; documented in CLI help)."""
    from vezir import config
    from vezir.server import queue, voiceprints

    queue.create_team("team-a", "Team A")
    queue.create_team("team-b", "Team B")
    voiceprints.ensure_db_exists("team-a")
    voiceprints.ensure_db_exists("team-b")
    # Pre-populate team-a with a label embedding.
    config.team_speaker_profiles_path("team-a").write_text(
        json.dumps({"alice": {"n_sessions": 3}})
    )

    queue.enqueue("01SID", github="alice", title="t", team_id="team-a")
    queue.set_job_team("01SID", "team-b")

    # team-a's DB unchanged.
    assert json.loads(
        config.team_speaker_profiles_path("team-a").read_text(encoding="utf-8")
    ) == {"alice": {"n_sessions": 3}}
    # team-b's DB unchanged (still empty).
    assert json.loads(
        config.team_speaker_profiles_path("team-b").read_text(encoding="utf-8")
    ) == {}
