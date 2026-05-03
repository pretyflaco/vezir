"""Tests for browser-friendly auth (cookie + bearer combined)."""
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
def client_and_token(tmp_data):
    from fastapi.testclient import TestClient
    from vezir.server import auth
    from vezir.server.app import create_app

    token = auth.issue("alice")
    app = create_app()
    return TestClient(app, follow_redirects=False), token


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ── /login GET with token (the GUI hand-off) ────────────────────────────────


def test_login_get_valid_token_sets_cookie_and_redirects(client_and_token):
    client, token = client_and_token
    resp = client.get(f"/login?token={token}&next=/s/abc123")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/s/abc123"
    sc = resp.headers.get("set-cookie", "")
    assert "vezir_session=" in sc
    assert "HttpOnly" in sc
    assert "SameSite=lax" in sc.lower() or "samesite=lax" in sc.lower()


def test_login_get_invalid_token_returns_401_form(client_and_token):
    client, _ = client_and_token
    resp = client.get("/login?token=vzr_bogus&next=/s/abc")
    assert resp.status_code == 401
    assert "Invalid token" in resp.text
    assert "vezir_session=" not in resp.headers.get("set-cookie", "")


def test_login_get_no_token_renders_form(client_and_token):
    client, _ = client_and_token
    resp = client.get("/login")
    assert resp.status_code == 200
    assert "Sign in" in resp.text
    assert "<form" in resp.text


# ── /login open-redirect protection ─────────────────────────────────────────


@pytest.mark.parametrize("bad_next", [
    "//evil.example.com",
    "http://evil.example.com",
    "https://evil.example.com/x",
    "javascript:alert(1)",
    "no-leading-slash",
])
def test_login_rejects_unsafe_next(client_and_token, bad_next):
    client, token = client_and_token
    resp = client.get(f"/login?token={token}&next={bad_next}")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"


# ── POST /login (manual paste-token form) ───────────────────────────────────


def test_login_post_valid_token_sets_cookie(client_and_token):
    client, token = client_and_token
    resp = client.post("/login", data={"token": token, "next": "/"})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    assert "vezir_session=" in resp.headers.get("set-cookie", "")


def test_login_post_invalid_token_401(client_and_token):
    client, _ = client_and_token
    resp = client.post("/login", data={"token": "vzr_bogus", "next": "/"})
    assert resp.status_code == 401
    assert "Invalid token" in resp.text


# ── /logout ─────────────────────────────────────────────────────────────────


def test_logout_clears_cookie(client_and_token):
    client, token = client_and_token
    # log in
    r1 = client.get(f"/login?token={token}&next=/")
    assert "vezir_session=" in r1.headers["set-cookie"]
    # logout
    r2 = client.get("/logout")
    assert r2.status_code == 303
    assert r2.headers["location"] == "/login"
    sc = r2.headers.get("set-cookie", "")
    # Cookie clearing has either Max-Age=0 or expired date in the past
    assert "vezir_session=" in sc
    assert ('Max-Age=0' in sc) or ('expires=' in sc.lower())


# ── Dashboard accepts cookie OR bearer ───────────────────────────────────────


def test_dashboard_with_cookie(client_and_token):
    client, token = client_and_token
    client.get(f"/login?token={token}&next=/")  # establishes cookie
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Recent sessions" in resp.text or "No sessions yet" in resp.text


def test_dashboard_with_bearer(client_and_token):
    client, token = client_and_token
    resp = client.get("/", headers=_bearer(token))
    assert resp.status_code == 200


def test_session_detail_with_bearer(client_and_token):
    client, token = client_and_token

    from vezir.server import queue

    queue.enqueue("01TESTSESSION", "alice", "demo meeting")

    resp = client.get("/s/01TESTSESSION", headers=_bearer(token))
    assert resp.status_code == 200
    assert "demo meeting" in resp.text


def test_dashboard_no_auth_401(client_and_token):
    client, _ = client_and_token
    resp = client.get("/")
    assert resp.status_code == 401


# ── API stays bearer-only (cookie should NOT grant API access) ──────────────


def test_api_sessions_rejects_cookie(client_and_token):
    client, token = client_and_token
    client.get(f"/login?token={token}&next=/")  # set cookie
    # Cookie-only — must fail without bearer header
    resp = client.get("/api/sessions")
    assert resp.status_code == 401


def test_api_sessions_accepts_bearer(client_and_token):
    client, token = client_and_token
    resp = client.get("/api/sessions", headers=_bearer(token))
    assert resp.status_code == 200
    assert "sessions" in resp.json()


# ── Upload endpoint produces dashboard_login_url ────────────────────────────


def test_upload_response_includes_login_url(client_and_token):
    client, token = client_and_token
    # tiny fake WAV
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 16000)
    buf.seek(0)

    resp = client.post(
        "/upload",
        headers=_bearer(token),
        files={"audio": ("foo.wav", buf.read(), "audio/wav")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "dashboard_url" in body
    assert "dashboard_login_url" in body
    assert "/login?token=" in body["dashboard_login_url"]
    assert f"%2Fs%2F{body['session_id']}" in body["dashboard_login_url"]
