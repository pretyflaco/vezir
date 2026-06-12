"""NIP-44 v2 validated against the official nip44.vectors.json.

The vector file ships in tests/vectors/ and its sha256 matches the
checksum published in NIP-44 (269ed0f6...25040), so passing these tests
proves byte-for-byte spec compliance of vezir's NIP-44 implementation.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

pytest.importorskip("coincurve")
pytest.importorskip("cryptography")

from vezir.client.nostr import nip44  # noqa: E402

_VECTORS_PATH = Path(__file__).parent / "vectors" / "nip44.vectors.json"
# The checksum published in the NIP-44 spec; guards against a swapped file.
_EXPECTED_SHA256 = "269ed0f69e4c192512cc779e78c555090cebc7c785b609e338a62afc3ce25040"


def _load_vectors() -> dict:
    raw = _VECTORS_PATH.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == _EXPECTED_SHA256, (
        "nip44.vectors.json checksum mismatch -- file may be corrupt/swapped"
    )
    return json.loads(raw)["v2"]


V = _load_vectors()


def _priv_to_xonly(sec_hex: str) -> str:
    from coincurve import PrivateKey
    return PrivateKey(bytes.fromhex(sec_hex)).public_key_xonly.format().hex()


# ── valid.get_conversation_key ───────────────────────────────────────────────


@pytest.mark.parametrize("vec", V["valid"]["get_conversation_key"])
def test_conversation_key(vec):
    ck = nip44.get_conversation_key(vec["sec1"], vec["pub2"])
    assert ck.hex() == vec["conversation_key"]


# ── valid.get_message_keys ───────────────────────────────────────────────────


def test_message_keys():
    ck = bytes.fromhex(V["valid"]["get_message_keys"]["conversation_key"])
    for vec in V["valid"]["get_message_keys"]["keys"]:
        chacha_key, chacha_nonce, hmac_key = nip44._get_message_keys(
            ck, bytes.fromhex(vec["nonce"])
        )
        assert chacha_key.hex() == vec["chacha_key"]
        assert chacha_nonce.hex() == vec["chacha_nonce"]
        assert hmac_key.hex() == vec["hmac_key"]


# ── valid.calc_padded_len ────────────────────────────────────────────────────


@pytest.mark.parametrize("pair", V["valid"]["calc_padded_len"])
def test_calc_padded_len(pair):
    unpadded, padded = pair
    assert nip44._calc_padded_len(unpadded) == padded


# ── valid.encrypt_decrypt ────────────────────────────────────────────────────


@pytest.mark.parametrize("vec", V["valid"]["encrypt_decrypt"])
def test_encrypt_decrypt(vec):
    # conversation key from (sec1, pub2)
    pub2 = _priv_to_xonly(vec["sec2"])
    ck = nip44.get_conversation_key(vec["sec1"], pub2)
    assert ck.hex() == vec["conversation_key"]

    # encrypt with the fixed nonce must reproduce the exact payload
    payload = nip44.encrypt(
        vec["plaintext"], ck, bytes.fromhex(vec["nonce"])
    )
    assert payload == vec["payload"]

    # decrypt (from the other side) must recover the plaintext
    pub1 = _priv_to_xonly(vec["sec1"])
    ck2 = nip44.get_conversation_key(vec["sec2"], pub1)
    assert ck2.hex() == vec["conversation_key"]
    assert nip44.decrypt(vec["payload"], ck2) == vec["plaintext"]


# ── valid.encrypt_decrypt_long_msg (checksum'd) ──────────────────────────────


@pytest.mark.parametrize("vec", V["valid"]["encrypt_decrypt_long_msg"])
def test_encrypt_decrypt_long(vec):
    ck = bytes.fromhex(vec["conversation_key"])
    plaintext = vec["pattern"] * vec["repeat"]
    payload = nip44.encrypt(plaintext, ck, bytes.fromhex(vec["nonce"]))
    # The vector gives sha256 of plaintext + sha256 of payload.
    assert hashlib.sha256(plaintext.encode()).hexdigest() == vec["plaintext_sha256"]
    assert hashlib.sha256(payload.encode()).hexdigest() == vec["payload_sha256"]
    assert nip44.decrypt(payload, ck) == plaintext


# ── invalid.get_conversation_key ─────────────────────────────────────────────


@pytest.mark.parametrize("vec", V["invalid"]["get_conversation_key"])
def test_invalid_conversation_key(vec):
    with pytest.raises(Exception):
        nip44.get_conversation_key(vec["sec1"], vec["pub2"])


# ── invalid.decrypt ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("vec", V["invalid"]["decrypt"])
def test_invalid_decrypt(vec):
    ck = bytes.fromhex(vec["conversation_key"])
    with pytest.raises(Exception):
        nip44.decrypt(vec["payload"], ck)


# ── invalid.encrypt_msg_lengths ──────────────────────────────────────────────


@pytest.mark.parametrize("length", V["invalid"]["encrypt_msg_lengths"])
def test_invalid_encrypt_lengths(length):
    ck = b"\x01" * 32
    with pytest.raises(Exception):
        nip44.encrypt("a" * length, ck, b"\x00" * 32)


# ── round-trip via convenience helpers ───────────────────────────────────────


def test_encrypt_for_decrypt_from_roundtrip():
    from coincurve import PrivateKey

    a = PrivateKey()
    b = PrivateKey()
    a_pub = a.public_key_xonly.format().hex()
    b_pub = b.public_key_xonly.format().hex()
    msg = "hello milfort \u2014 nostrconnect"
    payload = nip44.encrypt_for(msg, a.to_hex(), b_pub)
    assert nip44.decrypt_from(payload, b.to_hex(), a_pub) == msg
