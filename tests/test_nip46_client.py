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
import threading
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


def test_default_relays_are_probe_verified():
    """vezir ships a 5-relay set verified reachable + fast (<1s handshake).

    relay.nsec.app (dead; its connect timeout blocked the read loop) and
    relay.getportal.cc were removed 2026-08; nostr.oxtr.dev was considered
    but failed the probe."""
    assert nip46.DEFAULT_RELAYS == [
        "wss://relay.damus.io",
        "wss://nos.lol",
        "wss://offchain.pub",
        "wss://relay.primal.net",
        "wss://theforest.nostr1.com",
    ]
    # No explicit relays -> default set is used.
    c = nip46.Nip46Client()
    assert c.relays == nip46.DEFAULT_RELAYS
    q = parse_qs(urlsplit(c.build_connect_uri()).query)
    assert q["relay"] == nip46.DEFAULT_RELAYS


def test_build_connect_uri_includes_url_and_image_when_set():
    """App identity metadata is emitted so signers can render the consent
    screen and origin-bind a sign_event:27235 grant."""
    c = nip46.Nip46Client(
        relay="wss://relay.example",
        url="https://vezir.example.com",
        image="https://example.com/vezir.png",
    )
    q = parse_qs(urlsplit(c.build_connect_uri()).query)
    assert q["url"] == ["https://vezir.example.com"]
    assert q["image"] == ["https://example.com/vezir.png"]


def test_build_connect_uri_omits_url_and_image_when_unset():
    """Back-compat: no metadata params for default construction."""
    c = nip46.Nip46Client(relay="wss://relay.example")
    q = parse_qs(urlsplit(c.build_connect_uri()).query)
    assert "url" not in q
    assert "image" not in q


def test_build_connect_uri_url_is_base_not_endpoint():
    """The connect ``url`` must be the app base origin — signers compare
    its host against the NIP-98 u-tag host.  Guards against passing the
    full /api/auth/... login endpoint by mistake."""
    c = nip46.Nip46Client(
        relay="wss://relay.example", url="https://vezir.example.com"
    )
    q = parse_qs(urlsplit(c.build_connect_uri()).query)
    assert urlsplit(q["url"][0]).path in ("", "/")
    assert "/api/auth/" not in q["url"][0]


# ── simulated remote signer ──────────────────────────────────────────────────


def _nip46_event(priv, client_pubkey, content, created_at=None):
    return nostr_event.finalize_event(
        private_key_hex=priv.to_hex(),
        kind=nip46.NIP46_KIND,
        tags=[["p", client_pubkey]],
        content=content,
        created_at=created_at,
    )


@pytest.fixture(autouse=True)
def _legacy_ack_compatibility(monkeypatch):
    monkeypatch.setenv(nip46.ALLOW_LEGACY_ACK_ENV, "1")


class FakeSigner:
    """Minimal NIP-46 remote signer for the in-process round-trip.

    ``scheme`` selects the encryption used for BOTH decrypting the
    client's requests and encrypting responses: "nip44" (newer signers)
    or "nip04" (Amber).  This lets tests prove interop with each.
    """

    def __init__(
        self, scheme: str = "nip44", echo_id: bool = True, mutate_signed=None,
    ):
        self._priv = PrivateKey()
        self.pubkey = self._priv.public_key_xonly.format().hex()
        # the "user" key the signer controls (same as signer key here)
        self._user_priv = self._priv
        self.scheme = scheme
        # Some signers (certain Amber versions) mint their own response id
        # instead of echoing the request id.  echo_id=False simulates that.
        self.echo_id = echo_id
        self.mutate_signed = mutate_signed

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
            signed = nostr_event.finalize_event(
                private_key_hex=self._user_priv.to_hex(),
                kind=unsigned["kind"],
                tags=unsigned["tags"],
                content=unsigned.get("content", ""),
                created_at=unsigned.get("created_at"),
            )
            if self.mutate_signed:
                signed = self.mutate_signed(signed, unsigned)
            result = json.dumps(signed)
        else:
            result = "ack"
        import uuid as _uuid
        resp_id = req["id"] if self.echo_id else str(_uuid.uuid4())
        resp = json.dumps({"id": resp_id, "result": result})
        ct = self._encrypt(resp, client_pubkey)
        return _nip46_event(self._priv, client_pubkey, ct)


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
        return ["EVENT", self._client._sub_id, _nip46_event(
            self._noise_priv, self._client.client_pubkey, ct,
        )]

    def send(self, raw):
        msg = json.loads(raw)
        if msg[0] == "EVENT":
            ev = msg[1]
            try:
                resp = self._signer.handle_request(
                    self._client.client_pubkey, ev["content"]
                )
            except Exception:
                # A relay delivers blindly; a request encrypted for some
                # OTHER pubkey simply never gets an answer from this signer.
                return
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
            return json.dumps(["EVENT", self._client._sub_id, _nip46_event(
                self._signer._priv, self._client.client_pubkey, ct,
            )])
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


