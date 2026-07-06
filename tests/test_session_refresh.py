"""Rotating refresh-token sessions with reuse detection (0.8.10).

Covers ``vezir.server.sessions_auth`` and the ``/api/auth/refresh`` +
``/api/auth/logout`` endpoints:

  * create → access JWT works, refresh token issued.
  * rotate → new pair; the OLD access token still validates until its own
    exp, the OLD refresh token is single-use.
  * reuse detection → replaying a consumed refresh token revokes the whole
    family (RFC 9700).
  * idle expiry + absolute cap → refresh rejected past the windows.
  * revocation → an explicit revoke stops further refreshes.
  * one-generation grace does NOT resurrect the family on a *stale* replay.
"""
from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import time
from pathlib import Path

import pytest

coincurve = pytest.importorskip("coincurve")
from coincurve import PrivateKey  # noqa: E402

LOGIN_PATH = "/api/auth/nostr/login"
REFRESH_PATH = "/api/auth/refresh"
LOGOUT_PATH = "/api/auth/logout"


@pytest.fixture
def tmp_data(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("VEZIR_DATA", d)
        yield Path(d)


@pytest.fixture
def client(tmp_data):
    from fastapi.testclient import TestClient

    from vezir.server.app import create_app

    return TestClient(create_app(), follow_redirects=False)


def _canonical_id(event: dict) -> str:
    serialized = json.dumps(
        [0, event["pubkey"], event["created_at"], event["kind"],
         event["tags"], event["content"]],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _login_header(priv: PrivateKey, url: str) -> str:
    pubkey = priv.public_key_xonly.format().hex()
    event = {
        "pubkey": pubkey,
        "created_at": int(time.time()),
        "kind": 27235,
        "tags": [["u", url], ["method", "POST"]],
        "content": "",
    }
    event["id"] = _canonical_id(event)
    event["sig"] = priv.sign_schnorr(bytes.fromhex(event["id"])).hex()
    raw = json.dumps(event).encode("utf-8")
    return "Nostr " + base64.b64encode(raw).decode("ascii")


def _login(client) -> dict:
    """Perform a full nostr login for a fresh allowlisted key; return body."""
    from vezir.server import nostr_members

    priv = PrivateKey()
    pubkey = priv.public_key_xonly.format().hex()
    nostr_members.add(pubkey, "alice")
    header = _login_header(priv, f"http://testserver{LOGIN_PATH}")
    resp = client.post(LOGIN_PATH, headers={"Authorization": header})
    assert resp.status_code == 200, resp.text
    return resp.json()


# ── sessions_auth unit level ────────────────────────────────────────────────


def test_create_and_rotate(tmp_data):
    from vezir.server import sessions_auth

    first = sessions_auth.create_session("alice", "ab" * 32, False, "nostr")
    assert first["refresh_token"].startswith("vzrt_")
    assert first["access_jwt"].count(".") == 2
    sid = first["sid"]

    second = sessions_auth.rotate(first["refresh_token"])
    assert second["sid"] == sid  # same family
    assert second["refresh_token"] != first["refresh_token"]  # rotated
    assert second["access_jwt"] != first["access_jwt"]


def test_rotated_out_refresh_token_is_single_use(tmp_data, monkeypatch):
    from vezir import config
    from vezir.server import sessions_auth

    # Strict mode (grace window off): any replay of a consumed token is
    # confirmed reuse.  The grace behavior has its own tests below.
    monkeypatch.setattr(config, "refresh_grace_seconds", lambda: 0)

    first = sessions_auth.create_session("alice", "", False, "nostr")
    sessions_auth.rotate(first["refresh_token"])

    # The just-consumed token is now the family's prev-hash: presenting it
    # is reuse of a consumed token → family revoked.
    with pytest.raises(sessions_auth.SessionError) as exc:
        sessions_auth.rotate(first["refresh_token"])
    assert exc.value.reuse is True


def test_reuse_detection_revokes_family(tmp_data, monkeypatch):
    from vezir import config
    from vezir.server import sessions_auth

    monkeypatch.setattr(config, "refresh_grace_seconds", lambda: 0)

    first = sessions_auth.create_session("alice", "", False, "nostr")
    second = sessions_auth.rotate(first["refresh_token"])

    # Attacker replays the old (consumed) refresh token → family killed.
    with pytest.raises(sessions_auth.SessionError):
        sessions_auth.rotate(first["refresh_token"])

    # Now even the *legitimate* current refresh token is dead.
    with pytest.raises(sessions_auth.SessionError) as exc:
        sessions_auth.rotate(second["refresh_token"])
    assert "revoked" in str(exc.value)


# ── lost-response grace window (v0.11.0) ────────────────────────────────────


def test_grace_reissues_within_window(tmp_data):
    """A replay of the one-generation-old token WITHIN the grace window
    (default 60s) is a lost-response retry: re-issue instead of revoking.
    Flaky-link clients no longer lose their whole session to a dropped
    /refresh response."""
    from vezir.server import sessions_auth

    first = sessions_auth.create_session("alice", "", False, "nostr")
    second = sessions_auth.rotate(first["refresh_token"])
    sid = second["sid"]

    # The rotation response was "lost"; the client retries with the token
    # it still holds.  This is within seconds of the rotation → grace.
    retried = sessions_auth.rotate(first["refresh_token"])
    assert retried["sid"] == sid  # same family, not revoked
    assert retried["refresh_token"] not in (
        first["refresh_token"], second["refresh_token"],
    )

    # The re-issued pair works normally afterwards.
    nxt = sessions_auth.rotate(retried["refresh_token"])
    assert nxt["sid"] == sid


def test_grace_retry_is_repeatable_within_window(tmp_data):
    """The SAME lost-response token can be retried more than once inside
    the window (the presented hash stays in prev_refresh_hash)."""
    from vezir.server import sessions_auth

    first = sessions_auth.create_session("alice", "", False, "nostr")
    sessions_auth.rotate(first["refresh_token"])

    a = sessions_auth.rotate(first["refresh_token"])
    b = sessions_auth.rotate(first["refresh_token"])
    assert a["sid"] == b["sid"]


def test_grace_expired_window_revokes(tmp_data, monkeypatch):
    """Outside the grace window the replay is confirmed reuse → family
    revoked (RFC 9700)."""
    from vezir.server import sessions_auth

    first = sessions_auth.create_session("alice", "", False, "nostr")
    second = sessions_auth.rotate(first["refresh_token"])

    # Jump time forward past the 60s window.
    real_now = sessions_auth._now()
    monkeypatch.setattr(sessions_auth, "_now", lambda: real_now + 3600)

    with pytest.raises(sessions_auth.SessionError) as exc:
        sessions_auth.rotate(first["refresh_token"])
    assert exc.value.reuse is True

    # The whole family is dead, including the legitimate current token.
    with pytest.raises(sessions_auth.SessionError):
        sessions_auth.rotate(second["refresh_token"])


def test_grace_never_applies_to_revoked_family(tmp_data):
    from vezir.server import sessions_auth

    first = sessions_auth.create_session("alice", "", False, "nostr")
    second = sessions_auth.rotate(first["refresh_token"])
    sessions_auth.revoke_session(second["sid"])

    with pytest.raises(sessions_auth.SessionError):
        sessions_auth.rotate(first["refresh_token"])


def test_unknown_refresh_token_rejected(tmp_data):
    from vezir.server import sessions_auth

    with pytest.raises(sessions_auth.SessionError):
        sessions_auth.rotate("vzrt_nonexistent")
    with pytest.raises(sessions_auth.SessionError):
        sessions_auth.rotate("not-even-a-refresh-token")


def test_idle_expiry_rejected(tmp_data, monkeypatch):
    from vezir import config
    from vezir.server import sessions_auth

    # Idle TTL of 0 → the refresh window is already closed on next use.
    monkeypatch.setattr(config, "refresh_idle_ttl_seconds", lambda: 0)
    first = sessions_auth.create_session("alice", "", False, "nostr")
    with pytest.raises(sessions_auth.SessionError) as exc:
        sessions_auth.rotate(first["refresh_token"])
    assert "idle" in str(exc.value)


def test_absolute_cap_rejected(tmp_data, monkeypatch):
    from vezir import config
    from vezir.server import sessions_auth

    monkeypatch.setattr(config, "session_max_ttl_seconds", lambda: 0)
    first = sessions_auth.create_session("alice", "", False, "nostr")
    with pytest.raises(sessions_auth.SessionError) as exc:
        sessions_auth.rotate(first["refresh_token"])
    assert "absolute" in str(exc.value)


def test_revoke_session_blocks_refresh(tmp_data):
    from vezir.server import sessions_auth

    first = sessions_auth.create_session("alice", "", False, "nostr")
    assert sessions_auth.revoke_session(first["sid"]) is True
    with pytest.raises(sessions_auth.SessionError):
        sessions_auth.rotate(first["refresh_token"])
    # Revoking again is a no-op (already revoked).
    assert sessions_auth.revoke_session(first["sid"]) is False


def test_revoke_all_for(tmp_data):
    from vezir.server import sessions_auth

    sessions_auth.create_session("alice", "", False, "nostr")
    sessions_auth.create_session("alice", "", False, "nostr")
    sessions_auth.create_session("bob", "", False, "nostr")
    assert sessions_auth.revoke_all_for("alice") == 2
    # Bob is untouched.
    assert sessions_auth.revoke_all_for("bob") == 1


# ── endpoint level ──────────────────────────────────────────────────────────


def test_refresh_endpoint_rotates(client, tmp_data):
    body = _login(client)
    r = client.post(REFRESH_PATH, json={"refresh_token": body["refresh_token"]})
    assert r.status_code == 200, r.text
    new = r.json()
    assert new["refresh_token"] != body["refresh_token"]
    # New access token works on /api/me.
    me = client.get(
        "/api/me", headers={"Authorization": f"Bearer {new['access_jwt']}"}
    )
    assert me.status_code == 200
    assert me.json()["github"] == "alice"


def test_refresh_endpoint_reuse_returns_401(client, tmp_data, monkeypatch):
    from vezir import config

    # Strict mode: with the grace window disabled, an immediate replay of
    # the consumed refresh token is a 401 (with grace on it would be a
    # lost-response re-issue — covered by the grace tests above).
    monkeypatch.setattr(config, "refresh_grace_seconds", lambda: 0)

    body = _login(client)
    client.post(REFRESH_PATH, json={"refresh_token": body["refresh_token"]})
    # Replay the consumed refresh token.
    r = client.post(REFRESH_PATH, json={"refresh_token": body["refresh_token"]})
    assert r.status_code == 401
    assert "refresh failed" in r.json()["detail"]


def test_refresh_unknown_token_401(client, tmp_data):
    r = client.post(REFRESH_PATH, json={"refresh_token": "vzrt_bogus"})
    assert r.status_code == 401


def test_logout_revokes_session(client, tmp_data):
    body = _login(client)
    access = body["access_jwt"]

    out = client.post(LOGOUT_PATH, headers={"Authorization": f"Bearer {access}"})
    assert out.status_code == 200
    assert out.json()["revoked"] is True

    # After logout the refresh token can no longer rotate.
    r = client.post(REFRESH_PATH, json={"refresh_token": body["refresh_token"]})
    assert r.status_code == 401


def test_logout_idempotent_without_session(client, tmp_data):
    # A vzr_ bearer has no session family; logout is a no-op success.
    from vezir.server import auth

    token = auth.issue("carol")
    out = client.post(LOGOUT_PATH, headers={"Authorization": f"Bearer {token}"})
    assert out.status_code == 200
    assert out.json()["revoked"] is False
