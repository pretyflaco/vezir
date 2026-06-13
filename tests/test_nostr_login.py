"""End-to-end tests for the nostr login flow + session-JWT auth.

Covers:
  * POST /api/auth/nostr/login with a real signed NIP-98 event.
  * Rejection of unknown (un-allowlisted) but valid signers.
  * The minted session JWT working as a Bearer token through the full
    auth chain: /api/me (require_bearer) and a team-scoped route
    (require_team_context).
  * vzr_ bearer tokens still working unchanged (no regression).
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


def _public_login_url(client) -> str:
    # TestClient default base is http://testserver
    return f"http://testserver{LOGIN_PATH}"


def test_login_unknown_pubkey_forbidden(client):
    priv = PrivateKey()
    header = _login_header(priv, _public_login_url(client))
    resp = client.post(LOGIN_PATH, headers={"Authorization": header})
    assert resp.status_code == 403
    assert "not authorized" in resp.json()["detail"]


def test_login_missing_header_unauthorized(client):
    resp = client.post(LOGIN_PATH)
    assert resp.status_code == 401


def test_login_success_returns_jwt(client, tmp_data):
    from vezir.server import nostr_members

    priv = PrivateKey()
    pubkey = priv.public_key_xonly.format().hex()
    nostr_members.add(pubkey, "alice", is_admin=False, label="laptop")

    header = _login_header(priv, _public_login_url(client))
    resp = client.post(LOGIN_PATH, headers={"Authorization": header})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["github"] == "alice"
    assert body["is_admin"] is False
    assert body["npub"] == pubkey
    assert body["expires_in"] == 24 * 60 * 60
    assert body["session_jwt"].count(".") == 2


def test_session_jwt_works_on_api_me(client, tmp_data):
    from vezir.server import nostr_members

    priv = PrivateKey()
    pubkey = priv.public_key_xonly.format().hex()
    nostr_members.add(pubkey, "alice")

    header = _login_header(priv, _public_login_url(client))
    jwt_token = client.post(
        LOGIN_PATH, headers={"Authorization": header}
    ).json()["session_jwt"]

    # The JWT must be accepted by require_bearer (/api/me).
    me = client.get("/api/me", headers={"Authorization": f"Bearer {jwt_token}"})
    assert me.status_code == 200, me.text
    assert me.json()["github"] == "alice"


def test_session_jwt_works_on_team_scoped_route(client, tmp_data):
    """A nostr session must traverse require_team_context like a token."""
    from vezir.server import nostr_members, queue

    # The 0.6.0 migration already seeds a 'blink' team; reuse it and add
    # a membership for alice so X-Team-Id validates.
    team = queue.get_team("blink")
    queue.add_membership("alice", team["id"], role="scribe", added_by="test")

    priv = PrivateKey()
    pubkey = priv.public_key_xonly.format().hex()
    nostr_members.add(pubkey, "alice")

    header = _login_header(priv, _public_login_url(client))
    jwt_token = client.post(
        LOGIN_PATH, headers={"Authorization": header}
    ).json()["session_jwt"]

    resp = client.get(
        "/api/sessions",
        headers={
            "Authorization": f"Bearer {jwt_token}",
            "X-Team-Id": team["id"],
        },
    )
    assert resp.status_code == 200, resp.text


def test_admin_npub_gets_admin_jwt(client, tmp_data):
    from vezir.server import nostr_members

    priv = PrivateKey()
    pubkey = priv.public_key_xonly.format().hex()
    nostr_members.add(pubkey, "boss", is_admin=True)

    header = _login_header(priv, _public_login_url(client))
    body = client.post(
        LOGIN_PATH, headers={"Authorization": header}
    ).json()
    assert body["is_admin"] is True

    me = client.get(
        "/api/me", headers={"Authorization": f"Bearer {body['session_jwt']}"}
    )
    assert me.json()["is_admin"] is True


def test_vzr_token_still_works(client, tmp_data):
    """Regression: opaque vzr_ bearer tokens must remain valid."""
    from vezir.server import auth

    token = auth.issue("carol")
    me = client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["github"] == "carol"


def test_replay_after_expiry_rejected(client, tmp_data, monkeypatch):
    """An expired session JWT must be rejected by lookup_identity."""
    from vezir.server import nostr_auth

    monkeypatch.setattr(nostr_auth, "SESSION_TTL_SECONDS", -1)
    token = nostr_auth.issue_session_jwt("alice", "ab" * 32, False)
    assert nostr_auth.verify_session_jwt(token) is None


# ── H1: NIP-98 replay protection ─────────────────────────────────────────────
# (The consumed-id store is cleared between tests by an autouse fixture in
# conftest.py: _reset_nip98_replay_store.)


def test_nip98_replay_within_window_rejected(client, tmp_data):
    """The same signed login event must not mint a second JWT."""
    from vezir.server import nostr_members

    priv = PrivateKey()
    pubkey = priv.public_key_xonly.format().hex()
    nostr_members.add(pubkey, "alice")

    header = _login_header(priv, _public_login_url(client))
    first = client.post(LOGIN_PATH, headers={"Authorization": header})
    assert first.status_code == 200, first.text

    # Replaying the exact same header (same event id) is rejected.
    second = client.post(LOGIN_PATH, headers={"Authorization": header})
    assert second.status_code == 401
    assert "replay" in second.json()["detail"].lower() or \
        "already used" in second.json()["detail"].lower()


def test_nip98_distinct_events_both_accepted(client, tmp_data):
    """Two distinct (freshly-signed) events from the same key both work."""
    from vezir.server import nostr_members

    priv = PrivateKey()
    pubkey = priv.public_key_xonly.format().hex()
    nostr_members.add(pubkey, "alice")
    url = _public_login_url(client)

    # Two separate signed events (different created_at → different id).
    h1 = _login_header(priv, url)
    import time as _t
    _t.sleep(1)
    h2 = _login_header(priv, url)
    assert h1 != h2
    assert client.post(LOGIN_PATH, headers={"Authorization": h1}).status_code == 200
    assert client.post(LOGIN_PATH, headers={"Authorization": h2}).status_code == 200


# ── H2: VEZIR_PUBLIC_URL pins the login URL (header-injection resistant) ──────


def test_login_url_uses_public_url_over_headers(client, tmp_data, monkeypatch):
    """With VEZIR_PUBLIC_URL set, a spoofed Host/X-Forwarded-Proto must not
    change the URL the NIP-98 event is validated against."""
    from vezir.server import nostr_members

    monkeypatch.setenv("VEZIR_PUBLIC_URL", "https://vezir.example.com")

    priv = PrivateKey()
    pubkey = priv.public_key_xonly.format().hex()
    nostr_members.add(pubkey, "alice")

    # Event signed for the configured public URL succeeds even though the
    # request carries different (spoofed) Host/proto headers.
    header = _login_header(priv, "https://vezir.example.com/api/auth/nostr/login")
    resp = client.post(
        LOGIN_PATH,
        headers={
            "Authorization": header,
            "Host": "attacker.example",
            "X-Forwarded-Proto": "http",
        },
    )
    assert resp.status_code == 200, resp.text


def test_login_url_public_url_rejects_other_url(client, tmp_data, monkeypatch):
    """An event signed for a spoofed URL is rejected when public_url is set."""
    from vezir.server import nostr_members

    monkeypatch.setenv("VEZIR_PUBLIC_URL", "https://vezir.example.com")
    priv = PrivateKey()
    pubkey = priv.public_key_xonly.format().hex()
    nostr_members.add(pubkey, "alice")

    # Signed for an attacker-chosen URL → must NOT validate against the
    # pinned public URL.
    header = _login_header(priv, "https://attacker.example/api/auth/nostr/login")
    resp = client.post(LOGIN_PATH, headers={"Authorization": header})
    assert resp.status_code == 401


# ── M5: /health must not disclose the data_dir filesystem path ────────────────


def test_health_omits_data_dir(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "version" in body
    assert "data_dir" not in body
