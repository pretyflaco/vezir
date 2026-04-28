from __future__ import annotations

import io
import stat
import tempfile
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


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _wav_bytes() -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 16000)
    return buf.getvalue()


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_upload_accepts_wav(client_and_token):
    client, token, tmp_data = client_and_token

    resp = client.post(
        "/upload",
        headers=_bearer(token),
        files={"audio": ("foo.wav", _wav_bytes(), "audio/wav")},
    )

    assert resp.status_code == 200
    body = resp.json()
    uploaded = tmp_data / "sessions" / body["session_id"] / f"{body['session_id']}.wav"
    assert uploaded.exists()
    assert _mode(uploaded.parent) == 0o700
    assert _mode(uploaded) == 0o600


def test_upload_accepts_ogg(client_and_token):
    client, token, tmp_data = client_and_token

    resp = client.post(
        "/upload",
        headers=_bearer(token),
        files={"audio": ("foo.ogg", b"OggS" + b"\x00" * 64, "audio/ogg")},
    )

    assert resp.status_code == 200
    body = resp.json()
    uploaded = tmp_data / "sessions" / body["session_id"] / f"{body['session_id']}.ogg"
    assert uploaded.exists()


def test_upload_rejects_unknown_type(client_and_token):
    client, token, tmp_data = client_and_token

    resp = client.post(
        "/upload",
        headers=_bearer(token),
        files={"audio": ("foo.txt", b"not audio", "text/plain")},
    )

    assert resp.status_code == 415
    assert list((tmp_data / "sessions").iterdir()) == []


def test_upload_rejects_invalid_wav_header(client_and_token):
    client, token, tmp_data = client_and_token

    resp = client.post(
        "/upload",
        headers=_bearer(token),
        files={"audio": ("foo.wav", b"not a real wav", "audio/wav")},
    )

    assert resp.status_code == 415
    assert list((tmp_data / "sessions").iterdir()) == []


def test_upload_rejects_oversized_body(monkeypatch, client_and_token):
    client, token, tmp_data = client_and_token
    monkeypatch.setenv("VEZIR_MAX_UPLOAD_BYTES", "100")

    resp = client.post(
        "/upload",
        headers=_bearer(token),
        files={"audio": ("foo.wav", _wav_bytes(), "audio/wav")},
    )

    assert resp.status_code == 413
    assert list((tmp_data / "sessions").iterdir()) == []


def test_cli_upload_existing_file(monkeypatch, tmp_path):
    from click.testing import CliRunner
    from vezir import cli
    from vezir.client import uploader

    audio = tmp_path / "prior.wav"
    audio.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")

    def fake_upload(server_url, token, audio_path, title=None):
        assert server_url == "http://server.test"
        assert token == "vzr_test"
        assert audio_path == audio
        assert title == "prior meeting"
        return {
            "session_id": "01TEST",
            "bytes": 12,
            "dashboard_url": "http://server.test/s/01TEST",
            "dashboard_login_url": "http://server.test/login?next=%2Fs%2F01TEST",
        }

    monkeypatch.setattr(uploader, "upload", fake_upload)
    result = CliRunner().invoke(
        cli.main,
        [
            "upload",
            str(audio),
            "--server",
            "http://server.test",
            "--token",
            "vzr_test",
            "--title",
            "prior meeting",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "uploaded as session 01TEST" in result.output
