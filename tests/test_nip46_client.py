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
    nip04,  # noqa: E402
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


def test_connect_uri_multi_relay():
    """Multiple relays must each appear as a repeated ``relay=`` param
    (NIP-46 / nostr-tools ``getAll("relay")`` convention) so Amber stores
    and replies on all of them — the redundancy that makes login reliable."""
    relays = ["wss://r1.example", "wss://r2.example", "wss://r3.example"]
    c = nip46.Nip46Client(relays=relays, name="vezir")
    uri = c.build_connect_uri()
    q = parse_qs(urlsplit(uri).query)
    assert q["relay"] == relays  # order preserved, one entry each


def test_default_relays_match_blink():
    """vezir ships blink-terminal's exact proven NIP-46 relay set."""
    assert nip46.DEFAULT_RELAYS == [
        "wss://relay.nsec.app",
        "wss://relay.damus.io",
        "wss://nos.lol",
        "wss://relay.getportal.cc",
        "wss://offchain.pub",
    ]
    # No explicit relays -> default set is used.
    c = nip46.Nip46Client()
    assert c.relays == nip46.DEFAULT_RELAYS
    q = parse_qs(urlsplit(c.build_connect_uri()).query)
    assert q["relay"] == nip46.DEFAULT_RELAYS


# ── simulated remote signer ──────────────────────────────────────────────────


class FakeSigner:
    """Minimal NIP-46 remote signer for the in-process round-trip.

    ``scheme`` selects the encryption used for BOTH decrypting the
    client's requests and encrypting responses: "nip44" (newer signers)
    or "nip04" (Amber).  This lets tests prove interop with each.
    """

    def __init__(self, scheme: str = "nip44", echo_id: bool = True):
        self._priv = PrivateKey()
        self.pubkey = self._priv.public_key_xonly.format().hex()
        # the "user" key the signer controls (same as signer key here)
        self._user_priv = self._priv
        self.scheme = scheme
        # Some signers (certain Amber versions) mint their own response id
        # instead of echoing the request id.  echo_id=False simulates that.
        self.echo_id = echo_id

    def _decrypt(self, ciphertext: str, client_pubkey: str) -> str:
        if self.scheme == "nip04":
            return nip04.decrypt(ciphertext, self._priv.to_hex(), client_pubkey)
        return nip44.decrypt_from(ciphertext, self._priv.to_hex(), client_pubkey)

    def _encrypt(self, plaintext: str, client_pubkey: str) -> str:
        if self.scheme == "nip04":
            return nip04.encrypt(plaintext, self._priv.to_hex(), client_pubkey)
        return nip44.encrypt_for(plaintext, self._priv.to_hex(), client_pubkey)

    def handle_request(self, client_pubkey: str, ciphertext: str) -> dict:
        """Decrypt a client request, return the encrypted response event dict."""
        req = json.loads(self._decrypt(ciphertext, client_pubkey))
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
        import uuid as _uuid
        resp_id = req["id"] if self.echo_id else str(_uuid.uuid4())
        resp = json.dumps({"id": resp_id, "result": result})
        ct = self._encrypt(resp, client_pubkey)
        return {"pubkey": self.pubkey, "kind": 24133, "content": ct,
                "tags": [["p", client_pubkey]], "created_at": int(time.time())}


class FakeWS:
    """A fake websocket wiring the client to a FakeSigner synchronously."""

    def __init__(self, client: nip46.Nip46Client, signer: FakeSigner,
                 *, send_connect: bool = True, connect_result: str = "secret",
                 inject_noise: bool = False):
        self._client = client
        self._signer = signer
        self._outbox = []
        self._send_connect = send_connect
        # What the simulated signer returns as the connect result:
        # "secret" (echo our secret), "ack" (Amber-style), or any other
        # literal string (treated as a spoof/wrong response).
        self._connect_result = connect_result
        # If set, prepend a stray kind-24133 event from an UNRELATED key
        # (with a non-echoed UUID id) before each real response, to verify
        # the client ignores relay noise (the f99a269e/UUID case observed
        # with Amber).
        self._inject_noise = inject_noise
        self._noise_priv = PrivateKey()
        self._connect_sent = False

    def _noise_event(self):
        import uuid as _uuid
        resp = json.dumps({"id": str(_uuid.uuid4()),
                           "result": "f" * 64})  # bogus pubkey-looking result
        ct = nip44.encrypt_for(
            resp, self._noise_priv.to_hex(), self._client.client_pubkey
        )
        return ["EVENT", self._client._sub_id, {
            "pubkey": self._noise_priv.public_key_xonly.format().hex(),
            "kind": 24133, "content": ct,
            "tags": [["p", self._client.client_pubkey]],
            "created_at": int(time.time())}]

    def send(self, raw):
        msg = json.loads(raw)
        if msg[0] == "EVENT":
            ev = msg[1]
            resp = self._signer.handle_request(
                self._client.client_pubkey, ev["content"]
            )
            if self._inject_noise:
                self._outbox.append(self._noise_event())
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
            ct = self._signer._encrypt(resp, self._client.client_pubkey)
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


