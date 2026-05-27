"""Bearer-token auth + team context resolution.

v0.7.0: tokens no longer carry a ``team_id``.  A token identifies a
human (``github`` handle) and a privilege tier (``is_admin``).  The
team scope for each request is supplied by the client via an
``X-Team-Id`` request header and validated against the ``memberships``
table.  This lets one human switch between every team they belong to
without re-issuing tokens.

Tokens are stored hashed in ``~/vezir-data/tokens.json``:

    {
      "tokens": [
        {
          "github": "kasita",
          "token_hash": "<sha256>",
          "issued_at": "...",
          "expires_at": "..." | null,
          "last_used_at": "..." | null,
          "is_admin": false,
          "label": "..." | null
        }
      ]
    }

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
import json
import logging
import secrets
import time

from fastapi import Header, HTTPException, status

from .. import config
from . import queue

log = logging.getLogger("vezir.auth")

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
    """Remove all tokens for a given github handle.  Returns count removed."""
    data = _load_tokens()
    before = len(data["tokens"])
    data["tokens"] = [t for t in data["tokens"] if t["github"] != github]
    _save_tokens(data)
    return before - len(data["tokens"])


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

    data = _load_tokens()
    kept: list[dict] = []
    removed: list[dict] = []
    for entry in data["tokens"]:
        if github is not None and entry.get("github") != github:
            kept.append(entry)
            continue
        if label is not None:
            entry_label = entry.get("label")
            # ``-`` in the CLI is the visual stand-in for ``None``;
            # treat both equivalently so the operator can revoke a
            # label-less token by passing ``--label -``.
            if label == "-":
                if entry_label is not None:
                    kept.append(entry)
                    continue
            else:
                if entry_label != label:
                    kept.append(entry)
                    continue
        if token_id_prefix is not None:
            tid = entry.get("token_hash") or ""
            if not tid.startswith(token_id_prefix):
                kept.append(entry)
                continue
        # Survived every filter -> remove.
        removed.append(
            {
                "github": entry.get("github"),
                "label": entry.get("label"),
                "token_id": (entry.get("token_hash") or "")[:12],
                "issued_at": entry.get("issued_at"),
            }
        )
    if removed:
        data["tokens"] = kept
        _save_tokens(data)
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
    data = _load_tokens()
    out: list[dict] = []
    for entry in data.get("tokens", []):
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
    team_id = x_team_id.strip()
    if not queue.is_member(github, team_id):
        log.info(
            "auth: %s denied access to team %s (not a member)",
            github, team_id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"user {github!r} is not a member of team {team_id!r}; "
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
