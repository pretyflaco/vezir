"""Top-level Textual app for the vezir desktop thin client.

Architecture:

* ``VezirTuiApp`` owns one ``VezirClient`` instance (constructed from
  ``VEZIR_URL`` / ``VEZIR_TOKEN`` or the persisted client.json) and
  passes it via ``self.app.api`` so every screen reads the same auth
  state (token rotation in a future settings screen is a one-line
  change).
* A single root ``MainScreen`` wraps the two top-level views
  (RecordScreen, SessionsScreen) inside a ``TabbedContent``.  This is
  the Textual-idiomatic shape for "bottom-nav" UIs and dodges the
  switch_screen / install_screen state-tracking edge cases.
* Transient screens (DetailScreen, ArtifactScreen, LabelScreen,
  HelpScreen) are pushed on top of MainScreen and pop themselves via
  ``escape`` -- standard Textual screen stack semantics.
* Heavyweight imports (meetscribe-record, textual widgets that pull
  rich extras) are lazy inside the screen modules so a `vezir tui`
  startup on a box with the bare-minimum install still gives a
  legible error message before falling over.

Global bindings (priority on the App):
  ctrl+r  Record tab
  ctrl+s  Sessions tab
  ctrl+l  Refresh
  ctrl+q  Quit
  f1 / ?  Help
"""
from __future__ import annotations

import logging
import os

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, Header, TabbedContent, TabPane

from ..api import VezirClient
from ..config import load_client_prefs

log = logging.getLogger("vezir.client.tui")


def _resolve_credentials() -> tuple[str, str | None]:
    """Resolve server URL + token: env > client.json > defaults."""
    url = os.environ.get("VEZIR_URL")
    token = os.environ.get("VEZIR_TOKEN")
    if not url or not token:
        cfg = load_client_prefs()
        url = url or cfg.get("url")
        token = token or cfg.get("token")
    if not url:
        url = "http://localhost:8000"
    return url, token


# ─── MainScreen: tabbed root holding the two top-level views ────────────────


class MainScreen(Screen):
    """Single root screen with Record / Sessions tabs."""

    BINDINGS = [
        Binding("ctrl+r", "show_tab('record')", "Record"),
        Binding("ctrl+s", "show_tab('sessions')", "Sessions"),
        Binding("ctrl+l", "refresh_current", "Refresh", show=False),
    ]

    CSS = """
    MainScreen TabbedContent { height: 1fr; }
    """

    def compose(self) -> ComposeResult:
        # Lazy imports so `vezir --help` stays snappy on minimal installs.
        from .record_screen import RecordBody
        from .sessions_screen import SessionsBody

        yield Header(show_clock=True)
        with TabbedContent(id="main-tabs"):
            with TabPane("Record", id="record"):
                yield RecordBody.body_widget()
            with TabPane("Sessions", id="sessions"):
                yield SessionsBody.body_widget()
        yield Footer()

    def action_show_tab(self, tab_id: str) -> None:
        tabs = self.query_one(TabbedContent)
        tabs.active = tab_id

    def action_refresh_current(self) -> None:
        """Forward refresh to whichever tab's body widget exposes it."""
        from .record_screen import RecordBody
        from .sessions_screen import SessionsBody

        tabs = self.query_one(TabbedContent)
        active = tabs.active_pane
        if active is None:
            return
        try:
            body = active.query_one((RecordBody, SessionsBody))
        except Exception:
            return
        action = getattr(body, "action_refresh", None)
        if callable(action):
            action()


# ─── App ─────────────────────────────────────────────────────────────────────


class VezirTuiApp(App):
    """Top-level Textual app."""

    CSS = """
    Screen { layout: vertical; }
    .error { color: $error; }
    .ok { color: $success; }
    .muted { color: $text-muted; }
    .key { color: $accent; text-style: bold; }
    """

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit"),
        Binding("f1", "help", "Help"),
        Binding("question_mark", "help", show=False),
    ]

    TITLE = "vezir"
    SUB_TITLE = "thin client"

    def __init__(self) -> None:
        super().__init__()
        self.server_url, self.token = _resolve_credentials()
        if not self.token:
            log.warning("VEZIR_TOKEN is not set; TUI will run in degraded mode")
        self.api = VezirClient(
            self.server_url,
            self.token or "vzr_unset",  # placeholder; server will 401
        )

    def on_mount(self) -> None:
        self.push_screen(MainScreen())

    # ── global actions ──

    def action_help(self) -> None:
        from .help_screen import HelpScreen
        self.push_screen(HelpScreen())
