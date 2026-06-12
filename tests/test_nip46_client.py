"""Tests for the NIP-46 client logic (event helpers + protocol framing).

The relay websocket is not exercised here (no network); instead we drive
a *simulated remote signer* through the same NIP-44 crypto the real flow
uses, validating: ephemeral key handling, nostrconnect:// URI shape,
request encrypt/sign, and signed-event verification.  The websocket
recv/send loop is covered by a fake ws object.
"""
from __future__ import annotations

import base64
import json
import time
from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip("coincurve")
pytest.importorskip("cryptography")

from coincurve import PrivateKey  # noqa: E402

from vezir.client.nostr import event as nostr_event  # noqa: E402
from vezir.client.nostr import (
    nip44,  # noqa: E402
    nip46,  # noqa: E402
)
from vezir.server import nip98  # noqa: E402

# ── event helpers ────────────────────────────────────────────────────────────


def test_finalize_and_verify_event():
    priv = PrivateKey()
    ev = nostr_event.finalize_event(
        private_key_hex=priv.to_hex(),
        kind=27235,
        tags=[["u", "https://x/login"], ["method", "POST"]],
        content="",
    )
    assert ev["pubkey"] == priv.public_key_xonly.format().hex()
    assert nostr_event.verify_event(ev)


def test_verify_rejects_tampered_event():
    priv = PrivateKey()
    ev = nostr_event.finalize_event(
        private_key_hex=priv.to_hex(), kind=1, tags=[], content="hi"
    )
    ev["content"] = "tampered"
    assert not nostr_event.verify_event(ev)


def test_compute_id_matches_server():
    """Client + server canonical-id must agree byte-for-byte."""
    from vezir.server import nip98

    priv = PrivateKey()
    ev = nostr_event.finalize_event(
        private_key_hex=priv.to_hex(), kind=27235,
        tags=[["u", "https://x"], ["method", "POST"]], content="café",
    )
    # server's private recompute must equal the client's id
    assert nip98._canonical_id(ev) == ev["id"]


# ── nostrconnect:// URI ──────────────────────────────────────────────────────


def test_connect_uri_shape():
    c = nip46.Nip46Client(relay="wss://relay.example", name="vezir")
    uri = c.build_connect_uri()
    assert uri.startswith(f"nostrconnect://{c.client_pubkey}?")
    q = parse_qs(urlsplit(uri).query)
    assert q["relay"] == ["wss://relay.example"]
    assert q["secret"] == [c.secret]
    assert q["name"] == ["vezir"]
    assert "sign_event:27235" in q["perms"][0]


# ── simulated remote signer ──────────────────────────────────────────────────


class FakeSigner:
    """Minimal NIP-46 remote signer for the in-process round-trip."""

    def __init__(self):
        self._priv = PrivateKey()
        self.pubkey = self._priv.public_key_xonly.format().hex()
        # the "user" key the signer controls (same as signer key here)
        self._user_priv = self._priv

    def handle_request(self, client_pubkey: str, ciphertext: str) -> dict:
        """Decrypt a client request, return the encrypted response event dict."""
        req = json.loads(
            nip44.decrypt_from(ciphertext, self._priv.to_hex(), client_pubkey)
        )
        method = req["method"]
        if method == "get_public_key":
            result = self._user_priv.public_key_xonly.format().hex()
        elif method == "sign_event":
            unsigned = json.loads(req["params"][0])
            result = json.dumps(
                nostr_event.finalize_event(
                    private_key_hex=self._user_priv.to_hex(),
                    kind=unsigned["kind"],
                    tags=unsigned["tags"],
                    content=unsigned.get("content", ""),
                    created_at=unsigned.get("created_at"),
                )
            )
        else:
            result = "ack"
        resp = json.dumps({"id": req["id"], "result": result})
        ct = nip44.encrypt_for(resp, self._priv.to_hex(), client_pubkey)
        return {"pubkey": self.pubkey, "kind": 24133, "content": ct,
                "tags": [["p", client_pubkey]], "created_at": int(time.time())}


