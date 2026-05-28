"""v0.6.2 ``vezir session move`` tests (Feature C).

Covers:

* ``queue.set_job_team`` with the new ``require_team_exists=True``
  default refuses unknown destination slugs (closes the v0.6.0
  silent-orphan hole).
* Backfill path (``require_team_exists=False``) still works for the
  v0.6.0 migration shape.
* End-to-end: a session moved from blink -> twentyone becomes
  invisible to a blink token (404) and visible to a twentyone token.
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


def _bearer(token: str, team: str = "blink") -> dict:
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
    queue.create_team("blink", "Blink")
    queue.enqueue("01TEST", github="alice", title="t", team_id="blink")

    with pytest.raises(ValueError, match="does not exist"):
        queue.set_job_team("01TEST", "ghost")

    # Original team_id preserved (stored as blink's uuid).
    assert queue.get("01TEST")["team_id"] == queue.get_team("blink")["id"]


def test_set_job_team_happy_path(tmp_data):
    from vezir.server import queue
    queue.create_team("blink", "Blink")
    queue.create_team("twentyone", "Twentyone")
    queue.enqueue("01TEST", github="alice", title="t", team_id="blink")

    queue.set_job_team("01TEST", "twentyone")
    # v0.7.4: jobs store the team's uuid, not the slug.
    assert queue.get("01TEST")["team_id"] == queue.get_team("twentyone")["id"]


def test_set_job_team_backfill_mode_skips_existence_check(tmp_data):
    """The v0.6.0 migration uses require_team_exists=False to insert
    rows for teams that are being created in the same transaction."""
    from vezir.server import queue
    queue.enqueue("01TEST", github="alice", title="t", team_id="blink")
    # 'newteam' does NOT exist; backfill mode allows it anyway.
    queue.set_job_team("01TEST", "newteam", require_team_exists=False)
    assert queue.get("01TEST")["team_id"] == "newteam"


# ── End-to-end: visibility flips after move ─────────────────────────────────


def test_moved_session_invisible_to_old_team(client):
    """blink uploads, then session moves to twentyone -- blink scope
    can no longer see it; twentyone scope can.
    """
    from vezir.server import queue

    blink_tok = _issue_raw("alice", team_id="blink")
    t21_tok = _issue_raw("bob", team_id="twentyone")

    # Upload as blink (X-Team-Id: blink).
    resp = client.post(
        "/upload",
        headers=_bearer(blink_tok, team="blink"),
        files={"audio": ("x.wav", _tiny_wav(), "audio/wav")},
    )
    assert resp.status_code == 200
    sid = resp.json()["session_id"]
    assert queue.get(sid)["team_id"] == queue.get_team("blink")["id"]

    # blink scope can see it.
    r1 = client.get(
        f"/api/sessions/{sid}", headers=_bearer(blink_tok, team="blink"),
    )
    assert r1.status_code == 200
    # twentyone scope cannot.
    r2 = client.get(
        f"/api/sessions/{sid}", headers=_bearer(t21_tok, team="twentyone"),
    )
    assert r2.status_code == 404

    # Move it.
    queue.set_job_team(sid, "twentyone")

    # Visibility flips.
    r3 = client.get(
        f"/api/sessions/{sid}", headers=_bearer(blink_tok, team="blink"),
    )
    assert r3.status_code == 404, (
        "blink should NOT see a session that has been moved to twentyone"
    )
    r4 = client.get(
        f"/api/sessions/{sid}", headers=_bearer(t21_tok, team="twentyone"),
    )
    assert r4.status_code == 200


# ── CLI: session move ───────────────────────────────────────────────────────


def test_cli_session_move_happy_path(tmp_data):
    from vezir.server import queue
    queue.create_team("blink", "Blink")
    queue.create_team("twentyone", "Twentyone")
    queue.enqueue("01SID", github="alice", title="t", team_id="blink")

    from vezir.cli import main
    runner = CliRunner()
    result = runner.invoke(
        main, ["session", "move", "01SID", "--to-team", "twentyone", "--yes"],
    )
    assert result.exit_code == 0, result.output
    assert queue.get("01SID")["team_id"] == queue.get_team("twentyone")["id"]


def test_cli_session_move_unknown_dest_team(tmp_data):
    from vezir.server import queue
    queue.create_team("blink", "Blink")
    queue.enqueue("01SID", github="alice", title="t", team_id="blink")

    from vezir.cli import main
    runner = CliRunner()
    result = runner.invoke(
        main, ["session", "move", "01SID", "--to-team", "ghost", "--yes"],
    )
    assert result.exit_code == 2
    assert "does not exist" in result.output


def test_cli_session_move_unknown_session(tmp_data):
    from vezir.server import queue
    queue.create_team("blink", "Blink")
    queue.create_team("twentyone", "Twentyone")

    from vezir.cli import main
    runner = CliRunner()
    result = runner.invoke(
        main, ["session", "move", "NOPE", "--to-team", "twentyone", "--yes"],
    )
    assert result.exit_code == 2
    assert "not found" in result.output


def test_cli_session_move_same_team_noop(tmp_data):
    from vezir.server import queue
    queue.create_team("blink", "Blink")
    queue.enqueue("01SID", github="alice", title="t", team_id="blink")

    from vezir.cli import main
    runner = CliRunner()
    result = runner.invoke(
        main, ["session", "move", "01SID", "--to-team", "blink", "--yes"],
    )
    assert result.exit_code == 0
    assert "already in team" in result.output


# ── Documented limitation: voiceprint backwash ──────────────────────────────


def test_move_does_not_touch_voiceprint_dbs(tmp_data):
    """Moving a session leaves both teams' voiceprint DBs untouched
    (locked-in policy decision; documented in CLI help)."""
    from vezir import config
    from vezir.server import queue, voiceprints

    queue.create_team("blink", "Blink")
    queue.create_team("twentyone", "Twentyone")
    voiceprints.ensure_db_exists("blink")
    voiceprints.ensure_db_exists("twentyone")
    # Pre-populate blink with a label embedding.
    config.team_speaker_profiles_path("blink").write_text(
        json.dumps({"alice": {"n_sessions": 3}})
    )

    queue.enqueue("01SID", github="alice", title="t", team_id="blink")
    queue.set_job_team("01SID", "twentyone")

    # blink's DB unchanged.
    assert json.loads(
        config.team_speaker_profiles_path("blink").read_text(encoding="utf-8")
    ) == {"alice": {"n_sessions": 3}}
    # twentyone's DB unchanged (still empty).
    assert json.loads(
        config.team_speaker_profiles_path("twentyone").read_text(encoding="utf-8")
    ) == {}
