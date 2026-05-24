"""In-memory browser session + exchange-code store.

Why this exists
---------------
Pre-0.1.12 vezir put the plaintext bearer token directly into:
  - the `vezir_session` cookie value, and
  - the `dashboard_login_url` query string (``?token=vzr_...``).

Both surfaces are easy to exfiltrate: the URL leaks into browser
history, server access logs, and any reverse-proxy log (relevant now
that we sit behind Caddy). The cookie value being the plaintext bearer
means any future XSS or JS injection grabs a long-lived credential.

This module decouples browser sessions from bearer tokens:

* `mint_exchange_code(token)` — one-time, short-lived (60s) random code
  that swaps for a session in `/login?code=...`. Used by upload/scribe
  response builders.
* `open_session(github)` — opaque 32-byte session id stored only in
  process memory. Returned to the browser as the `vezir_session` cookie.
* `lookup_session(sid)` — resolves a cookie to a github handle, or None.
* `close_session(sid)` — explicit logout.

Process-restart invalidates all sessions. That's acceptable for a
self-hosted dogfood deployment; the cost is "users sign in again after
the operator restarts the server", which is rare and visible.

Concurrency: a single threading.Lock guards both maps. The data is tiny
(handful of entries during dogfood) so even a global lock is fine.
"""
from __future__ import annotations

import logging
import secrets
import threading
import time

log = logging.getLogger("vezir.web_sessions")

# Exchange codes: ?code=<code> in /login swaps a code for a fresh
# session. Single-use, short TTL. Map: code -> (token_plaintext, expires_at).
_EXCHANGE_TTL_SEC = 60.0
_EXCHANGE_PREFIX = "vzx_"

# Browser sessions: opaque id stored in `vezir_session` cookie. Map:
# session_id -> (github, created_at). No expiry; lifetime is bounded by
# the server process. Operators rotate by restarting `vezir serve`.
_SESSION_PREFIX = "vzs_"

_lock = threading.Lock()
_exchange: dict[str, tuple[str, float]] = {}
_sessions: dict[str, tuple[str, float]] = {}


def _now() -> float:
    return time.time()


def _purge_expired_exchange_locked() -> None:
    """Caller must hold _lock. Drop expired codes; cheap during low load."""
    now = _now()
    expired = [c for c, (_, exp) in _exchange.items() if exp <= now]
    for c in expired:
        _exchange.pop(c, None)


# ── exchange codes ─────────────────────────────────────────────────────────


def mint_exchange_code(token: str) -> str:
    """Mint a one-time exchange code that swaps for a session via /login.

    The code carries the *bearer token* (not a github handle) because we
    want `/login?code=...` to verify the token at consume time. That way
    a revoked bearer token can't be redeemed even if a code was minted
    moments before revocation.

    Returns the printable code (with ``vzx_`` prefix) for use in URLs.
    Caller should not log it.
    """
    code = _EXCHANGE_PREFIX + secrets.token_urlsafe(24)
    with _lock:
        _purge_expired_exchange_locked()
        _exchange[code] = (token, _now() + _EXCHANGE_TTL_SEC)
    return code


def consume_exchange_code(code: str) -> str | None:
    """Atomically consume an exchange code. Returns the bearer token or None.

    Single-use: a successful consume removes the entry. Expired or unknown
    codes return None.
    """
    if not code or not code.startswith(_EXCHANGE_PREFIX):
        return None
    with _lock:
        _purge_expired_exchange_locked()
        entry = _exchange.pop(code, None)
    if entry is None:
        return None
    token, _expires_at = entry
    return token


# ── browser sessions ────────────────────────────────────────────────────────


def open_session(
    github: str,
    *,
    team_id: str = "",
    is_admin: bool = False,
) -> str:
    """Create a new opaque session id bound to a github handle.

    ``team_id`` and ``is_admin`` are captured at session-creation time
    (i.e. at /login) from the bearer token that was presented.  This
    lets ``require_admin`` check the specific token's admin flag and
    lets the visibility filter scope queries to the team the user
    actually signed in with — without scanning all tokens for the
    github handle.

    ``team_id`` added in v0.6.0; required in normal use but the
    parameter defaults to ``""`` for the migration window (cookies
    minted before the upgrade have no captured team_id; the auth chain
    rejects them with a "please /logout and sign in again" 401 so the
    next session is properly tagged).
    """
    sid = _SESSION_PREFIX + secrets.token_urlsafe(24)
    with _lock:
        _sessions[sid] = (github, _now(), bool(is_admin), team_id or "")
    log.debug(
        "opened session for %s (team=%s admin=%s)",
        github, team_id, is_admin,
    )
    return sid


def lookup_session(sid: str | None) -> tuple[str, str, bool] | None:
    """Resolve a session id to ``(github, team_id, is_admin)`` or None.

    v0.6.0: returns a 3-tuple including ``team_id`` so the visibility
    filter can scope to the team the user actually signed in with.
    Sessions created before v0.6.0 (within the same process lifetime)
    return ``team_id=""``; auth handlers reject those with a force-
    re-login 401 to avoid silent cross-team leakage.
    """
    if not sid or not sid.startswith(_SESSION_PREFIX):
        return None
    with _lock:
        entry = _sessions.get(sid)
    if entry is None:
        return None
    # entry layout history:
    #   pre-0.1.12-hotfix:  (github, created_at)
    #   0.1.12-hotfix..0.5.x: (github, created_at, is_admin)
    #   0.6.0+:             (github, created_at, is_admin, team_id)
    github = entry[0]
    is_admin = entry[2] if len(entry) > 2 else False
    team_id = entry[3] if len(entry) > 3 else ""
    return (github, team_id, is_admin)


def close_session(sid: str | None) -> None:
    """Invalidate a session id (used by /logout)."""
    if not sid:
        return
    with _lock:
        _sessions.pop(sid, None)


def session_count() -> int:
    """Diagnostic helper for tests and /status."""
    with _lock:
        return len(_sessions)


def exchange_count() -> int:
    """Diagnostic helper for tests."""
    with _lock:
        _purge_expired_exchange_locked()
        return len(_exchange)


def _reset_for_tests() -> None:
    """Clear all state. Used by tests; never call from production code."""
    with _lock:
        _exchange.clear()
        _sessions.clear()
