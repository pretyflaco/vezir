"""Android (and any QR-friendly) enrollment endpoint.

GET /admin/enroll
    Auth: admin token (header) or session cookie tied to an admin
    bearer. Pre-0.1.12 tokens cannot reach this page until re-issued
    with ``vezir token issue --admin`` (see ``auth.require_admin``).

    Renders an HTML page with:
      - a paste-the-token form (when no `?token=` is supplied), OR
      - a QR code encoding a versioned JSON payload, plus a copyable
        text representation, when both `token` and `url` are supplied.

POST /admin/enroll
    Same render flow; lets the operator submit token+url via a form rather
    than putting the token in the URL bar (avoids browser-history leakage).

QR payload schema versions
--------------------------
v1 (pre-0.1.12 and Android < 0.1.4):
    {"v": 1, "url": "...", "token": "..."}

v2 (0.1.12+, when VEZIR_CADDY_ROOT_CERT_PATH is set):
    {"v": 2, "url": "...", "token": "...", "ca_pem": "-----BEGIN CERT..."}
    The PEM-encoded Caddy internal CA cert is included so the Android
    app can trust the server before the first real request. Hostname is
    encoded by ``url``. Older Android versions ignore ``ca_pem`` and use
    only ``url`` + ``token`` (graceful degradation).

Security posture
----------------
  - This page deliberately renders the plaintext token so it can be scanned
    or copied. The page warns the operator and recommends closing the tab
    after enrollment.
  - The page is not linked from the dashboard.
  - The QR payload is generated server-side as inline SVG (segno), no JS.
  - The CA root is *not* secret; embedding it in the QR is a UX choice,
    not a confidentiality risk. The risk to manage is *integrity*: an
    attacker who can rewrite the QR could swap the CA. The same attacker
    can swap the URL, so embedding the CA does not enlarge the trust
    surface — the operator must still scan from a trusted screen.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from urllib.parse import urlsplit

import segno
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from . import auth
from .templating import templates

log = logging.getLogger("vezir.enroll")

router = APIRouter()


# Latest QR payload schema version. v1 omits ca_pem; v2 includes it.
PAYLOAD_VERSION = 2

# Hard cap on the embedded CA PEM length. A normal RSA-2048 or Ed25519
# Caddy root is ~1-2 KiB. Anything much larger is operator error and we
# bail rather than producing a multi-kilobyte QR that won't scan.
_MAX_CA_PEM_BYTES = 8 * 1024


def _load_caddy_root_cert() -> str | None:
    """Return the PEM-encoded Caddy internal CA cert, if configured.

    Configured via ``VEZIR_CADDY_ROOT_CERT_PATH``. Returns None when the
    env var is unset, the file is missing, or its contents are clearly
    not a PEM certificate. Logs but does not raise on read errors so
    that a misconfigured cert path does not break the enrollment form
    entirely — it just downgrades the QR payload to v1.
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


def build_payload(server_url: str, token: str, ca_pem: str | None = None) -> str:
    """Return the canonical QR payload JSON string.

    Includes the CA PEM (and bumps ``v`` to 2) when ``ca_pem`` is given
    and non-empty. When omitted, returns the v1 shape unchanged so old
    Android builds keep parsing it.
    """
    if ca_pem:
        obj = {
            "v": PAYLOAD_VERSION,
            "url": server_url,
            "token": token,
            "ca_pem": ca_pem,
        }
    else:
        obj = {"v": 1, "url": server_url, "token": token}
    return json.dumps(obj, separators=(",", ":"), sort_keys=True)


def _is_safe_server_url(url: str) -> bool:
    """Reject obvious garbage. We don't enforce a scheme allow-list because
    operators may want http:// over an encrypted VPN (Tailscale, nostr-vpn).
    """
    if not url:
        return False
    if len(url) > 2048:
        return False
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return False
    if not parts.netloc:
        return False
    return True


def _render_qr_svg(payload: str) -> str:
    """Render a QR code as inline SVG. Caller embeds the SVG directly."""
    qr = segno.make(payload, error="m")
    # svg_inline() returns a self-contained <svg> string with no XML
    # declaration, safe to drop into the HTML body. It internally forces
    # svgns=False, so we don't pass it ourselves (would collide).
    return qr.svg_inline(scale=8, border=2)


def _default_server_url(request: Request) -> str:
    """Best-effort default for the server URL field — what this very page
    was reached at. Operator can override.
    """
    base = str(request.base_url).rstrip("/")
    return base


def _render(
    request: Request,
    me: str,
    *,
    token: str | None,
    server_url: str | None,
    error: str | None = None,
) -> HTMLResponse:
    """Common render path for both GET and POST."""
    qr_svg: str | None = None
    payload: str | None = None
    if token and server_url and _is_safe_server_url(server_url):
        github = auth.lookup(token)
        if not github:
            error = error or "Invalid token."
        else:
            ca_pem = _load_caddy_root_cert()
            payload = build_payload(server_url, token, ca_pem=ca_pem)
            qr_svg = _render_qr_svg(payload)

    return templates.TemplateResponse(
        request,
        "enroll.html",
        {
            "request": request,
            "me": me,
            "error": error,
            "default_url": server_url or _default_server_url(request),
            "token": token or "",
            "qr_svg": qr_svg,
            "payload": payload,
        },
    )


@router.get("/admin/enroll", response_class=HTMLResponse)
def enroll_get(
    request: Request,
    token: str | None = None,
    url: str | None = None,
    me: str = Depends(auth.require_admin),
):
    """Render the enrollment page.

    If `token` and `url` are present in the query string, the page also shows
    a QR code. We accept this convenience but the form-POST path is preferred
    because it avoids embedding the token in the URL.
    """
    return _render(request, me, token=token, server_url=url)


@router.post("/admin/enroll", response_class=HTMLResponse)
def enroll_post(
    request: Request,
    token: str = Form(...),
    url: str = Form(...),
    me: str = Depends(auth.require_admin),
):
    """Same render flow but token+url come from a form (no URL leakage)."""
    token = token.strip()
    url = url.strip()
    if not token or not url:
        return _render(
            request, me,
            token=token, server_url=url,
            error="Both token and server URL are required.",
        )
    if not _is_safe_server_url(url):
        return _render(
            request, me,
            token=token, server_url=url,
            error="Server URL must be a valid http:// or https:// URL.",
        )
    return _render(request, me, token=token, server_url=url)
