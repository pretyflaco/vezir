"""Token model + rate-limit tests.

Covers what remains of v0.1.12 token hardening after v0.7.0:

* Token model: expires_at, last_used_at, is_admin, label, hmac compare.
* require_admin gating on /admin/* routes (now /admin/teams).
* Rate limiting on /upload and /api/sessions.

v0.7.0 removals: cookie sessions, exchange codes, the HTML
/admin/enroll page, and the /login flow are gone.  Tests targeting
those surfaces were dropped wholesale.
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


def _team_headers(token: str, team: str = "blink") -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "X-Team-Id": team,
    }


def _token_rows() -> list[dict]:
    """Read the tokens table from the active VEZIR_DATA vezir.sqlite."""
    import sqlite3

    from vezir import config

    conn = sqlite3.connect(str(config.queue_db_path()))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM tokens")]
    finally:
        conn.close()


def _set_token_field(token_hash_prefix_github: str, field: str, value) -> None:
    """Update a single token row's field by github handle (test helper)."""
    import sqlite3

    from vezir import config

    conn = sqlite3.connect(str(config.queue_db_path()))
    try:
        conn.execute(
            f"UPDATE tokens SET {field} = ? WHERE github = ?",
            (value, token_hash_prefix_github),
        )
        conn.commit()
    finally:
        conn.close()


def _tiny_wav_bytes() -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 16000)
    return buf.getvalue()


# ── token model ──────────────────────────────────────────────────────────────


def test_issue_records_new_fields(tmp_data):
    from vezir.server import auth

    auth.issue("alice", expires_in_seconds=3600, is_admin=True, label="laptop")
    rows = _token_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["github"] == "alice"
    assert bool(row["is_admin"]) is True
    assert row["label"] == "laptop"
    assert row["expires_at"] is not None
    assert row["last_used_at"] is None
    # plaintext is never persisted
    assert "token" not in row
    assert "plaintext" not in row
    # v0.7.0+: team_id no longer baked into the token (no such column)
    assert "team_id" not in row


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

    tok = auth.issue("alice", expires_in_seconds=10)
    _set_token_field(
        "alice",
        "expires_at",
        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 60)),
    )

    assert auth.lookup(tok) is None
    assert auth.is_admin_token(tok) is False


def test_legacy_row_without_expires_at_still_works(tmp_data):
    """A token row with NULL expires_at is treated as 'no expiry'.

    Mirrors a pre-0.1.12 row imported by the 0.7.2 migration where the
    optional columns are NULL.  Required for upgrade safety.
    """
    from vezir.server import auth

    tok = auth.issue("alice")
    _set_token_field("alice", "expires_at", None)
    _set_token_field("alice", "last_used_at", None)
    _set_token_field("alice", "is_admin", 0)
    _set_token_field("alice", "label", None)

    assert auth.lookup(tok) == "alice"


def test_last_used_at_updates_on_success(tmp_data):
    from vezir.server import auth

    tok = auth.issue("alice")
    assert _token_rows()[0]["last_used_at"] is None
    assert auth.lookup(tok) == "alice"
    after = _token_rows()[0]["last_used_at"]
    assert after is not None


def test_last_used_at_is_debounced(tmp_data, monkeypatch):
    """Two consecutive lookups should not write twice."""
    from vezir.server import auth

    tok = auth.issue("alice")
    auth.lookup(tok)
    first = _token_rows()[0]["last_used_at"]
    auth.lookup(tok)
    second = _token_rows()[0]["last_used_at"]
    assert first == second  # not touched again


def test_is_admin_token_distinguishes_roles(tmp_data):
    from vezir.server import auth
    admin = auth.issue("alice", is_admin=True)
    scribe = auth.issue("bob", is_admin=False)
    assert auth.is_admin_token(admin) is True
    assert auth.is_admin_token(scribe) is False
    assert auth.is_admin_token("vzr_bogus") is False


# ── require_admin gating (now /admin/teams) ──────────────────────────────────


def test_admin_teams_denies_non_admin_token(client_factory):
    from vezir.server import auth
    client = client_factory()
    scribe = auth.issue("bob", is_admin=False)
    resp = client.get("/admin/teams", headers=_bearer(scribe))
    assert resp.status_code == 403
    assert "admin" in resp.text.lower()


def test_admin_teams_allows_admin_token(client_factory):
    from vezir.server import auth
    client = client_factory()
    admin = auth.issue("alice", is_admin=True)
    resp = client.get("/admin/teams", headers=_bearer(admin))
    assert resp.status_code == 200


