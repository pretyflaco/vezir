"""`vezir login` flow: NIP-46 remote-signed NIP-98 login → session JWT.

Orchestrates:
  1. Spin up a NIP-46 client, print the ``nostrconnect://`` URI + QR.
  2. Wait for the user's signer to connect; learn their user pubkey.
  3. Build the NIP-98 login event (kind 27235, ``u``/``method`` tags for
     ``POST <url>/api/auth/nostr/login``) and have the signer sign it.
  4. POST it with ``Authorization: Nostr <base64>``; receive the session
     JWT; persist it into teams.json via ``config.set_team_session``.

Kept out of ``cli.py`` so the heavy/optional nostr imports stay lazy and
the flow is unit-testable without click.
"""
from __future__ import annotations

import base64
import json
import logging
import time

log = logging.getLogger("vezir.login")


def build_login_event_template(login_url: str, clock_offset: int = 0) -> dict:
    """Return the unsigned NIP-98 event the signer will sign.

    ``pubkey``/``id``/``sig`` are filled in by the remote signer.

    ``clock_offset`` (seconds, from ``Nip46Client.clock_offset``) corrects
    ``created_at`` for a skewed local clock so the SERVER's NIP-98 freshness
    check passes — without it a skewed scribe machine 401s at login even
    though the signer handshake succeeded.
    """
    return {
        "kind": 27235,
        "created_at": int(time.time()) + int(clock_offset),
        "tags": [["u", login_url], ["method", "POST"]],
        "content": "",
    }


def auth_header_from_event(signed_event: dict) -> str:
    """Base64-wrap a signed event into an ``Authorization: Nostr`` value."""
    raw = json.dumps(signed_event).encode("utf-8")
    return "Nostr " + base64.b64encode(raw).decode("ascii")


def post_login(
    base_url: str,
    auth_header: str,
    *,
    verify=True,
    timeout: float = 30,
) -> dict:
    """POST the signed login event; return the parsed JSON body.

    Raises ``RuntimeError`` with the server detail on non-200.
    """
    import httpx

    url = f"{base_url.rstrip('/')}/api/auth/nostr/login"
    with httpx.Client(timeout=timeout, verify=verify) as c:
        r = c.post(url, headers={"Authorization": auth_header})
    if r.status_code != 200:
        detail = r.text[:300]
        try:
            detail = r.json().get("detail", detail)
        except Exception:
            pass
        raise RuntimeError(f"login failed (HTTP {r.status_code}): {detail}")
    return r.json()


def login_url_for(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/api/auth/nostr/login"
