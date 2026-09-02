"""`vezir login --method google`: device-grant flow → session JWT.

The vezir server proxies Google's OAuth device + token endpoints (it holds
the client_secret), so the client only talks to vezir:

  1. GET  /api/auth/google/config         → is Google sign-in available?
  2. POST /api/auth/google/device/start   → user_code + verification_url + device_code
  3. show the user the code + URL; they approve in a browser (any device)
  4. POST /api/auth/google/device/poll    → poll until 200 (JWT) or terminal error
     * 202 ``authorization_pending`` → keep polling at ``interval``

Returns the parsed login body (``session_jwt``, ``github``, ``email``,
``memberships``, …), matching the nostr login shape so the CLI stores it
the same way.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable


class GoogleLoginError(Exception):
    """Raised on a terminal failure of the Google device-grant flow."""


def _base(url: str) -> str:
    return url.rstrip("/")


def fetch_config(base_url: str, *, verify=True, timeout: float = 15) -> dict:
    """Return the server's Google sign-in config (``configured`` bool, …)."""
    import httpx

    with httpx.Client(timeout=timeout, verify=verify) as c:
        r = c.get(f"{_base(base_url)}/api/auth/google/config")
    if r.status_code != 200:
        raise GoogleLoginError(
            f"could not read Google sign-in config (HTTP {r.status_code})"
        )
    return r.json()


def login(
    base_url: str,
    *,
    verify=True,
    timeout: float = 300,
    on_prompt: Callable[[str, str], None] | None = None,
    stop: threading.Event | None = None,
) -> dict:
    """Run the full device-grant login; return the login response body.

    ``on_prompt(user_code, verification_url)`` is invoked once the device
    code is obtained so the caller can show the user where to go.  Polls
    until the user approves (or ``timeout`` seconds elapse).

    ``stop`` (0.17.1): an optional ``threading.Event`` a caller on another
    thread can set to abort the poll loop promptly (e.g. the TUI reauth
    modal on Esc), instead of leaving a background thread polling for the
    full ``timeout``.
    """
    import httpx

    cfg = fetch_config(base_url, verify=verify)
    if not cfg.get("configured"):
        raise GoogleLoginError(
            "Google sign-in is not configured on this server; use "
            "`vezir login` (nostr) instead."
        )

    with httpx.Client(timeout=30, verify=verify) as c:
        # Start the device grant.
        r = c.post(f"{_base(base_url)}/api/auth/google/device/start")
        if r.status_code != 200:
            detail = _detail(r)
            raise GoogleLoginError(f"could not start Google sign-in: {detail}")
        start = r.json()
        device_code = start["device_code"]
        user_code = start["user_code"]
        verification_url = start.get("verification_url") or "https://www.google.com/device"
        interval = max(int(start.get("interval", 5)), 1)

        if on_prompt:
            on_prompt(user_code, verification_url)

        # Poll for completion.
        deadline = time.time() + timeout
        while time.time() < deadline:
            if stop is not None and stop.wait(interval):
                raise GoogleLoginError("cancelled")
            if stop is None:
                time.sleep(interval)
            pr = c.post(
                f"{_base(base_url)}/api/auth/google/device/poll",
                json={"device_code": device_code},
            )
            if pr.status_code == 200:
                return pr.json()
            if pr.status_code == 202:
                # authorization_pending / slow_down — keep waiting.  The
                # device-grant spec says to increase the interval by 5s on
                # slow_down (L-8).
                body = pr.json() if pr.content else {}
                if body.get("error") == "slow_down":
                    interval += 5
                continue
            # Terminal error (401/403/5xx).
            raise GoogleLoginError(_detail(pr))

    raise GoogleLoginError(
        "timed out waiting for you to approve the Google sign-in."
    )


def _detail(resp) -> str:
    """Extract a human-readable error detail from an httpx response."""
    try:
        return resp.json().get("detail") or resp.text[:200]
    except Exception:
        return resp.text[:200] if resp.text else f"HTTP {resp.status_code}"
