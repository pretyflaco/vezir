"""Bearer-token auth.

Tokens are stored hashed in ~/vezir-data/tokens.json:

    {
      "tokens": [
        {
          "github": "kasita",
          "token_hash": "<sha256>",
          "issued_at": "...",
          "expires_at": "..." | null,    # 0.1.12+, null = legacy no-expiry
          "last_used_at": "..." | null,  # 0.1.12+, debounced 60s
          "is_admin": false,             # 0.1.12+, gates /admin/* routes
          "label": "..." | null          # 0.1.12+, free-text per-device hint
        }
      ]
    }

The plaintext token is shown ONCE at issue time and is never persisted.
Lookup is by SHA-256 of the presented bearer token, in constant time.

Cookies
-------
Pre-0.1.12, the ``vezir_session`` cookie carried the plaintext bearer
token. From 0.1.12 onward, the cookie carries an opaque, in-memory
session id (see ``web_sessions``) instead. The /login flow uses
short-lived exchange codes (``?code=vzx_...``) to swap a bearer for a
session, so the bearer never appears in URLs, browser history, or
reverse-proxy access logs.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time

from fastapi import Cookie, Header, HTTPException, status

from .. import config
from . import web_sessions

log = logging.getLogger("vezir.auth")

# Cookie used by the browser hand-off flow (see server.login). Value is an
# opaque in-memory session id (``vzs_...``), NOT the bearer token itself.
# HttpOnly + SameSite=Lax. The ``secure`` flag flips on once TLS is in
# front (set VEZIR_COOKIE_SECURE=1 when running behind Caddy).
COOKIE_NAME = "vezir_session"

# Debounce window for `last_used_at` writes. Prevents a write storm when
# a single client polls /api/sessions every few seconds. We still observe
# every use; we just only persist when the previously-stored timestamp is
# older than this many seconds.
_LAST_USED_WRITE_DEBOUNCE_SEC = 60


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _load_tokens() -> dict:
    p = config.tokens_json_path()
    if not p.exists():
        return {"tokens": []}
    return json.loads(p.read_text(encoding="utf-8"))


def _save_tokens(data: dict) -> None:
    p = config.tokens_json_path()
    config.secure_write_text(p, json.dumps(data, indent=2))


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _parse_iso(ts: str | None) -> float | None:
    """Parse an ISO 8601 UTC ``YYYY-MM-DDTHH:MM:SSZ`` string to epoch seconds."""
    if not ts:
        return None
    try:
        return time.mktime(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
    except Exception:
        return None


def _is_expired(entry: dict) -> bool:
    """Return True iff this token row has an ``expires_at`` in the past."""
    exp = _parse_iso(entry.get("expires_at"))
    if exp is None:
        return False  # legacy row, no expiry
    return time.time() >= exp


def issue(
    github: str,
    expires_in_seconds: int | None = None,
    is_admin: bool = False,
    label: str | None = None,
) -> str:
    """Generate a new token for a GitHub handle. Returns the plaintext token.

    Plaintext is never written to disk; only the hash is persisted. Caller
    must capture and hand the plaintext to the user.

    Parameters
    ----------
    expires_in_seconds:
        If given (>0), the token expires that many seconds from now.
        ``None`` or 0 means no expiry (matches pre-0.1.12 behavior).
    is_admin:
        Marks the token as admin-tier. Required by routes that use the
        ``require_admin`` dependency (currently /admin/enroll).
    label:
        Free-text annotation displayed by ``vezir token list``. Useful
        for "android-phone" / "linux-laptop" style hints when one human
        owns multiple tokens. Never used in auth decisions.
    """
    data = _load_tokens()
    plaintext = "vzr_" + secrets.token_urlsafe(32)
    issued_at = _now_iso()
    expires_at: str | None = None
    if expires_in_seconds and expires_in_seconds > 0:
        expires_at = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(time.time() + expires_in_seconds),
        )
    data["tokens"].append(
        {
            "github": github,
            "token_hash": _hash(plaintext),
            "issued_at": issued_at,
            "expires_at": expires_at,
            "last_used_at": None,
            "is_admin": bool(is_admin),
            "label": label or None,
        }
    )
    _save_tokens(data)
    return plaintext


def revoke(github: str) -> int:
    """Remove all tokens for a given github handle. Returns count removed."""
    data = _load_tokens()
    before = len(data["tokens"])
    data["tokens"] = [t for t in data["tokens"] if t["github"] != github]
    _save_tokens(data)
    return before - len(data["tokens"])


def _maybe_touch_last_used(entry_hash: str) -> None:
    """Update ``last_used_at`` for the matching token row, debounced.

    Called on every successful lookup. Persists only if the previously
    stored timestamp is older than ``_LAST_USED_WRITE_DEBOUNCE_SEC`` to
    avoid hammering the JSON file on every poll.
    """
    try:
        data = _load_tokens()
    except Exception:
        return
    now = time.time()
    changed = False
    for entry in data["tokens"]:
        if entry.get("token_hash") != entry_hash:
            continue
        prev = _parse_iso(entry.get("last_used_at"))
        if prev is None or (now - prev) >= _LAST_USED_WRITE_DEBOUNCE_SEC:
            entry["last_used_at"] = _now_iso()
            changed = True
        break
    if changed:
        try:
            _save_tokens(data)
        except Exception:
            # Best-effort: a transient FS error on the touch path must
            # not break auth. The next successful touch will catch up.
            log.exception("failed to update last_used_at")


def _lookup_entry(token: str) -> dict | None:
    """Return the full token row (dict) matching this bearer, or None.

    Uses ``hmac.compare_digest`` for constant-time hash comparison so an
    attacker cannot learn matching prefixes from timing differences.
    Expired rows are treated as misses.
    """
    if not token:
        return None
    h = _hash(token)
    data = _load_tokens()
    for entry in data["tokens"]:
        stored = entry.get("token_hash") or ""
        if not hmac.compare_digest(stored, h):
            continue
        if _is_expired(entry):
            return None
        return entry
    return None


def lookup(token: str) -> str | None:
    """Resolve a bearer token to its GitHub handle, or None.

    Updates ``last_used_at`` (debounced) as a side effect of a successful
    match. Constant-time hash comparison; expired tokens return None.
    """
    entry = _lookup_entry(token)
    if entry is None:
        return None
    _maybe_touch_last_used(entry.get("token_hash") or "")
    return entry.get("github")


def is_admin_token(token: str) -> bool:
    """True iff the token resolves AND the matching row has ``is_admin=true``.

    Legacy rows (missing the field) are treated as non-admin. This is
    intentional: pre-0.1.12 tokens must be re-issued with ``--admin`` to
    keep their /admin/enroll access.
    """
    entry = _lookup_entry(token)
    if entry is None:
        return False
    return bool(entry.get("is_admin", False))


def _token_from_authorization(authorization: str | None) -> str | None:
    """Extract the bearer token from an `Authorization` header, or None."""
    if not authorization:
        return None
    if not authorization.lower().startswith("bearer "):
        return None
    return authorization.split(None, 1)[1].strip()


def require_bearer(authorization: str | None = Header(default=None)) -> str:
    """FastAPI dependency: validates Authorization: Bearer <token>.

    Returns the GitHub handle of the authenticated scribe.

    Use for JSON / programmatic endpoints (e.g. /api/..., /upload). For
    browser-facing routes prefer `require_bearer_or_cookie` so users can
    click links from the GUI's dashboard URL.
    """
    token = _token_from_authorization(authorization)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    github = lookup(token)
    if not github:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return github


def require_bearer_or_cookie(
    authorization: str | None = Header(default=None),
    vezir_session: str | None = Cookie(default=None, alias=COOKIE_NAME),
) -> str:
    """FastAPI dependency: accept either Authorization: Bearer or session cookie.

    Used for browser-facing routes (dashboard, session detail, label page,
    artifact downloads).

    Cookie semantics changed in 0.1.12:
      * Before: cookie value was the plaintext bearer token (``vzr_...``).
      * Now:    cookie value is an opaque session id (``vzs_...``) backed
        by ``web_sessions``. The /login flow swaps a short-lived
        exchange code (``?code=vzx_...``) for a session.

    For backward compatibility (e.g. cookies persisted in browsers from a
    pre-0.1.12 install), a cookie value starting with ``vzr_`` is still
    accepted as a bearer token until next major release.

    Returns the GitHub handle of the authenticated scribe.
    """
    # 1. Prefer the explicit Authorization header (programmatic clients).
    token = _token_from_authorization(authorization)
    if token:
        github = lookup(token)
        if not github:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        log.debug("auth: %s via header", github)
        return github

    # 2. Fall back to the session cookie.
    cookie_value = (vezir_session or "").strip()
    if cookie_value:
        github = web_sessions.lookup_session(cookie_value)
        if github:
            log.debug("auth: %s via session cookie", github)
            return github
        # Pre-0.1.12 cookies stored the plaintext bearer. Accept once,
        # so users with a stale browser session aren't surprise-logged-out
        # after upgrading. The next /login round-trip migrates them to an
        # opaque session id automatically.
        if cookie_value.startswith("vzr_"):
            github = lookup(cookie_value)
            if github:
                log.info("auth: %s via legacy bearer-in-cookie", github)
                return github

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=(
            "not signed in. Visit /login and paste your token, "
            "or use the URL from `vezir scribe` / `vezir upload` "
            "output which signs you in automatically."
        ),
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_admin(
    authorization: str | None = Header(default=None),
    vezir_session: str | None = Cookie(default=None, alias=COOKIE_NAME),
) -> str:
    """FastAPI dependency: same surface as ``require_bearer_or_cookie`` but
    additionally checks that the underlying token has ``is_admin=true``.

    Currently gates /admin/enroll. Legacy tokens (no ``is_admin`` field)
    are rejected — operators must re-issue with ``--admin`` to keep
    enrollment access. This is a one-time migration cost in exchange for
    making /admin/enroll non-self-serve for ordinary scribe tokens.
    """
    github_unverified = require_bearer_or_cookie(
        authorization=authorization, vezir_session=vezir_session,
    )

    # Re-resolve to the actual bearer token to read is_admin. Sessions
    # don't carry the flag directly (we keep them as opaque ids); we look
    # up the latest row for this github handle in tokens.json.
    #
    # Edge case: a github handle with multiple tokens, some admin, some
    # not. We treat "any admin token for this github" as sufficient —
    # consistent with how `revoke` already operates per-github rather
    # than per-token.
    data = _load_tokens()
    for entry in data["tokens"]:
        if entry.get("github") != github_unverified:
            continue
        if _is_expired(entry):
            continue
        if entry.get("is_admin"):
            return github_unverified

    log.info("auth: %s lacks admin role for /admin/* route", github_unverified)
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=(
            "this route requires an admin token. Ask the operator to "
            "re-issue your token with `vezir token issue --admin "
            f"--github {github_unverified}`."
        ),
    )
