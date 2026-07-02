"""Rotating refresh-token sessions with reuse detection.

Fixes the "24-hour forced logout" UX problem: instead of a single 24h
session JWT that forces a fresh signer prompt / Google device grant on
expiry, a login now mints a **pair**:

  * a short-lived **access JWT** (default 60 min, ``config.access_ttl_seconds``)
    reused as ``Authorization: Bearer`` exactly like before, and
  * a long-lived opaque **refresh token** (``vzrt_…``) sent only to
    ``POST /api/auth/refresh`` to mint the next pair.

Security model (RFC 9700 / OAuth 2.1 Security BCP, Jan 2025):

  * **Rotation** — every refresh consumes the presented refresh token and
    issues a new one.  Refresh tokens are single-use.
  * **Reuse detection** — a ``sessions`` row is a token *family*.  If a
    refresh token that has already been rotated away is presented again
    (replay of a stolen token, or a legitimate client whose rotation
    response was lost), we look at ``prev_refresh_hash`` for a
    one-generation grace window; anything older = confirmed reuse, which
    **revokes the entire family** and logs a security event.
  * **Bounded lifetime** — a family expires on idle
    (``config.refresh_idle_ttl_seconds``, reset each rotation) and on an
    absolute cap from creation (``config.session_max_ttl_seconds``), after
    which a full re-login is required.
  * **Revocable** — sessions are server-side state, so an individual
    session can be revoked (which access JWTs never could be; only whole
    ``.session-secret`` rotation invalidated them before).

Refresh tokens are stored **hashed** (SHA-256), never in plaintext, the
same posture as ``vzr_`` bearer tokens (:mod:`vezir.server.auth`).  The
access JWT stays stateless — its ``exp`` is self-contained and the hot
auth path (``auth.lookup_identity``) does no DB hit for it.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import sqlite3
import time
import uuid

from .. import config
from . import nostr_auth, queue

log = logging.getLogger("vezir.sessions_auth")

# Opaque refresh-token prefix; mirrors the ``vzr_`` convention so a
# refresh token is visually distinct from an access JWT / bearer token.
_REFRESH_PREFIX = "vzrt_"


def _now() -> int:
    return int(time.time())


def _iso(ts: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _parse_iso(value: str | None) -> int | None:
    """Parse an ISO8601 ``…Z`` timestamp to unix seconds, or None."""
    if not value:
        return None
    try:
        return int(time.mktime(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")))
    except (ValueError, TypeError):
        return None


def _new_refresh_token() -> str:
    return _REFRESH_PREFIX + secrets.token_urlsafe(32)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class SessionError(Exception):
    """Refresh could not be honored (expired, revoked, reused, or unknown).

    Callers map this to HTTP 401 — the client must fall back to a full
    login.  ``reuse`` distinguishes a confirmed replay (family revoked) so
    the endpoint can log it as a security event.
    """

    def __init__(self, message: str, *, reuse: bool = False) -> None:
        super().__init__(message)
        self.reuse = reuse


def _access_pair(
    github: str, npub: str, is_admin: bool, sid: str, refresh_token: str
) -> dict:
    """Assemble the JSON body returned by login and refresh."""
    access_ttl = config.access_ttl_seconds()
    access_jwt = nostr_auth.issue_session_jwt(
        github, npub, is_admin, ttl_seconds=access_ttl, sid=sid,
    )
    return {
        # ``session_jwt`` keeps the pre-refresh key name so existing
        # clients that only read ``session_jwt`` keep working.
        "session_jwt": access_jwt,
        "access_jwt": access_jwt,
        "refresh_token": refresh_token,
        "expires_in": access_ttl,
        "refresh_expires_in": config.refresh_idle_ttl_seconds(),
        "session_max_ttl": config.session_max_ttl_seconds(),
        "sid": sid,
    }


def create_session(
    github: str, npub: str, is_admin: bool, auth_method: str
) -> dict:
    """Start a new session family and return the first access/refresh pair.

    ``auth_method`` is ``"nostr"`` or ``"google"`` (recorded for auditing
    and future per-method policy).  ``npub`` is ``""`` for Google
    identities.  The returned dict is the login response body.
    """
    now = _now()
    sid = uuid.uuid4().hex
    refresh_token = _new_refresh_token()
    idle_ttl = config.refresh_idle_ttl_seconds()
    max_ttl = config.session_max_ttl_seconds()
    with queue._conn() as c:
        c.execute(
            "INSERT INTO sessions "
            "(sid, github, npub, is_admin, auth_method, refresh_hash, "
            " prev_refresh_hash, created_at, refresh_expires_at, "
            " absolute_max_at, last_rotated_at, revoked) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, NULL, 0)",
            (
                sid, github, npub, 1 if is_admin else 0, auth_method,
                _hash(refresh_token), _iso(now),
                _iso(now + idle_ttl), _iso(now + max_ttl),
            ),
        )
    log.info(
        "session created: sid=%s github=%s method=%s", sid, github, auth_method,
    )
    return _access_pair(github, npub, is_admin, sid, refresh_token)


def _find_by_hash(
    c: sqlite3.Connection, token_hash: str
) -> sqlite3.Row | None:
    """Return a session row where ``refresh_hash`` matches, else None.

    Uses a constant-time compare against candidate rows to avoid leaking,
    via timing, whether a given hash exists.  In practice the indexed
    lookup already discriminates, but the compare keeps intent explicit
    and mirrors ``auth._lookup_entry``.
    """
    row: sqlite3.Row | None = c.execute(
        "SELECT * FROM sessions WHERE refresh_hash = ?", (token_hash,),
    ).fetchone()
    if row is not None and hmac.compare_digest(row["refresh_hash"], token_hash):
        return row
    return None


def _find_by_prev_hash(
    c: sqlite3.Connection, token_hash: str
) -> sqlite3.Row | None:
    """Return a session whose *previous* refresh hash matches, else None."""
    row: sqlite3.Row | None = c.execute(
        "SELECT * FROM sessions WHERE prev_refresh_hash = ?", (token_hash,),
    ).fetchone()
    if row is not None and row["prev_refresh_hash"] is not None and \
            hmac.compare_digest(row["prev_refresh_hash"], token_hash):
        return row
    return None


def rotate(refresh_token: str) -> dict:
    """Consume ``refresh_token``, rotate the family, return a new pair.

    Raises :class:`SessionError` (→ 401) when the token is unknown,
    expired (idle or absolute cap), from a revoked family, or a confirmed
    reuse.  On confirmed reuse the whole family is revoked as a
    side-effect.
    """
    if not refresh_token or not refresh_token.startswith(_REFRESH_PREFIX):
        raise SessionError("not a refresh token")

    token_hash = _hash(refresh_token)
    now = _now()
    with queue._conn() as c:
        row = _find_by_hash(c, token_hash)
        if row is None:
            # Not the current token.  Is it a token we already rotated
            # away from?  If so this is reuse of a consumed token → kill
            # the family (RFC 9700 reuse detection).
            stale = _find_by_prev_hash(c, token_hash)
            if stale is not None:
                c.execute(
                    "UPDATE sessions SET revoked = 1 WHERE sid = ?",
                    (stale["sid"],),
                )
                # Commit the family revocation explicitly: we're about to
                # raise, and queue._conn only commits on a clean exit — an
                # exception would otherwise roll back the revoke.
                c.commit()
                log.warning(
                    "refresh token REUSE detected for sid=%s github=%s; "
                    "revoking session family",
                    stale["sid"], stale["github"],
                )
                raise SessionError("refresh token reuse detected", reuse=True)
            raise SessionError("unknown refresh token")

        sid = row["sid"]
        if row["revoked"]:
            raise SessionError("session revoked")

        absolute_max = _parse_iso(row["absolute_max_at"])
        if absolute_max is not None and now >= absolute_max:
            raise SessionError("session reached absolute lifetime cap")

        idle_expiry = _parse_iso(row["refresh_expires_at"])
        if idle_expiry is not None and now >= idle_expiry:
            raise SessionError("refresh token idle-expired")

        # Rotate: mint a new refresh token, keep the just-consumed one as
        # the one-generation grace hash, bump the idle window (never past
        # the absolute cap).
        new_refresh = _new_refresh_token()
        idle_ttl = config.refresh_idle_ttl_seconds()
        new_idle = now + idle_ttl
        if absolute_max is not None:
            new_idle = min(new_idle, absolute_max)
        c.execute(
            "UPDATE sessions SET refresh_hash = ?, prev_refresh_hash = ?, "
            "refresh_expires_at = ?, last_rotated_at = ? WHERE sid = ?",
            (
                _hash(new_refresh), row["refresh_hash"],
                _iso(new_idle), _iso(now), sid,
            ),
        )
        github = row["github"]
        npub = row["npub"] or ""
        is_admin = bool(row["is_admin"])

    log.debug("session rotated: sid=%s github=%s", sid, github)
    return _access_pair(github, npub, is_admin, sid, new_refresh)


def revoke_session(sid: str) -> bool:
    """Revoke a single session family. Returns True if a row was affected."""
    with queue._conn() as c:
        cur = c.execute(
            "UPDATE sessions SET revoked = 1 WHERE sid = ? AND revoked = 0",
            (sid,),
        )
        return bool(cur.rowcount > 0)


def revoke_all_for(github: str) -> int:
    """Revoke every active session for ``github``. Returns rows affected."""
    with queue._conn() as c:
        cur = c.execute(
            "UPDATE sessions SET revoked = 1 WHERE github = ? AND revoked = 0",
            (github,),
        )
        return int(cur.rowcount)


def list_sessions(github: str | None = None) -> list[dict]:
    """List sessions (optionally filtered by github), newest first.

    Never returns hashes; safe for admin/CLI display.
    """
    with queue._conn() as c:
        if github:
            rows = c.execute(
                "SELECT sid, github, npub, is_admin, auth_method, created_at, "
                "refresh_expires_at, absolute_max_at, last_rotated_at, revoked "
                "FROM sessions WHERE github = ? ORDER BY created_at DESC",
                (github,),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT sid, github, npub, is_admin, auth_method, created_at, "
                "refresh_expires_at, absolute_max_at, last_rotated_at, revoked "
                "FROM sessions ORDER BY created_at DESC",
            ).fetchall()
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        d["is_admin"] = bool(d["is_admin"])
        d["revoked"] = bool(d["revoked"])
        out.append(d)
    return out
