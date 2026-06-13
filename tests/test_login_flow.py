"""End-to-end test of the `vezir login` flow against the real server.

Wires the NIP-46 client (driven by an in-process FakeSigner over a fake
websocket) to a FastAPI TestClient, proving:
  * the remote-signed NIP-98 event the client produces is accepted by
    POST /api/auth/nostr/login,
  * the returned session JWT is persisted to teams.json and works as a
    Bearer credential,
  * resolve_credentials surfaces the stored JWT with no special-casing.
"""
from __future__ import annotations

import tempfile
import time
from pathlib import Path

import pytest

pytest.importorskip("coincurve")
pytest.importorskip("cryptography")

# Reuse the fake signer/ws harness from the nip46 client tests.
from test_nip46_client import FakeSigner, FakeWS  # noqa: E402

from vezir.client.nostr import login as nostr_login  # noqa: E402
from vezir.client.nostr import nip46  # noqa: E402


@pytest.fixture
def tmp_data(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("VEZIR_DATA", d)
        yield Path(d)


@pytest.fixture
def server(tmp_data):
    from fastapi.testclient import TestClient

    from vezir.server.app import create_app

    return TestClient(create_app(), follow_redirects=False)


@pytest.fixture
def home(monkeypatch):
    """Isolate ~/.config/vezir for teams.json writes."""
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("HOME", d)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path(d)))
        yield Path(d)


def test_login_end_to_end(server, tmp_data, home, monkeypatch):
    from vezir.client import config as client_config
    from vezir.server import nostr_members

    # 1. Authorize the signer's pubkey server-side.
    signer = FakeSigner()
    nostr_members.add(signer.pubkey, "milfort", label="laptop")

    # 2. Drive the NIP-46 client with the fake signer.
    client = nip46.Nip46Client(relay="wss://relay.example")
    client._ws = FakeWS(client, signer)
    client.wait_for_connection(timeout=5)

    login_url = nostr_login.login_url_for("http://testserver")
    template = nostr_login.build_login_event_template(login_url)
    signed = client.sign_event(template, timeout=5)
    header = nostr_login.auth_header_from_event(signed)

    # 3. POST through the actual TestClient (monkeypatch httpx -> TestClient).
    resp = server.post(
        "/api/auth/nostr/login", headers={"Authorization": header}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["github"] == "milfort"

    # 4. Persist the session and verify resolve_credentials surfaces it.
    client_config.set_team_session(
        "blink", "http://testserver", body["session_jwt"], body["npub"],
    )
    url, token, team_id, source = client_config.resolve_credentials()
    assert token == body["session_jwt"]
    assert team_id == "blink"
    assert source.startswith("teams:")

    # 5. The stored JWT works as a Bearer credential on /api/me.
    me = server.get("/api/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["github"] == "milfort"


def test_login_event_template_shape():
    tmpl = nostr_login.build_login_event_template("https://x/api/auth/nostr/login")
    assert tmpl["kind"] == 27235
    assert ["u", "https://x/api/auth/nostr/login"] in tmpl["tags"]
    assert ["method", "POST"] in tmpl["tags"]
    assert abs(tmpl["created_at"] - int(time.time())) < 5


def test_post_login_raises_on_unknown(server, tmp_data, home):
    """An un-allowlisted signer should surface the server's 403 detail."""
    signer = FakeSigner()  # never added to nostr_members
    client = nip46.Nip46Client(relay="wss://relay.example")
    client._ws = FakeWS(client, signer)
    client.wait_for_connection(timeout=5)
    template = nostr_login.build_login_event_template(
        nostr_login.login_url_for("http://testserver")
    )
    signed = client.sign_event(template, timeout=5)
    header = nostr_login.auth_header_from_event(signed)
    resp = server.post("/api/auth/nostr/login", headers={"Authorization": header})
    assert resp.status_code == 403
    assert "not authorized" in resp.json()["detail"]
