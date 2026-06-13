"""Tests for Google sign-in (vezir/server/google_auth.py).

The real Google network calls (device-code endpoint, token endpoint, and
ID-token JWKS verification) are mocked so the policy + wiring are tested
deterministically:
  * /config reports configured vs not.
  * ID-token policy: domain, email_verified, issuer enforcement.
  * /device/poll end-to-end: allowlisted @blinkbtc.com email → session JWT
    that works as a Bearer on /api/me; wrong-domain / not-allowlisted /
    unverified are rejected; authorization_pending → 202.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def tmp_data(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("VEZIR_DATA", d)
        yield Path(d)


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("VEZIR_GOOGLE_CLIENT_ID", "test-client-id.apps.googleusercontent.com")
    monkeypatch.setenv("VEZIR_GOOGLE_CLIENT_SECRET", "test-secret")
    monkeypatch.setenv("VEZIR_GOOGLE_ALLOWED_DOMAIN", "blinkbtc.com")


@pytest.fixture
def client(tmp_data):
    from fastapi.testclient import TestClient

    from vezir.server.app import create_app

    return TestClient(create_app(), follow_redirects=False)


# ── /config ──────────────────────────────────────────────────────────────────


def test_config_not_configured(client):
    r = client.get("/api/auth/google/config")
    assert r.status_code == 200
    assert r.json()["configured"] is False
    assert r.json()["client_id"] is None


def test_config_configured(client, configured):
    r = client.get("/api/auth/google/config")
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is True
    assert body["client_id"].endswith(".apps.googleusercontent.com")
    assert body["allowed_domain"] == "blinkbtc.com"


# ── ID-token policy (_verify_id_token) ───────────────────────────────────────


def _patch_verify(monkeypatch, claims):
    """Make google's verify_oauth2_token return ``claims`` (no network)."""
    import google.oauth2.id_token as g

    monkeypatch.setattr(g, "verify_oauth2_token", lambda *a, **k: claims)


def test_verify_accepts_blinkbtc(monkeypatch):
    from vezir.server import google_auth
    _patch_verify(monkeypatch, {
        "iss": "https://accounts.google.com",
        "email": "alice@blinkbtc.com", "email_verified": True,
        "hd": "blinkbtc.com",
    })
    claims = google_auth._verify_id_token("tok", "cid", "blinkbtc.com")
    assert claims["email"] == "alice@blinkbtc.com"


def test_verify_rejects_wrong_domain(monkeypatch):
    from fastapi import HTTPException

    from vezir.server import google_auth
    _patch_verify(monkeypatch, {
        "iss": "https://accounts.google.com",
        "email": "mallory@gmail.com", "email_verified": True,
    })
    with pytest.raises(HTTPException) as exc:
        google_auth._verify_id_token("tok", "cid", "blinkbtc.com")
    assert exc.value.status_code == 403


def test_verify_rejects_unverified_email(monkeypatch):
    from fastapi import HTTPException

    from vezir.server import google_auth
    _patch_verify(monkeypatch, {
        "iss": "https://accounts.google.com",
        "email": "alice@blinkbtc.com", "email_verified": False,
    })
    with pytest.raises(HTTPException) as exc:
        google_auth._verify_id_token("tok", "cid", "blinkbtc.com")
    assert exc.value.status_code == 401


def test_verify_rejects_bad_issuer(monkeypatch):
    from fastapi import HTTPException

    from vezir.server import google_auth
    _patch_verify(monkeypatch, {
        "iss": "https://evil.example", "email": "alice@blinkbtc.com",
        "email_verified": True, "hd": "blinkbtc.com",
    })
    with pytest.raises(HTTPException) as exc:
        google_auth._verify_id_token("tok", "cid", "blinkbtc.com")
    assert exc.value.status_code == 401


# ── /device/poll end-to-end (mocked Google token exchange + JWKS) ────────────


class _FakeResp:
    def __init__(self, status_code, json_body):
        self.status_code = status_code
        self._json = json_body
        self.content = b"x"
        self.text = str(json_body)

    def json(self):
        return self._json


