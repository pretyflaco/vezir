"""Tests for 0.1.12 token-model hardening and login-URL hardening:

* Patch 1   — expires_at / last_used_at / is_admin / label, hmac compare.
* Patch 1b  — require_admin gates /admin/enroll.
* Patch 2   — exchange codes, opaque session cookies, legacy ?token=
              deprecation path.
"""
from __future__ import annotations

import io
import tempfile
import time
import wave
from pathlib import Path

import pytest


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_data(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("VEZIR_DATA", d)
        # Make sure the in-memory session store starts fresh per test.
        from vezir.server import web_sessions
        web_sessions._reset_for_tests()
        yield Path(d)


@pytest.fixture
def client_factory(tmp_data):
    from fastapi.testclient import TestClient
    from vezir.server.app import create_app

    def _make() -> TestClient:
        app = create_app()
        return TestClient(app, follow_redirects=False)

    return _make


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _tiny_wav_bytes() -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 16000)
    return buf.getvalue()


# ── Patch 1: token model ────────────────────────────────────────────────────


def test_issue_records_new_fields(tmp_data):
    from vezir.server import auth
    import json

    auth.issue("alice", expires_in_seconds=3600, is_admin=True, label="laptop")
    rows = json.loads((tmp_data / "tokens.json").read_text())["tokens"]
    assert len(rows) == 1
    row = rows[0]
    assert row["github"] == "alice"
    assert row["is_admin"] is True
    assert row["label"] == "laptop"
    assert row["expires_at"] is not None
    assert row["last_used_at"] is None
    # plaintext is never persisted
    assert "token" not in row
    assert "plaintext" not in row


def test_lookup_uses_constant_time_compare(monkeypatch, tmp_data):
    """`lookup` must call hmac.compare_digest, not == on hashes."""
    from vezir.server import auth

    calls = {"n": 0}
    real = auth.hmac.compare_digest

    def spy(a, b):
        calls["n"] += 1
        return real(a, b)

    monkeypatch.setattr(auth.hmac, "compare_digest", spy)
    tok = auth.issue("alice")
    auth.issue("bob")
    assert auth.lookup(tok) == "alice"
    assert calls["n"] >= 1


def test_expired_token_is_rejected(tmp_data):
    from vezir.server import auth

    # Issue and then manually backdate expiry to one minute ago.
    tok = auth.issue("alice", expires_in_seconds=10)
    import json
    p = tmp_data / "tokens.json"
    data = json.loads(p.read_text())
    data["tokens"][0]["expires_at"] = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 60)
    )
    auth._save_tokens(data)

    assert auth.lookup(tok) is None
    assert auth.is_admin_token(tok) is False


def test_legacy_row_without_expires_at_still_works(tmp_data):
    """A token row from pre-0.1.12 (no expires_at field) is treated as
    'no expiry' rather than 'instantly expired'. Required for upgrade
    safety: we must not log everyone out on first server restart after
    upgrade.
    """
    from vezir.server import auth
    import json

    tok = auth.issue("alice")
    p = tmp_data / "tokens.json"
    data = json.loads(p.read_text())
    # Strip 0.1.12 fields entirely, mimicking an old DB.
    del data["tokens"][0]["expires_at"]
    del data["tokens"][0]["last_used_at"]
    del data["tokens"][0]["is_admin"]
    del data["tokens"][0]["label"]
    auth._save_tokens(data)

    assert auth.lookup(tok) == "alice"


def test_last_used_at_updates_on_success(tmp_data):
    from vezir.server import auth
    import json

    tok = auth.issue("alice")
    p = tmp_data / "tokens.json"
    assert json.loads(p.read_text())["tokens"][0]["last_used_at"] is None
    assert auth.lookup(tok) == "alice"
    after = json.loads(p.read_text())["tokens"][0]["last_used_at"]
    assert after is not None


