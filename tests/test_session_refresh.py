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
    (default 60s) is a lost-response retry: replay the EXACT pair the
    (lost) rotation produced instead of revoking.  Flaky-link clients no
    longer lose their whole session to a dropped /refresh response, AND a
    stale token can no longer fork a fresh family (v0.12.1 hijack fix)."""
    from vezir.server import sessions_auth

    first = sessions_auth.create_session("alice", "", False, "nostr")
    second = sessions_auth.rotate(first["refresh_token"])
    sid = second["sid"]

    # The rotation response was "lost"; the client retries with the token
    # it still holds.  Within the window → replay the identical pair.
    retried = sessions_auth.rotate(first["refresh_token"])
    assert retried["sid"] == sid  # same family, not revoked
    # Idempotent: the retry returns the SAME pair the first rotation minted,
    # never a new generation (that was the old hijack vector).
    assert retried["refresh_token"] == second["refresh_token"]
    assert retried["access_jwt"] == second["access_jwt"]

    # The re-issued (current) token works normally afterwards.
    nxt = sessions_auth.rotate(second["refresh_token"])
    assert nxt["sid"] == sid


def test_grace_retry_is_repeatable_within_window(tmp_data):
    """The SAME lost-response token can be retried more than once inside
    the window, each time replaying the identical cached pair."""
    from vezir.server import sessions_auth

    first = sessions_auth.create_session("alice", "", False, "nostr")
    second = sessions_auth.rotate(first["refresh_token"])

    a = sessions_auth.rotate(first["refresh_token"])
    b = sessions_auth.rotate(first["refresh_token"])
    assert a["sid"] == b["sid"] == second["sid"]
    # Every replay is byte-identical — no family fork, no window slide.
    assert a["refresh_token"] == b["refresh_token"] == second["refresh_token"]


def test_grace_does_not_slide_window(tmp_data, monkeypatch):
    """A stale-token holder cannot renew indefinitely: the grace window is
    anchored to the original rotation, so once it lapses the replay is
    confirmed reuse even if the attacker replayed steadily inside it."""
    from vezir.server import sessions_auth

    first = sessions_auth.create_session("alice", "", False, "nostr")
    second = sessions_auth.rotate(first["refresh_token"])

    base = sessions_auth._now()
    # Replay repeatedly, always inside the (15 s, since 0.27.0) window
    # measured from each previous replay — the OLD code would keep
    # re-anchoring and never expire.  Here it must expire relative to the
    # single real rotation.
    monkeypatch.setattr(sessions_auth, "_now", lambda: base + 8)
    sessions_auth.rotate(first["refresh_token"])  # still cached → replay
    monkeypatch.setattr(sessions_auth, "_now", lambda: base + 3600)
    with pytest.raises(sessions_auth.SessionError) as exc:
        sessions_auth.rotate(first["refresh_token"])
    assert exc.value.reuse is True
    # Legitimate current token also dies (family revoked on reuse).
    with pytest.raises(sessions_auth.SessionError):
        sessions_auth.rotate(second["refresh_token"])


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


def test_refresh_grace_rejects_mismatched_user_agent(client, tmp_data):
    body = _login(client)
    payload = {"refresh_token": body["refresh_token"]}
    rotated = client.post(
        REFRESH_PATH, json=payload, headers={"User-Agent": "vezir-client"}
    )
    assert rotated.status_code == 200, rotated.text

    replay = client.post(
        REFRESH_PATH, json=payload, headers={"User-Agent": "different-client"}
    )
    assert replay.status_code == 401
    assert "reuse" in replay.json()["detail"]

    current = client.post(
        REFRESH_PATH,
        json={"refresh_token": rotated.json()["refresh_token"]},
        headers={"User-Agent": "vezir-client"},
    )
    assert current.status_code == 401


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


# ── H-2 (security audit 2026-10-30): allowlist changes end live sessions ────
#
# Before: removing an npub/email only deleted the allowlist row; rotate()
# re-minted from the sessions row, so a removed identity kept refreshing —
# with its login-time is_admin bit — until the 30-day absolute cap.


def _login_as(client, github: str, *, is_admin: bool = False):
    from vezir.server import nostr_members

    priv = PrivateKey()
    pubkey = priv.public_key_xonly.format().hex()
    nostr_members.add(pubkey, github, is_admin=is_admin)
    header = _login_header(priv, f"http://testserver{LOGIN_PATH}")
    resp = client.post(LOGIN_PATH, headers={"Authorization": header})
    assert resp.status_code == 200, resp.text
    return pubkey, resp.json()


def test_removed_npub_cannot_refresh_and_access_jwt_dies(client, tmp_data):
    from vezir.server import nostr_members

    pubkey, body = _login_as(client, "mallory")
    # Sanity: the pair works.
    r = client.post(REFRESH_PATH, json={"refresh_token": body["refresh_token"]})
    assert r.status_code == 200
    body = r.json()
    me = client.get("/api/me", headers={"Authorization": f"Bearer {body['access_jwt']}"})
    assert me.status_code == 200

    assert nostr_members.remove(pubkey) == 1

    # Refresh is refused ...
    r = client.post(REFRESH_PATH, json={"refresh_token": body["refresh_token"]})
    assert r.status_code == 401
    # ... and the still-unexpired access JWT is dead too (family revoked).
    me = client.get("/api/me", headers={"Authorization": f"Bearer {body['access_jwt']}"})
    assert me.status_code == 401


def test_removed_google_email_cannot_refresh(client, tmp_data):
    from vezir.server import google_members, sessions_auth

    google_members.add("eve@example.test", "eve")
    pair = sessions_auth._create_session_raw("eve", "", False, "google")
    assert sessions_auth.rotate(pair["refresh_token"])["sid"] == pair["sid"]

    google_members.remove("eve@example.test")
    with pytest.raises(sessions_auth.SessionError):
        sessions_auth.rotate(pair["refresh_token"])
    assert sessions_auth.is_sid_revoked(pair["sid"])


def test_demoted_admin_loses_admin_on_next_rotation_and_live_jwt(client, tmp_data):
    from vezir.server import nostr_auth, nostr_members

    pubkey, body = _login_as(client, "root", is_admin=True)
    assert nostr_auth.verify_session_jwt(body["access_jwt"])[1] is True

    # Demote (same handle, admin bit off).
    nostr_members.add(pubkey, "root", is_admin=False)

    # The live access JWT is revoked immediately ...
    assert client.get(
        "/admin/teams", headers={"Authorization": f"Bearer {body['access_jwt']}"}
    ).status_code == 401
    # ... and refresh is refused for the revoked family.
    r = client.post(REFRESH_PATH, json={"refresh_token": body["refresh_token"]})
    assert r.status_code == 401


def test_rotation_refreshes_admin_bit_from_allowlist(tmp_data):
    """A promotion lands on the next refresh without re-login."""
    from vezir.server import nostr_auth, nostr_members, sessions_auth

    pubkey = "cd" * 32
    nostr_members.add(pubkey, "grace", is_admin=False)
    pair = sessions_auth._create_session_raw("grace", pubkey, False, "nostr")
    assert nostr_auth.verify_session_jwt(pair["access_jwt"])[1] is False

    nostr_members.add(pubkey, "grace", is_admin=True)  # promotion: no revocation
    nxt = sessions_auth.rotate(pair["refresh_token"])
    assert nxt["sid"] == pair["sid"]
    assert nostr_auth.verify_session_jwt(nxt["access_jwt"])[1] is True


def test_repointed_handle_revokes_old_sessions(tmp_data):
    from vezir.server import nostr_members, sessions_auth

    pubkey = "ef" * 32
    nostr_members.add(pubkey, "old-handle")
    pair = sessions_auth._create_session_raw("old-handle", pubkey, False, "nostr")
    nostr_members.add(pubkey, "new-handle")  # same key, different github
    with pytest.raises(sessions_auth.SessionError):
        sessions_auth.rotate(pair["refresh_token"])


def test_rotation_fails_closed_when_lookup_errors(tmp_data, monkeypatch):
    from vezir.server import nostr_members, sessions_auth

    pubkey = "01" * 32
    nostr_members.add(pubkey, "heidi")
    pair = sessions_auth._create_session_raw("heidi", pubkey, False, "nostr")

    def boom(_pk):
        raise RuntimeError("db down")

    monkeypatch.setattr(nostr_members, "lookup_npub", boom)
    with pytest.raises(sessions_auth.SessionError):
        sessions_auth.rotate(pair["refresh_token"])


# ── M-11 / L-1 (0.27.0): reuse memory across generations; concurrent grace ──


def test_token_two_generations_old_revokes_family(tmp_data, monkeypatch):
    """Before 0.27.0 only prev_refresh_hash was remembered: a token >= 2
    rotations old was a quiet 401 with no revocation."""
    from vezir.server import sessions_auth

    monkeypatch.setattr(config_mod(), "refresh_grace_seconds", lambda: 0)
    g1 = sessions_auth.create_session("alice", "", False, "nostr")
    g2 = sessions_auth.rotate(g1["refresh_token"])
    g3 = sessions_auth.rotate(g2["refresh_token"])
    g4 = sessions_auth.rotate(g3["refresh_token"])

    with pytest.raises(sessions_auth.SessionError) as exc:
        sessions_auth.rotate(g1["refresh_token"])  # three generations old
    assert exc.value.reuse is True
    assert sessions_auth.is_sid_revoked(g1["sid"])
    with pytest.raises(sessions_auth.SessionError):
        sessions_auth.rotate(g4["refresh_token"])  # live token dies with the family


def test_unknown_random_token_does_not_revoke_anything(tmp_data):
    from vezir.server import sessions_auth

    g1 = sessions_auth.create_session("alice", "", False, "nostr")
    with pytest.raises(sessions_auth.SessionError) as exc:
        sessions_auth.rotate("vzrt_" + "z" * 43)
    assert exc.value.reuse is False
    assert sessions_auth.rotate(g1["refresh_token"])["sid"] == g1["sid"]


def test_history_rows_follow_the_family(tmp_data):
    from vezir.server import queue, sessions_auth

    g1 = sessions_auth.create_session("alice", "", False, "nostr")
    g2 = sessions_auth.rotate(g1["refresh_token"])
    sessions_auth.rotate(g2["refresh_token"])
    with queue._conn() as c:
        n = c.execute(
            "SELECT COUNT(*) FROM session_refresh_history WHERE sid = ?", (g1["sid"],),
        ).fetchone()[0]
    assert n == 2


def test_concurrent_same_user_agent_refresh_shares_grace_pair(tmp_data):
    """L-1/L-2: same-client concurrent refreshes share one pair."""
    import threading

    from vezir.server import sessions_auth

    first = sessions_auth.create_session("alice", "", False, "nostr")
    results: list = []
    errors: list = []
    barrier = threading.Barrier(4)

    def go():
        barrier.wait()
        try:
            results.append(
                sessions_auth.rotate(first["refresh_token"], user_agent="vezir-client")
            )
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    ts = [threading.Thread(target=go) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errors, errors
    assert len({r["refresh_token"] for r in results}) == 1
    assert not sessions_auth.is_sid_revoked(first["sid"])
    # The family keeps working afterwards.
    assert sessions_auth.rotate(results[0]["refresh_token"])["sid"] == first["sid"]


def config_mod():
    from vezir import config

    return config