def test_admin_teams_rejects_missing_credentials(client_factory):
    client = client_factory()
    resp = client.get("/admin/teams")
    assert resp.status_code == 401


def test_admin_check_is_per_token_not_per_handle(client_factory):
    """A scribe-tier token must NOT inherit admin access from a separate
    admin-tier token issued to the same github handle.
    """
    from vezir.server import auth
    client = client_factory()
    _admin_tok = auth.issue("alice", is_admin=True)
    scribe_tok = auth.issue("alice", is_admin=False)
    resp = client.get("/admin/teams", headers=_bearer(scribe_tok))
    assert resp.status_code == 403


# ── X-Team-Id header enforcement ─────────────────────────────────────────────


def test_api_sessions_requires_x_team_id_header(client_factory):
    """v0.7.0: team-scoped endpoints require the X-Team-Id header."""
    from vezir.server import auth
    client = client_factory()
    tok = auth.issue("alice")
    resp = client.get("/api/sessions", headers=_bearer(tok))
    assert resp.status_code == 400
    assert "X-Team-Id" in resp.text


def test_api_sessions_rejects_non_member(client_factory):
    """A token whose handle is not in the requested team's memberships
    gets 403, NOT 200-with-empty list.  This is the v0.7.0 cross-team
    access control."""
    from vezir.server import auth
    client = client_factory()
    tok = auth.issue("alice")  # the conftest shim memberships alice into 'blink'
    resp = client.get(
        "/api/sessions",
        headers={
            "Authorization": f"Bearer {tok}",
            "X-Team-Id": "nonexistent-team",
        },
    )
    assert resp.status_code == 403


def test_api_sessions_accepts_member(client_factory):
    from vezir.server import auth
    client = client_factory()
    tok = auth.issue("alice")  # auto-memberships into 'blink'
    resp = client.get("/api/sessions", headers=_team_headers(tok))
    assert resp.status_code == 200
    assert "sessions" in resp.json()


# ── rate limiting ────────────────────────────────────────────────────────────


@pytest.fixture
def ratelimit_enabled(monkeypatch):
    """Flip the rate limiter on for this test and reset bucket state."""
    monkeypatch.setenv("VEZIR_DISABLE_RATELIMIT", "0")
    from vezir.server import ratelimit
    ratelimit._reset_for_tests()
    yield
    ratelimit._reset_for_tests()


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
            headers=_team_headers(tok_a),
            files={"audio": ("x.wav", wav, "audio/wav")},
        )
        if resp.status_code == 429:
            saw_429 = True
            break
    assert saw_429

    # Bob still has a fresh bucket.  bob also needs membership in blink.
    from vezir.server import queue
    queue.add_membership("bob", "blink", role="scribe")
    resp = client.post(
        "/upload",
        headers=_team_headers(tok_b),
        files={"audio": ("y.wav", wav, "audio/wav")},
    )
    assert resp.status_code == 200, resp.text


def test_ratelimit_disabled_by_env(client_factory, monkeypatch):
    """With VEZIR_DISABLE_RATELIMIT=1 (set by conftest) the limiter is a no-op."""
    from vezir.server import auth, ratelimit
    ratelimit._reset_for_tests()

    client = client_factory()
    tok = auth.issue("alice")
    # Many requests, no 429 ever.
    for _ in range(60):
        resp = client.get("/api/sessions", headers=_team_headers(tok))
        assert resp.status_code != 429


# ── DST/UTC: _parse_iso must interpret the timestamp as UTC ───────────────────


def test_parse_iso_is_utc_not_local_dst():
    """``_parse_iso`` parses ``...Z`` as UTC regardless of host TZ/DST.

    The pre-0.8.2 impl used ``mktime(...) - time.timezone`` which is off by
    the DST offset for part of the year.  Cross-check against calendar.timegm.
    """
    import calendar
    import time as _time

    from vezir.server.auth import _parse_iso

    ts = "2026-07-01T12:00:00Z"  # a date inside DST for northern TZs
    expected = float(calendar.timegm(_time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")))
    assert _parse_iso(ts) == expected
    # And a winter date too.
    ts2 = "2026-01-01T12:00:00Z"
    expected2 = float(calendar.timegm(_time.strptime(ts2, "%Y-%m-%dT%H:%M:%SZ")))
    assert _parse_iso(ts2) == expected2
    assert _parse_iso(None) is None
    assert _parse_iso("garbage") is None
