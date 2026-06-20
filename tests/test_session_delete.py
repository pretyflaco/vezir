"""v0.8.12 session deletion tests.

Covers the "team admins (or the original uploader) can remove a session"
feature:

* ``queue.delete_session`` removes the jobs row, session_teams rows, the
  on-disk ``sessions/<id>/`` dir and the ``logs/<id>.log`` file.
* ``DELETE /api/sessions/{id}`` authorization: admin OR original uploader
  succeed; a same-team non-owner non-admin gets 403; a cross-team caller
  gets 404; a missing session is 404.
* The backwash ``warning`` is returned when the session was synced.
* CLI ``vezir session rm`` (via the HTTP client) and the client API
  ``delete_session`` plumbing.
"""
from __future__ import annotations

import io
import tempfile
import wave
from pathlib import Path

import pytest


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
    """Issue a token AND add a membership for ``github`` in ``team_id``."""
    from vezir.server import auth, queue
    if queue.get_team(team_id) is None:
        queue.create_team(team_id, team_id.capitalize())
    role = "admin" if kwargs.get("is_admin") else "scribe"
    queue.add_membership(github, team_id, role=role, added_by="test")
    return auth._issue_raw(github, **kwargs)


# ── queue.delete_session (unit) ─────────────────────────────────────────────


def test_delete_session_removes_row_and_files(tmp_data):
    from vezir import config
    from vezir.server import queue

    queue.create_team("blink", "Blink")
    queue.enqueue("01SID", github="alice", title="t", team_id="blink")

    # Fabricate on-disk artifacts + a log file.
    sdir = config.sessions_dir() / "01SID"
    sdir.mkdir(parents=True)
    (sdir / "01SID.wav").write_bytes(b"RIFF....WAVE")
    (sdir / "01SID.summary.md").write_text("hi")
    log_file = config.logs_dir() / "01SID.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text("log")

    stats = queue.delete_session("01SID")

    assert stats["db_deleted"] is True
    assert stats["dir_removed"] is True
    assert stats["log_removed"] is True
    assert queue.get("01SID") is None
    assert not sdir.exists()
    assert not log_file.exists()


def test_delete_session_missing_is_noop(tmp_data):
    from vezir.server import queue
    queue.create_team("blink", "Blink")

    stats = queue.delete_session("NOPE")
    assert stats["db_deleted"] is False
    assert stats["dir_removed"] is False


def test_delete_session_clears_session_teams(tmp_data):
    from vezir.server import queue
    queue.create_team("blink", "Blink")
    queue.create_team("twentyone", "Twentyone")
    queue.enqueue("01SID", github="alice", title="t", team_id="blink")
    queue.share_session_with_team("01SID", "twentyone")
    assert queue.get_session_teams("01SID")  # non-empty

    queue.delete_session("01SID")
    assert queue.get_session_teams("01SID") == []


def test_delete_session_was_synced_flag(tmp_data):
    from vezir.server import queue
    queue.create_team("blink", "Blink")
    # A sync-enabled job that reached the terminal `done` state ran sync.
    queue.enqueue("01SID", github="alice", title="t", team_id="blink")
    queue.update_status("01SID", "done")

    stats = queue.delete_session("01SID")
    assert stats["was_synced"] is True


def test_delete_session_not_synced_flag(tmp_data):
    from vezir.server import queue
    queue.create_team("blink", "Blink")
    # A personal (sync-disabled) job that finished was never pushed.
    queue.enqueue(
        "01SID", github="alice", title="t", team_id="blink", personal=True,
    )
    queue.update_status("01SID", "done")

    stats = queue.delete_session("01SID")
    assert stats["was_synced"] is False


# ── DELETE /api/sessions/{id} (endpoint authz) ──────────────────────────────


def _upload(client, token, team="blink"):
    resp = client.post(
        "/upload",
        headers=_bearer(token, team=team),
        files={"audio": ("x.wav", _tiny_wav(), "audio/wav")},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["session_id"]


def test_admin_can_delete_any_session(client):
    from vezir.server import queue
    uploader_tok = _issue_raw("alice", team_id="blink")
    admin_tok = _issue_raw("boss", team_id="blink", is_admin=True)

    sid = _upload(client, uploader_tok)
    resp = client.delete(f"/api/sessions/{sid}", headers=_bearer(admin_tok))
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    assert queue.get(sid) is None


def test_uploader_can_delete_own_session(client):
    from vezir.server import queue
    uploader_tok = _issue_raw("alice", team_id="blink")

    sid = _upload(client, uploader_tok)
    resp = client.delete(f"/api/sessions/{sid}", headers=_bearer(uploader_tok))
    assert resp.status_code == 200, resp.text
    assert queue.get(sid) is None


def test_non_owner_non_admin_gets_403(client):
    from vezir.server import queue
    uploader_tok = _issue_raw("alice", team_id="blink")
    other_tok = _issue_raw("bob", team_id="blink")  # member, not owner/admin

    sid = _upload(client, uploader_tok)
    resp = client.delete(f"/api/sessions/{sid}", headers=_bearer(other_tok))
    assert resp.status_code == 403
    assert queue.get(sid) is not None  # untouched


def test_cross_team_delete_gets_404(client):
    from vezir.server import queue
    uploader_tok = _issue_raw("alice", team_id="blink")
    # bob is an ADMIN, but of a different team.
    t21_admin = _issue_raw("bob", team_id="twentyone", is_admin=True)

    sid = _upload(client, uploader_tok)
    resp = client.delete(
        f"/api/sessions/{sid}", headers=_bearer(t21_admin, team="twentyone"),
    )
    assert resp.status_code == 404
    assert queue.get(sid) is not None  # untouched (existence not leaked)


def test_delete_missing_session_404(client):
    admin_tok = _issue_raw("boss", team_id="blink", is_admin=True)
    resp = client.delete("/api/sessions/NOPE", headers=_bearer(admin_tok))
    assert resp.status_code == 404


def test_delete_synced_session_returns_warning(client):
    from vezir.server import queue
    uploader_tok = _issue_raw("alice", team_id="blink")

    sid = _upload(client, uploader_tok)
    # Upload defaults sync_enabled=True; mark it done so it counts as synced.
    queue.update_status(sid, "done")

    resp = client.delete(f"/api/sessions/{sid}", headers=_bearer(uploader_tok))
    assert resp.status_code == 200
    assert resp.json()["warning"]  # non-empty backwash warning
    assert queue.get(sid) is None


# ── CLI: vezir session rm ───────────────────────────────────────────────────


def test_cli_session_rm_confirm_abort(tmp_data):
    """Declining the prompt aborts without deleting."""
    from vezir.server import queue
    queue.create_team("blink", "Blink")
    queue.enqueue("01SID", github="alice", title="t", team_id="blink")

    from click.testing import CliRunner

    from vezir.cli import main
    runner = CliRunner()
    # Answer "n" to the confirmation.
    result = runner.invoke(
        main,
        ["session", "rm", "01SID", "--team", "blink",
         "--server", "http://x", "--token", "vzr_" + "a" * 40],
        input="n\n",
    )
    assert result.exit_code != 0  # aborted
    assert queue.get("01SID") is not None
