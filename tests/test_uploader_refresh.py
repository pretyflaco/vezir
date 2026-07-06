"""Uploader refresh-on-401 (0.10.1).

The upload path used to bypass the session-refresh logic: a 401 during
``POST /upload/resumable`` (or ``/upload``) surfaced immediately as
"session expired", forcing a manual re-login even when a valid refresh
token was stored.  These tests lock in that all three upload functions
now call the ``refresh_cb`` on a 401 and retry once with the new token.
"""
from __future__ import annotations

import wave
from pathlib import Path

import httpx
import pytest

from vezir.client import uploader


def _wav(path: Path, seconds: int = 1) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 16000 * seconds)


@pytest.fixture
def mock_transport(monkeypatch):
    """Install a MockTransport factory; return a setter for the handler."""
    holder: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        return holder["fn"](request)

    transport = httpx.MockTransport(handler)
    orig = uploader.httpx.Client

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return orig(*args, **kwargs)

    monkeypatch.setattr(uploader.httpx, "Client", factory)
    monkeypatch.setattr(uploader.time, "sleep", lambda *_a, **_k: None)

    def set_handler(fn):
        holder["fn"] = fn

    return set_handler


def test_resumable_refreshes_on_401_create(mock_transport, tmp_path):
    audio = tmp_path / "m.wav"
    _wav(audio, seconds=1)
    total = audio.stat().st_size

    state = {"create_calls": 0, "offset": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("authorization", "")
        p = request.url.path
        if request.method == "POST" and p == "/upload/resumable":
            state["create_calls"] += 1
            # First create with the stale token 401s; after refresh the
            # Authorization header changes and it succeeds.
            if auth == "Bearer stale":
                return httpx.Response(401, json={"detail": "invalid bearer token"})
            return httpx.Response(201, json={"upload_id": "U1"})
        if request.method == "PATCH":
            body = request.content
            state["offset"] += len(body)
            done = state["offset"] >= total
            code = 200 if done else 204
            js = {"session_id": "S1", "bytes": state["offset"]} if done else None
            return httpx.Response(
                code, json=js,
                headers={"Upload-Offset": str(state["offset"])},
            )
        return httpx.Response(404)

    mock_transport(handler)

    refresh_calls = {"n": 0}

    def refresh_cb():
        refresh_calls["n"] += 1
        return "fresh"

    result = uploader.upload_resumable(
        "http://stub", "stale", audio, team_id="blink",
        chunk_bytes=8192, refresh_cb=refresh_cb,
    )
    assert result["session_id"] == "S1"
    assert refresh_calls["n"] == 1           # refresh was attempted
    assert state["create_calls"] == 2        # 401 then retry


def test_resumable_401_without_refresh_token_raises(mock_transport, tmp_path):
    """No refresh_cb (or it returns None) -> the 401 surfaces as before."""
    audio = tmp_path / "m.wav"
    _wav(audio, seconds=1)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/upload/resumable":
            return httpx.Response(401, json={"detail": "invalid bearer token"})
        return httpx.Response(404)

    mock_transport(handler)

    with pytest.raises(httpx.HTTPStatusError):
        uploader.upload_resumable(
            "http://stub", "stale", audio, team_id="blink",
            refresh_cb=lambda: None,
        )


def test_oneshot_upload_refreshes_on_401(mock_transport, tmp_path):
    audio = tmp_path / "m.wav"
    _wav(audio, seconds=1)
    total = audio.stat().st_size

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/upload":
            if request.headers.get("authorization") == "Bearer stale":
                return httpx.Response(401, json={"detail": "invalid bearer token"})
            return httpx.Response(200, json={"session_id": "S1", "bytes": total})
        return httpx.Response(404)

    mock_transport(handler)

    calls = {"n": 0}

    def refresh_cb():
        calls["n"] += 1
        return "fresh"

    result = uploader.upload(
        "http://stub", "stale", audio, team_id="blink", refresh_cb=refresh_cb,
    )
    assert result["session_id"] == "S1"
    assert calls["n"] == 1


def test_multi_upload_refreshes_on_401(mock_transport, tmp_path):
    a1 = tmp_path / "a.wav"
    a2 = tmp_path / "b.wav"
    _wav(a1, 1)
    _wav(a2, 1)
    total = a1.stat().st_size + a2.stat().st_size

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/upload/multi":
            if request.headers.get("authorization") == "Bearer stale":
                return httpx.Response(401, json={"detail": "invalid bearer token"})
            return httpx.Response(200, json={"session_id": "S1", "bytes": total})
        return httpx.Response(404)

    mock_transport(handler)

    calls = {"n": 0}

    def refresh_cb():
        calls["n"] += 1
        return "fresh"

    result = uploader.upload_multi(
        "http://stub", "stale", [a1, a2], team_id="blink", refresh_cb=refresh_cb,
    )
    assert result["session_id"] == "S1"
    assert calls["n"] == 1
