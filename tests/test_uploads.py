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
    # v0.7.0: every team-scoped endpoint (incl. /upload) needs X-Team-Id.
    # The conftest shim adds 'alice' to 'blink' on issue() so this
    # combination always validates.
    return {
        "Authorization": f"Bearer {token}",
        "X-Team-Id": "blink",
    }


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


def test_upload_accepts_mp3_id3(client_and_token):
    client, token, tmp_data = client_and_token

    resp = client.post(
        "/upload",
        headers=_bearer(token),
        files={"audio": ("foo.mp3", b"ID3" + b"\x00" * 64, "audio/mpeg")},
    )

    assert resp.status_code == 200
    body = resp.json()
    uploaded = tmp_data / "sessions" / body["session_id"] / f"{body['session_id']}.mp3"
    assert uploaded.exists()


def test_upload_accepts_mp3_frame_sync(client_and_token):
    client, token, tmp_data = client_and_token

    # No ID3 tag: a raw MPEG audio frame sync (0xFF 0xFB).
    resp = client.post(
        "/upload",
        headers=_bearer(token),
        files={"audio": ("foo.mp3", b"\xff\xfb" + b"\x00" * 64, "audio/mpeg")},
    )

    assert resp.status_code == 200
    body = resp.json()
    uploaded = tmp_data / "sessions" / body["session_id"] / f"{body['session_id']}.mp3"
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


