"""Tests for NIP-04 encryption (Amber/NIP-46 interop)."""
from __future__ import annotations

import base64

import pytest

pytest.importorskip("coincurve")
pytest.importorskip("cryptography")

from coincurve import PrivateKey  # noqa: E402

from vezir.client.nostr import nip04  # noqa: E402


def test_roundtrip_symmetric():
    a = PrivateKey()
    b = PrivateKey()
    a_pub = a.public_key_xonly.format().hex()
    b_pub = b.public_key_xonly.format().hex()
    msg = '{"id":"abc","result":"ack"}'
    ct = nip04.encrypt(msg, a.to_hex(), b_pub)
    # B decrypts what A encrypted.
    assert nip04.decrypt(ct, b.to_hex(), a_pub) == msg


def test_payload_shape():
    a = PrivateKey()
    b = PrivateKey()
    ct = nip04.encrypt("hello", a.to_hex(), b.public_key_xonly.format().hex())
    assert "?iv=" in ct
    body, iv = ct.split("?iv=", 1)
    # both halves are valid base64; iv decodes to 16 bytes
    assert len(base64.b64decode(iv)) == 16
    base64.b64decode(body)


def test_ecdh_symmetric():
    a = PrivateKey()
    b = PrivateKey()
    a_pub = a.public_key_xonly.format().hex()
    b_pub = b.public_key_xonly.format().hex()
    assert nip04._shared_x(a.to_hex(), b_pub) == nip04._shared_x(b.to_hex(), a_pub)


def test_is_nip04_detection():
    from vezir.client.nostr import nip44
    a = PrivateKey()
    b = PrivateKey()
    b_pub = b.public_key_xonly.format().hex()
    nip04_ct = nip04.encrypt("x", a.to_hex(), b_pub)
    nip44_ct = nip44.encrypt_for("x", a.to_hex(), b_pub)
    assert nip04.is_nip04(nip04_ct)
    assert not nip04.is_nip04(nip44_ct)


def test_decrypt_rejects_non_nip04():
    a = PrivateKey()
    b = PrivateKey()
    with pytest.raises(ValueError, match="not a NIP-04"):
        nip04.decrypt("no-iv-here", b.to_hex(), a.public_key_xonly.format().hex())


def test_unicode_roundtrip():
    a = PrivateKey()
    b = PrivateKey()
    a_pub = a.public_key_xonly.format().hex()
    b_pub = b.public_key_xonly.format().hex()
    msg = "café — milfort ☕ 日本語"
    ct = nip04.encrypt(msg, a.to_hex(), b_pub)
    assert nip04.decrypt(ct, b.to_hex(), a_pub) == msg