def test_last_used_at_is_debounced(tmp_data, monkeypatch):
    """Two consecutive lookups should not write twice."""
    from vezir.server import auth
    import json

    tok = auth.issue("alice")
    auth.lookup(tok)
    p = tmp_data / "tokens.json"
    first = json.loads(p.read_text())["tokens"][0]["last_used_at"]
    # Second lookup well within the debounce window (60s).
    auth.lookup(tok)
    second = json.loads(p.read_text())["tokens"][0]["last_used_at"]
    assert first == second  # not touched again


def test_is_admin_token_distinguishes_roles(tmp_data):
    from vezir.server import auth
    admin = auth.issue("alice", is_admin=True)
    scribe = auth.issue("bob", is_admin=False)
    assert auth.is_admin_token(admin) is True
    assert auth.is_admin_token(scribe) is False
    assert auth.is_admin_token("vzr_bogus") is False


# ── Patch 1b: require_admin ─────────────────────────────────────────────────


def test_admin_enroll_denies_non_admin_token(client_factory):
    from vezir.server import auth
    client = client_factory()
    scribe = auth.issue("bob", is_admin=False)
    resp = client.get("/admin/enroll", headers=_bearer(scribe))
    assert resp.status_code == 403
    assert "admin" in resp.text.lower()


def test_admin_enroll_allows_admin_token(client_factory):
    from vezir.server import auth
    client = client_factory()
    admin = auth.issue("alice", is_admin=True)
    resp = client.get("/admin/enroll", headers=_bearer(admin))
    assert resp.status_code == 200


def test_admin_enroll_rejects_missing_credentials(client_factory):
    client = client_factory()
    resp = client.get("/admin/enroll")
    assert resp.status_code == 401


def test_admin_check_is_per_token_not_per_handle(client_factory):
    """A scribe-tier token must NOT inherit admin access from a separate
    admin-tier token issued to the same github handle. This was a bug
    in the initial 0.1.12 release where require_admin scanned all tokens
    for the handle rather than checking the specific token presented.
    """
    from vezir.server import auth
    client = client_factory()
    _admin_tok = auth.issue("alice", is_admin=True)
    scribe_tok = auth.issue("alice", is_admin=False)
    # Scribe token for alice must get 403, even though alice also has an admin token.
    resp = client.get("/admin/enroll", headers=_bearer(scribe_tok))
    assert resp.status_code == 403


def test_admin_check_per_token_via_session_cookie(client_factory):
    """Same per-token check must hold when auth comes through a session
    cookie rather than a direct bearer header. The is_admin flag is
    captured at session creation time (/login).
    """
    from vezir.server import auth, web_sessions
    client = client_factory()
    _admin_tok = auth.issue("alice", is_admin=True)
    scribe_tok = auth.issue("alice", is_admin=False)
    # Log in with the scribe token via exchange code → opaque session.
    code = web_sessions.mint_exchange_code(scribe_tok)
    client.get(f"/login?code={code}&next=/")
    # Cookie is set; try /admin/enroll.
    resp = client.get("/admin/enroll")
    assert resp.status_code == 403


# ── Patch 2: exchange codes ─────────────────────────────────────────────────


def test_exchange_code_round_trip(tmp_data):
    from vezir.server import auth, web_sessions

    tok = auth.issue("alice")
    code = web_sessions.mint_exchange_code(tok)
    assert code.startswith("vzx_")
    consumed = web_sessions.consume_exchange_code(code)
    assert consumed == tok
    # single use
    assert web_sessions.consume_exchange_code(code) is None


def test_exchange_code_expires(monkeypatch, tmp_data):
    from vezir.server import auth, web_sessions

    tok = auth.issue("alice")
    code = web_sessions.mint_exchange_code(tok)
    # Fast-forward: monkeypatch time inside web_sessions.
    real_now = web_sessions._now
    monkeypatch.setattr(web_sessions, "_now", lambda: real_now() + 120.0)
    assert web_sessions.consume_exchange_code(code) is None