class _FakeClient:
    """Stand-in for httpx.Client used INSIDE google_auth only (so the test's
    own TestClient httpx isn't affected). Returns a canned token response."""

    _status = 200
    _body: dict = {}

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, **kwargs):
        return _FakeResp(self._status, self._body)


def _patch_token_exchange(monkeypatch, status_code, body):
    """Replace the httpx.Client *referenced by google_auth* with a fake, so
    only the server's Google calls are stubbed (not the TestClient)."""
    from vezir.server import google_auth

    fake = type("FC", (_FakeClient,), {"_status": status_code, "_body": body})
    monkeypatch.setattr(google_auth.httpx, "Client", fake)


def test_device_poll_success_mints_jwt(client, configured, monkeypatch):
    from vezir.server import google_members
    google_members.add("alice@blinkbtc.com", "alice", is_admin=False)

    # Google returns an id_token; verification yields allowlisted claims.
    _patch_token_exchange(monkeypatch, 200, {"id_token": "fake.jwt.tok"})
    _patch_verify(monkeypatch, {
        "iss": "https://accounts.google.com",
        "email": "alice@blinkbtc.com", "email_verified": True,
        "hd": "blinkbtc.com",
    })

    r = client.post("/api/auth/google/device/poll", json={"device_code": "dc"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["github"] == "alice"
    assert body["email"] == "alice@blinkbtc.com"
    jwt = body["session_jwt"]

    # The minted JWT works as a Bearer on /api/me.
    me = client.get("/api/me", headers={"Authorization": f"Bearer {jwt}"})
    assert me.status_code == 200
    assert me.json()["github"] == "alice"


def test_device_poll_not_allowlisted_403(client, configured, monkeypatch):
    _patch_token_exchange(monkeypatch, 200, {"id_token": "fake.jwt.tok"})
    _patch_verify(monkeypatch, {
        "iss": "https://accounts.google.com",
        "email": "stranger@blinkbtc.com", "email_verified": True,
        "hd": "blinkbtc.com",
    })
    r = client.post("/api/auth/google/device/poll", json={"device_code": "dc"})
    assert r.status_code == 403


def test_device_poll_pending_returns_202(client, configured, monkeypatch):
    _patch_token_exchange(monkeypatch, 428, {"error": "authorization_pending"})
    r = client.post("/api/auth/google/device/poll", json={"device_code": "dc"})
    assert r.status_code == 202
    assert r.json()["status"] == "authorization_pending"


def test_device_poll_expired_401(client, configured, monkeypatch):
    _patch_token_exchange(monkeypatch, 400, {"error": "expired_token"})
    r = client.post("/api/auth/google/device/poll", json={"device_code": "dc"})
    assert r.status_code == 401


def test_endpoints_501_when_unconfigured(client):
    # No env → start/poll report not-configured (501).
    r = client.post("/api/auth/google/device/start")
    assert r.status_code == 501
    r2 = client.post("/api/auth/google/device/poll", json={"device_code": "dc"})
    assert r2.status_code == 501


def test_device_start_surfaces_verification_url_complete(client, configured, monkeypatch):
    # Google's device-code response includes a *complete* URL with the
    # user_code embedded; the server must pass it through so clients can
    # open a pre-filled page (no manual code typing).
    _patch_token_exchange(monkeypatch, 200, {
        "device_code": "dc",
        "user_code": "JLZ-TTC-KHD",
        "verification_url": "https://www.google.com/device",
        "verification_url_complete": "https://www.google.com/device?user_code=JLZ-TTC-KHD",
        "interval": 5,
        "expires_in": 1800,
    })
    r = client.post("/api/auth/google/device/start")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user_code"] == "JLZ-TTC-KHD"
    assert body["verification_url"] == "https://www.google.com/device"
    assert body["verification_url_complete"] == (
        "https://www.google.com/device?user_code=JLZ-TTC-KHD"
    )


def test_device_poll_transient_jwks_error_returns_202(client, configured, monkeypatch):
    # The token exchange succeeds (Google returns an id_token), but the
    # subsequent JWKS fetch in verify_oauth2_token hits a DNS failure.  That
    # is retryable, not a bad token — the client should keep polling (202),
    # NOT see a terminal 401.
    import socket

    import google.oauth2.id_token as g

    _patch_token_exchange(monkeypatch, 200, {"id_token": "fake.jwt.tok"})

    def _boom(*a, **k):
        raise Exception(
            "HTTPSConnectionPool(host='www.googleapis.com', port=443): "
            "Max retries exceeded ... Failed to resolve 'www.googleapis.com' "
            "(Errno -2 Name or service not known)"
        ) from socket.gaierror(-2, "Name or service not known")

    monkeypatch.setattr(g, "verify_oauth2_token", _boom)
    # Keep retries fast.
    from vezir.server import google_auth
    monkeypatch.setattr(google_auth, "_VERIFY_BACKOFF_SECONDS", 0.0)

    r = client.post("/api/auth/google/device/poll", json={"device_code": "dc"})
    assert r.status_code == 202, r.text
    assert r.json()["status"] == "authorization_pending"


def test_is_transient_network_error_detects_dns_vs_token():
    import socket

    from vezir.server import google_auth as ga
    assert ga._is_transient_network_error(socket.gaierror(-2, "Name or service not known"))
    assert ga._is_transient_network_error(
        Exception("Max retries exceeded: Failed to resolve 'www.googleapis.com'")
    )
    # A real token error is NOT transient.
    assert not ga._is_transient_network_error(ValueError("Token expired"))
    assert not ga._is_transient_network_error(ValueError("Invalid audience"))


# ── H3: exact-domain check (no suffix/subdomain bypass) ──────────────────────


def _poll_with_email(client, monkeypatch, email, hd="blinkbtc.com"):
    _patch_token_exchange(monkeypatch, 200, {"id_token": "fake.jwt.tok"})
    _patch_verify(monkeypatch, {
        "iss": "https://accounts.google.com",
        "email": email, "email_verified": True, "hd": hd,
    })
    return client.post("/api/auth/google/device/poll", json={"device_code": "dc"})


def test_domain_check_rejects_lookalike_domain(client, configured, monkeypatch):
    from vezir.server import google_members
    google_members.add("kemal@blinkbtc.com", "pretyflaco")
    # Lookalike domain with empty hd must be rejected (403), not allowed.
    r = _poll_with_email(client, monkeypatch, "mallory@evilblinkbtc.com", hd="")
    assert r.status_code == 403


def test_domain_check_rejects_subdomain(client, configured, monkeypatch):
    r = _poll_with_email(client, monkeypatch, "mallory@sub.blinkbtc.com", hd="")
    assert r.status_code == 403


def test_domain_check_accepts_exact_and_allowlisted(client, configured, monkeypatch):
    from vezir.server import google_members
    google_members.add("kemal@blinkbtc.com", "pretyflaco")
    r = _poll_with_email(client, monkeypatch, "kemal@blinkbtc.com", hd="")
    assert r.status_code == 200, r.text
    assert r.json()["github"] == "pretyflaco"


# ── prefill: synthesize verification_url_complete when Google omits it ────────


def test_device_start_synthesizes_complete_url(client, configured, monkeypatch):
    # Google returns only the bare verification_url (no *_complete); the
    # server must synthesize one with the user_code embedded.
    _patch_token_exchange(monkeypatch, 200, {
        "device_code": "dc",
        "user_code": "JLZ-TTC-KHD",
        "verification_url": "https://www.google.com/device",
        "interval": 5,
    })
    r = client.post("/api/auth/google/device/start")
    assert r.status_code == 200, r.text
    assert r.json()["verification_url_complete"] == (
        "https://www.google.com/device?user_code=JLZ-TTC-KHD"
    )


def test_device_start_prefers_googles_complete_url_if_present(client, configured, monkeypatch):
    _patch_token_exchange(monkeypatch, 200, {
        "device_code": "dc",
        "user_code": "ABCD",
        "verification_url": "https://www.google.com/device",
        "verification_url_complete": "https://www.google.com/device?user_code=ABCD&x=1",
        "interval": 5,
    })
    r = client.post("/api/auth/google/device/start")
    assert r.json()["verification_url_complete"] == (
        "https://www.google.com/device?user_code=ABCD&x=1"
    )
