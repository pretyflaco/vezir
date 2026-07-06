"""In-TUI re-authentication modal (0.8.9).

When an upload (or any request) is rejected with HTTP 401 because the ~24h
session JWT expired, the user previously had to quit the TUI, run
``vezir login`` in a shell, and re-run ``vezir upload <path>``.  This modal lets
them sign in again **without leaving the TUI**; on success the caller re-binds
the in-memory token and retries the upload.

Reuses the same click-free login building blocks the CLI uses:
  * nostr (NIP-46): ``nostr.nip46.Nip46Client`` + ``nostr.login`` helpers.
  * Google device grant: ``google_login.login(on_prompt=...)``.

The login runs in a Textual thread worker (blocking network I/O) and posts
progress/result messages back to the screen.  On success the screen dismisses
with the parsed login body (containing ``session_jwt``); on failure/cancel it
dismisses with ``None``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Static

log = logging.getLogger("vezir.client.tui.reauth")


@dataclass
class ReauthProgress(Message):
    """A human-readable progress line from the login worker."""

    text: str


@dataclass
class ReauthDone(Message):
    """Login worker finished.  ``body`` is the login response (with
    ``session_jwt``) on success, or ``None`` with ``error`` set on failure."""

    body: dict | None
    error: str = ""


class ReauthScreen(ModalScreen["dict | None"]):
    """Modal: re-sign-in via nostr (default) or Google, in-TUI.

    Dismisses with the login response body (``{"session_jwt", "npub",
    "github", ...}``) on success, or ``None`` on cancel/failure.
    """

    DEFAULT_CSS = """
    ReauthScreen {
        align: center middle;
    }
    #reauth-box {
        width: 80%;
        max-width: 90;
        height: auto;
        max-height: 90%;
        border: round $primary;
        padding: 1 2;
        background: $surface;
    }
    #reauth-title {
        height: 1;
        margin-bottom: 1;
        text-style: bold;
    }
    #reauth-body {
        height: auto;
        margin-bottom: 1;
    }
    #reauth-buttons {
        height: auto;
    }
    #reauth-buttons Button {
        margin-right: 2;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("n", "login_nostr", "Nostr"),
        Binding("g", "login_google", "Google"),
    ]

    def __init__(self, server_url: str, team_id: str | None) -> None:
        super().__init__()
        self._server_url = server_url
        self._team_id = team_id or ""
        self._busy = False

    def compose(self) -> ComposeResult:
        with Vertical(id="reauth-box"):
            yield Static(
                "Session expired — sign in again", id="reauth-title",
            )
            yield Static(
                "Your session couldn't be refreshed and needs a fresh "
                "sign-in (HTTP 401).\n\n"
                "  n  Sign in with nostr (NIP-46 / Amber / nsec.app)\n"
                "  g  Sign in with Google\n"
                "  Esc  Cancel\n",
                id="reauth-body",
            )
            with Horizontal(id="reauth-buttons"):
                yield Button("Nostr (n)", id="reauth-nostr", variant="primary")
                yield Button("Google (g)", id="reauth-google")
                yield Button("Cancel (Esc)", id="reauth-cancel")

    # ── actions / buttons ──
    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "reauth-nostr":
            self.action_login_nostr()
        elif event.button.id == "reauth-google":
            self.action_login_google()
        elif event.button.id == "reauth-cancel":
            self.action_cancel()

    def action_cancel(self) -> None:
        if self._busy:
            return  # don't cancel mid-login
        self.dismiss(None)

    def action_login_nostr(self) -> None:
        if self._busy:
            return
        self._busy = True
        self._set_body("Starting nostr sign-in…")
        self._nostr_worker()

    def action_login_google(self) -> None:
        if self._busy:
            return
        self._busy = True
        self._set_body("Starting Google sign-in…")
        self._google_worker()

    # ── helpers ──
    def _set_body(self, text: str) -> None:
        try:
            self.query_one("#reauth-body", Static).update(text)
        except Exception:
            pass

    def _verify(self):
        try:
            from ..trust import resolve_verify
            return resolve_verify()
        except Exception:
            return True

    # ── workers ──
    @work(thread=True, exclusive=True, group="reauth")
    def _nostr_worker(self) -> None:
        try:
            from ..nostr import login as nostr_login
            from ..nostr import nip46
        except Exception as exc:  # pragma: no cover - import guard
            self.post_message(ReauthDone(
                None, error=f"nostr support not installed ({exc})"
            ))
            return

        client = None
        try:
            def _on_auth_url(u: str) -> None:
                self.post_message(ReauthProgress(text=f"Approve in signer: {u}"))

            client = nip46.Nip46Client(name="vezir", on_auth_url=_on_auth_url)
            connect_uri = client.build_connect_uri()
            # Render a scannable QR for phone signers (Amber); on the same
            # machine the nostrconnect request is already live, so a signer
            # like nsec.app just needs approval — no copy/paste required.
            qr = ""
            try:
                from ...server.enroll import render_qr_terminal
                qr = render_qr_terminal(connect_uri) + "\n\n"
            except Exception:
                qr = ""
            self.post_message(ReauthProgress(
                text=(
                    "Sign in again with nostr.\n\n"
                    "• Phone signer (Amber): scan the QR below.\n"
                    "• Same-device signer (nsec.app): just approve the "
                    "request — it's already sent.\n\n"
                    f"{qr}"
                    "Waiting for approval…  (Esc to cancel)"
                )
            ))
            client.wait_for_connection(timeout=180)
            template = nostr_login.build_login_event_template(
                nostr_login.login_url_for(self._server_url)
            )
            signed = client.sign_event(template, timeout=180)
            body = nostr_login.post_login(
                self._server_url,
                nostr_login.auth_header_from_event(signed),
                verify=self._verify(),
            )
            body.setdefault("npub", client.user_pubkey or "")
            self.post_message(ReauthDone(body))
        except Exception as exc:
            self.post_message(ReauthDone(None, error=str(exc)))
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

    @work(thread=True, exclusive=True, group="reauth")
    def _google_worker(self) -> None:
        try:
            from .. import google_login
        except Exception as exc:  # pragma: no cover - import guard
            self.post_message(ReauthDone(
                None, error=f"Google support unavailable ({exc})"
            ))
            return
        try:
            def _on_prompt(user_code: str, url: str) -> None:
                self.post_message(ReauthProgress(
                    text=(
                        f"Open {url}\nand enter code:  {user_code}\n\n"
                        "Waiting for approval…"
                    )
                ))

            body = google_login.login(
                self._server_url,
                verify=self._verify(),
                on_prompt=_on_prompt,
            )
            self.post_message(ReauthDone(body))
        except Exception as exc:
            self.post_message(ReauthDone(None, error=str(exc)))

    # ── message handlers ──
    def on_reauth_progress(self, message: ReauthProgress) -> None:
        self._set_body(message.text)

    def on_reauth_done(self, message: ReauthDone) -> None:
        self._busy = False
        if message.body is not None and (
            message.body.get("session_jwt") or message.body.get("access_jwt")
        ):
            self.dismiss(message.body)
        else:
            self._set_body(
                f"Sign-in failed: {message.error or 'no session returned'}\n\n"
                "  n  Try nostr again   g  Try Google   Esc  Cancel"
            )
