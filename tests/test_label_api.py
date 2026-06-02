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
    from vezir.server import queue
    blink_uuid = queue.get_team("blink")["id"]
    mock_apply.assert_called_once_with(
        "01TEST",
        {"REMOTE_0": "kasita", "REMOTE_1": "alice"},
        "alice",  # github handle of the authenticated user
        blink_uuid,  # team_id (v0.7.4+): the auth-resolved team uuid
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
    from vezir.server import queue
    blink_uuid = queue.get_team("blink")["id"]
    mock_apply.assert_called_once_with(
        "01TEST", {"REMOTE_0": "kasita"}, "alice", blink_uuid,
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


# ── GET /label/{session_id}/clip/{speaker_id} ───────────────────────────────
# Regression: once voiceprint auto-labeling persists matches into the
# transcript, speaker ids reaching the clip endpoint can be real names with
# spaces (e.g. "Juan Pablo").  The old ``^[A-Za-z0-9_]+$`` guard rejected
# these with 400; the cache filename must also stay path-safe.


def test_safe_clip_id_accepts_names_with_spaces():
    from vezir.server.labels import _is_safe_clip_id

    assert _is_safe_clip_id("Juan Pablo")
    assert _is_safe_clip_id("O'Brien")
    assert _is_safe_clip_id("SPEAKER_08")
    assert _is_safe_clip_id("Anne-Marie")


def test_safe_clip_id_rejects_traversal_and_separators():
    from vezir.server.labels import _is_safe_clip_id

    assert not _is_safe_clip_id("../etc/passwd")
    assert not _is_safe_clip_id("a/b")
    assert not _is_safe_clip_id("a\\b")
    assert not _is_safe_clip_id("bad\x00id")
    assert not _is_safe_clip_id("")


def test_safe_clip_filename_is_path_safe_and_unique():
    from vezir.server.labels import _safe_clip_filename

    fn = _safe_clip_filename("Juan Pablo")
    assert "/" not in fn and "\\" not in fn and " " not in fn
    assert fn.endswith(".wav")
    # Distinct ids that slugify alike must not collide (hash suffix differs).
    assert _safe_clip_filename("Juan Pablo") != _safe_clip_filename("Juan-Pablo")


def test_label_clip_rejects_unsafe_speaker_id(client_and_token, tmp_data):
    """A ``..`` traversal id that reaches the handler is rejected with 400.

    (URL-encoded slashes like ``a%2Fb`` are normalized away by Starlette's
    router and 404 before reaching us; ``%2e%2e`` decodes to a bare ``..``
    segment that *does* reach the handler, where ``_is_safe_clip_id`` blocks
    it.)
    """
    client, token = client_and_token
    resp = client.get(
        "/label/01TEST/clip/%2e%2e",
        headers=_bearer(token),
    )
    assert resp.status_code == 400


@patch("vezir.server.labels.shutil.move")
@patch("vezir.server.labels._get_speakers")
def test_label_clip_accepts_named_speaker(
    mock_get_speakers, mock_move, client_and_token, tmp_data,
):
    """A speaker named "Juan Pablo" gets past validation and is cached under
    a path-safe filename (no 400, no traversal)."""
    client, token = client_and_token
    _seed_session(tmp_data, "01TEST", status="done")

    sdir = tmp_data / "sessions" / "01TEST"
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "01TEST.wav").write_bytes(b"RIFF" + b"\x00" * 100)

    sp = MagicMock()
    sp.id = "Juan Pablo"
    mock_get_speakers.return_value = [sp]

    from vezir.server.labels import _safe_clip_filename

    def _fake_move(src, dst):
        Path(dst).write_bytes(b"RIFF" + b"\x00" * 10)

    mock_move.side_effect = _fake_move

    with patch(
        "millet.label.extract_speaker_clip",
        return_value=sdir / "tmp_clip.wav",
    ):
        (sdir / "tmp_clip.wav").write_bytes(b"RIFF" + b"\x00" * 10)
        resp = client.get(
            "/label/01TEST/clip/Juan%20Pablo",
            headers=_bearer(token),
        )

    assert resp.status_code == 200
    # Cached under the slugified filename, within the clips dir (no escape).
    cached = sdir / "clips" / _safe_clip_filename("Juan Pablo")
    assert cached.exists()
