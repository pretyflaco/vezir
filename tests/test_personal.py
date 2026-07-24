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
        yield Path(d)


@pytest.fixture
def client_factory(tmp_data):
    from fastapi.testclient import TestClient

    from vezir.server.app import create_app

    def _make() -> TestClient:
        return TestClient(create_app(), follow_redirects=False)

    return _make


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


# ── per-session endpoint enforcement (v0.12.1) ──────────────────────────────


def test_detail_of_other_users_personal_is_404(client_factory):
    """A same-team member who guesses/learns the ULID of another user's
    personal session must NOT be able to read its detail (v0.12.1)."""
    from vezir.server import auth, queue
    client = client_factory()
    auth.issue("alice")
    bob_tok = auth.issue("bob")

    queue.enqueue("01PRIV", "alice", personal=True, team_id="blink")

    resp = client.get("/api/sessions/01PRIV", headers=_bearer(bob_tok))
    assert resp.status_code == 404


def test_owner_can_read_own_personal(client_factory):
    from vezir.server import auth, queue
    client = client_factory()
    alice_tok = auth.issue("alice")

    queue.enqueue("01PRIV", "alice", personal=True, team_id="blink")

    resp = client.get("/api/sessions/01PRIV", headers=_bearer(alice_tok))
    assert resp.status_code == 200
    assert resp.json()["id"] == "01PRIV"


def test_artifact_of_other_users_personal_is_404(client_factory):
    from vezir.server import auth, queue
    client = client_factory()
    auth.issue("alice")
    bob_tok = auth.issue("bob")

    queue.enqueue("01PRIV", "alice", personal=True, team_id="blink")

    resp = client.get(
        "/artifact/01PRIV/transcript.md", headers=_bearer(bob_tok)
    )
    assert resp.status_code == 404


def test_sync_now_of_other_users_personal_is_404(client_factory):
    """`sync now` on another user's personal session must be rejected
    BEFORE it can flip sync_enabled and publish it to the team repo."""
    from vezir.server import auth, queue
    client = client_factory()
    auth.issue("alice")
    bob_tok = auth.issue("bob")

    queue.enqueue("01PRIV", "alice", personal=True, team_id="blink")

    resp = client.post("/session/01PRIV/sync", headers=_bearer(bob_tok))
    assert resp.status_code == 404
    # sync must not have been enabled by the rejected request
    assert queue.get("01PRIV")["sync_enabled"] == 0


def test_admin_can_read_others_personal(client_factory):
    """Global admins (token admin bit) retain visibility into personal
    sessions for moderation/support."""
    from vezir.server import auth, queue
    client = client_factory()
    auth.issue("alice")
    admin_tok = auth.issue("carol", is_admin=True)

    queue.enqueue("01PRIV", "alice", personal=True, team_id="blink")

    resp = client.get("/api/sessions/01PRIV", headers=_bearer(admin_tok))
    assert resp.status_code == 200


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

    from vezir.server import auth, queue
    client = client_factory()
    tok = auth.issue("alice")  # default team_id='blink' via conftest shim

    # v0.7.4: per-team dirs are keyed by the team uuid.
    blink_uuid = queue.get_team("blink")["id"]
    roster_path = tmp_data / "teams" / blink_uuid / "roster.json"
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
