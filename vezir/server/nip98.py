"""NIP-98 HTTP-Auth verification (server side).

A NIP-98 token is a signed nostr event (kind 27235) carried in the
``Authorization: Nostr <base64-json>`` header.  It proves that whoever
sent the request controls the secret key for ``event.pubkey``, *and*
binds the proof to a specific URL + HTTP method + moment in time
(replay-resistant).

This is a direct port of blink-terminal's ``Nip98Verifier.ts`` so the
two implementations stay byte-for-byte compatible:

  1. base64-decode the header → JSON event.
  2. structural checks: kind==27235, hex shapes for ``pubkey`` (64),
     ``sig`` (128), ``id`` (64); ``tags`` is a list.
  3. freshness: ``created_at`` within ``max_age_seconds`` of now and not
     more than 60s in the future.
  4. ``u`` tag (normalized) == request URL; ``method`` tag == HTTP method.
  5. recompute the event id = ``sha256(canonical_json)`` and compare.
  6. BIP-340 Schnorr verify ``sig`` over ``id`` with ``pubkey``.

Canonical serialization MUST match JS ``JSON.stringify`` exactly:
``[0, pubkey, created_at, kind, tags, content]`` with no insignificant
whitespace (``separators=(",", ":")``) and non-ASCII preserved verbatim
(``ensure_ascii=False``) — otherwise the recomputed id won't match a
client that used the JS canonical form.

Schnorr is verified via ``coincurve`` (libsecp256k1 bindings).  This
module is server-only; the thin clients never call it.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
from urllib.parse import urlsplit, urlunsplit

log = logging.getLogger("vezir.nip98")

NIP98_KIND = 27235

# Freshness window.  The blink reference uses 60s; vezir widens to 120s
# because clients (notably milfort's Starlink link) can have meaningful
# clock skew + send latency, and the single-use short-lived session JWT
# minted on success bounds the actual replay value regardless.
DEFAULT_MAX_AGE_SECONDS = 120

# Tolerance for clocks running ahead of the server.
_FUTURE_TOLERANCE_SECONDS = 60

# Replay protection.  A NIP-98 event is single-use: once a valid event id
# has minted a session, it must not be reusable within its freshness window.
# We keep a small in-memory map of consumed event ids -> expiry epoch and
# reject any id seen again.  TTL = the full window an event can be accepted
# (max age + future tolerance), after which it's stale anyway and the entry
# can be pruned.  In-memory is sufficient for the single-process uvicorn
# deployment; a restart only resets the window to the freshness bound.
_REPLAY_TTL_SECONDS = DEFAULT_MAX_AGE_SECONDS + _FUTURE_TOLERANCE_SECONDS
_consumed_ids: dict[str, float] = {}


class Nip98Error(Exception):
    """Raised on any NIP-98 verification failure, with a human reason."""


def _normalize_url(url: str) -> str:
    """Strip trailing slashes from path + whole URL for stable comparison.

    Mirrors the reference ``normalizeUrl``: parse, drop trailing slashes
    on the path, re-serialize, then drop any trailing slash on the
    result.  Falls back to a plain trailing-slash strip if the URL can't
    be parsed.
    """
    try:
        parts = urlsplit(url)
        path = parts.path.rstrip("/")
        rebuilt = urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))
        return rebuilt.rstrip("/")
    except Exception:
        return url.rstrip("/")


def _get_tag_value(tags: list, name: str) -> str | None:
    """Return the first value of the first tag whose name matches, or None."""
    for t in tags:
        if isinstance(t, list) and len(t) >= 2 and t[0] == name:
            return t[1]
    return None


def _canonical_id(event: dict) -> str:
    """Recompute the nostr event id = sha256 of the canonical serialization.

    Canonical form is ``[0, pubkey, created_at, kind, tags, content]``
    serialized exactly like JS ``JSON.stringify`` (compact separators,
    non-ASCII preserved).
    """
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


def _is_hex(s: object, length: int) -> bool:
    if not isinstance(s, str) or len(s) != length:
        return False
    try:
        int(s, 16)
    except ValueError:
        return False
    return True


def extract_event(auth_header: str | None) -> dict:
    """Decode ``Authorization: Nostr <base64-json>`` to an event dict.

    Raises ``Nip98Error`` if the header is missing, not a Nostr scheme,
    or doesn't base64/JSON-decode to an object.
    """
    if not auth_header:
        raise Nip98Error("missing Authorization header")
    parts = auth_header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "nostr":
        raise Nip98Error("Authorization header is not a 'Nostr' token")
    try:
        decoded = base64.b64decode(parts[1], validate=True)
        event = json.loads(decoded.decode("utf-8"))
    except Exception as exc:
        raise Nip98Error(f"failed to decode NIP-98 token: {exc}") from exc
    if not isinstance(event, dict):
        raise Nip98Error("NIP-98 token did not decode to an event object")
    return event


def _validate_structure(event: dict) -> None:
    for field in ("id", "pubkey", "created_at", "kind", "tags", "sig"):
        if field not in event:
            raise Nip98Error(f"missing required field: {field}")
    if event.get("kind") != NIP98_KIND:
        raise Nip98Error(
            f"invalid event kind: {event.get('kind')}, expected {NIP98_KIND}"
        )
    if not _is_hex(event.get("pubkey"), 64):
        raise Nip98Error("invalid pubkey format")
    if not _is_hex(event.get("sig"), 128):
        raise Nip98Error("invalid signature format")
    if not _is_hex(event.get("id"), 64):
        raise Nip98Error("invalid event id format")
    if not isinstance(event.get("tags"), list):
        raise Nip98Error("tags must be an array")
    if not isinstance(event.get("created_at"), int):
        raise Nip98Error("created_at must be an integer unix timestamp")


def _validate_timestamp(event: dict, max_age_seconds: int) -> None:
    now = int(time.time())
    event_time = event["created_at"]
    if now - event_time > max_age_seconds:
        raise Nip98Error(
            f"event too old: {now - event_time}s (max {max_age_seconds}s)"
        )
    if event_time > now + _FUTURE_TOLERANCE_SECONDS:
        raise Nip98Error("event timestamp is in the future")


def _validate_url_tag(event: dict, request_url: str) -> None:
    url_tag = _get_tag_value(event["tags"], "u")
    if not url_tag:
        raise Nip98Error("missing 'u' (URL) tag")
    if _normalize_url(url_tag) != _normalize_url(request_url):
        raise Nip98Error(
            f"URL mismatch: event={_normalize_url(url_tag)!r}, "
            f"request={_normalize_url(request_url)!r}"
        )


def _validate_method_tag(event: dict, request_method: str) -> None:
    method_tag = _get_tag_value(event["tags"], "method")
    if not method_tag:
        raise Nip98Error("missing 'method' tag")
    if method_tag.upper() != request_method.upper():
        raise Nip98Error(
            f"method mismatch: event={method_tag}, request={request_method}"
        )


def _verify_id(event: dict) -> None:
    calculated = _canonical_id(event)
    if calculated.lower() != event["id"].lower():
        raise Nip98Error("event id does not match canonical hash")


def _verify_signature(event: dict) -> None:
    """BIP-340 Schnorr verify ``sig`` over the event ``id`` with ``pubkey``.

    Uses ``coincurve.PublicKeyXOnly.verify`` (libsecp256k1).  Any failure
    — bad sig, malformed key, missing backend — surfaces as a Nip98Error
    so the caller treats it as an auth failure, never a 500.
    """
    try:
        from coincurve import PublicKeyXOnly
    except Exception as exc:  # pragma: no cover - import guard
        raise Nip98Error(
            "coincurve is required for NIP-98 signature verification "
            "(install vezir[server])"
        ) from exc

    try:
        sig = bytes.fromhex(event["sig"])
        msg = bytes.fromhex(event["id"])
        pub = PublicKeyXOnly(bytes.fromhex(event["pubkey"]))
    except Exception as exc:
        raise Nip98Error(f"malformed signature/key bytes: {exc}") from exc

    try:
        ok = pub.verify(sig, msg)
    except Exception as exc:
        raise Nip98Error(f"signature verification raised: {exc}") from exc
    if not ok:
        raise Nip98Error("invalid signature")


def _check_and_record_replay(event_id: str, ttl: int = _REPLAY_TTL_SECONDS) -> None:
    """Reject a NIP-98 event id that has already been consumed.

    Records ``event_id`` so it can't mint a second session within its
    freshness window; prunes expired entries opportunistically.  Must be
    called only after the event is otherwise fully verified (valid sig),
    so we never poison the store with unverified ids.
    """
    now = time.time()
    # Opportunistic prune (cheap; the store stays tiny at team scale).
    if _consumed_ids:
        expired = [k for k, exp in _consumed_ids.items() if exp <= now]
        for k in expired:
            _consumed_ids.pop(k, None)
    if event_id in _consumed_ids:
        raise Nip98Error("event already used (replay rejected)")
    _consumed_ids[event_id] = now + ttl


def verify_nip98(
    auth_header: str | None,
    request_url: str,
    method: str,
    *,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
) -> str:
    """Fully verify a NIP-98 auth header.  Return the signer pubkey (hex).

    On any failure raises ``Nip98Error`` with a human-readable reason.
    On success returns the lowercase 64-char hex x-only pubkey of the
    proven signer — the caller then maps it to a member via
    ``nostr_members.lookup_npub``.

    Verification order (fail fast, cheapest first): extract → structure →
    freshness → URL tag → method tag → id recompute → Schnorr.  The
    expensive signature check runs last, only on an otherwise-valid event.
    """
    event = extract_event(auth_header)
    _validate_structure(event)
    _validate_timestamp(event, max_age_seconds)
    _validate_url_tag(event, request_url)
    _validate_method_tag(event, method)
    _verify_id(event)
    _verify_signature(event)
    # Replay guard last: only record an id once the event is fully valid,
    # so an attacker can't pre-poison the store with bogus ids.
    _check_and_record_replay(event["id"].lower())
    return event["pubkey"].lower()