def test_login_with_code_sets_opaque_cookie(client_factory, tmp_data):
    from vezir.server import auth, web_sessions

    client = client_factory()
    tok = auth.issue("alice")
    code = web_sessions.mint_exchange_code(tok)

    resp = client.get(f"/login?code={code}&next=/s/abc")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/s/abc"
    sc = resp.headers.get("set-cookie", "")
    # Cookie value must be an opaque session id, NOT the bearer token.
    assert "vezir_session=" in sc
    assert tok not in sc
    assert "vezir_session=vzs_" in sc


def test_login_with_expired_code_returns_401(client_factory, monkeypatch, tmp_data):
    from vezir.server import auth, web_sessions

    client = client_factory()
    tok = auth.issue("alice")
    code = web_sessions.mint_exchange_code(tok)
    real_now = web_sessions._now
    monkeypatch.setattr(web_sessions, "_now", lambda: real_now() + 120.0)
    resp = client.get(f"/login?code={code}&next=/")
    assert resp.status_code == 401


def test_login_with_code_for_revoked_token_returns_401(client_factory, tmp_data):
    from vezir.server import auth, web_sessions

    client = client_factory()
    tok = auth.issue("alice")
    code = web_sessions.mint_exchange_code(tok)
    auth.revoke("alice")
    resp = client.get(f"/login?code={code}&next=/")
    assert resp.status_code == 401


def test_legacy_token_login_still_works_with_deprecation_header(client_factory, tmp_data):
    """One-release back-compat: ?token= must still sign the user in, but
    must add a Deprecation header so we can flag clients that need to be
    upgraded.
    """
    from vezir.server import auth

    client = client_factory()
    tok = auth.issue("alice")
    resp = client.get(f"/login?token={tok}&next=/")
    assert resp.status_code == 303
    assert resp.headers.get("Deprecation") == "true"
    assert "Warning" in resp.headers
    # And it should still issue an opaque session cookie, not echo the bearer.
    sc = resp.headers.get("set-cookie", "")
    assert tok not in sc
    assert "vezir_session=vzs_" in sc


def test_logout_invalidates_session(client_factory, tmp_data):
    from vezir.server import auth, web_sessions

    client = client_factory()
    tok = auth.issue("alice")
    code = web_sessions.mint_exchange_code(tok)

    r1 = client.get(f"/login?code={code}&next=/")
    set_cookie = r1.headers["set-cookie"]
    # Pull out the opaque sid value for direct inspection.
    sid = None
    for part in set_cookie.split(";"):
        if part.strip().startswith("vezir_session="):
            sid = part.strip().split("=", 1)[1]
    assert sid is not None
    result = web_sessions.lookup_session(sid)
    assert result is not None
    assert result[0] == "alice"

    client.cookies.set("vezir_session", sid)
    r2 = client.get("/logout")
    assert r2.status_code == 303
    # Session is now invalid.
    assert web_sessions.lookup_session(sid) is None


