"""Google sign-in → session JWT (OAuth 2.0 Device Authorization Grant).

A second human auth path alongside nostr (see ``nostr_auth.py``), for
Blink members who'd rather sign in with their ``@blinkbtc.com`` Google
account than a nostr signer.  Both paths mint the **same** session JWT, so
the rest of the auth chain (``auth.lookup_identity`` →
``require_team_context``) works unchanged.

Why the device grant?  vezir's clients are terminal-first (CLI/TUI) with
no hosted web callback.  The Device Authorization Grant
(``urn:ietf:params:oauth:grant-type:device_code``) is built for exactly
this: the client shows the user a short code + URL, the user approves in a
browser on any device, and the client polls for the result.

Why proxy through the server?  The OAuth client_secret must not live on
every teammate's laptop.  The server holds it and performs the device-code
and token exchanges; the client only ever sees the public ``user_code`` /
``verification_url`` and, at the end, the vezir session JWT.  (The
client_id is public — it's the ID token's ``aud``.)

Flow:
  1. ``GET  /api/auth/google/config``        → {configured, client_id, allowed_domain}
  2. ``POST /api/auth/google/device/start``  → server calls Google's device
       endpoint; returns user_code, verification_url, device_code, interval.
  3. ``POST /api/auth/google/device/poll``   → server exchanges device_code
       at Google's token endpoint (adds client_secret).  While the user
       hasn't approved, returns 202 (authorization_pending).  On success it
       verifies the returned **ID token** (signature via Google's JWKS,
       ``aud``==client_id, ``iss``, ``exp``, ``email_verified``, and the
       ``hd``/email domain == allowed domain), maps the email to a member,
       and returns the session JWT (same shape as the nostr login).

Security: a valid Google token is not enough — the email must be on the
``google_members`` allowlist AND in the allowed Workspace domain AND
``email_verified``.  This blocks a personal Gmail from spoofing a
``@blinkbtc.com`` address.
"""
from __future__ import annotations

import logging
import time
import urllib.parse

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, status

from .. import config
from . import google_members, nostr_auth, queue, ratelimit

log = logging.getLogger("vezir.google_auth")

router = APIRouter()

# Google OAuth endpoints (device grant).
_DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
_SCOPE = "openid email profile"
# Accepted issuers for a Google-minted ID token.
_ISSUERS = ("accounts.google.com", "https://accounts.google.com")

_HTTP_TIMEOUT = 15.0

# Tolerance for clocks running slightly behind Google's ``iat``/``nbf``.
_CLOCK_SKEW_SECONDS = 10
# Retry the ID-token verification on transient network/DNS errors.  The
# verify call fetches Google's JWKS from www.googleapis.com, which on a
# host with flaky DNS (see infra notes) can fail the first time and
# succeed moments later.  We retry briefly, then — if it's *still* a
# network error — treat it as retryable (the poll endpoint returns 202 so
# the client keeps polling) instead of a terminal 401.
_VERIFY_RETRIES = 3
_VERIFY_BACKOFF_SECONDS = 1.0


class _TransientVerifyError(Exception):
    """ID-token verification could not be completed due to a network/DNS
    failure reaching Google's JWKS endpoint (not a bad token)."""


def _is_transient_network_error(exc: BaseException) -> bool:
    """True if ``exc`` (or its cause chain) is a DNS/connection failure
    rather than a genuine token-validation failure."""
    import socket

    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, (socket.gaierror, ConnectionError, TimeoutError, OSError)):
            return True
        text = f"{type(cur).__name__}: {cur}".lower()
        if (
            "name or service not known" in text
            or "failed to resolve" in text
            or "temporary failure in name resolution" in text
            or "max retries exceeded" in text
            or ("connection" in text and "refused" in text)
        ):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def _require_configured() -> tuple[str, str, str]:
    """Return (client_id, client_secret, allowed_domain) or raise 501.

    Google sign-in is optional; if the operator hasn't set the client_id
    + secret, every endpoint here reports "not configured" rather than
    failing obscurely.
    """
    client_id = config.google_client_id()
    client_secret = config.google_client_secret()
    if not client_id or not client_secret:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=(
                "Google sign-in is not configured on this server "
                "(set VEZIR_GOOGLE_CLIENT_ID and VEZIR_GOOGLE_CLIENT_SECRET"
                "[_FILE])."
            ),
        )
    return client_id, client_secret, config.google_allowed_domain()