def test_connect_secret_is_redacted_from_debug_logs(caplog):
    caplog.set_level(10, logger="vezir.nip46")
    signer = FakeSigner()
    client = nip46.Nip46Client(relay="wss://relay.example")
    client._ws = FakeWS(client, signer, connect_result="secret")
    client.wait_for_connection(timeout=5)
    assert client.secret not in caplog.text


@pytest.mark.parametrize("field", ["pubkey", "kind", "tags", "content", "created_at"])
def test_sign_event_rejects_validly_signed_changed_fields(field):
    def mutate(_signed, unsigned):
        priv = PrivateKey() if field == "pubkey" else signer._user_priv
        return nostr_event.finalize_event(
            private_key_hex=priv.to_hex(),
            kind=unsigned["kind"] + (field == "kind"),
            tags=unsigned["tags"] + ([["injected", "tag"]] if field == "tags" else []),
            content=unsigned["content"] + ("tampered" if field == "content" else ""),
            created_at=unsigned["created_at"] + (field == "created_at"),
        )

    signer = FakeSigner(mutate_signed=mutate)
    client = nip46.Nip46Client(relay="wss://relay.example")
    client._ws = FakeWS(client, signer, connect_result="secret")
    client.wait_for_connection(timeout=5)
    unsigned = {
        "kind": 27235, "created_at": int(time.time()),
        "tags": [["u", "https://x/login"], ["method", "POST"]], "content": "",
    }
    with pytest.raises(nip46.Nip46Error, match=rf"does not match request \({field}\)"):
        client.sign_event(unsigned, timeout=5)


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
                return json.dumps(["EVENT", self._client._sub_id, _nip46_event(
                    self._signer._priv, self._client.client_pubkey, ct,
                )])
            raise TimeoutError("no message")

    client._ws = BadSecretWS(client, signer)
    with pytest.raises(nip46.Nip46Error, match="timed out"):
        client.wait_for_connection(timeout=2)


def test_connect_accepts_ack_with_legacy_opt_in():
    """Bare-ack signers retain compatibility only with the explicit legacy
    environment opt-in installed by the autouse fixture."""
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


@pytest.mark.parametrize("value", [None, "0", "false"])
def test_connect_rejects_bare_ack_by_default(monkeypatch, value):
    if value is None:
        monkeypatch.delenv(nip46.ALLOW_LEGACY_ACK_ENV, raising=False)
    else:
        monkeypatch.setenv(nip46.ALLOW_LEGACY_ACK_ENV, value)
    signer = FakeSigner()
    client = nip46.Nip46Client(relay="wss://relay.example")
    client._ws = FakeWS(client, signer, connect_result="ack")
    with pytest.raises(nip46.Nip46Error, match="timed out"):
        client.wait_for_connection(timeout=0.1)
    assert client.remote_signer_pubkey is None