def test_upload_rejects_invalid_mp3_header(client_and_token):
    client, token, tmp_data = client_and_token

    resp = client.post(
        "/upload",
        headers=_bearer(token),
        files={"audio": ("foo.mp3", b"not a real mp3", "audio/mpeg")},
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


def test_upload_rejects_incomplete_body_by_expected_size(client_and_token):
    client, token, tmp_data = client_and_token
    body = _wav_bytes()

    resp = client.post(
        "/upload",
        headers=_bearer(token),
        data={"audio_bytes": str(len(body) + 1)},
        files={"audio": ("foo.wav", body, "audio/wav")},
    )

    assert resp.status_code == 400
    assert list((tmp_data / "sessions").iterdir()) == []


def test_cli_upload_existing_file(monkeypatch, tmp_path):
    from click.testing import CliRunner

    from vezir import cli
    from vezir.client import uploader

    audio = tmp_path / "prior.wav"
    audio.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")

    def fake_upload(server_url, token, audio_path, title=None, progress=None,
                    on_retry=None, **kwargs):
        # **kwargs swallows summary_preset, auto_label, sync, etc. — fields
        # added after this test was written and irrelevant to its assertion.
        assert server_url == "http://server.test"
        assert token == "vzr_test"
        assert audio_path == audio
        assert title == "prior meeting"
        assert progress is not None
        assert on_retry is not None
        return {
            "session_id": "01TEST",
            "bytes": 12,
            "dashboard_url": "http://server.test/s/01TEST",
            "dashboard_login_url": "http://server.test/login?code=vzx_fake&next=%2Fs%2F01TEST",
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
            # Hermetic team selection: without --team the CLI falls back to
            # the HOST's ~/.config/vezir/teams.json — green on a dev box
            # with a real login, red on every clean CI runner ("no team
            # selected").  This is why CI had been red on main since
            # 2026-06-20.  (VEZIR_TEAM_ID alone doesn't help here: env team
            # scope is only honored when URL+token also come from env.)
            "--team",
            "blink",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "uploaded as session 01TEST" in result.output


def test_cli_upload_compresses_wav_when_requested(monkeypatch, tmp_path):
    from click.testing import CliRunner

    from vezir import cli
    from vezir.client import uploader

    wav = tmp_path / "prior.wav"
    ogg = tmp_path / "prior.ogg"
    wav.write_bytes(b"RIFF\x00\x00\x00\x00WAVE" + b"x" * 100)
    ogg.write_bytes(b"OggS" + b"y" * 10)

    def fake_compress(audio_path, keep_wav=True, bitrate="48k"):
        assert audio_path == wav
        assert keep_wav is True
        return ogg

    def fake_upload(server_url, token, audio_path, title=None, progress=None,
                    on_retry=None, **kwargs):
        # **kwargs absorbs summary_preset / auto_label / sync added later.
        assert audio_path == ogg
        return {
            "session_id": "01TEST",
            "bytes": ogg.stat().st_size,
            "dashboard_url": "http://server.test/s/01TEST",
            "dashboard_login_url": "http://server.test/login?code=vzx_fake&next=%2Fs%2F01TEST",
        }

    monkeypatch.setattr(uploader, "compress_wav_for_upload", fake_compress)
    monkeypatch.setattr(uploader, "upload", fake_upload)
    result = CliRunner().invoke(
        cli.main,
        [
            "upload",
            str(wav),
            "--compress",
            "--server",
            "http://server.test",
            "--token",
            "vzr_test",
            "--team",
            "blink",  # hermetic (see test above)
        ],
    )

    assert result.exit_code == 0, result.output
    assert "compressing WAV" in result.output


# ─── Multi-audio upload (v0.9.0) ─────────────────────────────────────────────


def _ogg(marker: bytes) -> bytes:
    return b"OggS" + marker + b"\x00" * 64


def test_upload_multi_stores_ordered_parts(client_and_token):
    client, token, tmp_data = client_and_token

    resp = client.post(
        "/upload/multi",
        headers=_bearer(token),
        files=[
            ("audio", ("a.ogg", _ogg(b"A"), "audio/ogg")),
            ("audio", ("b.ogg", _ogg(b"B"), "audio/ogg")),
            ("audio", ("c.ogg", _ogg(b"C"), "audio/ogg")),
        ],
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["parts"] == 3
    sid = body["session_id"]
    sdir = tmp_data / "sessions" / sid
    parts = sorted(p.name for p in sdir.glob(f"{sid}.part-*"))
    assert parts == [
        f"{sid}.part-000.ogg",
        f"{sid}.part-001.ogg",
        f"{sid}.part-002.ogg",
    ]
    # Order preserved: part-000 holds the first uploaded file.
    assert (sdir / f"{sid}.part-000.ogg").read_bytes() == _ogg(b"A")
    assert (sdir / f"{sid}.part-002.ogg").read_bytes() == _ogg(b"C")
    assert _mode(sdir / f"{sid}.part-000.ogg") == 0o600


def test_upload_multi_enqueues_one_multi_job(client_and_token):
    client, token, tmp_data = client_and_token
    from vezir.server import queue

    resp = client.post(
        "/upload/multi",
        headers=_bearer(token),
        files=[
            ("audio", ("a.ogg", _ogg(b"A"), "audio/ogg")),
            ("audio", ("b.ogg", _ogg(b"B"), "audio/ogg")),
        ],
    )
    assert resp.status_code == 200, resp.text
    sid = resp.json()["session_id"]
    job = queue.get(sid)
    assert job is not None
    assert job["multi_audio"] == 1
    assert job["status"] == "queued"


def test_upload_multi_rejects_mixed_types(client_and_token):
    client, token, tmp_data = client_and_token

    resp = client.post(
        "/upload/multi",
        headers=_bearer(token),
        files=[
            ("audio", ("a.ogg", _ogg(b"A"), "audio/ogg")),
            ("audio", ("b.wav", _wav_bytes(), "audio/wav")),
        ],
    )
    assert resp.status_code == 415
    assert list((tmp_data / "sessions").iterdir()) == []


def test_upload_multi_validates_each_magic(client_and_token):
    client, token, tmp_data = client_and_token

    resp = client.post(
        "/upload/multi",
        headers=_bearer(token),
        files=[
            ("audio", ("a.ogg", _ogg(b"A"), "audio/ogg")),
            ("audio", ("b.ogg", b"not ogg at all", "audio/ogg")),
        ],
    )
    assert resp.status_code == 415
    assert list((tmp_data / "sessions").iterdir()) == []


def test_upload_multi_aggregate_size_cap(monkeypatch, client_and_token):
    client, token, tmp_data = client_and_token
    monkeypatch.setenv("VEZIR_MAX_UPLOAD_BYTES", "100")

    resp = client.post(
        "/upload/multi",
        headers=_bearer(token),
        files=[
            ("audio", ("a.ogg", _ogg(b"A") + b"x" * 80, "audio/ogg")),
            ("audio", ("b.ogg", _ogg(b"B") + b"y" * 80, "audio/ogg")),
        ],
    )
    assert resp.status_code == 413
    assert list((tmp_data / "sessions").iterdir()) == []


def test_cli_upload_multi_orders_dir_by_filename(monkeypatch, tmp_path):
    from click.testing import CliRunner

    from vezir import cli
    from vezir.client import uploader

    # Create out-of-order on disk; the CLI must sort by filename.
    d = tmp_path / "notes"
    d.mkdir()
    (d / "audio_3.ogg").write_bytes(b"OggS3")
    (d / "audio_1.ogg").write_bytes(b"OggS1")
    (d / "audio_2.ogg").write_bytes(b"OggS2")

    captured = {}

    def fake_upload_multi(server_url, token, audio_paths, **kwargs):
        captured["names"] = [p.name for p in audio_paths]
        captured["title"] = kwargs.get("title")
        captured["auto_label"] = kwargs.get("auto_label")
        return {"session_id": "01MULTI", "bytes": 15, "parts": len(audio_paths)}

    monkeypatch.setattr(uploader, "upload_multi", fake_upload_multi)
    result = CliRunner().invoke(
        cli.main,
        [
            "upload-multi",
            "--dir", str(d),
            "--server", "http://server.test",
            "--token", "vzr_test",
            "--team", "blink",
            "--title", "Edwin feedback",
            "--no-auto-label",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["names"] == ["audio_1.ogg", "audio_2.ogg", "audio_3.ogg"]
    assert captured["title"] == "Edwin feedback"
    assert captured["auto_label"] is False
    assert "uploaded as session 01MULTI" in result.output
    assert "parts: 3" in result.output


def test_cli_upload_multi_errors_without_files(monkeypatch, tmp_path):
    from click.testing import CliRunner

    from vezir import cli

    result = CliRunner().invoke(
        cli.main,
        [
            "upload-multi",
            "--server", "http://server.test",
            "--token", "vzr_test",
            "--team", "blink",
        ],
    )
    assert result.exit_code != 0
    assert "no audio files" in result.output
