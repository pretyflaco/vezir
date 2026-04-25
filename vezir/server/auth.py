"""Bearer-token auth.

Tokens are stored hashed in ~/vezir-data/tokens.json:

    {
      "tokens": [
        { "github": "kasita", "token_hash": "<sha256>", "issued_at": "..." }
      ]
    }

The plaintext token is shown ONCE at issue time and is never persisted.
Lookup is by SHA-256 of the presented bearer token.
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
import time
from pathlib import Path

from fastapi import Cookie, Header, HTTPException, status

from .. import config

log = logging.getLogger("vezir.auth")

# Cookie used by the browser hand-off flow (see server.login). Value is the
# plaintext bearer token; HttpOnly + SameSite=Lax. Equivalent risk profile
# to the bearer header (which also carries plaintext) — the network surface
# is Tailscale-only either way.
COOKIE_NAME = "vezir_session"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _load_tokens() -> dict:
    p = config.tokens_json_path()
    if not p.exists():
        return {"tokens": []}
    return json.loads(p.read_text(encoding="utf-8"))


def _save_tokens(data: dict) -> None:
    p = config.tokens_json_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")


def issue(github: str) -> str:
    """Generate a new token for a GitHub handle. Returns the plaintext token.

    Plaintext is never written to disk; only the hash is persisted. Caller
    must capture and hand the plaintext to the user.
    """
    data = _load_tokens()
    plaintext = "vzr_" + secrets.token_urlsafe(32)
    data["tokens"].append(
        {
            "github": github,
            "token_hash": _hash(plaintext),
            "issued_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
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


def lookup(token: str) -> str | None:
    """Resolve a bearer token to its GitHub handle, or None."""
    h = _hash(token)
    data = _load_tokens()
    for entry in data["tokens"]:
        if entry["token_hash"] == h:
            return entry["github"]
    return None


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
                "not signed in. Open the dashboard via vezir gui's "
                "'Open dashboard' button, or visit /login to sign in."
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
