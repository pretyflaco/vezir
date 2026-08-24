"""Sessions list screen.

Mirrors vezir-android's SessionListScreen.kt: shows team + own personal
sessions in a DataTable, refreshes on demand (ctrl+r) or when this
screen is re-entered.  Row selection opens the detail screen.

Status badges are color-coded; personal sessions get a small marker.

v0.17.0: ``/`` opens a filter modal (date range / title / status / who);
pagination ("load more") keeps working inside the active filter.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Label, Select, Static

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
    "empty": "[dim][b]empty[/b][/dim]",
    "imported": "[blue][b]imported[/b][/blue]",
}


def _status_cell(s: Session) -> str:
    base = _STATUS_TAGS.get(s.status, s.status)
    parts = [base]
    if s.summary_error:
        parts.append("[red]· summary err[/red]")
    if s.summary_fallback:
        parts.append("[yellow]· fallback[/yellow]")
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
    # v0.16.0: set when this is an appended page ("load more"), not a
    # full refresh.
    append: bool = False


@dataclass
class SessionsRefreshFailed(Message):
    error: str


# Row key for the "load more" sentinel row appended when the last page
# was full — selecting it fetches the next page of older sessions.
_LOAD_MORE_KEY = "__load_more__"
_PAGE_SIZE = 50


# ── filter modal (v0.17.0) ──────────────────────────────────────────────────

_FILTER_STATUSES = [
    "all", "needs_labeling", "done", "sync_failed", "error", "empty",
    "imported", "queued", "transcribing", "summarizing", "syncing",
]


class SessionFilterScreen(ModalScreen["dict | None"]):
    """Modal: filter the sessions list by date range / title / status / who.

    Dismisses with a filter dict on Apply, ``{}`` on Clear, or ``None``
    on cancel.  Dates are YYYY-MM-DD (or ISO datetime); blank = unbounded.
    ``who`` accepts a github handle substring or an npub (server resolves
    npub -> handle).
    """

    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    CSS = """
    SessionFilterScreen { align: center middle; }
    #filter-box {
        width: 72;
        max-width: 90%;
        height: auto;
        border: solid $primary;
        padding: 1 2;
        background: $surface;
    }
    #filter-box Label { margin-top: 1; }
    #filter-box Input { margin-bottom: 0; }
    """

    def __init__(self, current: dict) -> None:
        super().__init__()
        self._current = current

    def compose(self) -> ComposeResult:
        with Vertical(id="filter-box"):
            yield Label("[b]Filter sessions[/b]")
            yield Label("Date from (YYYY-MM-DD):")
            yield Input(
                value=self._current.get("since", ""),
                placeholder="2026-05-01", id="f-since",
            )
            yield Label("Date to (YYYY-MM-DD):")
            yield Input(
                value=self._current.get("until", ""),
                placeholder="2026-05-31", id="f-until",
            )
            yield Label("Title contains:")
            yield Input(
                value=self._current.get("q", ""),
                placeholder="weekly sync", id="f-q",
            )
            yield Label("Status:")
            yield Select(
                [(s, s) for s in _FILTER_STATUSES],
                value=self._current.get("status") or "all",
                id="f-status", allow_blank=False,
            )
            yield Label("Who (github handle or npub):")
            yield Input(
                value=self._current.get("who", ""),
                placeholder="kasita / npub1…", id="f-who",
            )
            with Horizontal():
                yield Button("Apply", id="f-apply", variant="primary")
                yield Button("Clear", id="f-clear")
                yield Button("Cancel", id="f-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "f-apply":
            filters: dict = {}
            since = self.query_one("#f-since", Input).value.strip()
            until = self.query_one("#f-until", Input).value.strip()
            q = self.query_one("#f-q", Input).value.strip()
            who = self.query_one("#f-who", Input).value.strip()
            status = self.query_one("#f-status", Select).value
            if since:
                filters["since"] = since
            if until:
                filters["until"] = until
            if q:
                filters["q"] = q
            if who:
                filters["who"] = who
            if status and status != "all":
                filters["status"] = str(status)
            self.dismiss(filters)
        elif bid == "f-clear":
            self.dismiss({})
        else:
            self.dismiss(None)


def _filter_summary(filters: dict) -> str:
    """One-line human summary of the active filter for the sub-title."""
    parts = []
    if filters.get("since"):
        parts.append(f"from {filters['since']}")
    if filters.get("until"):
        parts.append(f"to {filters['until']}")
    if filters.get("q"):
        parts.append(f"title~{filters['q']!r}")
    if filters.get("status"):
        parts.append(filters["status"])
    if filters.get("who"):
        parts.append(f"by {filters['who']}")
    return " · ".join(parts)


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
        Binding("slash", "filter", "Filter"),
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
        # v0.16.0 pagination: True while the server may hold older
        # sessions beyond what's shown (last page came back full).
        self._has_more = False
        self._loading_more = False
        # v0.17.0: row key to restore the cursor to after an append —
        # the last visible row before "load more" was clicked, so the
        # user keeps browsing where the previous page ended instead of
        # being thrown back to the top of the table.
        self._anchor_key: str | None = None
        # v0.17.0: active filter set (date range / title / status / who),
        # applied on every fetch including load-more pages.
        self._filters: dict = {}

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

    def action_filter(self) -> None:
        """Open the filter modal (/). Applies or clears the session filter."""
        self.app.push_screen(
            SessionFilterScreen(self._filters), self._on_filter_chosen,
        )

    def _on_filter_chosen(self, choice: dict | None) -> None:
        if choice is None:
            return  # cancelled
        self._filters = dict(choice)
        self.action_refresh()

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
        row_key = str(event.row_key.value)
        # "Load more" sentinel row: fetch the next page instead of opening
        # a session detail.
        if row_key == _LOAD_MORE_KEY:
            self._load_more()
            return
        session = self._row_index.get(row_key)
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
            result = self.app.api.get_sessions(_PAGE_SIZE, **self._filters)
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

    def _load_more(self) -> None:
        """Fetch the next page of older sessions (sentinel row selected)."""
        if self._loading_more or not self._has_more:
            return
        self._loading_more = True
        # Anchor the cursor on the last visible session row so the append
        # re-render can restore it there (v0.17.0).
        if self.sessions:
            self._anchor_key = self.sessions[-1].id
        self.app.sub_title = "loading older sessions…"
        self._load_more_worker(offset=len(self.sessions))

    @work(thread=True, exclusive=True, group="sessions-load-more")
    def _load_more_worker(self, offset: int) -> None:
        result = self.app.api.get_sessions(
            _PAGE_SIZE, offset=offset, **self._filters,
        )
        if result.is_ok():
            self.post_message(
                SessionsRefreshed(sessions=result.ok, append=True),
            )
        else:
            self.post_message(SessionsRefreshFailed(error=result.error_message()))

    def on_sessions_refreshed(self, message: SessionsRefreshed) -> None:
        if message.append:
            # De-dupe defensively: a session created between page fetches
            # would shift the window and could repeat boundary rows.
            known = {s.id for s in self.sessions}
            new = [s for s in message.sessions if s.id not in known]
            self.sessions = [*self.sessions, *new]
        else:
            self.sessions = message.sessions
            self._anchor_key = None  # full refresh: no restore
        # A full page means the server may have more; a short page is the end.
        self._has_more = len(message.sessions) == _PAGE_SIZE
        self._loading_more = False
        self._render_table()
        # Restore the cursor to the pre-append anchor (v0.17.0).
        if message.append and self._anchor_key is not None:
            self._restore_cursor(self._anchor_key)
            self._anchor_key = None
        total = len(self.sessions)
        more = "+" if self._has_more else ""
        sub = f"sessions ({total}{more})"
        if self._filters:
            sub += f"  ·  {_filter_summary(self._filters)}"
        self.app.sub_title = sub

    def _restore_cursor(self, row_key: str) -> None:
        """Move the DataTable cursor back to ``row_key`` after a re-render."""
        if self._table is None:
            return
        try:
            idx = self._table.get_row_index(row_key)
        except Exception:
            return
        self._table.move_cursor(row=idx, animate=False)

    def on_sessions_refresh_failed(self, message: SessionsRefreshFailed) -> None:
        self._loading_more = False
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
        # "Load more" sentinel row at the bottom while the server may hold
        # older sessions.  Selecting it fetches the next page.
        if self._has_more:
            self._table.add_row(
                "", "[dim]▼ load more (older sessions)[/dim]", "", "",
                key=_LOAD_MORE_KEY,
            )
