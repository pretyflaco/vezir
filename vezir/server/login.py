"""Browser-friendly login: GUI hand-off + manual paste-token fallback.

Flow A — GUI hand-off (preferred):
    1. vezir gui composes URL `/login?token=<token>&next=/s/<id>`
    2. GUI calls webbrowser.open_new_tab on it
    3. Server validates token, sets HttpOnly cookie, 302 → next
    4. Cookie carries subsequent requests; URL bar shows clean path

Flow B — Manual login (fallback):
    1. User navigates to `/login` with no `?token=`
    2. Page renders a paste-the-token form
    3. POST /login form sets cookie + redirects

Open redirect protection: the `next` param must be a relative path
starting with `/` and not `//` and not contain `://`. Anything else is
ignored and the user is sent to `/`.
"""
from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from . import auth
from .templating import templates

log = logging.getLogger("vezir.login")
router = APIRouter()


# Cookie attributes shared by /login (set) and /logout (clear).
_COOKIE_KWARGS = {
    "key": auth.COOKIE_NAME,
    "httponly": True,
    "samesite": "lax",
    "secure": False,  # Tailscale-only HTTP for now; flip when TLS is added
    "path": "/",
}


def _safe_next(next_value: str | None) -> str:
    """Return a safe relative path or '/'."""
    if not next_value:
        return "/"
    # Strict: must be a single relative path under us.
    if not next_value.startswith("/"):
        return "/"
    if next_value.startswith("//"):
        return "/"
    if "://" in next_value:
        return "/"
    # Allow only printable ASCII, no control chars.
    if not re.fullmatch(r"[\x20-\x7e]+", next_value):
        return "/"
    # Sanity: no newline injection (already excluded by regex above).
    return next_value


def _redirect_with_session(token: str, next_path: str) -> RedirectResponse:
    """Issue 302 → next_path with the session cookie set."""
    resp = RedirectResponse(url=next_path, status_code=status.HTTP_303_SEE_OTHER)
    resp.set_cookie(value=token, **_COOKIE_KWARGS)
    return resp


@router.get("/login", response_class=HTMLResponse)
def login_get(request: Request, token: str | None = None, next: str | None = None):
    """Render login form, or consume `?token=...&next=...` from the GUI."""
    safe_next = _safe_next(next)

    if token:
        # GUI hand-off path: validate and redirect.
        github = auth.lookup(token)
        if github:
            log.info("login: %s via gui hand-off, next=%s", github, safe_next)
            return _redirect_with_session(token, safe_next)
        # Token in URL was invalid; fall through to render an error form.
        return templates.TemplateResponse(
            request,
            "login.html",
            {"request": request, "error": "Invalid token.", "next": safe_next},
            status_code=401,
        )

    # No token in URL → render the paste-token form.
    return templates.TemplateResponse(
        request,
        "login.html",
        {"request": request, "error": None, "next": safe_next},
    )


@router.post("/login", response_class=HTMLResponse)
def login_post(request: Request,
               token: str = Form(...),
               next: str = Form(default="/")):
    """Manual login form submit."""
    safe_next = _safe_next(next)
    token = token.strip()
    github = auth.lookup(token)
    if not github:
        log.info("login: invalid token via form, ip=%s",
                 request.client.host if request.client else "?")
        return templates.TemplateResponse(
            request,
            "login.html",
            {"request": request, "error": "Invalid token.", "next": safe_next},
            status_code=401,
        )
    log.info("login: %s via form, next=%s", github, safe_next)
    return _redirect_with_session(token, safe_next)


@router.get("/logout")
def logout():
    """Clear the session cookie and redirect to /login."""
    resp = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    resp.delete_cookie(key=auth.COOKIE_NAME, path="/")
    return resp
