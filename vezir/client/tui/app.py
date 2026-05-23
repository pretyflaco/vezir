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

    def on_mount(self) -> None:
        # Start the background labeling-needed poll.  Skipped under
        # test (VEZIR_TUI_DISABLE_NOTIFY_POLL=1) so unrelated tests
        # don't accumulate timers that fire after teardown.
        import os
        if os.environ.get("VEZIR_TUI_DISABLE_NOTIFY_POLL") == "1":
            return
        try:
            from .notify import install_labeling_poll
            install_labeling_poll(self)
        except Exception as exc:
            log.warning("labeling poll setup failed: %s", exc)

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
        # Emergency hard-exit.  priority=True so it fires even when a
        # focused widget would otherwise swallow ctrl+c (Input widgets
        # historically use it for "clear").  This is the user's escape
        # hatch when a screen wedges -- always works, restores terminal
        # state on the way out.  Trade-off: Input widgets lose their
        # default ctrl+c semantic; users can backspace / select-all
        # instead.  Acceptable for the reliability win.
        Binding("ctrl+c", "force_quit", "Force quit", priority=True, show=False),
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

    def action_force_quit(self) -> None:
        """Emergency hard-exit invoked by ctrl+c.

        Logs the event so post-mortem analysis can correlate a hung
        screen with the user's escape moment, then calls App.exit()
        which restores terminal state on its way out.
        """
        log.warning("force_quit invoked (ctrl+c)")
        self.exit()

    # ── exception handling ──

    def _handle_exception(self, error: Exception) -> None:
        """Catch unhandled exceptions so a single bug doesn't crash the TUI.

        Textual calls ``App._handle_exception`` from its message-pump and
        worker harness for uncaught errors.  Default behavior is to print
        a Rich traceback and exit; we override to log the error and show
        a transient notification so the user can keep working.  Errors
        in the *render* pipeline still crash (the screen's compositor
        path can't be recovered mid-flight), but everything else --
        worker thread exceptions, action callbacks, message handlers --
        stays survivable.

        Test-only escape hatch: set ``VEZIR_TUI_CRASH_ON_ERROR=1`` to
        restore the default fail-fast behavior; the test suite uses
        this to ensure regressions surface rather than getting hidden.
        """
        import os
        if os.environ.get("VEZIR_TUI_CRASH_ON_ERROR") == "1":
            super()._handle_exception(error)
            return
        log.exception("uncaught TUI exception: %s", error)
        try:
            self.notify(
                f"Internal error: {error}",
                severity="error",
                timeout=10,
            )
        except Exception:
            # Notification itself failed -- last-resort fallback to
            # the default traceback so the user at least sees something.
            super()._handle_exception(error)