def test_upload_response_uses_code_not_token(client_factory, tmp_data):
    from vezir.server import auth
    client = client_factory()
    tok = auth.issue("alice")
    wav = _tiny_wav_bytes()

    resp = client.post(
        "/upload",
        headers=_bearer(tok),
        files={"audio": ("foo.wav", wav, "audio/wav")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "dashboard_login_url" in body
    url = body["dashboard_login_url"]
    # Bearer must not appear; an exchange code must.
    assert tok not in url
    assert "code=vzx_" in url


def test_dashboard_with_opaque_session_cookie(client_factory, tmp_data):
    from vezir.server import auth, web_sessions

    client = client_factory()
    tok = auth.issue("alice")
    code = web_sessions.mint_exchange_code(tok)
    client.get(f"/login?code={code}&next=/")
    resp = client.get("/")
    assert resp.status_code == 200


def test_api_still_requires_bearer_not_session_cookie(client_factory, tmp_data):
    from vezir.server import auth, web_sessions

    client = client_factory()
    tok = auth.issue("alice")
    code = web_sessions.mint_exchange_code(tok)
    client.get(f"/login?code={code}&next=/")
    # Cookie alone must NOT grant /api/* access (programmatic surface).
    resp = client.get("/api/sessions")
    assert resp.status_code == 401
    # Bearer still works.
    resp2 = client.get("/api/sessions", headers=_bearer(tok))
    assert resp2.status_code == 200


# ── POST /api/exchange-code ─────────────────────────────────────────────────


def test_exchange_code_endpoint_returns_code_url(client_factory):
    from vezir.server import auth
    client = client_factory()
    tok = auth.issue("alice")
    resp = client.post(
        "/api/exchange-code?next=/label/abc123",
        headers=_bearer(tok),
    )
    assert resp.status_code == 200
    url = resp.json()["login_url"]
    assert "code=vzx_" in url
    assert tok not in url
    assert "%2Flabel%2Fabc123" in url


def test_exchange_code_endpoint_rejects_missing_bearer(client_factory):
    client = client_factory()
    resp = client.post("/api/exchange-code?next=/")
    assert resp.status_code == 401


def test_exchange_code_endpoint_code_is_consumable(client_factory):
    """The code returned by /api/exchange-code must actually work at /login."""
    from vezir.server import auth
    client = client_factory()
    tok = auth.issue("alice")
    resp = client.post(
        "/api/exchange-code?next=/s/test",
        headers=_bearer(tok),
    )
    login_url = resp.json()["login_url"]
    # Extract the relative path (TestClient needs relative URLs).
    from urllib.parse import urlparse
    path_and_query = urlparse(login_url)._replace(scheme="", netloc="").geturl()
    r2 = client.get(path_and_query)
    assert r2.status_code == 303
    assert r2.headers["location"] == "/s/test"


# ── Patch 3: rate limiting ──────────────────────────────────────────────────


@pytest.fixture
def ratelimit_enabled(monkeypatch):
    """Flip the rate limiter on for this test and reset bucket state."""
    monkeypatch.setenv("VEZIR_DISABLE_RATELIMIT", "0")
    from vezir.server import ratelimit
    ratelimit._reset_for_tests()
    yield
    ratelimit._reset_for_tests()


def test_login_rate_limit_blocks_burst(client_factory, ratelimit_enabled):
    """20 login attempts/min cap. Burst of 30 should produce some 429s."""
    client = client_factory()
    saw_429 = False
    for i in range(30):
        resp = client.post("/login", data={"token": "vzr_bogus", "next": "/"})
        if resp.status_code == 429:
            saw_429 = True
            assert "Retry-After" in resp.headers
            assert int(resp.headers["Retry-After"]) >= 1
            break
    assert saw_429, "rate limiter never tripped within 30 attempts"


def test_upload_rate_limit_per_token(client_factory, ratelimit_enabled, tmp_data):
    """The /upload bucket keys on the bearer token, not the IP, so two
    tokens get independent quotas even from the same client.
    """
    from vezir.server import auth

    client = client_factory()
    tok_a = auth.issue("alice")
    tok_b = auth.issue("bob")
    wav = _tiny_wav_bytes()

    # Burn alice's bucket (10 / min). 11th must 429.
    saw_429 = False
    for _ in range(12):
        resp = client.post(
            "/upload",
            headers=_bearer(tok_a),
            files={"audio": ("x.wav", wav, "audio/wav")},
        )
        if resp.status_code == 429:
            saw_429 = True
            break
    assert saw_429

    # Bob still has a fresh bucket.
    resp = client.post(
        "/upload",
        headers=_bearer(tok_b),
        files={"audio": ("y.wav", wav, "audio/wav")},
    )
    assert resp.status_code == 200, resp.text


def test_ratelimit_disabled_by_env(client_factory, monkeypatch):
    """With VEZIR_DISABLE_RATELIMIT=1 (set by conftest) the limiter is a no-op."""
    from vezir.server import ratelimit, auth
    ratelimit._reset_for_tests()

    client = client_factory()
    # 200 login attempts, no 429 ever.
    for _ in range(60):
        resp = client.post("/login", data={"token": "vzr_bogus", "next": "/"})
        assert resp.status_code != 429
