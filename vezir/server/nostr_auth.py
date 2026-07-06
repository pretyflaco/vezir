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
from pydantic import BaseModel

from .. import config
from . import auth, nip98, nostr_members, queue, ratelimit

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
        # Enforce 0600 on a PRE-EXISTING secret too (only newly-created
        # files got it via secure_write_text; an accidentally
        # world-readable file stayed that way silently).
        config.secure_chmod_file(path)
        _secret_cache = path.read_text().strip().encode("utf-8")
        return _secret_cache
    secret = secrets.token_hex(32)
    config.secure_write_text(path, secret)
    _secret_cache = secret.encode("utf-8")
    return _secret_cache


def issue_session_jwt(
    github: str,
    npub: str,
    is_admin: bool,
    *,
    ttl_seconds: int | None = None,
    sid: str | None = None,
) -> str:
    """Mint a signed session (access) JWT for an authenticated user.

    ``ttl_seconds`` overrides the default 24h lifetime — the refresh flow
    (:mod:`vezir.server.sessions_auth`) passes a short access TTL (~60m).
    Omitting it preserves the legacy 24h token so a client that never
    refreshes still gets a usable (if shorter-lived) session.

    ``sid`` binds the access token to a server-side session family so it
    is traceable back to a ``sessions`` row.  A ``jti`` is always added so
    each token is individually identifiable in logs.
    """
    now = int(time.time())
    ttl = SESSION_TTL_SECONDS if ttl_seconds is None else ttl_seconds
    payload = {
        "iss": _JWT_ISSUER,
        "sub": github,
        "npub": npub,
        "is_admin": bool(is_admin),
        "iat": now,
        "exp": now + ttl,
        "jti": secrets.token_hex(8),
    }
    if sid is not None:
        payload["sid"] = sid
    return jwt.encode(payload, _session_secret(), algorithm=_JWT_ALG)


