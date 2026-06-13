"""Nostr login → session JWT issuance + verification.

The human auth flow:

  1. Client builds a NIP-98 event (kind 27235) signed by the user's
     nostr key, targeting ``POST /api/auth/nostr/login``.
  2. ``POST /api/auth/nostr/login`` verifies it (``nip98.verify_nip98``),
     maps the proven pubkey to a member (``nostr_members.lookup_npub``),
     and — on success — mints a short-lived **session JWT**.
  3. The client reuses that JWT as ``Authorization: Bearer <jwt>`` on
     every subsequent request.  ``auth.lookup_identity`` decodes it back
     to ``(github, is_admin)`` so the whole ``require_team_context`` /
     ``require_admin`` chain works unchanged.

Why a session JWT rather than per-request NIP-98 signing?  Per-request
signing is unusable for Amber (a signer prompt on every call) and brittle
on flaky links (uploads, retries).  Signing once → reusing a 24h bearer
matches the existing transport (``Authorization: Bearer``) with zero
client-API changes.

The JWT is symmetric (HS256) signed with a server-local secret at
``config.session_secret_path()`` (0600, auto-created).  vezir is a single
instance, so a symmetric secret is sufficient and avoids key
distribution.  Deleting the secret file rotates: all sessions invalidate.
"""
from __future__ import annotations

import logging
import secrets
import time

import jwt
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status

from .. import config
from . import nip98, nostr_members, queue, ratelimit

log = logging.getLogger("vezir.nostr_auth")

router = APIRouter()

# Session lifetime.  24h balances "sign once a day" against bounding the
# blast radius of a leaked JWT.  Re-login is a single signer prompt.
SESSION_TTL_SECONDS = 24 * 60 * 60

_JWT_ALG = "HS256"
# Distinguishes our session tokens from any other JWT that might be
# presented; checked on decode.
_JWT_ISSUER = "vezir"

_secret_cache: bytes | None = None


def _session_secret() -> bytes:
    """Load (or create) the HMAC secret for signing session JWTs.

    Cached in-process after first read.  Created with 0600 perms via
    ``secure_write_text`` on first use.  32 random bytes, hex-encoded.
    """
    global _secret_cache
    if _secret_cache is not None:
        return _secret_cache
    path = config.session_secret_path()
    if path.is_file():
        _secret_cache = path.read_text().strip().encode("utf-8")
        return _secret_cache
    secret = secrets.token_hex(32)
    config.secure_write_text(path, secret)
    _secret_cache = secret.encode("utf-8")
    return _secret_cache


def issue_session_jwt(github: str, npub: str, is_admin: bool) -> str:
    """Mint a signed session JWT for a freshly-authenticated nostr user."""
    now = int(time.time())
    payload = {
        "iss": _JWT_ISSUER,
        "sub": github,
        "npub": npub,
        "is_admin": bool(is_admin),
        "iat": now,
        "exp": now + SESSION_TTL_SECONDS,
    }
    return jwt.encode(payload, _session_secret(), algorithm=_JWT_ALG)


def verify_session_jwt(token: str) -> tuple[str, bool] | None:
    """Decode + validate a session JWT.  Return ``(github, is_admin)`` or None.

    Returns None (never raises) for anything that isn't a currently-valid
    vezir session token — expired, wrong issuer, bad signature, or simply
    a ``vzr_`` bearer that isn't a JWT at all.  That lets the caller fall
    through to the legacy token-hash path cheaply.
    """
    if not token or token.count(".") != 2:
        # Fast reject: ``vzr_…`` opaque tokens aren't 3-segment JWTs.
        return None
    try:
        payload = jwt.decode(
            token,
            _session_secret(),
            algorithms=[_JWT_ALG],
            issuer=_JWT_ISSUER,
            options={"require": ["exp", "sub", "iss"]},
        )
    except jwt.InvalidTokenError:
        return None
    github = payload.get("sub")
    if not github:
        return None
    return (github, bool(payload.get("is_admin", False)))


def _login_url(request: Request) -> str:
    """Reconstruct the absolute URL the NIP-98 ``u`` tag must match.

    Behind Caddy/nftables the client signs the *public* URL
    (``https://vezir.example.com/api/auth/nostr/login``), but the app
    sees the proxied request.  We honor ``X-Forwarded-Proto`` / ``Host``
    so the reconstructed URL matches what the client signed.  Caddy sets
    these; direct/tunnel callers fall back to the request's own view.
    """
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}{request.url.path}"


@router.post(
    "/api/auth/nostr/login",
    dependencies=[Depends(ratelimit.limit_login)],
)
def nostr_login(
    request: Request,
    authorization: str | None = Header(default=None),
):
    """Verify a NIP-98 login event and return a session JWT.

    The client sends ``Authorization: Nostr <base64-event>`` where the
    event's ``u`` tag is this endpoint's public URL and ``method`` is
    ``POST``.  On success returns::

        {"session_jwt": "...", "github": "...", "is_admin": false,
         "npub": "<hex>", "expires_in": 86400,
         "memberships": [...], "alternate_urls": [...]}

    Errors:
      * 401 if the NIP-98 event is missing/invalid (bad sig, stale, tag
        mismatch).
      * 403 if the signature is valid but the pubkey is not on the
        allowlist (``vezir npub add`` first).
    """
    try:
        pubkey = nip98.verify_nip98(
            authorization, _login_url(request), "POST"
        )
    except nip98.Nip98Error as exc:
        log.info("nostr login rejected: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"NIP-98 verification failed: {exc}",
            headers={"WWW-Authenticate": "Nostr"},
        ) from exc

    resolved = nostr_members.lookup_npub(pubkey)
    if resolved is None:
        log.info("nostr login: valid signature but unknown pubkey %s", pubkey)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "your nostr key is not authorized on this server; "
                "ask an admin to run `vezir npub add`."
            ),
        )

    github, is_admin = resolved
    token = issue_session_jwt(github, pubkey, is_admin)
    log.info("nostr login ok: %s (admin=%s) via %s", github, is_admin, pubkey[:12])
    return {
        "session_jwt": token,
        "github": github,
        "is_admin": is_admin,
        "npub": pubkey,
        "expires_in": SESSION_TTL_SECONDS,
        "memberships": queue.get_memberships(github),
        "alternate_urls": config.alternate_urls(),
    }
