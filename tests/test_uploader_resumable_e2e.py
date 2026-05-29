"""End-to-end test of the desktop resumable upload client against a live server.

Uses a real uvicorn server in a background thread because the resumable
client uses ``httpx.Client`` over a real socket (TestClient's ASGI
transport won't serve those calls).
"""
from __future__ import annotations

import socket
import tempfile
import threading
import time
import wave
from pathlib import Path

import pytest

uvicorn = pytest.importorskip("uvicorn")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wav(path: Path, seconds: int = 1) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 16000 * seconds)


@pytest.fixture
def live_server(monkeypatch):
    d = tempfile.mkdtemp()
    monkeypatch.setenv("VEZIR_DATA", d)
    monkeypatch.setenv("VEZIR_DISABLE_RATELIMIT", "1")

    from vezir.server import auth
    from vezir.server.app import create_app

    token = auth.issue("alice")  # conftest shim memberships alice -> blink
    app = create_app()
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Wait for startup.
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 10
    import httpx
    while time.time() < deadline:
        try:
            if httpx.get(base + "/health", timeout=1).status_code == 200:
                break
        except Exception:
            time.sleep(0.1)
    else:
        server.should_exit = True
        pytest.skip("server did not start in time")

    yield base, token, Path(d)

    server.should_exit = True
    thread.join(timeout=5)


def test_resumable_e2e_happy_path(live_server):
    base, token, data_dir = live_server
    from vezir.client import uploader

    with tempfile.TemporaryDirectory() as td:
        audio = Path(td) / "m.wav"
        _wav(audio, seconds=2)

        assert uploader.server_supports_resumable(base, token, team_id="blink")

        result = uploader.upload_resumable(
            base, token, audio, team_id="blink", chunk_bytes=8192,
        )
        session_id = result["session_id"]
        out = data_dir / "sessions" / session_id / f"{session_id}.wav"
        assert out.exists()
        assert out.read_bytes() == audio.read_bytes()


def test_legacy_upload_carries_team_header(live_server):
    """The non-resumable upload() must send X-Team-Id (else 400 on v0.7.0)."""
    base, token, data_dir = live_server
    from vezir.client import uploader

    with tempfile.TemporaryDirectory() as td:
        audio = Path(td) / "m.wav"
        _wav(audio, seconds=1)
        result = uploader.upload(base, token, audio, team_id="blink")
        assert "session_id" in result


def test_resumable_client_honors_429_retry_after(monkeypatch, tmp_path):
    """v0.7.8: a 429 mid-upload must be retried (honoring Retry-After),
    not hard-fail.  Stubs the transport so the first PATCH returns 429
    and the resend succeeds."""
    import httpx

    from vezir.client import uploader

    audio = tmp_path / "m.wav"
    _wav(audio, seconds=1)
    total = audio.stat().st_size

    state = {"patches": 0, "offset": 0, "rate_limited_once": False}

    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if request.method == "POST" and p == "/upload/resumable":
            return httpx.Response(201, json={"upload_id": "U1"},
                                  headers={"Upload-Offset": "0"})
        if request.method == "HEAD":
            return httpx.Response(
                200, headers={"Upload-Offset": str(state["offset"])})
        if request.method == "PATCH":
            # First PATCH: rate-limit it once.
            if not state["rate_limited_once"]:
                state["rate_limited_once"] = True
                return httpx.Response(429, headers={"Retry-After": "1"},
                                      text="rate limited")
            state["patches"] += 1
            body = request.content
            state["offset"] += len(body)
            done = state["offset"] >= total
            if done:
                return httpx.Response(
                    200, json={"session_id": "S1", "bytes": state["offset"]},
                    headers={"Upload-Offset": str(state["offset"])})
            return httpx.Response(
                204, headers={"Upload-Offset": str(state["offset"])})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    orig_client = uploader.httpx.Client

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return orig_client(*args, **kwargs)

    monkeypatch.setattr(uploader.httpx, "Client", factory)
    # Don't actually sleep for Retry-After during the test.
    monkeypatch.setattr(uploader.time, "sleep", lambda *_a, **_k: None)

    result = uploader.upload_resumable(
        "http://stub", "vzr_x", audio, team_id="blink", chunk_bytes=8192,
    )
    assert result["session_id"] == "S1"
    assert state["rate_limited_once"] is True  # the 429 path was exercised
    assert result["bytes"] == total
