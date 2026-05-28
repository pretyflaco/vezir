"""Bearer-token auth + team context resolution.

v0.7.0: tokens no longer carry a ``team_id``.  A token identifies a
human (``github`` handle) and a privilege tier (``is_admin``).  The
team scope for each request is supplied by the client via an
``X-Team-Id`` request header and validated against the ``memberships``
table.  This lets one human switch between every team they belong to
without re-issuing tokens.

v0.7.2: tokens moved from a flat ``~/vezir-data/tokens.json`` file into
the ``tokens`` table in ``~/vezir-data/vezir.sqlite``.  The old file did
a full-file read-modify-write with **no lock**, which had a lost-update
race on concurrent ``issue``/``revoke`` and the ``last_used_at`` touch
that fires on nearly every authenticated request.  Storing rows in
SQLite reuses ``queue._conn`` (global ``_LOCK`` + WAL + ``busy_timeout``)
so each read-modify-write is atomic.  The one-shot migration
``migrate_0_7_2`` imports any existing ``tokens.json`` and renames it to
``tokens.json.migrated``.

Table shape::

    tokens(
      token_hash TEXT PRIMARY KEY,   -- sha256 of the plaintext bearer
      github     TEXT NOT NULL,
      issued_at  TEXT NOT NULL,
      expires_at TEXT,               -- nullable
      last_used_at TEXT,             -- nullable
      is_admin   INTEGER NOT NULL DEFAULT 0,
      label      TEXT                -- nullable
    )

The plaintext token is shown ONCE at issue time and is never persisted.
Lookup is by SHA-256 of the presented bearer token, in constant time.

Auth dependencies:

* ``require_bearer`` -- ``(github,)``; for routes that don't need a
  team context (``/api/me``, admin routes, /health).
* ``require_team_context`` -- ``(github, team_id, is_admin)``; for
  every team-scoped route.  Reads ``X-Team-Id`` and checks membership.
* ``require_admin`` -- like ``require_bearer`` but also requires
  ``is_admin=true`` on the token.  Used by ``/admin/*`` routes that
  manage teams + memberships globally.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time

from fastapi import Header, HTTPException, status

from . import queue

log = logging.getLogger("vezir.auth")

# Debounce window for `last_used_at` writes. Prevents a write storm when
# a single client polls /api/sessions every few seconds. We still observe
# every use; we just only persist when the previously-stored timestamp is
# older than this many seconds.
_LAST_USED_WRITE_DEBOUNCE_SEC = 60


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _row_to_entry(row) -> dict:
    """Normalize a sqlite ``tokens`` row into the legacy dict shape.

    The rest of this module historically operated on dicts loaded from
    ``tokens.json``; keeping that shape lets the public helpers stay
    unchanged after the v0.7.2 move to SQLite.  Tolerant of partial
    SELECTs: columns not present in the row are reported as ``None``
    (``is_admin`` as ``False``).
    """
    keys = set(row.keys())

    def _g(name):
        return row[name] if name in keys else None

    return {
        "github": _g("github"),
        "token_hash": _g("token_hash"),
        "issued_at": _g("issued_at"),
        "expires_at": _g("expires_at"),
        "last_used_at": _g("last_used_at"),
        "is_admin": bool(_g("is_admin")),
        "label": _g("label"),
    }


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
    """Generate a new token for a GitHub handle.  Returns the plaintext token.

    v0.7.0: ``team_id`` was removed.  Team scope is supplied per-request
    via the ``X-Team-Id`` header and validated against the memberships
    table.  To grant a freshly-issued token access to a team, the
    operator must also add a membership row -- see
    ``vezir team add-member``.

    Plaintext is never written to disk; only the hash is persisted.
    Caller must capture and hand the plaintext to the user.

    Parameters
    ----------
    github:
        GitHub handle the token belongs to.
    expires_in_seconds:
        If given (>0), the token expires that many seconds from now.
        ``None`` or 0 means no expiry.
    is_admin:
        Marks the token as admin-tier.  Admin tokens can manage teams
        and memberships across the whole server.
    label:
        Free-text annotation displayed by ``vezir token list``.  Useful
        for "android-phone" / "linux-laptop" style hints when one human
        owns multiple tokens.  Never used in auth decisions.
    """
    plaintext = "vzr_" + secrets.token_urlsafe(32)
    issued_at = _now_iso()
    expires_at: str | None = None
    if expires_in_seconds and expires_in_seconds > 0:
        expires_at = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(time.time() + expires_in_seconds),
        )
    with queue._conn() as c:
        c.execute(
            "INSERT INTO tokens (token_hash, github, issued_at, expires_at, "
            "last_used_at, is_admin, label) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                _hash(plaintext),
                github,
                issued_at,
                expires_at,
                None,
                1 if is_admin else 0,
                label or None,
            ),
        )
    return plaintext


def revoke(github: str) -> int:
    """Remove all tokens for a given github handle.  Returns count removed."""
    with queue._conn() as c:
        cur = c.execute("DELETE FROM tokens WHERE github = ?", (github,))
        return cur.rowcount


def revoke_by_filter(
    github: str | None = None,
    label: str | None = None,
    token_id_prefix: str | None = None,
) -> list[dict]:
    """Remove tokens matching ALL provided (non-None) filters.

    v0.7.0: removed the ``team_id`` filter -- tokens are no longer
    scoped to a team.  Use ``vezir team remove-member`` to remove
    a human's access to a team.

    Returns a list of the removed entries (with ``token_hash`` truncated
    to its first 12 chars for safe logging) so callers can present a
    confirmation summary.  Refuses to delete the entire store: at least
    one filter must be non-None.  When no rows match, returns an empty
    list (does not raise).
    """
    if github is None and label is None and token_id_prefix is None:
        raise ValueError(
            "revoke_by_filter requires at least one of "
            "github/label/token_id_prefix"
        )
    if token_id_prefix is not None and len(token_id_prefix) < 4:
        raise ValueError(
            "token_id_prefix must be at least 4 characters to avoid "
            "accidental wide matches"
        )

    removed: list[dict] = []
    with queue._conn() as c:
        rows = c.execute(
            "SELECT token_hash, github, issued_at, label FROM tokens"
        ).fetchall()
        to_delete: list[str] = []
        for row in rows:
            entry = _row_to_entry(row)
            if github is not None and entry.get("github") != github:
                continue
            if label is not None:
                entry_label = entry.get("label")
                # ``-`` in the CLI is the visual stand-in for ``None``;
                # treat both equivalently so the operator can revoke a
                # label-less token by passing ``--label -``.
                if label == "-":
                    if entry_label is not None:
                        continue
                else:
                    if entry_label != label:
                        continue
            if token_id_prefix is not None:
                tid = entry.get("token_hash") or ""
                if not tid.startswith(token_id_prefix):
                    continue
            # Survived every filter -> remove.
            to_delete.append(entry["token_hash"])
            removed.append(
                {
                    "github": entry.get("github"),
                    "label": entry.get("label"),
                    "token_id": (entry.get("token_hash") or "")[:12],
                    "issued_at": entry.get("issued_at"),
                }
            )
        if to_delete:
            c.executemany(
                "DELETE FROM tokens WHERE token_hash = ?",
                [(h,) for h in to_delete],
            )
    return removed


def list_tokens() -> list[dict]:
    """Return all token rows.

    v0.7.0: no per-team filtering (tokens aren't team-scoped anymore).
    Use ``vezir team members <slug>`` to list humans on a team.

    Returns a list of dicts with the persisted fields plus a derived
    ``token_id`` (first 12 chars of ``token_hash``) for display use.
    Never returns the full ``token_hash`` -- callers don't need it and
    leaking it would weaken the at-rest hashing.
    """
    with queue._conn() as c:
        rows = c.execute(
            "SELECT token_hash, github, issued_at, expires_at, last_used_at, "
            "is_admin, label FROM tokens ORDER BY issued_at"
        ).fetchall()
    out: list[dict] = []
    for row in rows:
        entry = _row_to_entry(row)
        out.append(
            {
                "github": entry.get("github"),
                "label": entry.get("label"),
                "token_id": (entry.get("token_hash") or "")[:12],
                "issued_at": entry.get("issued_at"),
                "expires_at": entry.get("expires_at"),
                "last_used_at": entry.get("last_used_at"),
                "is_admin": bool(entry.get("is_admin", False)),
            }
        )
    return out


def _maybe_touch_last_used(entry_hash: str) -> None:
    """Update ``last_used_at`` for the matching token row, debounced.

    Called on every successful lookup. Persists only if the previously
    stored timestamp is older than ``_LAST_USED_WRITE_DEBOUNCE_SEC`` to
    avoid hammering the JSON file on every poll.
    """
    now = time.time()
    try:
        with queue._conn() as c:
            row = c.execute(
                "SELECT last_used_at FROM tokens WHERE token_hash = ?",
                (entry_hash,),
            ).fetchone()
            if row is None:
                return
            prev = _parse_iso(row["last_used_at"])
            if prev is None or (now - prev) >= _LAST_USED_WRITE_DEBOUNCE_SEC:
                c.execute(
                    "UPDATE tokens SET last_used_at = ? WHERE token_hash = ?",
                    (_now_iso(), entry_hash),
                )
    except Exception:
        # Best-effort: a transient DB error on the touch path must not
        # break auth. The next successful touch will catch up.
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
    with queue._conn() as c:
        row = c.execute(
            "SELECT token_hash, github, issued_at, expires_at, last_used_at, "
            "is_admin, label FROM tokens WHERE token_hash = ?",
            (h,),
        ).fetchone()
    if row is None:
        return None
    entry = _row_to_entry(row)
    # Defensive constant-time confirmation of the stored hash.
    if not hmac.compare_digest(entry.get("token_hash") or "", h):
        return None
    if _is_expired(entry):
        return None
    return entry


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


def lookup_identity(token: str) -> tuple[str, bool] | None:
    """Resolve a bearer token to ``(github, is_admin)``, or None.

    v0.7.0: replaces ``lookup_full`` (which also returned ``team_id``).
    Team context is now derived per-request from the ``X-Team-Id``
    header, not from the token.

    Side-effect: debounced ``last_used_at`` touch on hit.
    """
    entry = _lookup_entry(token)
    if entry is None:
        return None
    _maybe_touch_last_used(entry.get("token_hash") or "")
    return (
        entry.get("github") or "",
        bool(entry.get("is_admin", False)),
    )


def is_admin_token(token: str) -> bool:
    """True iff the token resolves AND the matching row has ``is_admin=true``.

    Legacy rows (missing the field) are treated as non-admin.
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


def require_bearer(
    authorization: str | None = Header(default=None),
) -> tuple[str, bool]:
    """FastAPI dependency: bearer auth returning ``(github, is_admin)``.

    Use this for routes that don't need a team context (``/api/me``,
    ``/health``).  For team-scoped routes use
    ``require_team_context``.
    """
    token = _token_from_authorization(authorization)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    resolved = lookup_identity(token)
    if resolved is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    github, admin = resolved
    log.debug("auth: %s via header (admin=%s)", github, admin)
    return github, admin


def require_team_context(
    authorization: str | None = Header(default=None),
    x_team_id: str | None = Header(default=None),
) -> tuple[str, str, bool]:
    """FastAPI dependency: bearer + X-Team-Id, returns ``(github, team_id, is_admin)``.

    v0.7.0: the canonical dependency for every team-scoped route.

    Raises:
      * 401 on missing/invalid bearer token.
      * 400 on missing ``X-Team-Id`` header.
      * 403 when the token's github handle is not a member of the
        requested team (covers both "team doesn't exist" and "user
        not a member" -- we don't distinguish to avoid leaking team
        existence).

    The ``is_admin`` flag in the result reflects the *token's* admin
    bit (server-wide privilege), NOT the user's role inside the team.
    Use ``queue.get_role(github, team_id)`` if a handler needs the
    per-team role.
    """
    github, admin = require_bearer(authorization)
    if not x_team_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "missing X-Team-Id header; client must specify which "
                "team to operate on"
            ),
        )
    # v0.7.4: X-Team-Id carries the team's stable uuid, but we accept a
    # slug too (resolve to uuid) for curl/debug ergonomics.  Handlers
    # downstream always operate on the uuid so jobs/shares store the
    # stable key.
    raw = x_team_id.strip()
    team_id = queue.resolve_team_uuid(raw) or raw
    if not queue.is_member(github, team_id):
        log.info(
            "auth: %s denied access to team %s (not a member)",
            github, team_id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"user {github!r} is not a member of team {raw!r}; "
                "ask an admin to add you with `vezir team add-member`."
            ),
        )
    log.debug(
        "auth: %s via header (team=%s admin=%s)", github, team_id, admin,
    )
    return github, team_id, admin


def require_admin(
    authorization: str | None = Header(default=None),
) -> str:
    """FastAPI dependency: bearer-only, requires ``is_admin=True`` on the token.

    Gates ``/admin/*`` routes.  Returns the github handle.
    """
    github, admin = require_bearer(authorization)
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
