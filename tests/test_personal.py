"""Tests for per-session personal flag and share endpoint."""
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
        from vezir.server import web_sessions
        web_sessions._reset_for_tests()
        yield Path(d)


@pytest.fixture
def client_factory(tmp_data):
    from fastapi.testclient import TestClient
    from vezir.server.app import create_app

    def _make() -> TestClient:
        return TestClient(create_app(), follow_redirects=False)

    return _make


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _tiny_wav() -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 16000)
    return buf.getvalue()


# ── personal flag on upload ─────────────────────────────────────────────────


def test_upload_personal_forces_sync_disabled(client_factory):
    from vezir.server import auth, queue
    client = client_factory()
    tok = auth.issue("alice")
    wav = _tiny_wav()

    resp = client.post(
        "/upload",
        headers=_bearer(tok),
        files={"audio": ("x.wav", wav, "audio/wav")},
        data={"personal": "true", "sync": "true"},
    )
    assert resp.status_code == 200
    sid = resp.json()["session_id"]
    row = queue.get(sid)
    assert row["personal"] == 1
    # personal=true forces sync_enabled=0 even though sync=true was sent
    assert row["sync_enabled"] == 0


def test_upload_non_personal_default(client_factory):
    from vezir.server import auth, queue
    client = client_factory()
    tok = auth.issue("alice")
    wav = _tiny_wav()

    resp = client.post(
        "/upload",
        headers=_bearer(tok),
        files={"audio": ("x.wav", wav, "audio/wav")},
    )
    assert resp.status_code == 200
    row = queue.get(resp.json()["session_id"])
    assert row["personal"] == 0
    assert row["sync_enabled"] == 1


# ── session list filtering ──────────────────────────────────────────────────


def test_api_sessions_hides_other_users_personal(client_factory):
    from vezir.server import auth, queue
    client = client_factory()
    alice_tok = auth.issue("alice")
    bob_tok = auth.issue("bob")

    # alice uploads a personal session
    queue.enqueue("01PERSONAL", "alice", personal=True, team_id="blink")
    # bob's non-personal (visible to alice and bob)
    queue.enqueue("01TEAM", "alice", personal=False, team_id="blink")
    # bob's session, non-personal
    queue.enqueue("01BOB", "bob", personal=False, team_id="blink")

    # alice sees all three (her personal + both team)
    resp = client.get("/api/sessions", headers=_bearer(alice_tok))
    assert resp.status_code == 200
    ids = {s["id"] for s in resp.json()["sessions"]}
    assert ids == {"01PERSONAL", "01TEAM", "01BOB"}

    # bob sees only the two team sessions (alice's personal is hidden)
    resp = client.get("/api/sessions", headers=_bearer(bob_tok))
    ids = {s["id"] for s in resp.json()["sessions"]}
    assert ids == {"01TEAM", "01BOB"}


# ── share endpoint ──────────────────────────────────────────────────────────


def test_share_makes_personal_visible(client_factory):
    from vezir.server import auth, queue
    client = client_factory()
    alice_tok = auth.issue("alice")
    bob_tok = auth.issue("bob")

    queue.enqueue("01PRIV", "alice", personal=True, team_id="blink")

    # bob can't see it
    resp = client.get("/api/sessions", headers=_bearer(bob_tok))
    ids = {s["id"] for s in resp.json()["sessions"]}
    assert "01PRIV" not in ids

    # alice shares it
    resp = client.post("/api/sessions/01PRIV/share", headers=_bearer(alice_tok))
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    # now bob sees it
    resp = client.get("/api/sessions", headers=_bearer(bob_tok))
    ids = {s["id"] for s in resp.json()["sessions"]}
    assert "01PRIV" in ids

    # verify sync_enabled is still 0 (share doesn't auto-enable sync)
    row = queue.get("01PRIV")
    assert row["personal"] == 0
    assert row["sync_enabled"] == 0


def test_share_only_by_uploader(client_factory):
    from vezir.server import auth, queue
    client = client_factory()
    auth.issue("alice")
    bob_tok = auth.issue("bob")

    queue.enqueue("01PRIV", "alice", personal=True, team_id="blink")

    # bob can't share alice's personal session
    resp = client.post("/api/sessions/01PRIV/share", headers=_bearer(bob_tok))
    assert resp.status_code == 403


def test_share_idempotent_on_already_shared(client_factory):
    from vezir.server import auth, queue
    client = client_factory()
    tok = auth.issue("alice")

    queue.enqueue("01PUB", "alice", personal=False, team_id="blink")

    resp = client.post("/api/sessions/01PUB/share", headers=_bearer(tok))
    assert resp.status_code == 200
    assert resp.json()["already_shared"] is True


def test_share_not_found(client_factory):
    from vezir.server import auth
    client = client_factory()
    tok = auth.issue("alice")

    resp = client.post("/api/sessions/01NOPE/share", headers=_bearer(tok))
    assert resp.status_code == 404


# ── GET /api/team ───────────────────────────────────────────────────────────


def test_api_team_returns_handles(client_factory, tmp_data):
    import json
    from vezir.server import auth
    client = client_factory()
    tok = auth.issue("alice")  # default team_id='blink' via conftest shim

    # v0.6.0: write roster to the per-team path the new code reads from.
    roster_path = tmp_data / "teams" / "blink" / "roster.json"
    roster_path.parent.mkdir(parents=True, exist_ok=True)
    roster_path.write_text(json.dumps([
        {"github": "kasita", "name": "Kasita"},
        {"github": "pretyflaco"},
    ]))

    resp = client.get("/api/team", headers=_bearer(tok))
    assert resp.status_code == 200
    assert resp.json()["team"] == ["kasita", "pretyflaco"]


def test_api_team_requires_bearer(client_factory):
    client = client_factory()
    resp = client.get("/api/team")
    assert resp.status_code == 401
