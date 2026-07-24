"""NIP-44 v2 authenticated encryption (secp256k1 ECDH + HKDF + ChaCha20 + HMAC).

This is the wire encryption for NIP-46 (kind 24133) request/response
events.  It is security-critical, so it is a faithful implementation of
the spec pseudocode (https://github.com/nostr-protocol/nips/blob/master/44.md,
v2 / ``0x02``) and is validated against the project's official
``nip44.vectors.json`` test vectors in ``tests/test_nip44_vectors.py``.

Primitives:
  * ECDH via ``coincurve`` (``PublicKey.multiply``), taking the UNHASHED
    32-byte x-coordinate of the shared point — NIP-44 explicitly does
    not sha256 the ECDH output.
  * HKDF-SHA256 (RFC 5869) implemented locally from ``hmac``/``hashlib``.
  * ChaCha20 (RFC 8439, counter=0) via ``cryptography``.
  * HMAC-SHA256 over ``aad(nonce) || ciphertext`` with constant-time
    comparison on decrypt.

Do not "optimize" this module without re-running the vectors.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import math

from coincurve import PublicKey
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms

_SALT = b"nip44-v2"
_MIN_PLAINTEXT = 1
_MAX_PLAINTEXT = 65535


# ── HKDF (RFC 5869) ──────────────────────────────────────────────────────────


def _hkdf_extract(salt: bytes, ikm: bytes) -> bytes:
    return hmac.new(salt, ikm, hashlib.sha256).digest()


def _hkdf_expand(prk: bytes, info: bytes, length: int) -> bytes:
    out = b""
    t = b""
    counter = 1
    while len(out) < length:
        t = hmac.new(prk, t + info + bytes([counter]), hashlib.sha256).digest()
        out += t
        counter += 1
    return out[:length]


# ── conversation + message keys ──────────────────────────────────────────────


def get_conversation_key(private_key_hex: str, public_key_hex: str) -> bytes:
    """Long-term key between A and B: ``conv(a, B) == conv(b, A)``.

    ``public_key_hex`` is a 32-byte x-only nostr pubkey; secp256k1 has two
    points per x, and NIP-44 (BIP-340 convention) uses the even-y point,
    so we prefix ``02``.
    """
    priv = bytes.fromhex(private_key_hex)
    pub = PublicKey(b"\x02" + bytes.fromhex(public_key_hex))
    shared = pub.multiply(priv)
    shared_x = shared.format(compressed=False)[1:33]  # unhashed x coordinate
    return _hkdf_extract(_SALT, shared_x)


def _get_message_keys(conversation_key: bytes, nonce: bytes) -> tuple[bytes, bytes, bytes]:
    if len(conversation_key) != 32:
        raise ValueError("invalid conversation_key length")
    if len(nonce) != 32:
        raise ValueError("invalid nonce length")
    keys = _hkdf_expand(conversation_key, nonce, 76)
    return keys[0:32], keys[32:44], keys[44:76]


# ── padding ──────────────────────────────────────────────────────────────────


def _calc_padded_len(unpadded_len: int) -> int:
    if unpadded_len <= 32:
        return 32
    next_power = 1 << (math.floor(math.log2(unpadded_len - 1)) + 1)
    chunk = 32 if next_power <= 256 else next_power // 8
    return chunk * (math.floor((unpadded_len - 1) / chunk) + 1)


def _pad(plaintext: str) -> bytes:
    unpadded = plaintext.encode("utf-8")
    unpadded_len = len(unpadded)
    if unpadded_len < _MIN_PLAINTEXT or unpadded_len > _MAX_PLAINTEXT:
        raise ValueError("invalid plaintext length")
    prefix = unpadded_len.to_bytes(2, "big")
    suffix = bytes(_calc_padded_len(unpadded_len) - unpadded_len)
    return prefix + unpadded + suffix


def _unpad(padded: bytes) -> str:
    unpadded_len = int.from_bytes(padded[0:2], "big")
    unpadded = padded[2 : 2 + unpadded_len]
    if (
        unpadded_len == 0
        or len(unpadded) != unpadded_len
        or len(padded) != 2 + _calc_padded_len(unpadded_len)
    ):
        raise ValueError("invalid padding")
    return unpadded.decode("utf-8")


# ── chacha20 + hmac ──────────────────────────────────────────────────────────


def _chacha20(key: bytes, nonce_12: bytes, data: bytes) -> bytes:
    # cryptography's ChaCha20 expects a 16-byte nonce = 4-byte LE counter
    # (0) prepended to the 12-byte RFC-8439 nonce.
    full_nonce = (0).to_bytes(4, "little") + nonce_12
    cipher = Cipher(algorithms.ChaCha20(key, full_nonce), mode=None)
    enc = cipher.encryptor()
    return enc.update(data) + enc.finalize()


def _hmac_aad(key: bytes, message: bytes, aad: bytes) -> bytes:
    if len(aad) != 32:
        raise ValueError("AAD must be 32 bytes")
    return hmac.new(key, aad + message, hashlib.sha256).digest()


# ── public API ───────────────────────────────────────────────────────────────


def encrypt(plaintext: str, conversation_key: bytes, nonce: bytes) -> str:
    """Encrypt ``plaintext`` to a base64 NIP-44 v2 payload.

    ``nonce`` MUST be 32 random bytes from a CSPRNG, fresh per message.
    """
    chacha_key, chacha_nonce, hmac_key = _get_message_keys(conversation_key, nonce)
    padded = _pad(plaintext)
    ciphertext = _chacha20(chacha_key, chacha_nonce, padded)
    mac = _hmac_aad(hmac_key, ciphertext, nonce)
    return base64.b64encode(bytes([2]) + nonce + ciphertext + mac).decode("ascii")


def _decode_payload(payload: str) -> tuple[bytes, bytes, bytes]:
    plen = len(payload)
    if plen == 0 or payload[0] == "#":
        raise ValueError("unknown version")
    if plen < 132 or plen > 87472:
        raise ValueError("invalid payload size")
    # validate=True: reject non-alphabet chars instead of silently ignoring
    # them, so malformed payloads the spec/test-vectors say to reject don't
    # sneak through (L-7).  MAC verification still gates decryption.
    data = base64.b64decode(payload, validate=True)
    dlen = len(data)
    if dlen < 99 or dlen > 65603:
        raise ValueError("invalid data size")
    if data[0] != 2:
        raise ValueError(f"unknown version {data[0]}")
    nonce = data[1:33]
    ciphertext = data[33 : dlen - 32]
    mac = data[dlen - 32 : dlen]
    return nonce, ciphertext, mac


def decrypt(payload: str, conversation_key: bytes) -> str:
    """Decrypt a base64 NIP-44 v2 payload.  Raises on bad MAC/padding/version."""
    nonce, ciphertext, mac = _decode_payload(payload)
    chacha_key, chacha_nonce, hmac_key = _get_message_keys(conversation_key, nonce)
    calculated_mac = _hmac_aad(hmac_key, ciphertext, nonce)
    if not hmac.compare_digest(calculated_mac, mac):
        raise ValueError("invalid MAC")
    padded = _chacha20(chacha_key, chacha_nonce, ciphertext)
    return _unpad(padded)


def encrypt_for(plaintext: str, sender_priv_hex: str, recipient_pub_hex: str) -> str:
    """Convenience: derive the conversation key + fresh nonce, then encrypt."""
    import secrets

    ck = get_conversation_key(sender_priv_hex, recipient_pub_hex)
    return encrypt(plaintext, ck, secrets.token_bytes(32))


def decrypt_from(payload: str, recipient_priv_hex: str, sender_pub_hex: str) -> str:
    """Convenience: derive the conversation key, then decrypt."""
    ck = get_conversation_key(recipient_priv_hex, sender_pub_hex)
    return decrypt(payload, ck)
