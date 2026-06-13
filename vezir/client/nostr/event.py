"""Minimal nostr event construction + signing (client side).

Two consumers:
  * the NIP-98 login event (kind 27235) that ``vezir login`` ultimately
    POSTs to the server — signed by the *remote* signer over NIP-46, so
    here we only need the id computation + verification helpers;
  * the NIP-46 request events (kind 24133) that ``vezir login`` signs
    with its own *ephemeral* client key — fully local signing.

The canonical id serialization MUST match the server
(``vezir/server/nip98.py``) and the JS reference exactly:
``[0, pubkey, created_at, kind, tags, content]`` with compact separators
and non-ASCII preserved.
"""
from __future__ import annotations

import hashlib
import json
import time

from coincurve import PrivateKey, PublicKeyXOnly


def compute_id(event: dict) -> str:
    """Return the nostr event id (sha256 of the canonical serialization)."""
    serialized = json.dumps(
        [
            0,
            event["pubkey"],
            event["created_at"],
            event["kind"],
            event["tags"],
            event.get("content", ""),
        ],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def finalize_event(
    *,
    private_key_hex: str,
    kind: int,
    tags: list,
    content: str = "",
    created_at: int | None = None,
) -> dict:
    """Build, id, and Schnorr-sign a complete nostr event with a local key.

    Returns the full event dict (``id``/``pubkey``/``sig`` populated).
    Used for NIP-46 request events signed by the ephemeral client key.
    """
    priv = PrivateKey(bytes.fromhex(private_key_hex))
    pubkey = priv.public_key_xonly.format().hex()
    event = {
        "pubkey": pubkey,
        "created_at": created_at if created_at is not None else int(time.time()),
        "kind": kind,
        "tags": tags,
        "content": content,
    }
    event["id"] = compute_id(event)
    event["sig"] = priv.sign_schnorr(bytes.fromhex(event["id"])).hex()
    return event


def verify_event(event: dict) -> bool:
    """Verify an event's id recomputation + Schnorr signature.

    Used to validate events received from the remote signer (e.g. the
    signed NIP-98 login event) before trusting/sending them.
    """
    try:
        if compute_id(event) != event.get("id"):
            return False
        pub = PublicKeyXOnly(bytes.fromhex(event["pubkey"]))
        return pub.verify(bytes.fromhex(event["sig"]), bytes.fromhex(event["id"]))
    except Exception:
        return False