@router.get("/api/auth/google/config", dependencies=[Depends(ratelimit.limit_api)])
def google_config():
    """Public Google sign-in config the client needs to start the flow.

    Never returns the client_secret.  ``configured`` is false (200, not
    an error) when Google sign-in isn't set up, so clients can offer it
    conditionally.
    """
    client_id = config.google_client_id()
    secret = config.google_client_secret()
    configured = bool(client_id and secret)
    return {
        "configured": configured,
        "client_id": client_id if configured else None,
        "allowed_domain": config.google_allowed_domain() if configured else None,
    }


def _post_with_retry(url: str, data: dict):
    """POST to ``url`` retrying transient network/DNS errors with backoff.

    Google endpoints are reached from a host with occasionally-flaky DNS
    (see infra notes), so a first call can fail to resolve and succeed a
    second later.  Retries only on transient network/DNS errors; HTTP-level
    rejections are returned to the caller to handle.  Raises the last
    ``httpx.HTTPError`` if every attempt fails on the network.
    """
    last_exc: Exception | None = None
    for attempt in range(_VERIFY_RETRIES):
        try:
            with httpx.Client(timeout=_HTTP_TIMEOUT) as c:
                return c.post(url, data=data)
        except httpx.HTTPError as exc:
            last_exc = exc
            if _is_transient_network_error(exc) and attempt + 1 < _VERIFY_RETRIES:
                log.warning(
                    "google %s transient network error (attempt %d/%d): %s",
                    url, attempt + 1, _VERIFY_RETRIES, exc,
                )
                time.sleep(_VERIFY_BACKOFF_SECONDS * (attempt + 1))
                continue
            raise
    raise last_exc  # pragma: no cover - loop always returns or raises above


@router.post(
    "/api/auth/google/device/start",
    dependencies=[Depends(ratelimit.limit_login)],
)
def google_device_start():
    """Begin the device grant: ask Google for a user_code + device_code."""
    client_id, _secret, allowed_domain = _require_configured()
    try:
        r = _post_with_retry(
            _DEVICE_CODE_URL,
            {"client_id": client_id, "scope": _SCOPE},
        )
    except httpx.HTTPError as exc:
        log.warning("google device/start network error (after retries): %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="could not reach Google to start sign-in; try again.",
        ) from exc
    if r.status_code != 200:
        log.warning("google device/start failed: %s %s", r.status_code, r.text[:200])
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Google rejected the device-code request.",
        )
    data = r.json()
    verification_url = data.get("verification_url") or data.get("verification_uri")
    user_code = data["user_code"]
    # Google's device-code endpoint returns only the bare verification_url
    # (no *_complete variant).  Its consumer device page DOES accept the code
    # as a ``?user_code=`` query param, so synthesize the complete URL when
    # Google omits it — clients then open a pre-filled page and the user
    # doesn't have to type the code.  Falls back to whatever Google sent if
    # present.
    verification_url_complete = (
        data.get("verification_url_complete")
        or data.get("verification_uri_complete")
    )
    if not verification_url_complete and verification_url:
        sep = "&" if "?" in verification_url else "?"
        verification_url_complete = (
            f"{verification_url}{sep}user_code={urllib.parse.quote(user_code)}"
        )
    return {
        "device_code": data["device_code"],
        "user_code": user_code,
        "verification_url": verification_url,
        "verification_url_complete": verification_url_complete,
        "expires_in": data.get("expires_in"),
        "interval": data.get("interval", 5),
        "allowed_domain": allowed_domain,
    }


