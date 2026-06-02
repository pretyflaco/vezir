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

from ..api import Session

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
    "sync_failed": "[red][b]sync failed[/b][/red]",
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
        # Binding `enter` at the screen level would cause a
        # double-dispatch: DetailScreen gets pushed twice, leading
        # to a stuck UI state.  Smoked on muscle 2026-05-23.
        Binding("c", "copy_selected_id", "Copy id"),
        Binding("f", "open_selected_folder", "Open folder"),
        Binding("d", "copy_selected_path", "Copy path"),
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
    def body_widget(cls) -> SessionsBody:
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

    def _cursor_session(self) -> Session | None:
        """Return the Session under the cursor, or None."""
        if self._table is None:
            return None
        try:
            row_key = self._table.coordinate_to_cell_key(
                self._table.cursor_coordinate,
            ).row_key
        except Exception:
            return None
        return self._row_index.get(str(row_key.value)) if row_key.value else None

    def action_open_selected_folder(self) -> None:
        """Open the local recording folder for the cursor-row session."""
        session = self._cursor_session()
        if session is None:
            self.app.bell()
            return
        from ..pull import find_local_session_dir
        local = find_local_session_dir(session.id, session.team_id)
        if local is None:
            self.notify(
                "No local folder — pulling artifacts...",
                severity="information",
                timeout=4,
            )
            self._pull_and_open(session.id)
            return
        import shutil
        import subprocess
        import sys
        if sys.platform == "darwin":
            cmd = ["open", str(local)]
        elif shutil.which("xdg-open"):
            cmd = ["xdg-open", str(local)]
        else:
            self.notify(f"No file manager.  Path: {local}", severity="warning", timeout=8)
            return
        try:
            subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            self.notify(f"Opened {local.name}", severity="information", timeout=4)
        except Exception as exc:
            self.notify(f"Could not open folder: {exc}", severity="error")

    def action_copy_selected_path(self) -> None:
        """Copy the local recording path for the cursor-row session."""
        session = self._cursor_session()
        if session is None:
            self.app.bell()
            return
        from ..pull import find_local_session_dir
        local = find_local_session_dir(session.id, session.team_id)
        if local is None:
            self.notify(
                "No local folder — pulling artifacts...",
                severity="information",
                timeout=4,
            )
            self._pull_and_notify(session.id)
            return
        try:
            self.app.copy_to_clipboard(str(local))
        except Exception as exc:
            self.notify(f"Copy failed: {exc}", severity="error")
            return
        self.notify(f"Copied: {local}", severity="information", timeout=4)

    @work(thread=True, exclusive=True, group="session-pull")
    def _pull_and_open(self, session_id: str) -> None:
        from ..pull import find_local_session_dir, pull_team_sessions
        try:
            pull_team_sessions(self.app.api, session_id=session_id)
        except Exception:
            self.app.call_from_thread(
                self.notify, "Pull failed.", severity="error",
            )
            return
        local = find_local_session_dir(session_id)
        if local:
            import shutil
            import subprocess
            import sys
            if sys.platform == "darwin":
                cmd = ["open", str(local)]
            elif shutil.which("xdg-open"):
                cmd = ["xdg-open", str(local)]
            else:
                self.app.call_from_thread(
                    self.notify, f"Pulled to: {local}", severity="information", timeout=6,
                )
                return
            try:
                subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
                self.app.call_from_thread(
                    self.notify, f"Opened {local.name}", severity="information", timeout=4,
                )
            except Exception:
                self.app.call_from_thread(
                    self.notify, f"Pulled to: {local}", severity="information", timeout=6,
                )

    @work(thread=True, exclusive=True, group="session-pull")
    def _pull_and_notify(self, session_id: str) -> None:
        from ..pull import find_local_session_dir, pull_team_sessions
        try:
            pull_team_sessions(self.app.api, session_id=session_id)
        except Exception:
            self.app.call_from_thread(
                self.notify, "Pull failed.", severity="error",
            )
            return
        local = find_local_session_dir(session_id)
        if local:
            try:
                self.app.call_from_thread(self.app.copy_to_clipboard, str(local))
                self.app.call_from_thread(
                    self.notify, f"Copied: {local}", severity="information", timeout=4,
                )
            except Exception:
                self.app.call_from_thread(
                    self.notify, f"Pulled to: {local}", severity="information", timeout=6,
                )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        session = self._row_index.get(str(event.row_key.value))
        if session is None:
            return
        from .detail_screen import DetailScreen
        self.app.push_screen(DetailScreen(session_id=session.id))

    @work(thread=True, exclusive=True, group="sessions-refresh")
    def _refresh_worker(self) -> None:
        from textual.worker import get_current_worker
        worker = get_current_worker()
        last_error = ""
        for attempt in range(3):
            if worker.is_cancelled:
                return
            result = self.app.api.get_sessions(50)
            if result.is_ok():
                self.post_message(SessionsRefreshed(sessions=result.ok))
                return
            last_error = result.error_message()
            # HTTP errors (server reachable, returned error) don't retry.
            if result.http_error is not None:
                break
            # Network errors: retry with 10s delay (mesh may be establishing).
            if attempt < 2:
                worker.cancelled_event.wait(10)
        self.post_message(SessionsRefreshFailed(error=last_error))

    def on_sessions_refreshed(self, message: SessionsRefreshed) -> None:
        self.sessions = message.sessions
        self._render_table()
        self.app.sub_title = f"sessions ({len(message.sessions)})"

    def on_sessions_refresh_failed(self, message: SessionsRefreshFailed) -> None:
        self.app.sub_title = "refresh failed"
        self.query_one("#empty-state", Static).update(
            f"[red]Failed to fetch sessions: {message.error}[/red]\n"
            f"[dim]If using a VPN, check that the tunnel is established.[/dim]\n"
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
