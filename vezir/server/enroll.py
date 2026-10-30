"""Enrollment payload and QR utilities.

v0.7.0: HTML enrollment page removed. QR codes are now generated
by the CLI (``vezir token enroll``) directly in the terminal.
This module provides the shared payload-building and QR-rendering
functions used by the CLI.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import segno

log = logging.getLogger("vezir.enroll")


# QR payload schema versions.
#   v1  {url, token}                       bearer in the QR (legacy)
#   v2  {url, token, ca_pem}               + internal CA for Android pinning
#   v3  {url, enroll_code[, ca_pem]}       short-lived single-use code; the
#        device POSTs it to /api/enroll/exchange and receives the bearer
#        (security audit 2026-10-30, M-8).  v1/v2 are still emitted by
#        ``vezir token enroll --legacy`` until vezir-android learns v3.
PAYLOAD_VERSION = 3
LEGACY_PAYLOAD_VERSION = 2

# An enrollment code is the credential for ten minutes, exactly once.
ENROLL_CODE_TTL_SECONDS = 10 * 60
_CODE_PREFIX = "vzre_"

# Hard cap on the embedded CA PEM length. A normal RSA-2048 or Ed25519
# Caddy root is ~1-2 KiB. Anything much larger is operator error and we
# bail rather than producing a multi-kilobyte QR that won't scan.
_MAX_CA_PEM_BYTES = 8 * 1024


def _load_caddy_root_cert() -> str | None:
    """Return the PEM-encoded Caddy internal CA cert, if configured.

    Configured via ``VEZIR_CADDY_ROOT_CERT_PATH``. Returns None when the
    env var is unset, the file is missing, or its contents are clearly
    not a PEM certificate.
    """
    path_env = os.environ.get("VEZIR_CADDY_ROOT_CERT_PATH")
    if not path_env:
        return None
    p = Path(path_env)
    if not p.exists():
        log.warning(
            "VEZIR_CADDY_ROOT_CERT_PATH=%s does not exist; falling back to v1 payload",
            path_env,
        )
        return None
    try:
        text = p.read_text(encoding="utf-8")
    except Exception as exc:
        log.warning("could not read %s: %s", path_env, exc)
        return None
    if "BEGIN CERTIFICATE" not in text:
        log.warning(
            "%s does not look like a PEM-encoded certificate; falling back to v1",
            path_env,
        )
        return None
    if len(text.encode("utf-8")) > _MAX_CA_PEM_BYTES:
        log.warning(
            "CA cert at %s is %d bytes (cap %d); falling back to v1",
            path_env, len(text), _MAX_CA_PEM_BYTES,
        )
        return None
    return text


def build_payload(
    server_url: str,
    token: str | None = None,
    ca_pem: str | None = None,
    *,
    enroll_code: str | None = None,
) -> str:
    """Return the canonical QR payload JSON string.

    With ``enroll_code`` → v3 (the default path).  With ``token`` → the
    legacy v1/v2 shape that embeds the bearer itself.
    """
    if enroll_code:
        obj: dict = {"v": PAYLOAD_VERSION, "url": server_url, "enroll_code": enroll_code}
        if ca_pem:
            obj["ca_pem"] = ca_pem
    elif token:
        if ca_pem:
            obj = {
                "v": LEGACY_PAYLOAD_VERSION,
                "url": server_url,
                "token": token,
                "ca_pem": ca_pem,
            }
        else:
            obj = {"v": 1, "url": server_url, "token": token}
    else:
        raise ValueError("build_payload needs an enroll_code or a token")
    return json.dumps(obj, separators=(",", ":"), sort_keys=True)


# ── enrollment codes (v3) ───────────────────────────────────────────────────


class EnrollError(Exception):
    """Code unknown, expired, or already used.  One message for all three
    so the exchange endpoint leaks nothing about which."""


def _hash(code: str) -> str:
    import hashlib

    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def create_code(
    github: str,
    *,
    label: str | None = None,
    token_ttl_seconds: int | None = None,
) -> tuple[str, datetime]:
    """Mint a single-use enrollment code for ``github``.

    Returns ``(plaintext_code, expires_at)``.  Only the SHA-256 is stored.
    The bearer is NOT issued yet — it comes into existence at exchange
    time, so a code that is never scanned leaves no credential behind.
    """
    import secrets

    from . import queue

    github = github.strip()
    if not github:
        raise ValueError("github must be non-empty")
    code = _CODE_PREFIX + secrets.token_urlsafe(24)
    now = _now()
    expires = now + timedelta(seconds=ENROLL_CODE_TTL_SECONDS)
    with queue._conn() as c:
        c.execute(
            "INSERT INTO enroll_codes (code_hash, github, label, token_ttl_seconds, "
            "created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
            (_hash(code), github, label, token_ttl_seconds, _iso(now), _iso(expires)),
        )
        # Opportunistic sweep: codes dead for a day carry no information.
        c.execute(
            "DELETE FROM enroll_codes WHERE expires_at < ?",
            (_iso(now - timedelta(days=1)),),
        )
    log.info("enroll code issued for github=%s (expires %s)", github, _iso(expires))
    return code, expires


def exchange_code(code: str, *, client_ip: str | None = None) -> dict:
    """Consume ``code`` once and issue the real bearer.

    Atomic: the row is marked consumed with a conditional UPDATE, so two
    concurrent exchanges of the same code cannot both succeed.  Raises
    :class:`EnrollError` for unknown / expired / already-used codes.
    """
    import hmac as _hmac

    from . import auth, queue

    code = (code or "").strip()
    if not code.startswith(_CODE_PREFIX) or len(code) < len(_CODE_PREFIX) + 16:
        raise EnrollError("invalid enrollment code")
    h = _hash(code)
    now = _now()
    with queue._conn() as c:
        row = c.execute("SELECT * FROM enroll_codes WHERE code_hash = ?", (h,)).fetchone()
        if row is None or not _hmac.compare_digest(row["code_hash"], h):
            raise EnrollError("invalid enrollment code")
        if row["consumed_at"] is not None:
            log.warning(
                "enroll code for github=%s presented AGAIN (first used %s from %s); "
                "possible QR replay",
                row["github"], row["consumed_at"], row["consumed_from"],
            )
            raise EnrollError("invalid enrollment code")
        if _iso(now) >= row["expires_at"]:
            raise EnrollError("invalid enrollment code")
        cur = c.execute(
            "UPDATE enroll_codes SET consumed_at = ?, consumed_from = ? "
            "WHERE code_hash = ? AND consumed_at IS NULL",
            (_iso(now), client_ip, h),
        )
        if cur.rowcount != 1:
            raise EnrollError("invalid enrollment code")
        github = row["github"]
        label = row["label"]
        ttl = row["token_ttl_seconds"]

    token = auth.issue(github, expires_in_seconds=ttl, is_admin=False, label=label)
    log.info("enroll code exchanged for github=%s from %s", github, client_ip)
    expires_at = None
    if ttl:
        expires_at = _iso(now + timedelta(seconds=int(ttl)))
    return {
        "token": token,
        "github": github,
        "label": label,
        "expires_at": expires_at,
    }


def render_qr_terminal(payload: str) -> str:
    """Render a QR code as UTF-8 terminal art.

    Uses segno's ``terminal()`` in ``compact`` mode: half-block
    characters (``\\u2584 \\u2580 \\u2588``) with NO ANSI color escapes,
    so the result is safe to embed in Textual widgets (v0.15.0 fixed the
    reauth modal, where the default non-compact output's raw ``\\x1b[7m``
    sequences rendered as literal garbage and the ~106-col block
    overflowed the modal, clipping the buttons out of reach).
    Returns a multi-line string ready for print().
    """
    import io
    qr = segno.make(payload, error="m")
    buf = io.StringIO()
    qr.terminal(out=buf, border=2, compact=True)
    return buf.getvalue()


# ── HTTP: POST /api/enroll/exchange ─────────────────────────────────────────

from fastapi import APIRouter, Body, Depends, HTTPException, Request  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from . import ratelimit  # noqa: E402

router = APIRouter()


class _ExchangeBody(BaseModel):
    code: str


@router.post("/api/enroll/exchange", dependencies=[Depends(ratelimit.limit_login)])
def exchange_endpoint(request: Request, body: _ExchangeBody = Body(...)) -> dict:
    """Trade a scanned enrollment code for the bearer token, once.

    Unauthenticated (the code IS the credential), on the per-IP login
    bucket.  Returns the token plus ``ca_pem`` when an internal CA is
    configured, mirroring the legacy QR payload fields.
    """
    client_ip = request.client.host if request.client else None
    try:
        result = exchange_code(body.code, client_ip=client_ip)
    except EnrollError as exc:
        ratelimit.note_auth_failure(request)
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    ca_pem = _load_caddy_root_cert()
    if ca_pem:
        result["ca_pem"] = ca_pem
    return result