class FakeWS:
    """A fake websocket wiring the client to a FakeSigner synchronously."""

    def __init__(self, client: nip46.Nip46Client, signer: FakeSigner,
                 *, send_connect: bool = True, connect_result: str = "secret"):
        self._client = client
        self._signer = signer
        self._outbox = []
        self._send_connect = send_connect
        # What the simulated signer returns as the connect result:
        # "secret" (echo our secret), "ack" (Amber-style), or any other
        # literal string (treated as a spoof/wrong response).
        self._connect_result = connect_result
        self._connect_sent = False

    def send(self, raw):
        msg = json.loads(raw)
        if msg[0] == "EVENT":
            ev = msg[1]
            resp = self._signer.handle_request(
                self._client.client_pubkey, ev["content"]
            )
            self._outbox.append(["EVENT", self._client._sub_id, resp])

    def recv(self):
        # On first recv, deliver the unsolicited connect response.
        if self._send_connect and not self._connect_sent:
            self._connect_sent = True
            result = (
                self._client.secret if self._connect_result == "secret"
                else self._connect_result
            )
            resp = json.dumps({"id": "connect", "result": result})
            ct = nip44.encrypt_for(
                resp, self._signer._priv.to_hex(), self._client.client_pubkey
            )
            return json.dumps(["EVENT", self._client._sub_id, {
                "pubkey": self._signer.pubkey, "kind": 24133, "content": ct,
                "tags": [["p", self._client.client_pubkey]],
                "created_at": int(time.time())}])
        if self._outbox:
            return json.dumps(self._outbox.pop(0))
        raise TimeoutError("no message")

    def settimeout(self, t):
        pass

    def close(self):
        pass


def test_full_connect_and_sign_roundtrip():
    signer = FakeSigner()
    client = nip46.Nip46Client(relay="wss://relay.example")
    client._ws = FakeWS(client, signer)

    user_pubkey = client.wait_for_connection(timeout=5)
    assert user_pubkey == signer.pubkey
    assert client.remote_signer_pubkey == signer.pubkey

    unsigned = {
        "kind": 27235,
        "created_at": int(time.time()),
        "tags": [["u", "https://vezir.example/api/auth/nostr/login"],
                 ["method", "POST"]],
        "content": "",
    }
    signed = client.sign_event(unsigned, timeout=5)
    assert signed["pubkey"] == signer.pubkey
    assert nostr_event.verify_event(signed)
    # And the server verifier accepts it end-to-end.
    header = "Nostr " + base64.b64encode(json.dumps(signed).encode()).decode()
    pubkey = nip98.verify_nip98(
        header, "https://vezir.example/api/auth/nostr/login", "POST"
    )
    assert pubkey == signer.pubkey


def test_connect_rejects_wrong_secret():
    signer = FakeSigner()
    client = nip46.Nip46Client(relay="wss://relay.example")

    class BadSecretWS(FakeWS):
        def recv(self):
            if not self._connect_sent:
                self._connect_sent = True
                resp = json.dumps({"id": "connect", "result": "WRONG"})
                ct = nip44.encrypt_for(
                    resp, self._signer._priv.to_hex(), self._client.client_pubkey
                )
                return json.dumps(["EVENT", self._client._sub_id, {
                    "pubkey": self._signer.pubkey, "kind": 24133, "content": ct,
                    "tags": [["p", self._client.client_pubkey]],
                    "created_at": int(time.time())}])
            raise TimeoutError("no message")

    client._ws = BadSecretWS(client, signer)
    with pytest.raises(nip46.Nip46Error, match="timed out"):
        client.wait_for_connection(timeout=2)


def test_connect_accepts_ack():
    """Amber (and NDK/nostr-tools) reply to ``connect`` with ``"ack"`` and do
    NOT echo the secret. We MUST accept ``"ack"`` or login is impossible
    with the primary signer. The Dilger mitigation lives downstream (the
    server's npub allowlist rejects a non-authorized signed login event)."""
    signer = FakeSigner()
    client = nip46.Nip46Client(relay="wss://relay.example")
    client._ws = FakeWS(client, signer, connect_result="ack")

    user_pubkey = client.wait_for_connection(timeout=5)
    assert user_pubkey == signer.pubkey
    assert client.remote_signer_pubkey == signer.pubkey

    # And a full sign still works after an ack-based connect.
    signed = client.sign_event(
        {"kind": 27235, "created_at": int(time.time()),
         "tags": [["u", "https://x/login"], ["method", "POST"]], "content": ""},
        timeout=5,
    )
    assert nostr_event.verify_event(signed)