def _verify_id_token(id_token_str: str, client_id: str, allowed_domain: str):
    """Verify a Google ID token; return its claims dict or raise 401/403.

    Checks signature (Google JWKS), ``aud``, ``iss``, ``exp`` (via the
    library), then our extra policy: ``email_verified`` and the email's
    domain == allowed domain (``hd`` claim and/or email suffix).
    """
    from google.auth.transport import requests as g_requests
    from google.oauth2 import id_token as g_id_token

    claims = None
    last_exc: Exception | None = None
    for attempt in range(_VERIFY_RETRIES):
        try:
            claims = g_id_token.verify_oauth2_token(
                id_token_str,
                g_requests.Request(),
                client_id,
                clock_skew_in_seconds=_CLOCK_SKEW_SECONDS,
            )
            break
        except Exception as exc:  # bad sig / aud / expiry, OR a DNS/JWKS blip
            last_exc = exc
            if _is_transient_network_error(exc):
                log.warning(
                    "google id_token verify transient network error "
                    "(attempt %d/%d): %s",
                    attempt + 1, _VERIFY_RETRIES, exc,
                )
                if attempt + 1 < _VERIFY_RETRIES:
                    time.sleep(_VERIFY_BACKOFF_SECONDS * (attempt + 1))
                    continue
                # Still failing on the network — retryable, not a bad token.
                raise _TransientVerifyError(str(exc)) from exc
            # A genuine verification failure: fail fast (terminal 401).
            log.info("google id_token verification failed: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Google ID token verification failed.",
            ) from exc
    if claims is None:  # pragma: no cover - defensive
        raise _TransientVerifyError(str(last_exc))

    if claims.get("iss") not in _ISSUERS:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="unexpected ID token issuer.",
        )
    if not claims.get("email_verified", False):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="your Google email is not verified.",
        )
    email = (claims.get("email") or "").strip().lower()
    hd = (claims.get("hd") or "").strip().lower()
    # Exact-domain match (not a suffix): split on the LAST '@' and compare
    # the domain part precisely, so neither ``user@evilblinkbtc.com`` nor a
    # subdomain ``user@x.blinkbtc.com`` can pass.  The Google-set ``hd``
    # (Workspace domain, cryptographically in the verified token) is an
    # additional accept for Workspace accounts.
    email_domain = email.rsplit("@", 1)[-1] if "@" in email else ""
    domain_ok = (email_domain == allowed_domain) or (hd == allowed_domain)
    if not email or not domain_ok:
        log.info("google login rejected: email %r not in domain %s", email, allowed_domain)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"sign-in is restricted to @{allowed_domain} accounts.",
        )
    return claims


@router.post(
    "/api/auth/google/device/poll",
    dependencies=[Depends(ratelimit.limit_login)],
)
def google_device_poll(device_code: str = Body(..., embed=True)):
    """Exchange a device_code for tokens; verify + mint a session JWT.

    Returns:
      * 200 with the session JWT once the user has approved + is allowlisted.
      * 202 ``{"status": "authorization_pending"}`` while the user hasn't
        finished approving (client keeps polling at ``interval``).
      * 401 if the token is invalid / the grant expired or was denied.
      * 403 if the (verified) email is wrong-domain or not allowlisted.
    """
    client_id, client_secret, allowed_domain = _require_configured()
    try:
        r = _post_with_retry(
            _TOKEN_URL,
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "device_code": device_code,
                "grant_type": _DEVICE_GRANT,
            },
        )
    except httpx.HTTPError as exc:
        # Couldn't reach Google's token endpoint (DNS/network) even after
        # retries.  The grant may still be valid; tell the client to keep
        # polling rather than failing the whole sign-in.
        log.warning("google device/poll network error (after retries): %s", exc)
        return _pending("token_endpoint_unreachable")

    body = r.json() if r.content else {}
    if r.status_code != 200:
        err = body.get("error")
        # Still waiting for the user — tell the client to keep polling.
        if err in ("authorization_pending", "slow_down"):
            return _pending(err)
        # Terminal device-grant errors.
        detail = {
            "expired_token": "the sign-in code expired; run login again.",
            "access_denied": "sign-in was denied.",
        }.get(err, f"Google sign-in failed ({err or r.status_code}).")
        log.info("google device/poll terminal error: %s", err or r.status_code)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)

    id_token_str = body.get("id_token")
    if not id_token_str:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Google did not return an ID token.",
        )

    try:
        claims = _verify_id_token(id_token_str, client_id, allowed_domain)
    except _TransientVerifyError as exc:
        # Couldn't reach Google's JWKS (DNS/network blip).  The grant is
        # fine; tell the client to keep polling rather than failing hard.
        log.warning("google id_token verify deferred (network): %s", exc)
        return _pending("verification_pending")
    email = claims["email"].strip().lower()

    resolved = google_members.lookup_email(email)
    if resolved is None:
        log.info("google login: verified %s but not on allowlist", email)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"{email} is not authorized on this server; "
                "ask an admin to run `vezir google add`."
            ),
        )

    github, is_admin = resolved
    # npub slot is empty for a Google identity; the JWT shape is identical.
    token = nostr_auth.issue_session_jwt(github, "", is_admin)
    log.info("google login ok: %s (admin=%s) via %s", github, is_admin, email)
    return {
        "session_jwt": token,
        "github": github,
        "is_admin": is_admin,
        "email": email,
        "expires_in": nostr_auth.SESSION_TTL_SECONDS,
        "memberships": queue.get_memberships(github),
        "alternate_urls": config.alternate_urls(),
    }


def _pending(err: str):
    """202 response telling the client the user hasn't approved yet."""
    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={"status": "authorization_pending", "error": err},
    )
