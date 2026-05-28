"""Tests for the tus-subset resumable upload protocol (v0.7.3+)."""
from __future__ import annotations

import io
import tempfile
import time
import wave
from pathlib import Path

import pytest


@pytest.fixture
def tmp_data(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("VEZIR_DATA", d)
        monkeypatch.delenv("VEZIR_MAX_UPLOAD_BYTES", raising=False)
        yield Path(d)


@pytest.fixture
def client_and_token(tmp_data):
    from fastapi.testclient import TestClient

    from vezir.server import auth
    from vezir.server.app import create_app

    token = auth.issue("alice")
    app = create_app()
    return TestClient(app, follow_redirects=False), token, tmp_data


def _headers(token: str, extra: dict | None = None) -> dict:
    h = {"Authorization": f"Bearer {token}", "X-Team-Id": "blink"}
    if extra:
        h.update(extra)
    return h


def _wav_bytes(seconds: int = 2) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 16000 * seconds)
    return buf.getvalue()


def _create(client, token, data: bytes, fname="m.wav", ctype="audio/wav"):
    return client.post(
        "/upload/resumable",
        headers=_headers(token, {
            "Upload-Length": str(len(data)),
            "Upload-Filename": fname,
            "Upload-Content-Type": ctype,
        }),
    )


def _patch(client, token, upload_id, chunk, offset):
    return client.patch(
        f"/upload/resumable/{upload_id}",
        headers=_headers(token, {
            "Upload-Offset": str(offset),
            "Content-Type": "application/offset+octet-stream",
        }),
        content=chunk,
    )


def test_happy_path_single_patch(client_and_token):
    client, token, tmp_data = client_and_token
    data = _wav_bytes()

    resp = _create(client, token, data)
    assert resp.status_code == 201
    upload_id = resp.json()["upload_id"]
    assert resp.headers["Upload-Offset"] == "0"

    resp = _patch(client, token, upload_id, data, 0)
    assert resp.status_code == 200, resp.text
    session_id = resp.json()["session_id"]
    out = tmp_data / "sessions" / session_id / f"{session_id}.wav"
    assert out.exists()
    assert out.read_bytes() == data
    # Staging cleaned up.
    assert not (tmp_data / "uploads-tmp" / f"{upload_id}.part").exists()


def test_resume_after_drop(client_and_token):
    client, token, tmp_data = client_and_token
    data = _wav_bytes()
    half = len(data) // 2

    upload_id = _create(client, token, data).json()["upload_id"]

    # First chunk.
    r1 = _patch(client, token, upload_id, data[:half], 0)
    assert r1.status_code == 204
    assert r1.headers["Upload-Offset"] == str(half)

    # Simulate client crash: HEAD to discover offset.
    h = client.head(f"/upload/resumable/{upload_id}", headers=_headers(token))
    assert h.status_code == 200
    server_offset = int(h.headers["Upload-Offset"])
    assert server_offset == half

    # Resume from server offset.
    r2 = _patch(client, token, upload_id, data[server_offset:], server_offset)
    assert r2.status_code == 200
    session_id = r2.json()["session_id"]
    out = tmp_data / "sessions" / session_id / f"{session_id}.wav"
    assert out.read_bytes() == data


def test_offset_mismatch_409(client_and_token):
    client, token, _ = client_and_token
    data = _wav_bytes()
    upload_id = _create(client, token, data).json()["upload_id"]
    # Claim offset 50 when server is at 0.
    resp = _patch(client, token, upload_id, data, 50)
    assert resp.status_code == 409


def test_overshoot_rejected_413(client_and_token):
    client, token, _ = client_and_token
    data = _wav_bytes()
    # Declare a short length, then send more.
    resp = client.post(
        "/upload/resumable",
        headers=_headers(token, {
            "Upload-Length": "100",
            "Upload-Filename": "m.wav",
            "Upload-Content-Type": "audio/wav",
        }),
    )
    upload_id = resp.json()["upload_id"]
    r = _patch(client, token, upload_id, data, 0)  # data >> 100 bytes
    assert r.status_code == 413


def test_bad_magic_rejected(client_and_token):
    client, token, _ = client_and_token
    junk = b"NOTAWAV" + b"\x00" * 100
    upload_id = _create(client, token, junk, fname="m.wav").json()["upload_id"]
    r = _patch(client, token, upload_id, junk, 0)
    assert r.status_code == 415


def test_other_user_cannot_access_session(client_and_token):
    client, token, tmp_data = client_and_token
    from vezir.server import auth, queue
    data = _wav_bytes()
    upload_id = _create(client, token, data).json()["upload_id"]

    # A different user (bob) in a different team must get 404, not access.
    bob = auth.issue("bob")
    if queue.get_team("other") is None:
        queue.create_team("other", "Other")
    queue.add_membership("bob", "other", role="scribe")
    h = client.head(
        f"/upload/resumable/{upload_id}",
        headers={"Authorization": f"Bearer {bob}", "X-Team-Id": "other"},
    )
    assert h.status_code == 404


def test_missing_upload_length_400(client_and_token):
    client, token, _ = client_and_token
    resp = client.post(
        "/upload/resumable",
        headers=_headers(token, {
            "Upload-Filename": "m.wav",
            "Upload-Content-Type": "audio/wav",
        }),
    )
    assert resp.status_code == 400


def test_sweep_removes_old_staging(client_and_token):
    client, token, tmp_data = client_and_token
    from vezir.server import uploads
    data = _wav_bytes()
    upload_id = _create(client, token, data).json()["upload_id"]
    part = tmp_data / "uploads-tmp" / f"{upload_id}.part"
    assert part.exists()

    # Nothing swept when fresh.
    assert uploads.sweep_abandoned_uploads() == 0
    assert part.exists()

    # Sweep with a "now" far in the future → removed.
    future = time.time() + uploads.RESUMABLE_TTL_SEC + 10
    assert uploads.sweep_abandoned_uploads(now=future) == 1
    assert not part.exists()
