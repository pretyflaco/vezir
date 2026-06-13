"""Tests for the NIP-98 HTTP-Auth verifier (vezir/server/nip98.py).

These exercise a *real* BIP-340 Schnorr signature path via coincurve so
the canonical-id recomputation and signature verification are tested
end-to-end against actual nostr-style events, plus every documented
failure mode (kind, freshness, tag mismatch, tampered id, bad sig).
"""
from __future__ import annotations

import base64
import hashlib
import json
import time

import pytest

from vezir.server import nip98

coincurve = pytest.importorskip("coincurve")
from coincurve import PrivateKey  # noqa: E402


def _canonical_id(event: dict) -> str:
    serialized = json.dumps(
        [0, event["pubkey"], event["created_at"], event["kind"],
         event["tags"], event["content"]],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _make_event(
    priv: PrivateKey,
    *,
    url: str = "https://vezir.example/api/auth/nostr/login",
    method: str = "POST",
    kind: int = 27235,
    created_at: int | None = None,
    content: str = "",
    extra_tags: list | None = None,
) -> dict:
    """Build a fully-signed NIP-98 event for the given signer."""
    pubkey = priv.public_key_xonly.format().hex()
    tags = [["u", url], ["method", method]]
    if extra_tags:
        tags.extend(extra_tags)
    event = {
        "pubkey": pubkey,
        "created_at": created_at if created_at is not None else int(time.time()),
        "kind": kind,
        "tags": tags,
        "content": content,
    }
    event["id"] = _canonical_id(event)
    sig = priv.sign_schnorr(bytes.fromhex(event["id"]))
    event["sig"] = sig.hex()
    return event


def _auth_header(event: dict) -> str:
    raw = json.dumps(event).encode("utf-8")
    return "Nostr " + base64.b64encode(raw).decode("ascii")


URL = "https://vezir.example/api/auth/nostr/login"


def test_valid_event_returns_pubkey():
    priv = PrivateKey()
    ev = _make_event(priv, url=URL, method="POST")
    pubkey = nip98.verify_nip98(_auth_header(ev), URL, "POST")
    assert pubkey == priv.public_key_xonly.format().hex()


def test_missing_header():
    with pytest.raises(nip98.Nip98Error, match="missing Authorization"):
        nip98.verify_nip98(None, URL, "POST")


def test_wrong_scheme():
    priv = PrivateKey()
    ev = _make_event(priv)
    bad = "Bearer " + _auth_header(ev).split(" ", 1)[1]
    with pytest.raises(nip98.Nip98Error, match="not a 'Nostr'"):
        nip98.verify_nip98(bad, URL, "POST")


def test_bad_base64():
    with pytest.raises(nip98.Nip98Error, match="failed to decode"):
        nip98.verify_nip98("Nostr !!!not-base64!!!", URL, "POST")


def test_wrong_kind():
    priv = PrivateKey()
    ev = _make_event(priv, kind=1)
    with pytest.raises(nip98.Nip98Error, match="invalid event kind"):
        nip98.verify_nip98(_auth_header(ev), URL, "POST")


def test_too_old():
    priv = PrivateKey()
    ev = _make_event(priv, created_at=int(time.time()) - 9999)
    with pytest.raises(nip98.Nip98Error, match="too old"):
        nip98.verify_nip98(_auth_header(ev), URL, "POST")


def test_future_timestamp():
    priv = PrivateKey()
    ev = _make_event(priv, created_at=int(time.time()) + 9999)
    with pytest.raises(nip98.Nip98Error, match="future"):
        nip98.verify_nip98(_auth_header(ev), URL, "POST")


def test_url_mismatch():
    priv = PrivateKey()
    ev = _make_event(priv, url="https://evil.example/login")
    with pytest.raises(nip98.Nip98Error, match="URL mismatch"):
        nip98.verify_nip98(_auth_header(ev), URL, "POST")


def test_url_trailing_slash_normalized():
    priv = PrivateKey()
    ev = _make_event(priv, url=URL + "/")
    # Trailing slash on the event url must still match the bare request url.
    pubkey = nip98.verify_nip98(_auth_header(ev), URL, "POST")
    assert pubkey == priv.public_key_xonly.format().hex()


def test_method_mismatch():
    priv = PrivateKey()
    ev = _make_event(priv, method="GET")
    with pytest.raises(nip98.Nip98Error, match="method mismatch"):
        nip98.verify_nip98(_auth_header(ev), URL, "POST")


def test_method_case_insensitive():
    priv = PrivateKey()
    ev = _make_event(priv, method="post")
    pubkey = nip98.verify_nip98(_auth_header(ev), URL, "POST")
    assert pubkey == priv.public_key_xonly.format().hex()


def test_tampered_id():
    priv = PrivateKey()
    ev = _make_event(priv)
    # Flip the id so it no longer matches the canonical hash.
    ev["id"] = ("f" + ev["id"][1:]) if ev["id"][0] != "f" else ("0" + ev["id"][1:])
    with pytest.raises(nip98.Nip98Error, match="id does not match"):
        nip98.verify_nip98(_auth_header(ev), URL, "POST")


def test_tampered_content_breaks_id():
    """Changing content after signing must fail id recomputation."""
    priv = PrivateKey()
    ev = _make_event(priv, content="original")
    ev["content"] = "tampered"
    with pytest.raises(nip98.Nip98Error, match="id does not match"):
        nip98.verify_nip98(_auth_header(ev), URL, "POST")


def test_bad_signature():
    """A valid id but a signature from a different key must fail."""
    priv = PrivateKey()
    other = PrivateKey()
    ev = _make_event(priv)
    # Re-sign the id with a DIFFERENT key (pubkey stays priv's).
    ev["sig"] = other.sign_schnorr(bytes.fromhex(ev["id"])).hex()
    with pytest.raises(nip98.Nip98Error, match="invalid signature"):
        nip98.verify_nip98(_auth_header(ev), URL, "POST")


def test_missing_u_tag():
    priv = PrivateKey()
    pubkey = priv.public_key_xonly.format().hex()
    event = {
        "pubkey": pubkey,
        "created_at": int(time.time()),
        "kind": 27235,
        "tags": [["method", "POST"]],
        "content": "",
    }
    event["id"] = _canonical_id(event)
    event["sig"] = priv.sign_schnorr(bytes.fromhex(event["id"])).hex()
    with pytest.raises(nip98.Nip98Error, match="missing 'u'"):
        nip98.verify_nip98(_auth_header(event), URL, "POST")


def test_short_pubkey_rejected():
    priv = PrivateKey()
    ev = _make_event(priv)
    ev["pubkey"] = "abcd"
    with pytest.raises(nip98.Nip98Error, match="invalid pubkey"):
        nip98.verify_nip98(_auth_header(ev), URL, "POST")
