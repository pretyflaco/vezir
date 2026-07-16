"""v0.12.0 session-retitle tests.

Covers the "add/change a session title after it was created" feature:

* ``queue.set_title`` updates the jobs row; blank/empty normalizes to NULL.
* ``POST /api/sessions/{id}/title`` authorization mirrors delete: admin OR
  original uploader succeed; a same-team non-owner non-admin gets 403; a
  cross-team caller gets 404; a missing session is 404.
* The backwash ``warning`` is returned when the session was already synced.
* CLI ``vezir session set-title`` (via the HTTP client) and the client API
  ``set_title`` plumbing.
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


def _upload(client, token, team="blink"):
    resp = client.post(
        "/upload",
        headers=_bearer(token, team=team),
        files={"audio": ("x.wav", _tiny_wav(), "audio/wav")},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["session_id"]


# ── queue.set_title (unit) ──────────────────────────────────────────────────


def test_set_title_updates_row(tmp_data):
    from vezir.server import queue
    queue.create_team("blink", "Blink")
    queue.enqueue("01SID", github="alice", team_id="blink")

    queue.set_title("01SID", "Weekly sync")
    assert queue.get("01SID")["title"] == "Weekly sync"


def test_set_title_change_existing(tmp_data):
    from vezir.server import queue
    queue.create_team("blink", "Blink")
    queue.enqueue("01SID", github="alice", title="old", team_id="blink")

    queue.set_title("01SID", "new")
    assert queue.get("01SID")["title"] == "new"


def test_set_title_blank_clears_to_null(tmp_data):
    from vezir.server import queue
    queue.create_team("blink", "Blink")
    queue.enqueue("01SID", github="alice", title="old", team_id="blink")

    queue.set_title("01SID", "   ")
    assert queue.get("01SID")["title"] is None
    queue.set_title("01SID", "back")
    queue.set_title("01SID", None)
    assert queue.get("01SID")["title"] is None


# ── POST /api/sessions/{id}/title (endpoint authz) ──────────────────────────


def test_uploader_can_retitle_own_session(client):
    from vezir.server import queue
    uploader_tok = _issue_raw("alice", team_id="blink")

    sid = _upload(client, uploader_tok)
    resp = client.post(
        f"/api/sessions/{sid}/title",
        headers=_bearer(uploader_tok),
        json={"title": "Retro 2026-07"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["title"] == "Retro 2026-07"
    assert queue.get(sid)["title"] == "Retro 2026-07"


def test_admin_can_retitle_any_session(client):
    from vezir.server import queue
    uploader_tok = _issue_raw("alice", team_id="blink")
    admin_tok = _issue_raw("boss", team_id="blink", is_admin=True)

    sid = _upload(client, uploader_tok)
    resp = client.post(
        f"/api/sessions/{sid}/title",
        headers=_bearer(admin_tok),
        json={"title": "Renamed by admin"},
    )
    assert resp.status_code == 200, resp.text
    assert queue.get(sid)["title"] == "Renamed by admin"


def test_retitle_empty_clears(client):
    from vezir.server import queue
    uploader_tok = _issue_raw("alice", team_id="blink")

    sid = _upload(client, uploader_tok)
    queue.set_title(sid, "something")
    resp = client.post(
        f"/api/sessions/{sid}/title",
        headers=_bearer(uploader_tok),
        json={"title": ""},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["title"] is None
    assert queue.get(sid)["title"] is None


def test_non_owner_non_admin_gets_403(client):
    from vezir.server import queue
    uploader_tok = _issue_raw("alice", team_id="blink")
    other_tok = _issue_raw("bob", team_id="blink")  # member, not owner/admin

    sid = _upload(client, uploader_tok)
    resp = client.post(
        f"/api/sessions/{sid}/title",
        headers=_bearer(other_tok),
        json={"title": "nope"},
    )
    assert resp.status_code == 403
    assert queue.get(sid)["title"] != "nope"


def test_cross_team_retitle_gets_404(client):
    from vezir.server import queue
    uploader_tok = _issue_raw("alice", team_id="blink")
    t21_admin = _issue_raw("bob", team_id="twentyone", is_admin=True)

    sid = _upload(client, uploader_tok)
    resp = client.post(
        f"/api/sessions/{sid}/title",
        headers=_bearer(t21_admin, team="twentyone"),
        json={"title": "leak?"},
    )
    assert resp.status_code == 404
    assert queue.get(sid)["title"] != "leak?"


def test_retitle_missing_session_404(client):
    admin_tok = _issue_raw("boss", team_id="blink", is_admin=True)
    resp = client.post(
        "/api/sessions/NOPE/title",
        headers=_bearer(admin_tok),
        json={"title": "x"},
    )
    assert resp.status_code == 404


def test_retitle_synced_session_returns_warning(client):
    from vezir.server import queue
    uploader_tok = _issue_raw("alice", team_id="blink")

    sid = _upload(client, uploader_tok)
    # Upload defaults sync_enabled=True; mark it done so it counts as synced.
    queue.update_status(sid, "done")

    resp = client.post(
        f"/api/sessions/{sid}/title",
        headers=_bearer(uploader_tok),
        json={"title": "late title"},
    )
    assert resp.status_code == 200
    assert resp.json()["warning"]  # non-empty backwash warning
    assert queue.get(sid)["title"] == "late title"


# ── client API: VezirClient.set_title (request shape) ───────────────────────


def test_client_set_title_builds_correct_request():
    """set_title POSTs to the title path with a JSON body carrying title."""
    from vezir.client.api import ApiResult, VezirClient

    api = VezirClient("http://testserver", "vzr_" + "a" * 40, team_id="blink")
    captured = {}

    def _fake_post(path, json=None, **kwargs):
        captured["path"] = path
        captured["json"] = json
        return ApiResult.success({"ok": True, "title": json.get("title")})

    api._post = _fake_post  # type: ignore[assignment]
    result = api.set_title("01SID", "Hello")
    assert result.is_ok()
    assert captured["path"] == "/api/sessions/01SID/title"
    assert captured["json"] == {"title": "Hello"}


# ── CLI: vezir session set-title ────────────────────────────────────────────


def test_cli_session_set_title(client, monkeypatch):
    """The CLI command hits the server and updates the title."""
    from vezir.server import queue
    uploader_tok = _issue_raw("alice", team_id="blink")
    sid = _upload(client, uploader_tok)

    # Point the client API's _post at the in-process TestClient app.
    import vezir.client.api as api_mod

    def _fake_post(self, path, json=None, **kwargs):
        from vezir.client.api import ApiResult
        resp = client.post(
            path,
            headers={"Authorization": f"Bearer {self.token}",
                     "X-Team-Id": self.team_id or "blink"},
            json=json,
        )
        if resp.status_code >= 400:
            return ApiResult.http(resp.status_code, resp.text)
        return ApiResult.success(resp.json())

    monkeypatch.setattr(api_mod.VezirClient, "_post", _fake_post)

    from click.testing import CliRunner

    from vezir.cli import main
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["session", "set-title", sid, "From CLI", "--team", "blink",
         "--server", "http://testserver", "--token", uploader_tok],
    )
    assert result.exit_code == 0, result.output
    assert queue.get(sid)["title"] == "From CLI"