def test_amber_nip04_interop():
    """Amber encrypts NIP-46 with NIP-04 (?iv=). The client must auto-detect
    it, connect, learn the scheme, and reply NIP-04 for get_public_key /
    sign_event. A NIP-44-only client would silently time out here."""
    signer = FakeSigner(scheme="nip04")
    client = nip46.Nip46Client(relay="wss://relay.example")
    client._ws = FakeWS(client, signer, connect_result="ack")

    user_pubkey = client.wait_for_connection(timeout=5)
    assert user_pubkey == signer.pubkey
    # The client must have learned the peer speaks NIP-04.
    assert client._peer_scheme == "nip04"

    signed = client.sign_event(
        {"kind": 27235, "created_at": int(time.time()),
         "tags": [["u", "https://x/login"], ["method", "POST"]], "content": ""},
        timeout=5,
    )
    assert nostr_event.verify_event(signed)
    assert signed["pubkey"] == signer.pubkey


def test_responses_filtered_to_signer():
    """Responses must be matched to the signer (the connect author) and by
    the echoed request id, mirroring nostr-tools setupSubscription
    (authors=[signer]) + listeners[id]. The signed event's pubkey is the
    authoritative user key."""
    signer = FakeSigner()
    client = nip46.Nip46Client(relay="wss://relay.example")
    client._ws = FakeWS(client, signer, connect_result="ack")

    client.wait_for_connection(timeout=5)
    signed = client.sign_event(
        {"kind": 27235, "created_at": int(time.time()),
         "tags": [["u", "https://x/login"], ["method", "POST"]], "content": ""},
        timeout=5,
    )
    assert nostr_event.verify_event(signed)
    assert signed["pubkey"] == signer.pubkey
    assert client.user_pubkey == signer.pubkey


def test_amber_non_echoed_id_and_no_get_pubkey():
    """Full Amber emulation: connect returns ack, the signer does NOT echo
    our request ids (uses fresh UUIDs), and we never rely on
    get_public_key.  sign_event must still accept the signer's signed reply
    (accept_any) and login completes; the user pubkey comes from the
    signed event."""
    signer = FakeSigner(echo_id=False)
    client = nip46.Nip46Client(relay="wss://relay.example")
    client._ws = FakeWS(client, signer, connect_result="ack")

    provisional = client.wait_for_connection(timeout=5)
    # wait_for_connection returns the signer pubkey provisionally (no
    # get_public_key round-trip).
    assert provisional == signer.pubkey

    signed = client.sign_event(
        {"kind": 27235, "created_at": int(time.time()),
         "tags": [["u", "https://x/login"], ["method", "POST"]], "content": ""},
        timeout=5,
    )
    assert nostr_event.verify_event(signed)
    assert signed["pubkey"] == signer.pubkey
    assert client.user_pubkey == signer.pubkey


def test_get_public_key_used_for_user_pubkey():
    """After connect, the client resolves the user pubkey via
    get_public_key (the proven blink flow), not by waiting for a signed
    event.  FakeSigner's user key == signer key, so they match here, but
    the point is wait_for_connection returns BEFORE any sign_event."""
    signer = FakeSigner()
    client = nip46.Nip46Client(relay="wss://relay.example")
    client._ws = FakeWS(client, signer, connect_result="ack")

    user_pubkey = client.wait_for_connection(timeout=5)
    # user_pubkey came from get_public_key and is recorded.
    assert user_pubkey == signer.pubkey
    assert client.user_pubkey == signer.pubkey


