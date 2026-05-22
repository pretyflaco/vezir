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

log = logging.getLogger("vezir.auth")

# Cookie used by the browser hand-off flow (see server.login). Value is the
# plaintext bearer token; HttpOnly + SameSite=Lax. Equivalent risk profile
# to the bearer header (which also carries plaintext) — the network surface
# is VPN-only (Tailscale or nostr-vpn) either way.
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
        Marks the token as admin-tier. Reserved for a follow-up commit
        that adds the ``require_admin`` dependency; storing the field
        now means new tokens are ready when that lands.
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

    Legacy rows (missing the field) are treated as non-admin. Used by the
    ``require_admin`` dependency added in a follow-up commit.
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
    artifact downloads). The cookie is set via /login?token=...&next=...
    and contains the bearer token plaintext (see server.login).

    Returns the GitHub handle of the authenticated scribe.
    """
    # Prefer the explicit Authorization header when present (programmatic
    # access, e.g. curl/httpx tooling).
    token = _token_from_authorization(authorization)
    via = "header"
    if not token:
        token = (vezir_session or "").strip() or None
        via = "cookie"

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                "not signed in. Visit /login and paste your token, "
                "or use the URL from `vezir scribe` / `vezir upload` "
                "output which signs you in automatically."
            ),
            headers={"WWW-Authenticate": "Bearer"},
        )
    github = lookup(token)
    if not github:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    log.debug("auth: %s via %s", github, via)
    return github
