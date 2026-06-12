"""NIP-04 encryption (legacy ECDH + AES-256-CBC) for NIP-46 interop.

NIP-04 is deprecated for messaging, but **Amber and several other
signers still use it for NIP-46 (kind 24133) request/response
encryption**.  A NIP-44-only client silently fails to read their
responses, so — exactly like NDK and nostr-tools — we support both and
auto-detect by the presence of ``?iv=`` in the ciphertext (NIP-04 has
it; NIP-44 does not).

This is transport encryption for a one-shot login handshake over a
relay, NOT at-rest message security.  The actual auth guarantee is the
signed NIP-98 login event verified against the server npub allowlist, so
using NIP-04 here for signer compatibility is the pragmatic, correct
choice (see ``nip46`` for the threat model).

Algorithm (https://github.com/nostr-protocol/nips/blob/master/04.md):
  * shared secret = the **raw, unhashed** x-coordinate of the ECDH point
    ``a*B`` (same point we compute for NIP-44, but WITHOUT the HKDF
    step — NIP-04 uses the x-coord directly as the AES key);
  * AES-256-CBC with a random 16-byte IV, PKCS7 padding;
  * wire form: ``base64(ciphertext) + "?iv=" + base64(iv)``.
"""
from __future__ import annotations

import base64
import secrets

from coincurve import PublicKey
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


def _shared_x(private_key_hex: str, public_key_hex: str) -> bytes:
    """Raw 32-byte ECDH x-coordinate (NIP-04: used directly, not hashed).

    ``public_key_hex`` is a 32-byte x-only nostr pubkey; we use the
    even-y point (``02`` prefix) per BIP-340 convention, matching
    ``nip44.get_conversation_key``.
    """
    priv = bytes.fromhex(private_key_hex)
    pub = PublicKey(b"\x02" + bytes.fromhex(public_key_hex))
    shared = pub.multiply(priv)
    return shared.format(compressed=False)[1:33]


def encrypt(plaintext: str, sender_priv_hex: str, recipient_pub_hex: str) -> str:
    """Encrypt to a NIP-04 ``<b64-ciphertext>?iv=<b64-iv>`` payload."""
    key = _shared_x(sender_priv_hex, recipient_pub_hex)
    iv = secrets.token_bytes(16)
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plaintext.encode("utf-8")) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ct = enc.update(padded) + enc.finalize()
    return (
        base64.b64encode(ct).decode("ascii")
        + "?iv="
        + base64.b64encode(iv).decode("ascii")
    )


def decrypt(payload: str, recipient_priv_hex: str, sender_pub_hex: str) -> str:
    """Decrypt a NIP-04 ``<b64-ciphertext>?iv=<b64-iv>`` payload.

    Raises ``ValueError`` if the payload isn't NIP-04 shaped or fails to
    decrypt/unpad.
    """
    if "?iv=" not in payload:
        raise ValueError("not a NIP-04 payload (missing ?iv=)")
    ct_b64, iv_b64 = payload.split("?iv=", 1)
    try:
        ct = base64.b64decode(ct_b64)
        iv = base64.b64decode(iv_b64)
    except Exception as exc:
        raise ValueError(f"invalid base64 in NIP-04 payload: {exc}") from exc
    key = _shared_x(recipient_priv_hex, sender_pub_hex)
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = dec.update(ct) + dec.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    try:
        plaintext = unpadder.update(padded) + unpadder.finalize()
    except ValueError as exc:
        raise ValueError(f"NIP-04 unpad failed: {exc}") from exc
    return plaintext.decode("utf-8")


def is_nip04(content: str) -> bool:
    """Cheap scheme detector: NIP-04 ciphertext carries an ``?iv=`` suffix."""
    return "?iv=" in content
