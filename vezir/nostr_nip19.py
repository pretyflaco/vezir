"""Minimal NIP-19 (bech32) encode/decode for nostr ``npub`` keys.

Pure-Python, zero dependencies — shared by the server CLI (decode the
human-friendly ``npub1…`` an operator pastes into ``vezir npub add``)
and any client/display code (encode a 64-char hex x-only pubkey back to
``npub1…`` for readable output).

Scope is deliberately tiny: only the bech32 algorithm + the ``npub``
entity (a bare 32-byte key, NOT a TLV ``nprofile``).  That covers
everything vezir's auth path needs.  Reference: BIP-173 (bech32) and
nostr NIP-19.
"""
from __future__ import annotations

_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _bech32_polymod(values: list[int]) -> int:
    generator = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for v in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= generator[i] if ((top >> i) & 1) else 0
    return chk


def _bech32_hrp_expand(hrp: str) -> list[int]:
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]


def _bech32_verify_checksum(hrp: str, data: list[int]) -> bool:
    return _bech32_polymod(_bech32_hrp_expand(hrp) + data) == 1


def _bech32_create_checksum(hrp: str, data: list[int]) -> list[int]:
    values = _bech32_hrp_expand(hrp) + data
    polymod = _bech32_polymod([*values, 0, 0, 0, 0, 0, 0]) ^ 1
    return [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]


def _bech32_decode(bech: str) -> tuple[str, list[int]]:
    if any(ord(x) < 33 or ord(x) > 126 for x in bech):
        raise ValueError("bech32 string has out-of-range character")
    if bech.lower() != bech and bech.upper() != bech:
        raise ValueError("bech32 string is mixed case")
    bech = bech.lower()
    pos = bech.rfind("1")
    if pos < 1 or pos + 7 > len(bech):
        raise ValueError("bech32 string has invalid separator position")
    hrp = bech[:pos]
    try:
        data = [_CHARSET.index(x) for x in bech[pos + 1:]]
    except ValueError as exc:
        raise ValueError("bech32 string has invalid data character") from exc
    if not _bech32_verify_checksum(hrp, data):
        raise ValueError("bech32 checksum mismatch")
    return hrp, data[:-6]


def _bech32_encode(hrp: str, data: list[int]) -> str:
    combined = data + _bech32_create_checksum(hrp, data)
    return hrp + "1" + "".join(_CHARSET[d] for d in combined)


def _convertbits(
    data: list[int], frombits: int, tobits: int, pad: bool
) -> list[int]:
    acc = 0
    bits = 0
    ret: list[int] = []
    maxv = (1 << tobits) - 1
    max_acc = (1 << (frombits + tobits - 1)) - 1
    for value in data:
        if value < 0 or (value >> frombits):
            raise ValueError("convertbits: value out of range")
        acc = ((acc << frombits) | value) & max_acc
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad:
        if bits:
            ret.append((acc << (tobits - bits)) & maxv)
    elif bits >= frombits or ((acc << (tobits - bits)) & maxv):
        raise ValueError("convertbits: invalid padding")
    return ret


def decode_npub(npub: str) -> str:
    """Decode an ``npub1…`` bech32 string to a 64-char lowercase hex pubkey.

    Raises ``ValueError`` if the input is not a valid ``npub`` (wrong
    HRP, bad checksum, or not 32 bytes of payload).
    """
    hrp, data = _bech32_decode(npub.strip())
    if hrp != "npub":
        raise ValueError(f"expected 'npub' prefix, got {hrp!r}")
    decoded = _convertbits(data, 5, 8, False)
    if len(decoded) != 32:
        raise ValueError(
            f"npub payload must be 32 bytes, got {len(decoded)}"
        )
    return bytes(decoded).hex()


def encode_npub(pubkey_hex: str) -> str:
    """Encode a 64-char hex x-only pubkey to an ``npub1…`` bech32 string.

    Raises ``ValueError`` if the input is not exactly 32 bytes of hex.
    """
    pk = (pubkey_hex or "").strip().lower()
    if len(pk) != 64:
        raise ValueError(f"pubkey must be 64 hex chars, got {len(pk)}")
    raw = bytes.fromhex(pk)
    data = _convertbits(list(raw), 8, 5, True)
    return _bech32_encode("npub", data)


def to_hex(npub_or_hex: str) -> str:
    """Accept either an ``npub1…`` or a 64-char hex string; return hex.

    Convenience for CLI/API surfaces where the operator might paste
    either form.  Raises ``ValueError`` if neither shape validates.
    """
    s = (npub_or_hex or "").strip()
    if s.lower().startswith("npub1"):
        return decode_npub(s)
    pk = s.lower()
    if len(pk) == 64:
        int(pk, 16)  # raises ValueError if not hex
        return pk
    raise ValueError(
        "expected an 'npub1…' bech32 string or a 64-char hex pubkey"
    )
