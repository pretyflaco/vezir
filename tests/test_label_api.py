"""Tests for JSON labeling API endpoints (native client support).

GET  /api/label/{session_id}   → speaker list + team handles
POST /api/label/{session_id}   → apply labels from JSON body
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def tmp_data(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("VEZIR_DATA", d)
        yield Path(d)


@pytest.fixture
def client_and_token(tmp_data):
    from fastapi.testclient import TestClient

    from vezir.server import auth
    from vezir.server.app import create_app

    token = auth.issue("alice")
    app = create_app()
    return TestClient(app, follow_redirects=False), token


def _bearer(token: str, team: str = "blink") -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "X-Team-Id": team,
    }


def _seed_session(tmp_data, session_id: str, status: str = "needs_labeling"):
    """Create a minimal session row in the queue for testing."""
    from vezir.server import queue
    queue.enqueue(session_id, "alice", "test meeting", team_id="blink")
    queue.update_status(session_id, status)


# ── GET /api/label/{session_id} ─────────────────────────────────────────────


def test_api_label_get_requires_bearer(client_and_token):
    client, _ = client_and_token
    resp = client.get("/api/label/01TEST")
    assert resp.status_code == 401


def test_api_label_get_session_not_found(client_and_token):
    client, token = client_and_token
    resp = client.get("/api/label/01NONEXISTENT", headers=_bearer(token))
    assert resp.status_code == 404


def test_api_label_get_wrong_status(client_and_token, tmp_data):
    client, token = client_and_token
    _seed_session(tmp_data, "01TEST", status="transcribing")
    resp = client.get("/api/label/01TEST", headers=_bearer(token))
    assert resp.status_code == 409


@patch("vezir.server.labels._get_speakers")
def test_api_label_get_returns_speakers(mock_get_speakers, client_and_token, tmp_data):
    client, token = client_and_token
    _seed_session(tmp_data, "01TEST", status="needs_labeling")

    # Create session dir so _find_wav can check it
    sdir = tmp_data / "sessions" / "01TEST"
    sdir.mkdir(parents=True, exist_ok=True)

    # Mock millet's get_speakers
    sp1 = MagicMock()
    sp1.id = "REMOTE_0"
    sp1.channel = 1
    sp1.sample_text = "Hello there"
    sp2 = MagicMock()
    sp2.id = "YOU"
    sp2.channel = 0
    sp2.sample_text = "Hi"
    mock_get_speakers.return_value = [sp1, sp2]

    resp = client.get("/api/label/01TEST", headers=_bearer(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["session_id"] == "01TEST"
    assert body["status"] == "needs_labeling"
    assert len(body["speakers"]) == 2
    assert body["speakers"][0]["id"] == "REMOTE_0"
    assert body["speakers"][0]["channel"] == 1
    assert body["speakers"][0]["sample_text"] == "Hello there"
    assert isinstance(body["team"], list)
    assert body["audio_available"] is False  # no wav file in test dir


@patch("vezir.server.labels._get_speakers")
def test_api_label_get_audio_available(mock_get_speakers, client_and_token, tmp_data):
    """audio_available should be True when a WAV or OGG file exists."""
    client, token = client_and_token
    _seed_session(tmp_data, "01TEST", status="needs_labeling")

    sdir = tmp_data / "sessions" / "01TEST"
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "01TEST.ogg").write_bytes(b"OggS" + b"\x00" * 100)

    mock_get_speakers.return_value = []

    resp = client.get("/api/label/01TEST", headers=_bearer(token))
    assert resp.status_code == 200
    assert resp.json()["audio_available"] is True


# ── POST /api/label/{session_id} ────────────────────────────────────────────


def test_api_label_post_requires_bearer(client_and_token):
    client, _ = client_and_token
    resp = client.post(
        "/api/label/01TEST",
        json={"labels": {"REMOTE_0": "alice"}},
    )
    assert resp.status_code == 401


def test_api_label_post_session_not_found(client_and_token):
    client, token = client_and_token
    resp = client.post(
        "/api/label/01NONEXISTENT",
        headers=_bearer(token),
        json={"labels": {"REMOTE_0": "alice"}},
    )
    assert resp.status_code == 404


def test_api_label_post_wrong_status(client_and_token, tmp_data):
    client, token = client_and_token
    _seed_session(tmp_data, "01TEST", status="transcribing")
    resp = client.post(
        "/api/label/01TEST",
        headers=_bearer(token),
        json={"labels": {"REMOTE_0": "alice"}},
    )
    assert resp.status_code == 409


def test_api_label_post_bad_body(client_and_token, tmp_data):
    client, token = client_and_token
    _seed_session(tmp_data, "01TEST", status="needs_labeling")
    resp = client.post(
        "/api/label/01TEST",
        headers=_bearer(token),
        json={"wrong_key": "bad"},
    )
    assert resp.status_code == 400


@patch("vezir.server.labels._apply_and_finalize")
def test_api_label_post_success(mock_apply, client_and_token, tmp_data):
    client, token = client_and_token
    _seed_session(tmp_data, "01TEST", status="needs_labeling")

    resp = client.post(
        "/api/label/01TEST",
        headers=_bearer(token),
        json={"labels": {"REMOTE_0": "kasita", "REMOTE_1": "alice"}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["session_id"] == "01TEST"
    mock_apply.assert_called_once_with(
        "01TEST",
        {"REMOTE_0": "kasita", "REMOTE_1": "alice"},
        "alice",  # github handle of the authenticated user
        "blink",  # team_id (v0.6.2+): the auth-resolved team scope
    )


@patch("vezir.server.labels._apply_and_finalize")
def test_api_label_post_strips_empty_labels(mock_apply, client_and_token, tmp_data):
    """Empty or whitespace-only label values should be skipped."""
    client, token = client_and_token
    _seed_session(tmp_data, "01TEST", status="needs_labeling")

    resp = client.post(
        "/api/label/01TEST",
        headers=_bearer(token),
        json={"labels": {"REMOTE_0": "kasita", "REMOTE_1": "  ", "YOU": ""}},
    )
    assert resp.status_code == 200
    mock_apply.assert_called_once_with(
        "01TEST", {"REMOTE_0": "kasita"}, "alice", "blink",
    )


@patch("vezir.server.labels._get_speakers")
def test_api_label_get_works_for_done_sessions(mock_get_speakers, client_and_token, tmp_data):
    """Sessions in 'done' status should be re-labelable."""
    client, token = client_and_token
    _seed_session(tmp_data, "01TEST", status="done")

    sdir = tmp_data / "sessions" / "01TEST"
    sdir.mkdir(parents=True, exist_ok=True)
    mock_get_speakers.return_value = []

    resp = client.get("/api/label/01TEST", headers=_bearer(token))
    assert resp.status_code == 200
