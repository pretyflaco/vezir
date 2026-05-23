"""Sessions list screen.

Mirrors vezir-android's SessionListScreen.kt: shows team + own personal
sessions in a DataTable, refreshes on demand (ctrl+r) or when this
screen is re-entered.  Row selection opens the detail screen.

Status badges are color-coded; personal sessions get a small marker.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import DataTable, Static

from ..api import ApiResult, Session

log = logging.getLogger("vezir.client.tui.sessions")


# DataTable cells go through rich.markup directly (not Textual's
# $color variable substitution), so we use literal style names.
_STATUS_TAGS = {
    "queued": "[dim]queued[/dim]",
    "transcribing": "[cyan]transcribing[/cyan]",
    "summarizing": "[green]summarizing[/green]",
    "syncing": "[cyan]syncing[/cyan]",
    "needs_labeling": "[yellow][b]needs labeling[/b][/yellow]",
    "done": "[green]done[/green]",
    "error": "[red][b]error[/b][/red]",
}


def _status_cell(s: Session) -> str:
    base = _STATUS_TAGS.get(s.status, s.status)
    parts = [base]
    if s.summary_error:
        parts.append("[red]· summary err[/red]")
    if s.sync_error:
        parts.append("[red]· sync err[/red]")
    if s.is_personal:
        parts.append("[yellow]· personal[/yellow]")
    return " ".join(parts)


def _short_time(s: str | None) -> str:
    if not s:
        return ""
    # Server emits ISO-8601 "YYYY-MM-DDTHH:MM:SS[+...]"; keep date+HHMM.
    if "T" in s:
        d, t = s.split("T", 1)
        hh = t.split(":", 2)[:2]
        return f"{d} {':'.join(hh)}"
    return s


@dataclass
class SessionsRefreshed(Message):
    sessions: list[Session]


@dataclass
class SessionsRefreshFailed(Message):
    error: str


class SessionsBody(Vertical):
    """Browse team + own personal sessions (used inside MainScreen)."""

    BINDINGS = [
        # NOTE: do NOT bind "enter" here.  DataTable has its own
        # built-in `enter -> select_cursor` binding which fires
        # RowSelected (handled by `on_data_table_row_selected` below).
        # Binding `enter` at the body level would cause a
        # double-dispatch: DetailScreen gets pushed twice, leading
        # to a stuck UI state.  Smoked on muscle 2026-05-23.
        Binding("c", "copy_selected_id", "Copy id"),
    ]

    DEFAULT_CSS = """
    SessionsBody {
        padding: 1 2;
    }
    SessionsBody DataTable { height: 1fr; }
    SessionsBody #empty-state {
        color: $text-muted;
        height: 1fr;
        content-align: center middle;
    }
    """

    sessions: reactive[list[Session]] = reactive(list)

    def __init__(self) -> None:
        super().__init__()
        self._table: DataTable | None = None
        # Map row_key -> Session for O(1) lookup on selection.
        self._row_index: dict[str, Session] = {}

    @classmethod
    def body_widget(cls) -> "SessionsBody":
        return cls()

    def compose(self) -> ComposeResult:
        table = DataTable(id="sessions-table", zebra_stripes=True, cursor_type="row")
        table.add_columns("when", "title / id", "who", "status")
        self._table = table
        yield table
        yield Static("", id="empty-state")

    def on_mount(self) -> None:
        self.action_refresh()

    def action_refresh(self) -> None:
        self.app.sub_title = "loading sessions…"
        self._refresh_worker()

    def action_open_selected(self) -> None:
        if self._table is None:
            return
        try:
            row_key = self._table.coordinate_to_cell_key(
                self._table.cursor_coordinate,
            ).row_key
        except Exception:
            return
        session = self._row_index.get(str(row_key.value)) if row_key.value else None
        if session is None:
            return
        from .detail_screen import DetailScreen
        self.app.push_screen(DetailScreen(session_id=session.id))

    def action_copy_selected_id(self) -> None:
        """Copy the cursor-row's session id to the clipboard.

        Symmetric with DetailScreen's ``c`` binding so the user can
        get an id from either screen without breaking flow.
        """
        if self._table is None:
            return
        try:
            row_key = self._table.coordinate_to_cell_key(
                self._table.cursor_coordinate,
            ).row_key
        except Exception:
            return
        session = self._row_index.get(str(row_key.value)) if row_key.value else None
        if session is None:
            self.app.bell()
            return
        try:
            self.app.copy_to_clipboard(session.id)
        except Exception as exc:
            self.notify(f"Copy failed: {exc}", severity="error")
            return
        self.notify(
            f"Copied session id: {session.id}",
            severity="information",
            timeout=4,
        )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        session = self._row_index.get(str(event.row_key.value))
        if session is None:
            return
        from .detail_screen import DetailScreen
        self.app.push_screen(DetailScreen(session_id=session.id))

    @work(thread=True, exclusive=True, group="sessions-refresh")
    def _refresh_worker(self) -> None:
        result = self.app.api.get_sessions(50)
        if not result.is_ok():
            self.post_message(SessionsRefreshFailed(error=result.error_message()))
            return
        self.post_message(SessionsRefreshed(sessions=result.ok))

    def on_sessions_refreshed(self, message: SessionsRefreshed) -> None:
        self.sessions = message.sessions
        self._render_table()
        self.app.sub_title = f"sessions ({len(message.sessions)})"

    def on_sessions_refresh_failed(self, message: SessionsRefreshFailed) -> None:
        self.app.sub_title = "refresh failed"
        self.query_one("#empty-state", Static).update(
            f"[red]Failed to fetch sessions: {message.error}[/red]\n"
            f"Press [b]ctrl+l[/b] to retry.",
        )

    def _render_table(self) -> None:
        assert self._table is not None
        self._table.clear()
        self._row_index.clear()
        if not self.sessions:
            # PR9: the hint used to say "ctrl+r to refresh" but that
            # binding actually switches to the Record tab.  The real
            # refresh shortcut is ctrl+l (refresh_current at MainScreen)
            # -- though with PR9's auto-refresh-on-tab-activation +
            # auto-refresh-on-upload-complete, manual refresh is now
            # the exception rather than the rule.
            self.query_one("#empty-state", Static).update(
                "[dim]No sessions yet.  "
                "Press [b]ctrl+r[/b] to switch to Record, or "
                "[b]ctrl+l[/b] to refresh.[/dim]",
            )
            return
        self.query_one("#empty-state", Static).update("")
        for s in self.sessions:
            label = (s.title or s.id)[:48]
            row_key = self._table.add_row(
                _short_time(s.updated_at or s.created_at),
                label,
                s.github or "",
                _status_cell(s),
                key=s.id,
            )
            self._row_index[str(row_key.value)] = s