def test_legacy_ack_opt_in_warns_about_residual_dos(monkeypatch, caplog):
    monkeypatch.setattr(nip46, "_WARNED_LEGACY_ACK", False)
    caplog.set_level(10, logger="vezir.nip46")
    nip46.Nip46Client(relay="wss://relay.example")
    nip46.Nip46Client(relay="wss://relay.example")
    warnings = [r.message for r in caplog.records if nip46.ALLOW_LEGACY_ACK_ENV in r.message]
    assert len(warnings) == 1
    assert "denial of service" in warnings[0]


def test_auth_url_phishing_guard(monkeypatch):
    """An auth_url injected by an UNBOUND attacker key, or a non-HTTPS URL,
    must NOT be surfaced to the user (v0.12.1 phishing guard)."""
    signer = FakeSigner()
    client = nip46.Nip46Client(relay="wss://relay.example")

    seen: list[str] = []
    client.on_auth_url = seen.append

    attacker = PrivateKey()

    class AuthUrlWS(FakeWS):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self._phish_sent = False

        def recv(self):
            # Before the real connect response, an attacker key injects a
            # malicious auth_url (http:// AND from a non-signer author).
            if not self._phish_sent:
                self._phish_sent = True
                resp = json.dumps({
                    "id": "x", "result": "auth_url",
                    "error": "http://evil.example/steal",
                })
                ct = nip44.encrypt_for(
                    resp, attacker.to_hex(), self._client.client_pubkey
                )
                return json.dumps(["EVENT", self._client._sub_id, _nip46_event(
                    attacker, self._client.client_pubkey, ct,
                )])
            return super().recv()

    client._ws = AuthUrlWS(client, signer)
    user_pubkey = client.wait_for_connection(timeout=5)
    assert user_pubkey == signer.pubkey
    # The attacker's non-HTTPS, non-signer auth_url was dropped.
    assert seen == []


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
    priv = PrivateKey(bytes.fromhex(eid))
    ev = nostr_event.finalize_event(
        private_key_hex=priv.to_hex(), kind=nip46.NIP46_KIND,
        tags=[], content="c", created_at=1,
    )
    return json.dumps(["EVENT", sub_id, ev])


def test_recv_raw_dedupes_same_event_across_relays():
    """The same kind-24133 response delivered by two relays must be
    returned once; the duplicate is dropped by event-id de-dupe."""
    client = nip46.Nip46Client(relays=["wss://r0", "wss://r1"])
    eid = "a" * 64
    multi = MultiRelaySockets([[_event_frame(client._sub_id, eid)],
                               [_event_frame(client._sub_id, eid)]])
    client._wss = multi.as_dict()

    first = client._recv_raw(remaining=2.0)
    assert first is not None
    assert json.loads(first)[2]["id"] in client._seen_events
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
    assert got is not None
    assert json.loads(got)[2]["id"] in client._seen_events


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

    def _create_connection(url, timeout=30, sslopt=None):
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

    def _create_connection(url, timeout=30, sslopt=None):
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


class _RecordingFakeWS(FakeWS):
    """FakeWS that records every frame the client sends (to assert we never
    CLOSE/re-REQ the subscription mid-flow)."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.sent_verbs = []

    def send(self, raw):
        try:
            self.sent_verbs.append(json.loads(raw)[0])
        except Exception:
            pass
        super().send(raw)


def test_no_resubscribe_after_connect():
    """Regression for the laptop hang: the client must NOT CLOSE/re-REQ its
    subscription after connect (that races our own publishes so the signer
    never receives get_public_key/sign_event). Exactly one REQ for the
    whole session; zero CLOSE until close()."""
    signer = FakeSigner()
    client = nip46.Nip46Client(relay="wss://relay.example")
    ws = _RecordingFakeWS(client, signer, connect_result="ack")
    client._ws = ws

    client.wait_for_connection(timeout=5)
    client.sign_event(
        {"kind": 27235, "created_at": int(time.time()),
         "tags": [["u", "https://x/login"], ["method", "POST"]], "content": ""},
        timeout=5,
    )
    assert ws.sent_verbs.count("REQ") == 1, ws.sent_verbs
    assert ws.sent_verbs.count("CLOSE") == 0, ws.sent_verbs


class _RawRecordingFakeWS(FakeWS):
    """FakeWS that records the raw frames the client sends."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.sent_raw = []

    def send(self, raw):
        self.sent_raw.append(raw)
        super().send(raw)


