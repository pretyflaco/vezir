"""Browser-friendly login: GUI hand-off + manual paste-token fallback.

Flow A — GUI / upload-response hand-off (preferred, 0.1.12+):
    1. Server mints a single-use, 60-second exchange code (``vzx_...``)
       at upload time. The plaintext bearer never enters the URL.
    2. ``dashboard_login_url`` looks like /login?code=vzx_...&next=/s/<id>
    3. Browser GET → server consumes the code, validates the underlying
       bearer, opens an opaque session, sets the ``vezir_session`` cookie,
       and 303 → ``next``.

Flow B — Manual paste (fallback):
    1. User navigates to /login (no query params).
    2. Page renders a paste-the-token form.
    3. POST /login form validates the bearer, opens a session, sets the
       cookie, 303 → ``next``.

Backward compatibility (deprecation window):
    /login?token=<plaintext> still works for one release, but adds a
    ``Deprecation`` response header and a warning log line. The Android
    app pre-0.1.4 and any prior vezir GUI release fall through this path
    on first run after upgrade; their next upload round-trip lands them
    on the code-based path automatically (since the server now emits
    ``?code=`` URLs in upload responses regardless of client version).

Open-redirect protection:
    The ``next`` param must be a single relative path starting with ``/``,
    not ``//``, no ``://`` substring, printable ASCII only. Anything else
    silently falls back to ``/``. (Same shape as before.)
"""
from __future__ import annotations

import logging
import os
import re

from fastapi import APIRouter, Cookie, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from . import auth, ratelimit, web_sessions
from .templating import templates

log = logging.getLogger("vezir.login")
router = APIRouter()


def _cookie_secure() -> bool:
    """Set the ``Secure`` cookie flag iff VEZIR_COOKIE_SECURE is truthy.

    Recommended ``1`` once Caddy is in front. Left off by default for
    backward compatibility with plain-HTTP-over-VPN deployments.
    """
    return os.environ.get("VEZIR_COOKIE_SECURE", "").lower() in ("1", "true", "yes")


def _cookie_kwargs() -> dict:
    return {
        "key": auth.COOKIE_NAME,
        "httponly": True,
        "samesite": "lax",
        "secure": _cookie_secure(),
        "path": "/",
        # 30 days; server restart still invalidates sessions, but a long
        # cookie lifetime spares users a re-login on every browser restart.
        "max_age": 30 * 86400,
    }


def _safe_next(next_value: str | None) -> str:
    """Return a safe relative path or '/'."""
    if not next_value:
        return "/"
    if not next_value.startswith("/"):
        return "/"
    if next_value.startswith("//"):
        return "/"
    if "://" in next_value:
        return "/"
    if not re.fullmatch(r"[\x20-\x7e]+", next_value):
        return "/"
    return next_value


def _redirect_with_session(github: str, next_path: str) -> RedirectResponse:
    """Open a fresh in-memory session and 303 → next_path with the cookie."""
    sid = web_sessions.open_session(github)
    resp = RedirectResponse(url=next_path, status_code=status.HTTP_303_SEE_OTHER)
    resp.set_cookie(value=sid, **_cookie_kwargs())
    return resp


def _render_form(request: Request, *, error: str | None, next_path: str,
                 status_code: int = 200) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "login.html",
        {"request": request, "error": error, "next": next_path},
        status_code=status_code,
    )


@router.get(
    "/login",
    response_class=HTMLResponse,
    dependencies=[Depends(ratelimit.limit_login)],
)
def login_get(
    request: Request,
    code: str | None = None,
    token: str | None = None,
    next: str | None = None,
):
    """Consume an exchange code (``?code=``), a legacy bearer (``?token=``),
    or render the paste-token form when neither is present.
    """
    safe_next = _safe_next(next)

    # 0.1.12+ path: one-time exchange code.
    if code:
        bearer = web_sessions.consume_exchange_code(code)
        if not bearer:
            log.info("login: invalid or expired exchange code, next=%s", safe_next)
            return _render_form(
                request,
                error="Sign-in link expired. Open the dashboard from a fresh "
                      "upload response, or paste your token below.",
                next_path=safe_next,
                status_code=401,
            )
        github = auth.lookup(bearer)
        if not github:
            log.info("login: exchange code resolved to a revoked/invalid token")
            return _render_form(
                request,
                error="The token tied to that sign-in link is no longer "
                      "valid. Paste a current token below.",
                next_path=safe_next,
                status_code=401,
            )
        log.info("login: %s via exchange code, next=%s", github, safe_next)
        return _redirect_with_session(github, safe_next)

    # Deprecated path: bearer plaintext in URL. Kept for one release.
    if token:
        github = auth.lookup(token)
        if github:
            log.warning(
                "login: %s via deprecated ?token= URL (bearer leaked to URL/log); "
                "client should be upgraded to use the ?code= exchange flow",
                github,
            )
            resp = _redirect_with_session(github, safe_next)
            resp.headers["Deprecation"] = "true"
            resp.headers["Warning"] = (
                '299 - "?token= URLs are deprecated; clients should use '
                '?code= exchange codes from the upload response"'
            )
            return resp
        log.info("login: invalid token via deprecated ?token= URL")
        return _render_form(
            request, error="Invalid token.", next_path=safe_next, status_code=401,
        )

    # No query → render the paste-token form.
    return _render_form(request, error=None, next_path=safe_next)


@router.post(
    "/login",
    response_class=HTMLResponse,
    dependencies=[Depends(ratelimit.limit_login)],
)
def login_post(
    request: Request,
    token: str = Form(...),
    next: str = Form(default="/"),
):
    """Manual login form submit. The token comes from the POST body, not
    the URL, so it does not leak into access logs or browser history.
    """
    safe_next = _safe_next(next)
    token = token.strip()
    github = auth.lookup(token)
    if not github:
        log.info(
            "login: invalid token via form, ip=%s",
            request.client.host if request.client else "?",
        )
        return _render_form(
            request, error="Invalid token.",
            next_path=safe_next, status_code=401,
        )
    log.info("login: %s via form, next=%s", github, safe_next)
    return _redirect_with_session(github, safe_next)


@router.get("/logout")
def logout(
    vezir_session: str | None = Cookie(default=None, alias=auth.COOKIE_NAME),
):
    """Clear the session cookie and invalidate the underlying session id."""
    web_sessions.close_session(vezir_session)
    resp = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    resp.delete_cookie(key=auth.COOKIE_NAME, path="/")
    return resp
