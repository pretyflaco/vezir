"""Bearer-token auth.

Tokens are stored hashed in ~/vezir-data/tokens.json:

    {
      "tokens": [
        {
          "github": "kasita",
          "token_hash": "<sha256>",
          "team_id": "blink",            # 0.6.0+, team this token belongs to
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
    team_id: str,
    expires_in_seconds: int | None = None,
    is_admin: bool = False,
    label: str | None = None,
) -> str:
    """Generate a new token for a GitHub handle. Returns the plaintext token.

    Plaintext is never written to disk; only the hash is persisted. Caller
    must capture and hand the plaintext to the user.

    Parameters
    ----------
    github:
        GitHub handle the token belongs to.
    team_id:
        Slug of the team this token is scoped to.  Added in v0.6.0.  All
        sessions uploaded by this token, and all visibility queries this
        token authorizes, are restricted to this team.  Required.
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
    if not team_id:
        raise ValueError("issue() requires team_id (added in v0.6.0)")
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
            "team_id": team_id,
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


def lookup_full(token: str) -> tuple[str, str, bool] | None:
    """Resolve a bearer token to ``(github, team_id, is_admin)``, or None.

    v0.6.0: the single trusted path that maps a bearer to its
    server-side identity.  Use this in handlers that need the team
    discriminator (which is all of them — the whole point of v0.6.0 is
    that nothing crosses team boundaries).

    Side-effect: debounced ``last_used_at`` touch on hit.
    """
    entry = _lookup_entry(token)
    if entry is None:
        return None
    _maybe_touch_last_used(entry.get("token_hash") or "")
    return (
        entry.get("github") or "",
        entry.get("team_id") or "",
        bool(entry.get("is_admin", False)),
    )


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
    browser-facing routes prefer ``require_bearer_or_cookie`` so users
    can click links from the GUI's dashboard URL.

    See also ``require_bearer_full`` for routes that also need the
    token's team_id (v0.6.0+).  This dependency still works for legacy
    handler signatures that only need the github handle — but the auth
    chain underneath validates team_id presence so handlers stay safe.
    """
    github, _team, _admin = require_bearer_full(authorization)
    return github


def require_bearer_full(
    authorization: str | None = Header(default=None),
) -> tuple[str, str, bool]:
    """FastAPI dependency: bearer auth returning ``(github, team_id, is_admin)``.

    v0.6.0: the canonical bearer-only auth dependency for endpoints
    that need to scope queries to the caller's team.  Use this for
    every /api/sessions* and /upload route.

    Raises 401 on missing/invalid token, and on tokens that lack a
    team_id (pre-v0.6.0 tokens that didn't go through the migration).
    """
    token = _token_from_authorization(authorization)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    resolved = lookup_full(token)
    if resolved is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    github, team_id, admin = resolved
    if not team_id:
        # v0.6.0+: any token that survives the migration MUST have a
        # team_id.  A missing one means either a hand-edited tokens.json
        # or a migration bug.  Reject loudly so the operator notices,
        # rather than silently letting cross-team leakage past the
        # visibility filter.
        log.warning(
            "auth: token for %s has no team_id; rejecting "
            "(re-issue via `vezir token issue --team <id>`)",
            github,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                "token has no team assignment; ask the operator to "
                "re-issue it with `vezir token issue --team <id>`."
            ),
            headers={"WWW-Authenticate": "Bearer"},
        )
    log.debug(
        "auth: %s via header (team=%s admin=%s)", github, team_id, admin,
    )
    return github, team_id, admin


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

    See also ``require_bearer_or_cookie_full`` for the 3-tuple variant.
    """
    github, _team, _admin = _resolve_auth(authorization, vezir_session)
    return github


def require_bearer_or_cookie_full(
    authorization: str | None = Header(default=None),
    vezir_session: str | None = Cookie(default=None, alias=COOKIE_NAME),
) -> tuple[str, str, bool]:
    """FastAPI dependency: bearer-or-cookie auth returning ``(github, team_id, is_admin)``.

    v0.6.0: use this on browser-facing routes that also need the team
    discriminator (dashboard, session detail, artifact download, etc.).
    """
    return _resolve_auth(authorization, vezir_session)


def _resolve_auth(
    authorization: str | None,
    vezir_session: str | None,
) -> tuple[str, str, bool]:
    """Shared auth resolution returning ``(github, team_id, is_admin)``.

    The ``is_admin`` and ``team_id`` flags reflect the *specific token*
    or *specific session* used for this request — not "any token for
    the same github handle".  This prevents a scribe-tier token from
    inheriting admin access just because the same person also holds an
    admin-tier token, and prevents a token issued to team A from being
    silently treated as a token for team B if the same handle is in
    both teams.

    Raises 401 if no valid credential is found, or if the credential
    resolves to a token without a team_id (must be re-issued per
    v0.6.0).
    """
    # 1. Prefer the explicit Authorization header (programmatic clients).
    token = _token_from_authorization(authorization)
    if token:
        resolved = lookup_full(token)
        if not resolved:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        github, team_id, admin = resolved
        if not team_id:
            log.warning(
                "auth: token for %s has no team_id; rejecting", github,
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=(
                    "token has no team assignment; ask the operator to "
                    "re-issue it with `vezir token issue --team <id>`."
                ),
                headers={"WWW-Authenticate": "Bearer"},
            )
        log.debug(
            "auth: %s via header (team=%s admin=%s)",
            github, team_id, admin,
        )
        return github, team_id, admin

    # 2. Fall back to the session cookie.
    cookie_value = (vezir_session or "").strip()
    if cookie_value:
        result = web_sessions.lookup_session(cookie_value)
        if result is not None:
            github, team_id, admin = result
            if not team_id:
                # A pre-0.6.0 cookie was minted before team_id was
                # captured.  Force re-login rather than silently
                # leaking cross-team.
                log.info(
                    "auth: pre-0.6.0 cookie for %s (no team_id); "
                    "forcing re-login",
                    github,
                )
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=(
                        "session predates team isolation (v0.6.0); "
                        "please /logout and sign in again."
                    ),
                )
            log.debug(
                "auth: %s via session cookie (team=%s admin=%s)",
                github, team_id, admin,
            )
            return github, team_id, admin
        # Pre-0.1.12 cookies stored the plaintext bearer. Accept once.
        if cookie_value.startswith("vzr_"):
            resolved = lookup_full(cookie_value)
            if resolved:
                github, team_id, admin = resolved
                if not team_id:
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        detail=(
                            "legacy bearer-in-cookie has no team_id; "
                            "ask operator to re-issue your token."
                        ),
                    )
                log.info(
                    "auth: %s via legacy bearer-in-cookie (team=%s)",
                    github, team_id,
                )
                return github, team_id, admin

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
    additionally checks that the *specific token used for this request*
    has ``is_admin=true``.

    This is a per-token check, not per-handle. A scribe-tier token does
    NOT inherit admin access even if the same github handle also owns an
    admin-tier token. The admin flag is captured at session-creation time
    for cookie-based auth (see ``web_sessions.open_session``).

    Currently gates /admin/enroll and /admin/teams.  Legacy tokens (no
    ``is_admin`` field) are rejected — operators must re-issue with
    ``--admin`` to keep enrollment access.
    """
    github, _team, admin = _resolve_auth(authorization, vezir_session)
    if admin:
        return github

    log.info("auth: %s lacks admin role for /admin/* route", github)
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=(
            "this route requires an admin token. Ask the operator to "
            "re-issue your token with `vezir token issue --admin "
            f"--github {github}`."
        ),
    )
