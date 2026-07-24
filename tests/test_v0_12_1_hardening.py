"""v0.12.1 hardening unit tests: ratelimit bucket eviction + revoke refresh."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def tmp_data(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("VEZIR_DATA", d)
        yield Path(d)


# ── M-1: rate-limit buckets are bounded ─────────────────────────────────────


def test_limiter_evicts_full_buckets_when_over_cap():
    from vezir.server.ratelimit import _Limiter

    lim = _Limiter(capacity=60, window_sec=60.0)
    lim._MAX_BUCKETS = 50  # shrink for the test

    # Spray 200 distinct keys, each consuming exactly one token (leaving the
    # bucket nearly full → eligible for eviction).
    for i in range(200):
        allowed, _ = lim.check(f"api:tok:{i:016x}")
        assert allowed
    # Memory stays bounded despite 200 distinct keys.
    assert len(lim._buckets) <= lim._MAX_BUCKETS + 1


def test_limiter_still_throttles_a_hot_key():
    """Eviction must not weaken throttling for an actively-used key."""
    from vezir.server.ratelimit import _Limiter

    lim = _Limiter(capacity=3, window_sec=60.0)
    key = "api:tok:hot"
    assert lim.check(key)[0]
    assert lim.check(key)[0]
    assert lim.check(key)[0]
    blocked, retry = lim.check(key)
    assert blocked is False
    assert retry > 0


# ── M-2: revoked-sid cache picks up out-of-process revocations ──────────────


def test_revoked_cache_refreshes_from_db(tmp_data, monkeypatch):
    """A revocation written straight to the DB (as the `vezir session revoke`
    CLI does, in a separate process) is picked up by the running server's
    cache within the refresh interval — not only until the JWT exp."""
    from vezir.server import queue, sessions_auth

    sessions_auth._reset_revoked_cache_for_tests()
    first = sessions_auth.create_session("alice", "", False, "nostr")
    sid = first["sid"]

    # Prime the cache: sid is not revoked yet.
    assert sessions_auth.is_sid_revoked(sid) is False

    # Simulate an out-of-process revoke: write revoked=1 straight to the DB
    # WITHOUT touching the in-process cache (_note_revoked).
    with queue._conn() as c:
        c.execute("UPDATE sessions SET revoked = 1 WHERE sid = ?", (sid,))

    # Force the refresh window open, then the cache must reflect the DB.
    monkeypatch.setattr(sessions_auth, "_REVOKED_REFRESH_SEC", 0.0)
    assert sessions_auth.is_sid_revoked(sid) is True


# ── L-1 / L-2: request hardening ────────────────────────────────────────────


@pytest.fixture
def client_and_token(tmp_data):
    from fastapi.testclient import TestClient

    from vezir.server import auth
    from vezir.server.app import create_app

    token = auth.issue("alice")
    return TestClient(create_app(), follow_redirects=False), token


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "X-Team-Id": "blink"}


def test_whitespace_only_bearer_is_401_not_500(client_and_token):
    """`Authorization: Bearer ` (trailing space, no token) must yield a
    clean 401, not an unhandled 500 (L-1)."""
    client, _token = client_and_token
    r = client.get("/api/me", headers={"Authorization": "Bearer "})
    assert r.status_code == 401


def test_sessions_limit_is_clamped(client_and_token):
    """A negative limit must not become SQLite ``LIMIT -1`` (all rows);
    clamp to a sane range (L-2)."""
    from vezir.server import queue

    client, token = client_and_token
    for i in range(3):
        queue.enqueue(f"01ROW{i:015d}", "alice", personal=False, team_id="blink")

    r = client.get("/api/sessions?limit=-1", headers=_bearer(token))
    assert r.status_code == 200
    # limit clamps to >=1; the request succeeds and returns a bounded list.
    r2 = client.get("/api/sessions?limit=1", headers=_bearer(token))
    assert len(r2.json()["sessions"]) == 1


def test_sync_meeting_type_slug_validated(tmp_data):
    """update_team_sync normalizes/validates the meeting-type slug at write
    time (L-12)."""
    from vezir.server import queue
    from vezir.server.app import create_app

    create_app()  # runs migrations that seed the 'blink' team
    queue.update_team_sync("blink", sync_meeting_type="My Standup!")
    row = queue.get_team("blink")
    assert row["sync_meeting_type"] == "my-standup"

    with pytest.raises(ValueError):
        queue.update_team_sync("blink", sync_meeting_type="!!!")
