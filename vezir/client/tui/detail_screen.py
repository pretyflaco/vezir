"""Session detail screen: metadata, artifacts, actions.

Mirrors vezir-android's SessionDetailScreen.kt (v0.2.4 with the
retry-summary preset picker).

Layout (top to bottom):
  - metadata block (id, when, who, status, preset)
  - error notices (summary_error / sync_error in red)
  - artifact list (selectable)
  - action bar:
      [r] retry summary (with preset picker dialog)
      [y] sync now
      [p] share with team (un-personal)
      [l] open labeling
      [escape] back
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button, DataTable, Footer, Header, Label, Select, Static,
)

from ..api import Session

log = logging.getLogger("vezir.client.tui.detail")


_PRESETS = [
    ("High Quality (Sonnet 4.6)", "high-quality"),
    ("Confidential (TEE)", "confidential"),
    ("Alternative (Kimi)", "alternative"),
]


@dataclass
class DetailLoaded(Message):
    session: Session


@dataclass
class DetailFailed(Message):
    error: str


@dataclass
class ActionDone(Message):
    label: str
    ok: bool
    detail: str = ""


class PresetPickerScreen(ModalScreen[str | None]):
    """Modal: pick a summary preset for retry-summary, or cancel."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    CSS = """
    PresetPickerScreen { align: center middle; }
    #preset-box {
        width: 60;
        max-width: 90%;
        height: auto;
        border: solid $accent;
        padding: 1 2;
        background: $surface;
    }
    """

    def __init__(self, current: str | None) -> None:
        super().__init__()
        self._current = current or "high-quality"

    def compose(self) -> ComposeResult:
        with Vertical(id="preset-box"):
            yield Label("[b]Retry summary with which preset?[/b]")
            yield Select(
                options=_PRESETS,
                value=self._current,
                allow_blank=False,
                id="preset-select",
            )
            with Horizontal():
                yield Button("Cancel", id="cancel-btn")
                yield Button("Retry", id="confirm-btn", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel-btn":
            self.dismiss(None)
        elif event.button.id == "confirm-btn":
            value = self.query_one("#preset-select", Select).value
            self.dismiss(str(value) if value is not None else None)


class DetailScreen(Screen):
    """One session's metadata + artifacts + actions."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("ctrl+r", "refresh", "Refresh"),
        Binding("e", "retry_summary", "Retry summary"),
        Binding("y", "sync_now", "Sync now"),
        Binding("p", "share_with_team", "Share"),
        Binding("l", "open_labeling", "Label"),
        Binding("c", "copy_session_id", "Copy id"),
        # PR10: open this session in the web dashboard.  Useful while
        # the dashboard is still around (deprecates in v0.4 / removes
        # in v0.5).  Lazily constructed as ``{server_url}/s/{id}``
        # since the Session dataclass doesn't carry dashboard_url
        # (added per-upload via /api/upload response only).
        Binding("o", "open_in_browser", "Open in browser"),
        # NOTE: do NOT bind "enter" here.  DataTable has its own
        # built-in `enter -> select_cursor` binding which fires
        # RowSelected (handled by `on_data_table_row_selected` below).
        # Binding `enter` at the screen level would cause a
        # double-dispatch: ArtifactScreen gets pushed twice, leading
        # to a stuck UI state.  Smoked on muscle 2026-05-23.
    ]

    CSS = """
    DetailScreen { padding: 1 2; }
    #meta { height: auto; margin-bottom: 1; }
    #err-block { height: auto; margin-bottom: 1; }
    #artifacts-label { color: $text-muted; margin-bottom: 0; }
    DataTable { height: 1fr; }
    #actions {
        height: 3;
        margin-top: 1;
        background: $surface;
    }
    Button { margin-right: 1; }
    """

    def __init__(self, session_id: str) -> None:
        super().__init__()
        self.session_id = session_id
        self.session: Session | None = None
        self._table: DataTable | None = None
        # artifact_key -> filename mapping for the open-action dispatch
        self._artifact_index: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("loading…", id="meta")
        yield Static(" ", id="err-block", classes="error")
        yield Label("Artifacts:", id="artifacts-label")
        table = DataTable(
            id="artifacts-table",
            zebra_stripes=True,
            cursor_type="row",
        )
        table.add_columns("name", "filename")
        self._table = table
        yield table
        with Horizontal(id="actions"):
            yield Button("[e] Retry summary", id="retry-btn", variant="warning")
            yield Button("[y] Sync now", id="sync-btn")
            yield Button("[p] Share with team", id="share-btn")
            yield Button("[l] Label speakers", id="label-btn", variant="primary")
        yield Footer()

    def on_mount(self) -> None:
        self.action_refresh()

    def action_refresh(self) -> None:
        self.app.sub_title = f"session {self.session_id} (loading)"
        self._fetch_worker()

    def action_open_selected_artifact(self) -> None:
        if self._table is None:
            return
        try:
            row_key = self._table.coordinate_to_cell_key(
                self._table.cursor_coordinate,
            ).row_key
        except Exception:
            return
        name = self._artifact_index.get(str(row_key.value))
        if name is None:
            return
        self._open_artifact(name)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        name = self._artifact_index.get(str(event.row_key.value))
        if name is None:
            return
        self._open_artifact(name)

    def _open_artifact(self, name: str) -> None:
        from .artifact_screen import ArtifactScreen
        self.app.push_screen(ArtifactScreen(self.session_id, name))

    def action_open_labeling(self) -> None:
        from .label_screen import LabelScreen
        self.app.push_screen(LabelScreen(self.session_id))

    def action_retry_summary(self) -> None:
        current_preset = (self.session.summary_preset if self.session else None)
        self.app.push_screen(
            PresetPickerScreen(current_preset),
            self._on_preset_picked,
        )

    def _on_preset_picked(self, preset: str | None) -> None:
        if preset is None:
            return
        self._action_worker("retry summary", "retry_summary", preset=preset)

    def action_sync_now(self) -> None:
        self._action_worker("sync", "sync_now")

    def action_share_with_team(self) -> None:
        if self.session is None or not self.session.is_personal:
            self.app.bell()
            self.notify(
                "Session is not personal; nothing to share.",
                severity="warning",
            )
            return
        self._action_worker("share with team", "share_with_team")

    def action_copy_session_id(self) -> None:
        """Copy the session id to the system clipboard (OSC 52)."""
        try:
            self.app.copy_to_clipboard(self.session_id)
        except Exception as exc:
            self.notify(f"Copy failed: {exc}", severity="error")
            return
        self.notify(
            f"Copied session id: {self.session_id}",
            severity="information",
            timeout=4,
        )

    def action_open_in_browser(self) -> None:
        """Open the session's dashboard URL in the user's default browser.

        PR10 (2026-05-24): the thin-client TUI is most useful when the
        user can pivot to the full web dashboard for tasks the TUI
        doesn't surface (raw log inspection, admin actions, etc.).
        Constructed lazily from ``app.server_url`` because the Session
        dataclass doesn't carry dashboard_url (server only emits it
        as part of /api/upload responses, not /api/sessions/{id}).
        """
        import webbrowser
        base = (self.app.server_url or "").rstrip("/")
        if not base:
            self.notify("No server URL configured.", severity="error")
            return
        url = f"{base}/s/{self.session_id}"
        try:
            opened = webbrowser.open(url, new=2)
        except Exception as exc:
            self.notify(f"Could not open browser: {exc}", severity="error")
            return
        if opened:
            self.notify(
                f"Opening {url}", severity="information", timeout=4,
            )
        else:
            # webbrowser.open returns False when no usable browser was
            # found (e.g. headless server).  Surface the URL so the
            # user can copy/paste manually.
            self.notify(
                f"No browser available.  URL: {url}",
                severity="warning",
                timeout=8,
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "retry-btn":
            self.action_retry_summary()
        elif bid == "sync-btn":
            self.action_sync_now()
        elif bid == "share-btn":
            self.action_share_with_team()
        elif bid == "label-btn":
            self.action_open_labeling()

    # ── workers ──

    @work(thread=True, exclusive=True, group="detail-fetch")
    def _fetch_worker(self) -> None:
        result = self.app.api.get_session(self.session_id)
        if not result.is_ok():
            self.post_message(DetailFailed(error=result.error_message()))
            return
        self.post_message(DetailLoaded(session=result.ok))

    @work(thread=True, exclusive=False, group="detail-action")
    def _action_worker(
        self,
        label: str,
        api_method: str,
        **kwargs,
    ) -> None:
        method = getattr(self.app.api, api_method)
        result = method(self.session_id, **kwargs)
        if result.is_ok():
            self.post_message(ActionDone(label=label, ok=True))
        else:
            self.post_message(ActionDone(
                label=label, ok=False, detail=result.error_message(),
            ))

    # ── message handlers ──

    def on_detail_loaded(self, message: DetailLoaded) -> None:
        self.session = message.session
        self.app.sub_title = f"session {self.session_id}"
        self._refresh_view()

    def on_detail_failed(self, message: DetailFailed) -> None:
        self.app.sub_title = "load failed"
        self.query_one("#meta", Static).update(
            f"[red]Failed to load session: {message.error}[/red]",
        )

    def on_action_done(self, message: ActionDone) -> None:
        if message.ok:
            self.notify(f"{message.label}: ok", severity="information")
            # Refresh to surface any state change (e.g. retry-summary
            # flips status to summarizing).
            self.action_refresh()
        else:
            self.notify(
                f"{message.label} failed: {message.detail}",
                severity="error",
            )

    def _refresh_view(self) -> None:
        # NOTE: do NOT name this ``_render`` -- that shadows
        # ``textual.widget.Widget._render()`` which the render pipeline
        # calls to produce a Visual for ``to_strips()``.  Returning None
        # from the override breaks the entire widget rendering and the
        # app crashes with ``AttributeError: 'NoneType' object has no
        # attribute 'render_strips'`` the moment the screen is shown
        # on a real terminal.  Lesson learned the painful way in PR2.
        s = self.session
        if s is None:
            return
        # Build the meta block as plain text first; markup on individual
        # spans is brittle when Static.update gets a multi-line content.
        # Use rich.Text via markup conversion only on the lines that
        # actually need styling.
        title = s.title or s.id
        meta_lines = [
            f"{title}",
            f"  id: {s.id}",
            f"  who: {s.github or 'unknown'}",
            f"  status: {s.status}",
            f"  preset: {s.summary_preset or '-'}",
            f"  updated: {s.updated_at or '-'}",
        ]
        if s.is_personal:
            meta_lines.append("  personal (private to you)")
        self.query_one("#meta", Static).update("\n".join(meta_lines))

        err_lines = []
        if s.error:
            err_lines.append(f"error: {s.error}")
        if s.summary_error:
            err_lines.append(f"summary_error: {s.summary_error}")
        if s.sync_error:
            err_lines.append(f"sync_error: {s.sync_error}")
        # Use a single space when no errors -- empty string can render
        # as None in the visual layer on some textual versions.
        self.query_one("#err-block", Static).update("\n".join(err_lines) or " ")

        # Artifacts table
        assert self._table is not None
        self._table.clear()
        self._artifact_index.clear()
        for key, fname in sorted(s.artifacts.items()):
            row_key = self._table.add_row(key, fname, key=key)
            self._artifact_index[str(row_key.value)] = fname

        # Action availability
        self.query_one("#share-btn", Button).disabled = not s.is_personal
        self.query_one("#label-btn", Button).disabled = s.status not in (
            "needs_labeling", "done", "error",
        )
