"""Tests for the tus-subset resumable upload protocol (v0.7.3+)."""
from __future__ import annotations

import io
import tempfile
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def tmp_data(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("VEZIR_DATA", d)
        monkeypatch.delenv("VEZIR_MAX_UPLOAD_BYTES", raising=False)
        monkeypatch.delenv("VEZIR_RESUMABLE_USER_QUOTA_BYTES", raising=False)
        monkeypatch.delenv("VEZIR_RESUMABLE_MIN_FREE_BYTES", raising=False)
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


def test_resumable_accepts_mp3(client_and_token):
    client, token, tmp_data = client_and_token
    data = b"ID3" + b"\x00" * 256

    resp = _create(client, token, data, fname="m.mp3", ctype="audio/mpeg")
    assert resp.status_code == 201
    upload_id = resp.json()["upload_id"]

    resp = _patch(client, token, upload_id, data, 0)
    assert resp.status_code == 200, resp.text
    session_id = resp.json()["session_id"]
    out = tmp_data / "sessions" / session_id / f"{session_id}.mp3"
    assert out.exists()
    assert out.read_bytes() == data


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


def test_resumable_multipart_fields_remain_supported(client_and_token):
    client, token, _ = client_and_token
    data = _wav_bytes()
    r = client.post(
        "/upload/resumable",
        headers=_headers(token, {
            "Upload-Length": str(len(data)),
            "Upload-Filename": "m.wav",
            "Upload-Content-Type": "audio/wav",
        }),
        files={"title": (None, "Multipart title"), "personal": (None, "true")},
    )
    assert r.status_code == 201, r.text
    from vezir.server import uploads

    meta = uploads._load_meta(r.json()["upload_id"])
    assert meta["title"] == "Multipart title"
    assert meta["personal"] is True


def test_resumable_multipart_file_is_rejected_without_spooling(
    client_and_token, tmp_path, monkeypatch,
):
    client, token, tmp_data = client_and_token
    spool = tmp_path / "spool"
    spool.mkdir()
    monkeypatch.setenv("TMPDIR", str(spool))
    tempfile.tempdir = None
    try:
        r = client.post(
            "/upload/resumable",
            headers=_headers(token, {
                "Upload-Length": "100",
                "Upload-Filename": "m.wav",
                "Upload-Content-Type": "audio/wav",
            }),
            files={"audio": ("evil.wav", _wav_bytes(), "audio/wav")},
        )
    finally:
        tempfile.tempdir = None
    assert r.status_code == 400, r.text
    assert list(spool.iterdir()) == []
    staging = tmp_data / "uploads-tmp"
    assert not staging.exists() or list(staging.iterdir()) == []


def test_resumable_user_quota_reserves_and_releases(client_and_token, monkeypatch):
    client, token, tmp_data = client_and_token
    data = _wav_bytes()
    monkeypatch.setenv("VEZIR_RESUMABLE_USER_QUOTA_BYTES", str(len(data)))
    assert _create(client, token, data).status_code == 201
    assert _create(client, token, data).status_code == 413
    staging = tmp_data / "uploads-tmp"
    assert len(list(staging.glob("*.meta.json"))) == 1

    from vezir.server import uploads

    assert uploads.sweep_abandoned_uploads(
        now=time.time() + uploads.RESUMABLE_TTL_SEC + 1,
    ) == 1
    assert _create(client, token, data).status_code == 201


def test_resumable_free_space_rejection_leaves_no_reservation(client_and_token, monkeypatch):
    client, token, tmp_data = client_and_token
    data = _wav_bytes()
    min_free = 1024
    monkeypatch.setenv("VEZIR_RESUMABLE_MIN_FREE_BYTES", str(min_free))
    from vezir.server import uploads

    monkeypatch.setattr(
        uploads.shutil, "disk_usage",
        lambda _path: SimpleNamespace(free=len(data) + min_free - 1),
    )
    assert _create(client, token, data).status_code == 507
    staging = tmp_data / "uploads-tmp"
    assert not staging.exists() or list(staging.iterdir()) == []


def test_resumable_free_space_counts_existing_reservations(client_and_token, monkeypatch):
    client, token, _ = client_and_token
    data = _wav_bytes()
    min_free = 1024
    monkeypatch.setenv("VEZIR_RESUMABLE_MIN_FREE_BYTES", str(min_free))
    from vezir.server import uploads

    monkeypatch.setattr(
        uploads.shutil, "disk_usage",
        lambda _path: SimpleNamespace(free=2 * len(data) + min_free - 1),
    )
    assert _create(client, token, data).status_code == 201
    assert _create(client, token, data).status_code == 507


def test_concurrent_resumable_creates_cannot_exceed_user_quota(client_and_token, monkeypatch):
    client, token, tmp_data = client_and_token
    data = _wav_bytes()
    monkeypatch.setenv("VEZIR_RESUMABLE_USER_QUOTA_BYTES", str(len(data)))
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: _create(client, token, data), range(2)))
    assert sorted(r.status_code for r in responses) == [201, 413]
    assert len(list((tmp_data / "uploads-tmp").glob("*.meta.json"))) == 1


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


# ── v0.7.8: PATCH chunk endpoint is NOT rate-limited ────────────────────────


@pytest.fixture
def ratelimited_client_and_token(tmp_data, monkeypatch):
    """Like client_and_token but with the rate limiter ENABLED (conftest
    disables it globally).  Resets bucket state so the per-test budget
    is clean."""
    monkeypatch.delenv("VEZIR_DISABLE_RATELIMIT", raising=False)
    from fastapi.testclient import TestClient

    from vezir.server import auth, ratelimit
    from vezir.server.app import create_app

    ratelimit._reset_for_tests()
    token = auth.issue("alice")
    app = create_app()
    yield TestClient(app, follow_redirects=False), token, tmp_data
    ratelimit._reset_for_tests()


def test_many_patch_chunks_not_rate_limited(ratelimited_client_and_token):
    """Regression for the 'upload failed: 429' on a ~33 MB meeting.

    The resumable PATCH endpoint sends one request per chunk; with the
    old 10/min 'upload' bucket on PATCH, the 11th chunk got a 429 and
    the whole upload hard-failed.  v0.7.8 drops the limiter on PATCH so
    an arbitrary number of chunks succeeds.
    """
    client, token, tmp_data = ratelimited_client_and_token
    data = _wav_bytes(seconds=4)
    upload_id = _create(client, token, data).json()["upload_id"]

    # Send the body in 25 tiny chunks — well past the old cap of 10.
    n_chunks = 25
    step = max(1, len(data) // n_chunks)
    offset = 0
    statuses = []
    while offset < len(data):
        chunk = data[offset:offset + step]
        r = _patch(client, token, upload_id, chunk, offset)
        statuses.append(r.status_code)
        assert r.status_code != 429, (
            f"chunk at offset {offset} was rate-limited (429)"
        )
        offset = int(r.headers.get("Upload-Offset", offset + len(chunk)))
    # Final PATCH completes the upload (200); intermediate ones are 204.
    assert statuses[-1] == 200
    assert statuses.count(204) >= 11  # more than the old 10-token bucket


def test_resumable_create_still_rate_limited(ratelimited_client_and_token):
    """The creation endpoint keeps the 'upload' bucket (cap 10/min): the
    limit is meant to cap uploads STARTED, not chunks appended."""
    client, token, _ = ratelimited_client_and_token
    data = _wav_bytes()
    codes = [_create(client, token, data).status_code for _ in range(12)]
    assert 429 in codes, "create endpoint should still be rate-limited"
