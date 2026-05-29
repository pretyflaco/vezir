"""Teams list screen (v0.7.6).

Mirrors vezir-android's team picker: shows every team the user belongs
to — discovered from the server's GET /api/me memberships list, unioned
with any explicit teams.json entries — in a DataTable.  Selecting a row
switches the active team (token-preserving: the same bearer token
authorizes every team; only the per-request X-Team-Id changes).

Unlike the legacy ^t cycle, this requires NO manual `vezir team config
add`: teams auto-populate from the server each time the tab refreshes.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import DataTable, Static

log = logging.getLogger("vezir.client.tui.teams")


@dataclass
class TeamsRefreshed(Message):
    # The /api/me memberships list (each: team_id/uuid, slug, role,
    # team_name).  Empty list is valid (server returned no memberships).
    memberships: list = field(default_factory=list)


@dataclass
class TeamsRefreshFailed(Message):
    error: str


class TeamsBody(Vertical):
    """Pick the active team from every team the user belongs to."""

    BINDINGS = [
        # DataTable owns `enter` (fires RowSelected -> on_data_table_row_selected).
        # Do NOT bind enter here (see SessionsBody note).
        Binding("r", "refresh", "Refresh", show=False),
    ]

    DEFAULT_CSS = """
    TeamsBody {
        padding: 1 2;
    }
    TeamsBody DataTable { height: 1fr; }
    TeamsBody #teams-empty-state {
        color: $text-muted;
        height: 1fr;
        content-align: center middle;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._table: DataTable | None = None

    @classmethod
    def body_widget(cls) -> TeamsBody:
        return cls()

    def compose(self) -> ComposeResult:
        table = DataTable(id="teams-table", zebra_stripes=True, cursor_type="row")
        table.add_columns("", "team", "role", "source")
        self._table = table
        yield table
        yield Static("", id="teams-empty-state")

    def on_mount(self) -> None:
        self.action_refresh()

    def action_refresh(self) -> None:
        """Re-fetch /api/me memberships, then re-render the table."""
        self._refresh_worker()

    @work(thread=True, exclusive=True, group="teams-refresh")
    def _refresh_worker(self) -> None:
        from textual.worker import get_current_worker
        worker = get_current_worker()
        last_error = ""
        for attempt in range(3):
            if worker.is_cancelled:
                return
            result = self.app.api.get_me()
            if result.is_ok():
                mems = (result.ok or {}).get("memberships") or []
                self.post_message(TeamsRefreshed(memberships=list(mems)))
                return
            last_error = result.error_message()
            # HTTP errors (server reachable, returned error) don't retry.
            if result.http_error is not None:
                break
            # Network errors: retry (mesh may be establishing).
            if attempt < 2:
                worker.cancelled_event.wait(10)
        self.post_message(TeamsRefreshFailed(error=last_error))

    def on_teams_refreshed(self, message: TeamsRefreshed) -> None:
        # Update the app-level membership cache so all_teams() + ^t see
        # the fresh list, then render from the merged view.
        self.app.memberships = message.memberships
        self._render_table()

    def on_teams_refresh_failed(self, message: TeamsRefreshFailed) -> None:
        # Fall back to whatever the app already cached (e.g. from the
        # mount-time /api/me in _refresh_identity) so the tab isn't blank.
        self._render_table()
        if not self.app.all_teams():
            self.query_one("#teams-empty-state", Static).update(
                f"[red]Failed to fetch teams: {message.error}[/red]\n"
                f"[dim]If using a VPN, check that the tunnel is "
                f"established.[/dim]\n"
                f"Press [b]ctrl+l[/b] to retry.",
            )

    def _render_table(self) -> None:
        assert self._table is not None
        self._table.clear()
        teams = self.app.all_teams()
        if not teams:
            self.query_one("#teams-empty-state", Static).update(
                "[dim]No teams.  The server reported no memberships and "
                "teams.json is empty.[/dim]",
            )
            return
        self.query_one("#teams-empty-state", Static).update("")
        active = self.app.active_team_id
        for t in teams:
            marker = "[green]●[/green]" if t["slug"] == active else ""
            label = t["label"]
            if t["slug"] != label:
                label = f"{label} [dim]({t['slug']})[/dim]"
            self._table.add_row(
                marker,
                label,
                t.get("role") or "[dim]—[/dim]",
                t["source"],
                key=t["slug"],
            )

    def refresh_active_marker(self) -> None:
        """Re-render so the ● active marker tracks an external switch
        (e.g. the user pressed ^t while the Teams tab is mounted)."""
        if self._table is not None:
            self._render_table()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        slug = str(event.row_key.value) if event.row_key.value else None
        if slug is None:
            return
        team = next(
            (t for t in self.app.all_teams() if t["slug"] == slug), None
        )
        if team is None:
            return
        if slug == self.app.active_team_id:
            self.notify(f"{team['label']} is already active.", timeout=3)
            return
        self.app.switch_to_team(slug)
