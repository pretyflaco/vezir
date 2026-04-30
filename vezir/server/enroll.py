"""Android (and any QR-friendly) enrollment endpoint.

GET /admin/enroll
    Auth: bearer (header) or session cookie. Same posture as other browser
    routes — operator opens this page from an authenticated browser.

    Renders an HTML page with:
      - a paste-the-token form (when no `?token=` is supplied), OR
      - a QR code encoding a JSON payload `{"v":1,"url":"...","token":"..."}`
        plus a copyable text representation, when both `token` and `url` are
        supplied as query parameters.

POST /admin/enroll
    Same render flow; lets the operator submit token+url via a form rather
    than putting the token in the URL bar (avoids browser-history leakage).

Security posture:
  - This page deliberately renders the plaintext token so it can be scanned
    or copied. The page warns the operator and recommends closing the tab
    after enrollment. Long-term replacement is a one-time short-lived
    enrollment code; tracked in vezir_plan.md.
  - The page is not linked from the dashboard.
  - The QR payload is generated server-side as inline SVG (segno), no JS.
"""
from __future__ import annotations

import json
import logging
from urllib.parse import urlsplit

import segno
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from . import auth
from .templating import templates

log = logging.getLogger("vezir.enroll")

router = APIRouter()


# Versioned QR payload schema. Bump `v` if the Android app needs to
# distinguish a future shape.
PAYLOAD_VERSION = 1


def build_payload(server_url: str, token: str) -> str:
    """Return the canonical QR payload JSON string."""
    return json.dumps(
        {"v": PAYLOAD_VERSION, "url": server_url, "token": token},
        separators=(",", ":"),
        sort_keys=True,
    )


def _is_safe_server_url(url: str) -> bool:
    """Reject obvious garbage. We don't enforce a scheme allow-list because
    operators may want http:// over Tailscale.
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
            payload = build_payload(server_url, token)
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
    me: str = Depends(auth.require_bearer_or_cookie),
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
    me: str = Depends(auth.require_bearer_or_cookie),
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