def verify_session_jwt(token: str) -> tuple[str, bool] | None:
    """Decode + validate a session JWT.  Return ``(github, is_admin)`` or None.

    Returns None (never raises) for anything that isn't a currently-valid
    vezir session token — expired, wrong issuer, bad signature, or simply
    a ``vzr_`` bearer that isn't a JWT at all.  That lets the caller fall
    through to the legacy token-hash path cheaply.

    v0.11.0: a ``sid``-bearing access token is also checked against the
    in-process revoked-session cache, so revoking a session (logout,
    admin revoke) kills its already-minted access tokens immediately
    instead of letting them ride until ``exp``.  Still no DB hit on the
    hot path (the cache is an in-memory set).
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
    sid = payload.get("sid")
    if isinstance(sid, str) and sid:
        # Lazy import avoids the module-load cycle (sessions_auth imports
        # nostr_auth for the JWT mint).
        from . import sessions_auth
        if sessions_auth.is_sid_revoked(sid):
            return None
    return (github, bool(payload.get("is_admin", False)))


def _login_url(request: Request) -> str:
    """Resolve the absolute URL the NIP-98 ``u`` tag must match.

    Behind Caddy/nftables the client signs the *public* URL
    (``https://vezir.example.com/api/auth/nostr/login``), but the app
    sees the proxied request.

    Preferred: when ``VEZIR_PUBLIC_URL`` (config ``public_url``) is set,
    build the URL from that fixed base + the request path.  This is
    header-independent, so a caller reaching uvicorn directly cannot spoof
    ``X-Forwarded-Proto`` / ``Host`` to make an event signed for an
    arbitrary URL pass verification.  **Set this in any prod deployment.**

    Fallback (no public_url configured): honor ``X-Forwarded-Proto`` /
    ``Host`` (set by Caddy), else the request's own view — the pre-0.8.2
    behavior, kept for local/dev where no public URL is configured.
    """
    base = config.public_url()
    if base:
        return f"{base}{request.url.path}"
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
    ``POST``.  On success returns a rotating-session pair::

        {"session_jwt": "...", "access_jwt": "...",
         "refresh_token": "vzrt_...", "expires_in": 3600,
         "refresh_expires_in": 604800, "session_max_ttl": 2592000,
         "sid": "...", "github": "...", "is_admin": false,
         "npub": "<hex>", "memberships": [...], "alternate_urls": [...]}

    ``session_jwt`` and ``access_jwt`` are the same short-lived access
    token (``session_jwt`` retained for pre-refresh clients).  The
    ``refresh_token`` is exchanged at ``POST /api/auth/refresh``.

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
    # Lazy import avoids a module-load cycle (sessions_auth imports
    # nostr_auth for the JWT mint).
    from . import sessions_auth
    session = sessions_auth.create_session(github, pubkey, is_admin, "nostr")
    log.info("nostr login ok: %s (admin=%s) via %s", github, is_admin, pubkey[:12])
    return {
        **session,
        "github": github,
        "is_admin": is_admin,
        "npub": pubkey,
        "memberships": queue.get_memberships(github),
        "alternate_urls": config.alternate_urls(),
    }


class _RefreshBody(BaseModel):
    refresh_token: str


@router.post(
    "/api/auth/refresh",
    dependencies=[Depends(ratelimit.limit_login)],
)
def refresh_session(body: _RefreshBody):
    """Rotate a refresh token → a fresh access/refresh pair.

    The client sends ``{"refresh_token": "vzrt_…"}``.  On success returns
    the same shape as login (``access_jwt``/``session_jwt``,
    ``refresh_token``, ``expires_in``, …) with a **new** refresh token; the
    presented one is now consumed.

    Errors:
      * 401 for any unusable token — unknown, expired (idle or absolute
        cap), revoked, or a confirmed reuse (which additionally revokes the
        whole session family).  The client must fall back to a full login.

    Rate-limited on the shared login bucket (per-IP) as anti-abuse.
    """
    from . import sessions_auth

    try:
        return sessions_auth.rotate(body.refresh_token)
    except sessions_auth.SessionError as exc:
        if exc.reuse:
            log.warning("refresh rejected (reuse): %s", exc)
        else:
            log.info("refresh rejected: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"refresh failed: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


def session_id_of(token: str) -> str | None:
    """Return the ``sid`` claim of a valid session access JWT, or None.

    Used by ``/api/auth/logout`` to find which session family to revoke.
    Signature/expiry are verified (an expired token has no live session to
    revoke, and the refresh idle/absolute windows will reap it anyway).
    """
    if not token or token.count(".") != 2:
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
    sid = payload.get("sid")
    return sid if isinstance(sid, str) and sid else None


@router.post("/api/auth/logout", dependencies=[Depends(ratelimit.limit_api)])
def logout(authorization: str | None = Header(default=None)):
    """Revoke the caller's own session family (self-serve logout).

    Reads the ``sid`` from the presented access JWT and revokes that
    session, so its refresh token can no longer mint new access tokens.
    Idempotent: returns ``{"revoked": false}`` when there's nothing live
    to revoke (already expired, a ``vzr_`` bearer, or no sid).  Never
    errors on a well-formed request — logout should always "succeed" from
    the client's perspective.
    """
    from . import sessions_auth

    token = auth._token_from_authorization(authorization)
    sid = session_id_of(token) if token else None
    if not sid:
        return {"revoked": False}
    revoked = sessions_auth.revoke_session(sid)
    if revoked:
        log.info("session logout: sid=%s revoked", sid)
    return {"revoked": revoked}


@router.get("/api/auth/sessions")
def admin_list_sessions(
    github: str | None = None,
    _admin: str = Depends(auth.require_admin),
):
    """Admin: list session families (optionally filtered by ``github``).

    Never returns token hashes — only metadata safe for display.
    """
    from . import sessions_auth
    return {"sessions": sessions_auth.list_sessions(github)}


@router.post("/api/auth/sessions/{sid}/revoke")
def admin_revoke_session(
    sid: str,
    _admin: str = Depends(auth.require_admin),
):
    """Admin: revoke a single session family by its ``sid``."""
    from . import sessions_auth
    revoked = sessions_auth.revoke_session(sid)
    return {"revoked": revoked}


@router.post("/api/auth/sessions/revoke-all")
def admin_revoke_all_sessions(
    github: str,
    _admin: str = Depends(auth.require_admin),
):
    """Admin: revoke every active session for ``github``."""
    from . import sessions_auth
    count = sessions_auth.revoke_all_for(github)
    return {"revoked": count}
