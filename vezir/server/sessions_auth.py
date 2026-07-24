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
    one-generation grace window: within
    ``config.refresh_grace_seconds()`` (default 60 s) of the rotation the
    pair is re-issued (lost-response retry); outside the window — or for
    anything older than one generation — it is confirmed reuse, which
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

import calendar
import hashlib
import hmac
import logging
import secrets
import sqlite3
import threading
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
    """Parse an ISO8601 ``…Z`` timestamp to unix seconds, or None.

    ``calendar.timegm`` (UTC), NOT ``time.mktime`` (local time): ``_iso``
    writes UTC via ``time.gmtime``, so parsing must be UTC-symmetric.
    ``mktime`` on a non-UTC server skewed every expiry check by the UTC
    offset — the exact bug ``auth.py`` fixed and documented earlier.
    """
    if not value:
        return None
    try:
        return int(calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")))
    except (ValueError, TypeError):
        return None


def _new_refresh_token() -> str:
    return _REFRESH_PREFIX + secrets.token_urlsafe(32)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ── Grace replay cache (v0.12.1) ────────────────────────────────────────────
#
# When a rotation succeeds we cache the *exact* response pair keyed by the
# consumed token's hash.  A lost-response retry (the client presents the
# token it still holds because our /refresh reply never arrived) then gets
# the byte-identical pair back instead of minting a NEW generation.  This
# closes CVE-class hijack: previously the grace path forked a fresh family
# to whoever presented the stale token and slid ``last_rotated_at`` forward
# on every hit, letting a thief renew indefinitely and never tripping reuse
# detection.  Now a stale token can only ever replay one frozen response,
# the window is anchored to the original rotation, and the family cannot be
# forked.  Entries self-expire after the grace window; the cache is bounded
# by pruning expired entries on each access.
_GRACE_CACHE: dict[str, tuple[float, dict]] = {}
_GRACE_LOCK = threading.Lock()


def _grace_prune(now: float) -> None:
    """Drop expired grace entries (called under ``_GRACE_LOCK``)."""
    grace = config.refresh_grace_seconds()
    expired = [h for h, (ts, _) in _GRACE_CACHE.items() if now - ts > grace]
    for h in expired:
        _GRACE_CACHE.pop(h, None)


def _grace_store(consumed_hash: str, response: dict) -> None:
    now = time.time()
    with _GRACE_LOCK:
        _grace_prune(now)
        _GRACE_CACHE[consumed_hash] = (now, response)


def _grace_lookup(consumed_hash: str) -> dict | None:
    now = time.time()
    with _GRACE_LOCK:
        _grace_prune(now)
        entry = _GRACE_CACHE.get(consumed_hash)
        return entry[1] if entry is not None else None


def _reset_grace_cache_for_tests() -> None:
    with _GRACE_LOCK:
        _GRACE_CACHE.clear()


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
    pair = _access_pair(github, npub, is_admin, sid, refresh_token)
    # Cache under this token's hash so the FIRST rotation being lost still
    # replays idempotently within the grace window.
    _grace_store(_hash(refresh_token), pair)
    return pair


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
            # Not the current token.  Is it the token we rotated away
            # from ONE generation ago?  Within a short grace window after
            # that rotation this is almost certainly a legitimate client
            # whose rotation response was lost (flaky link, killed app) —
            # the exact scenario the module docstring promises to honor.
            # Rotate again from the presented token instead of killing the
            # session.  Outside the window (or for anything older than one
            # generation) it is confirmed reuse → revoke the family
            # (RFC 9700 reuse detection).
            stale = _find_by_prev_hash(c, token_hash)
            if stale is not None:
                grace = config.refresh_grace_seconds()
                last_rotated = _parse_iso(stale["last_rotated_at"])
                absolute_max = _parse_iso(stale["absolute_max_at"])
                within_grace = (
                    grace > 0
                    and not stale["revoked"]
                    and last_rotated is not None
                    and 0 <= now - last_rotated <= grace
                    and (absolute_max is None or now < absolute_max)
                )
                # Lost-response retry: replay the EXACT pair we minted when
                # this token was consumed — do NOT mint a new generation and
                # do NOT touch last_rotated_at (which would slide the window
                # and let a stale-token holder renew indefinitely, forking
                # the family away from the legitimate client).  The cached
                # response is idempotent: any number of retries of the same
                # stale token inside the window return byte-identical data,
                # and it expires with the window.
                if within_grace:
                    cached = _grace_lookup(token_hash)
                    if cached is not None:
                        log.info(
                            "refresh grace: sid=%s github=%s replayed cached "
                            "pair within %ds of rotation (lost-response retry)",
                            stale["sid"], stale["github"], grace,
                        )
                        return cached
                    # No cached response (e.g. server restarted since the
                    # rotation): we cannot safely replay, and minting a new
                    # generation here is the historic hijack vector.  Treat
                    # as reuse — the legitimate client will simply re-login.
                    log.warning(
                        "refresh grace: sid=%s github=%s stale token in "
                        "window but no cached response; treating as reuse",
                        stale["sid"], stale["github"],
                    )
                c.execute(
                    "UPDATE sessions SET revoked = 1 WHERE sid = ?",
                    (stale["sid"],),
                )
                # Commit the family revocation explicitly: we're about to
                # raise, and queue._conn only commits on a clean exit — an
                # exception would otherwise roll back the revoke.
                c.commit()
                _note_revoked(stale["sid"])
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
    pair = _access_pair(github, npub, is_admin, sid, new_refresh)
    # Cache keyed by the token we just consumed, so a lost-response retry of
    # THAT token replays this exact pair (grace path above) instead of
    # forking the family.
    _grace_store(token_hash, pair)
    return pair


# ── Revoked-sid cache (v0.11.0) ─────────────────────────────────────────────
#
# Access JWTs are stateless by design (no DB hit on the hot auth path),
# which meant a revoked session's already-minted access token kept
# working until its ``exp`` (up to 60 min; 24 h for legacy tokens).  This
# in-process cache closes that gap for the single-instance deployment:
# revocation endpoints populate it, and ``verify_session_jwt`` checks the
# token's ``sid`` against it — an O(1) set lookup, no DB hit.  Loaded
# from the DB once on first use so revocations survive a restart.

_REVOKED_SIDS: set[str] = set()
_REVOKED_LOADED = False
_REVOKED_LOADED_AT = 0.0
_REVOKED_LOCK = threading.Lock()

# How often to re-read the revoked set from the DB.  The in-process cache is
# updated instantly by same-process revocations, but an operator revoking via
# the ``vezir session revoke`` CLI does it in a SEPARATE process, straight
# against the shared SQLite — the running server would otherwise never learn
# of it and keep honoring that session's already-minted access JWTs until
# their (up to 60 min) ``exp``.  A short periodic refresh bounds that gap to
# ``_REVOKED_REFRESH_SEC`` for out-of-process revocations while keeping the
# hot auth path a plain O(1) set membership test almost always.
_REVOKED_REFRESH_SEC = 15.0


def _load_revoked_sids() -> None:
    """Load (and periodically refresh) the revoked-sid set from the DB.

    First call does a blocking load; subsequent calls re-query only once
    every ``_REVOKED_REFRESH_SEC`` so out-of-process (CLI) revocations
    propagate without a per-request DB hit.
    """
    global _REVOKED_LOADED, _REVOKED_LOADED_AT
    now = time.time()
    if _REVOKED_LOADED and (now - _REVOKED_LOADED_AT) < _REVOKED_REFRESH_SEC:
        return
    with _REVOKED_LOCK:
        if _REVOKED_LOADED and (now - _REVOKED_LOADED_AT) < _REVOKED_REFRESH_SEC:
            return
        try:
            with queue._conn() as c:
                rows = c.execute(
                    "SELECT sid FROM sessions WHERE revoked = 1",
                ).fetchall()
            # Union with any in-process notes so a just-revoked sid is never
            # dropped by a refresh that raced the DB commit.
            _REVOKED_SIDS.update(r["sid"] for r in rows)
        except sqlite3.Error:
            log.exception("could not (re)load revoked-sid cache")
        _REVOKED_LOADED = True
        _REVOKED_LOADED_AT = now


def is_sid_revoked(sid: str) -> bool:
    """True if the session family ``sid`` has been revoked.

    Cheap in-process check used on the access-JWT hot path so a revoked
    session's access tokens die immediately instead of surviving until
    ``exp``.
    """
    _load_revoked_sids()
    return sid in _REVOKED_SIDS


def _note_revoked(sid: str) -> None:
    """Record a revocation in the in-process cache.

    Does NOT touch the DB, so it is safe to call while a ``queue._conn()``
    is open (``_load_revoked_sids`` would deadlock on the non-reentrant
    global DB lock there).  If the cache hasn't been loaded yet, the
    eventual load reads the committed row from the DB anyway; adding the
    sid early is harmless because the load only ever ADDS to the set.
    """
    _REVOKED_SIDS.add(sid)


def _reset_revoked_cache_for_tests() -> None:
    global _REVOKED_LOADED, _REVOKED_LOADED_AT
    with _REVOKED_LOCK:
        _REVOKED_SIDS.clear()
        _REVOKED_LOADED = False
        _REVOKED_LOADED_AT = 0.0


def revoke_session(sid: str) -> bool:
    """Revoke a single session family. Returns True if a row was affected."""
    with queue._conn() as c:
        cur = c.execute(
            "UPDATE sessions SET revoked = 1 WHERE sid = ? AND revoked = 0",
            (sid,),
        )
        affected = bool(cur.rowcount > 0)
    if affected:
        _note_revoked(sid)
    return affected


def revoke_all_for(github: str) -> int:
    """Revoke every active session for ``github``. Returns rows affected."""
    with queue._conn() as c:
        cur = c.execute(
            "UPDATE sessions SET revoked = 1 WHERE github = ? AND revoked = 0",
            (github,),
        )
        count = int(cur.rowcount)
        sids = [
            r["sid"]
            for r in c.execute(
                "SELECT sid FROM sessions WHERE github = ? AND revoked = 1",
                (github,),
            ).fetchall()
        ]
    for sid in sids:
        _note_revoked(sid)
    return count


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