def test_req_filter_has_no_since():
    """Regression for the ~2min skewed-signer login: the subscription filter
    must NOT carry a relay-side ``since``.  The filter is computed from OUR
    clock while responses are stamped with the SIGNER's clock, so a signer
    >60s behind had every response relay-filtered and login took the full
    republish/timeout budget.  The client keypair is fresh per session, so
    there is no history to exclude — the filter is kinds + #p only."""
    signer = FakeSigner()
    client = nip46.Nip46Client(relay="wss://relay.example")
    ws = _RawRecordingFakeWS(client, signer, connect_result="ack")
    client._ws = ws

    client.wait_for_connection(timeout=5)
    reqs = [json.loads(f) for f in ws.sent_raw if json.loads(f)[0] == "REQ"]
    assert reqs, "no REQ sent"
    for _verb, _sub_id, filt in reqs:
        assert filt["kinds"] == [nip46.NIP46_KIND]
        assert filt["#p"] == [client.client_pubkey]
        assert "since" not in filt


class _DropFirstPublishWS(FakeWS):
    """Simulates the laptop overlap gap: the signer does NOT receive the
    FIRST publish of each method request (it lands on a relay the signer
    isn't reading). Only the client's periodic RE-publish gets through."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._seen_method_once: set[str] = set()

    def send(self, raw):
        msg = json.loads(raw)
        if msg[0] == "EVENT":
            ev = msg[1]
            # Decrypt to learn the method so we can drop only the first try.
            try:
                from vezir.client.nostr import nip04, nip44
                content = ev["content"]
                if nip04.is_nip04(content):
                    pt = nip04.decrypt(content, self._signer._priv.to_hex(),
                                       self._client.client_pubkey)
                else:
                    pt = nip44.decrypt_from(content, self._signer._priv.to_hex(),
                                            self._client.client_pubkey)
                method = json.loads(pt).get("method")
            except Exception:
                method = None
            if method and method not in self._seen_method_once:
                # Drop the first publish of this method entirely.
                self._seen_method_once.add(method)
                return
        super().send(raw)


def test_periodic_republish_recovers_dropped_request():
    """If the first publish of get_public_key/sign_event is lost (overlap
    gap), the client's periodic re-publish must still drive the flow to
    completion. Without re-publish this times out."""
    signer = FakeSigner(echo_id=False)
    client = nip46.Nip46Client(relay="wss://relay.example")
    client._ws = _DropFirstPublishWS(client, signer, connect_result="ack")

    # republish_interval defaults to 2s; give enough timeout for one cycle.
    user_pubkey = client.wait_for_connection(timeout=15)
    assert user_pubkey == signer.pubkey
    signed = client.sign_event(
        {"kind": 27235, "created_at": int(time.time()),
         "tags": [["u", "https://x/login"], ["method", "POST"]], "content": ""},
        timeout=15,
    )
    assert nostr_event.verify_event(signed)
    assert signed["pubkey"] == signer.pubkey


class _SinceFilterWS(FakeWS):
    """Emulates Amber's request subscription: the signer only RECEIVES our
    kind-24133 request events whose ``created_at >= signer_since`` (a
    relay-side ``since`` filter computed from the signer's own clock).

    Requests stamped before ``signer_since`` are silently dropped — exactly
    the laptop failure when the client clock is behind the signer's. The
    connect ack is still delivered (the signer is the publisher there).
    """

    def __init__(self, *a, signer_since: int, **kw):
        super().__init__(*a, **kw)
        self._signer_since = signer_since

    def send(self, raw):
        msg = json.loads(raw)
        if msg[0] == "EVENT":
            ev = msg[1]
            if ev.get("created_at", 0) < self._signer_since:
                return  # filtered out by the signer's `since` — never seen
            resp = self._signer.handle_request(
                self._client.client_pubkey, ev["content"]
            )
            self._outbox.append(["EVENT", self._client._sub_id, resp])

    def recv(self):
        # Connect ack carries the SIGNER's clock (== signer_since baseline),
        # which is how the client learns its offset.
        if self._send_connect and not self._connect_sent:
            self._connect_sent = True
            result = (
                self._client.secret if self._connect_result == "secret"
                else self._connect_result
            )
            resp = json.dumps({"id": "connect", "result": result})
            ct = self._signer._encrypt(resp, self._client.client_pubkey)
            return json.dumps(["EVENT", self._client._sub_id, _nip46_event(
                self._signer._priv, self._client.client_pubkey, ct,
                created_at=self._signer_since,
            )])
        if self._outbox:
            return json.dumps(self._outbox.pop(0))
        raise TimeoutError("no message")


def test_clock_offset_recovers_from_behind_skew(monkeypatch):
    """The laptop root cause: client clock BEHIND the signer's, so requests
    stamped created_at=our_now fall before the signer's `since` filter and
    are never delivered. The client must learn the offset from the connect
    event's timestamp and stamp requests forward so they clear `since`.
    """
    import vezir.client.nostr.nip46 as nip46_mod

    real = time.time
    skew = 120  # our clock is 120s BEHIND the signer

    # Make the client's wall clock read `skew` seconds behind real time.
    monkeypatch.setattr(nip46_mod.time, "time", lambda: real() - skew)

    signer = FakeSigner(echo_id=False)
    client = nip46.Nip46Client(relay="wss://relay.example")
    signer_since = int(real())  # signer subscribes with since = signer's now
    client._ws = _SinceFilterWS(
        client, signer, connect_result="ack", signer_since=signer_since
    )

    user_pubkey = client.wait_for_connection(timeout=15)
    # Offset learned (~ +skew so corrected created_at >= signer_since).
    assert client._clock_offset >= skew - 2
    assert user_pubkey == signer.pubkey

    signed = client.sign_event(
        {"kind": 27235, "created_at": int(real()),
         "tags": [["u", "https://x/login"], ["method", "POST"]], "content": ""},
        timeout=15,
    )
    assert nostr_event.verify_event(signed)
    assert signed["pubkey"] == signer.pubkey


def test_clock_offset_clamped(monkeypatch):
    """A bogus/hostile signer timestamp far in the future is clamped to
    MAX_CLOCK_OFFSET (we don't blindly trust the signer's clock)."""
    client = nip46.Nip46Client(relay="wss://relay.example")
    client._learn_clock_offset(int(time.time()) + 99999)
    assert client._clock_offset == nip46.MAX_CLOCK_OFFSET
    client._learn_clock_offset(int(time.time()) - 99999)
    assert client._clock_offset == -nip46.MAX_CLOCK_OFFSET
    client._learn_clock_offset("not-a-number")  # ignored, keeps last value
    assert client._clock_offset == -nip46.MAX_CLOCK_OFFSET


def test_reconnect_dead_relays_reopens_missing(monkeypatch):
    """_reconnect_dead_relays must reopen advertised relays that aren't
    currently connected (handles startup refusals / mid-session drops).
    Reopens run on background threads; join them before asserting."""
    client = nip46.Nip46Client(relays=["wss://r0", "wss://r1", "wss://r2"])

    class _Conn:
        def __init__(self):
            self.sent = []

        def send(self, raw):
            self.sent.append(raw)

    # Start with only r0 connected; r1/r2 were "refused" at startup.
    client._wss = {"wss://r0": _Conn()}
    opened = []

    def _fake_open(url):
        opened.append(url)
        return _Conn()

    monkeypatch.setattr(client, "_open_relay", _fake_open)
    client._reconnect_dead_relays()
    for t in client._bg_threads:
        t.join(timeout=5)

    assert set(opened) == {"wss://r1", "wss://r2"}
    assert set(client._wss.keys()) == {"wss://r0", "wss://r1", "wss://r2"}
    # Reconnected sockets get a REQ subscription.
    for url in ("wss://r1", "wss://r2"):
        assert any('"REQ"' in s for s in client._wss[url].sent)


def test_reconnect_does_not_block_read_loop(monkeypatch):
    """Regression for the ~2min login stall: reconnecting a DEAD relay must
    not block the caller.  The old synchronous _reconnect_dead_relays ran a
    30s-blocking create_connection inside _publish, freezing the response
    read loop while signer replies sat unread in socket buffers."""
    client = nip46.Nip46Client(relays=["wss://live", "wss://dead"])
    client._wss = {"wss://live": object()}
    open_started = threading.Event()
    open_release = threading.Event()
    caller_thread = threading.get_ident()
    open_threads = []

    def _slow_open(url):
        open_threads.append(threading.get_ident())
        open_started.set()
        open_release.wait(timeout=5)  # simulate a dead relay's slow timeout
        raise TimeoutError("timed out")

    monkeypatch.setattr(client, "_open_relay", _slow_open)
    t0 = time.time()
    client._reconnect_dead_relays()  # must return immediately
    elapsed = time.time() - t0
    assert elapsed < 1.0, f"reconnect blocked the caller for {elapsed:.1f}s"
    assert open_started.wait(timeout=2), "background open never started"
    open_release.set()
    for t in client._bg_threads:
        t.join(timeout=5)
    # The blocking open ran off the caller's thread.
    assert open_threads and all(t != caller_thread for t in open_threads)
    # Failed open: relay stays unregistered, inflight guard cleared.
    assert "wss://dead" not in client._wss
    assert "wss://dead" not in client._reconnect_inflight


def test_reconnect_dedupes_inflight(monkeypatch):
    """A burst of republishes against a dead relay must spawn at most ONE
    open attempt at a time (guarded by _reconnect_inflight)."""
    client = nip46.Nip46Client(relays=["wss://dead"])
    open_release = threading.Event()
    opens = []

    def _slow_open(url):
        opens.append(url)
        open_release.wait(timeout=5)
        raise TimeoutError("timed out")

    monkeypatch.setattr(client, "_open_relay", _slow_open)
    client._reconnect_dead_relays()
    client._reconnect_dead_relays()
    client._reconnect_dead_relays()
    assert len(client._bg_threads) == 1
    open_release.set()
    for t in client._bg_threads:
        t.join(timeout=5)
    assert opens == ["wss://dead"]
    # After the attempt settles, a later reconnect may try again.
    client._reconnect_dead_relays()
    assert len(client._bg_threads) == 2
    for t in client._bg_threads:
        t.join(timeout=5)


# ── cooperative cancellation (0.17.1) ─────────────────────────────────────────


class _SilentWS:
    """A fake ws the signer never answers on: recv always times out.

    Drives ``wait_for_connection`` into its blocking read loop so we can
    verify ``cancel()`` from another thread breaks it out promptly instead
    of spinning to the timeout deadline.
    """

    def send(self, raw):
        pass

    def recv(self):
        time.sleep(0.02)
        raise TimeoutError("no message")

    def settimeout(self, t):
        pass

    def close(self):
        pass


def test_cancel_unblocks_wait_for_connection():
    client = nip46.Nip46Client(relay="wss://relay.example")
    client._ws = _SilentWS()

    # Cancel shortly after the wait starts (from another thread, like the
    # TUI reauth modal's Esc handler does).
    threading.Timer(0.2, client.cancel).start()

    t0 = time.time()
    with pytest.raises(nip46.Nip46Error, match="cancelled"):
        # A long timeout: without cancel this would block ~10s.  With
        # cancel it must return in well under a second.
        client.wait_for_connection(timeout=10)
    elapsed = time.time() - t0
    assert elapsed < 3, f"cancel did not unblock the wait promptly ({elapsed:.1f}s)"


def test_cancel_is_idempotent_and_sets_flag():
    client = nip46.Nip46Client(relay="wss://relay.example")
    client.cancel()
    assert client._cancelled.is_set()
    # A second cancel (and a plain close) must not raise.
    client.cancel()
    client.close()


# ── M-7 (security audit 2026-10-30): "ack" binds are provisional ─────────────


def _attacker_event(client, priv: PrivateKey, body: dict) -> str:
    ct = nip44.encrypt_for(json.dumps(body), priv.to_hex(), client.client_pubkey)
    return json.dumps([
        "EVENT", client._sub_id, _nip46_event(priv, client.client_pubkey, ct),
    ])


class _InjectWS(FakeWS):
    """FakeWS that delivers a list of pre-built raw frames first."""

    def __init__(self, *a, inject: list[str], **k):
        super().__init__(*a, **k)
        self._inject = list(inject)

    def recv(self):
        if self._inject:
            return self._inject.pop(0)
        return super().recv()


def test_invalid_inbound_signature_is_rejected_before_decrypt(monkeypatch):
    signer = FakeSigner()
    attacker = PrivateKey()
    client = nip46.Nip46Client(relay="wss://relay.example")
    forged = json.loads(_attacker_event(
        client, attacker, {"id": "connect", "result": client.secret},
    ))
    forged[2]["content"] += "tampered"
    client._ws = _InjectWS(
        client, signer, connect_result="secret", inject=[json.dumps(forged)],
    )
    original_decrypt = client._decrypt_any

    def guarded_decrypt(content, sender):
        assert sender != attacker.public_key_xonly.format().hex()
        return original_decrypt(content, sender)

    monkeypatch.setattr(client, "_decrypt_any", guarded_decrypt)
    assert client.wait_for_connection(timeout=5) == signer.pubkey


def test_decrypted_non_object_json_is_ignored_cleanly():
    signer = FakeSigner()
    attacker = PrivateKey()
    client = nip46.Nip46Client(relay="wss://relay.example")
    client._ws = _InjectWS(
        client, signer, connect_result="secret",
        inject=[_attacker_event(client, attacker, ["not", "an", "object"])],
    )
    assert client.wait_for_connection(timeout=5) == signer.pubkey


def test_attacker_ack_cannot_block_the_genuine_signer():
    """A relay-position attacker races an encrypted {"result":"ack"} in
    before the real signer.  Before M-7 that bound the attacker and the
    genuine signer's connect reply was filtered out forever (login hung).
    Now the ack bind is provisional and the real secret echo takes over."""
    signer = FakeSigner()
    client = nip46.Nip46Client(relay="wss://relay.example")
    attacker = PrivateKey()
    client._ws = _InjectWS(
        client, signer, connect_result="secret",
        inject=[_attacker_event(client, attacker, {"id": "c", "result": "ack"})],
    )
    user_pubkey = client.wait_for_connection(timeout=5)
    assert user_pubkey == signer.pubkey
    assert client.remote_signer_pubkey == signer.pubkey
    assert client._bind_verified is True


def test_provisional_ack_bind_cannot_push_arbitrary_auth_url():
    """An ack-bound author (attacker or an ack-only signer) may not put an
    arbitrary HTTPS "approve here" link in front of the user."""
    signer = FakeSigner()
    client = nip46.Nip46Client(relay="wss://relay.example")
    seen: list[str] = []
    client.on_auth_url = seen.append
    attacker = PrivateKey()
    client._ws = _InjectWS(
        client, signer, send_connect=False,
        inject=[
            _attacker_event(client, attacker, {"id": "c", "result": "ack"}),
            _attacker_event(client, attacker, {
                "id": "p", "result": "auth_url", "error": "https://evil.example/approve",
            }),
        ],
    )
    # The ack author is bound only provisionally, the phishing link is
    # dropped, and get_public_key never gets an answer from the real signer
    # (the request was encrypted for the attacker) -> fallback to the
    # provisional author, which the server would then reject (403).
    client.wait_for_connection(timeout=2)
    assert seen == []
    assert client._bind_verified is False


def test_prebind_auth_url_only_from_known_signer_hosts(monkeypatch):
    """nsec.app legitimately sends auth_url BEFORE answering connect; an
    unknown host in that position is phishing and is dropped."""
    signer = FakeSigner()
    client = nip46.Nip46Client(relay="wss://relay.example")
    seen: list[str] = []
    client.on_auth_url = seen.append
    other = PrivateKey()
    client._ws = _InjectWS(
        client, signer, connect_result="secret",
        inject=[
            _attacker_event(client, other, {
                "id": "1", "result": "auth_url", "error": "https://evil.example/approve",
            }),
            _attacker_event(client, other, {
                "id": "2", "result": "auth_url", "error": "https://use.nsec.app/confirm/abc",
            }),
        ],
    )
    client.wait_for_connection(timeout=5)
    assert seen == ["https://use.nsec.app/confirm/abc"]

    # Operator-extended host list.
    monkeypatch.setenv("VEZIR_NIP46_AUTH_HOSTS", "signer.example.org")
    c2 = nip46.Nip46Client(relay="wss://relay.example")
    other_hex = other.public_key_xonly.format().hex()
    assert c2._auth_url_allowed("https://signer.example.org/ok", other_hex)
    assert c2._auth_url_allowed("https://sub.signer.example.org/ok", "x")
    assert not c2._auth_url_allowed("https://signer.example.org.evil/ok", "x")
    assert not c2._auth_url_allowed("http://use.nsec.app/x", "x")


def test_verified_signer_auth_url_any_host():
    """After a secret-echo bind, the signer is authenticated: any HTTPS
    auth_url from it is surfaced (unchanged behaviour)."""
    signer = FakeSigner()
    client = nip46.Nip46Client(relay="wss://relay.example")
    client._ws = FakeWS(client, signer, connect_result="secret")
    client.wait_for_connection(timeout=5)
    assert client._bind_verified
    assert client._auth_url_allowed("https://any.example/approve", signer.pubkey)
    assert not client._auth_url_allowed("https://any.example/approve", "ff" * 32)


def test_verified_bind_is_never_downgraded_by_a_later_ack():
    signer = FakeSigner()
    client = nip46.Nip46Client(relay="wss://relay.example")
    client._ws = FakeWS(client, signer, connect_result="secret")
    client.wait_for_connection(timeout=5)
    client._bind("ab" * 32, verified=False)
    assert client.remote_signer_pubkey == signer.pubkey
    assert client._bind_verified


# ── L-21 (0.27.0): relay TLS uses vezir's public trust; ws:// refused ─────────


@pytest.mark.parametrize("value", [None, "", "0", "false", "no", "off", "random"])
def test_plaintext_relay_is_refused_without_explicit_truthy_opt_in(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("VEZIR_NIP46_ALLOW_INSECURE_RELAY", raising=False)
    else:
        monkeypatch.setenv("VEZIR_NIP46_ALLOW_INSECURE_RELAY", value)
    client = nip46.Nip46Client(relays=["ws://relay.local"])
    with pytest.raises(nip46.Nip46Error, match="plaintext relay"):
        client._open_relay("ws://relay.local")


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", " TRUE "])
def test_plaintext_relay_truthy_opt_in(monkeypatch, value):
    import sys
    import types

    connection = object()
    fake = types.ModuleType("websocket")
    fake.create_connection = lambda *a, **k: connection
    monkeypatch.setitem(sys.modules, "websocket", fake)
    monkeypatch.setenv("VEZIR_NIP46_ALLOW_INSECURE_RELAY", value)
    client = nip46.Nip46Client(relays=["ws://relay.local"])
    assert client._open_relay("ws://relay.local") is connection


def test_relay_websocket_gets_public_trust_context(monkeypatch):
    import ssl
    import sys
    import types

    seen = {}

    def _create_connection(url, timeout=30, sslopt=None):
        seen["sslopt"] = sslopt
        raise OSError("no network in tests")

    fake = types.ModuleType("websocket")
    fake.create_connection = _create_connection
    monkeypatch.setitem(sys.modules, "websocket", fake)
    client = nip46.Nip46Client(relays=["wss://relay.example"])
    with pytest.raises(OSError):
        client._open_relay("wss://relay.example")
    ctx = seen["sslopt"]["context"]
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.check_hostname and ctx.verify_mode == ssl.CERT_REQUIRED