class MultiRelaySockets:
    """Minimal multi-socket fake for _recv_raw: each "relay" is a queue of
    raw frames; recv() pops or raises (timeout)."""

    def __init__(self, frames_per_socket):
        self._queues = [list(f) for f in frames_per_socket]
        self.timeout = None

    def as_dict(self):
        return {f"wss://r{i}": _SockView(q) for i, q in enumerate(self._queues)}


class _SockView:
    def __init__(self, queue):
        self._q = queue

    def settimeout(self, t):
        pass

    def recv(self):
        if self._q:
            return self._q.pop(0)
        raise TimeoutError("empty")

    def send(self, raw):
        pass

    def close(self):
        pass


def _event_frame(sub_id, eid):
    return json.dumps(["EVENT", sub_id, {"id": eid, "pubkey": "x", "kind": 24133,
                                         "content": "c", "tags": []}])


def test_recv_raw_dedupes_same_event_across_relays():
    """The same kind-24133 response delivered by two relays must be
    returned once; the duplicate is dropped by event-id de-dupe."""
    client = nip46.Nip46Client(relays=["wss://r0", "wss://r1"])
    eid = "a" * 64
    multi = MultiRelaySockets([[_event_frame(client._sub_id, eid)],
                               [_event_frame(client._sub_id, eid)]])
    client._wss = multi.as_dict()

    first = client._recv_raw(remaining=2.0)
    assert first is not None and eid in first
    # Second sweep: the duplicate from the other relay is suppressed.
    second = client._recv_raw(remaining=2.0)
    assert second is None


def test_recv_raw_reads_from_a_live_relay_when_another_is_quiet():
    """A response arriving on only ONE relay is still read even if the
    other relay has nothing (round-robin poll, no starvation)."""
    client = nip46.Nip46Client(relays=["wss://r0", "wss://r1"])
    eid = "b" * 64
    # r0 is silent; r1 has the event.
    multi = MultiRelaySockets([[], [_event_frame(client._sub_id, eid)]])
    client._wss = multi.as_dict()

    got = client._recv_raw(remaining=2.0)
    assert got is not None and eid in got


def test_connect_ws_tolerates_partial_relay_failure(monkeypatch):
    """If some relays fail to open, connect proceeds on the survivors;
    only a total failure raises."""
    client = nip46.Nip46Client(relays=["wss://good", "wss://bad"])

    class _FakeConn:
        def send(self, raw):
            pass

        def settimeout(self, t):
            pass

    import sys
    import types

    fake_ws_mod = types.ModuleType("websocket")

    def _create_connection(url, timeout=30):
        if "bad" in url:
            raise OSError("relay down")
        return _FakeConn()

    fake_ws_mod.create_connection = _create_connection
    monkeypatch.setitem(sys.modules, "websocket", fake_ws_mod)

    client._connect_ws()
    assert set(client._wss.keys()) == {"wss://good"}


def test_connect_ws_raises_when_all_relays_fail(monkeypatch):
    client = nip46.Nip46Client(relays=["wss://bad1", "wss://bad2"])

    import sys
    import types

    fake_ws_mod = types.ModuleType("websocket")

    def _create_connection(url, timeout=30):
        raise OSError("relay down")

    fake_ws_mod.create_connection = _create_connection
    monkeypatch.setitem(sys.modules, "websocket", fake_ws_mod)

    with pytest.raises(nip46.Nip46Error, match="any relay"):
        client._connect_ws()


def test_ignores_relay_noise_from_other_authors():
    """The real-world failure: a stray kind-24133 event from an UNRELATED
    key (UUID id) arrives on the relay. The client must ignore it (it's not
    the signer) and still complete via the signer's real response."""
    signer = FakeSigner()
    client = nip46.Nip46Client(relay="wss://relay.example")
    client._ws = FakeWS(client, signer, connect_result="secret", inject_noise=True)

    client.wait_for_connection(timeout=5)
    signed = client.sign_event(
        {"kind": 27235, "created_at": int(time.time()),
         "tags": [["u", "https://x/login"], ["method", "POST"]], "content": ""},
        timeout=5,
    )
    assert signed["pubkey"] == signer.pubkey
    assert client.remote_signer_pubkey == signer.pubkey
