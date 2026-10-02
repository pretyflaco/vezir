"""Startup crash-recovery dialog (vezir 0.23.0).

Incident 2026-09-28: a TUI crash mid-recording left an 87-minute meeting
on disk with no trace in the UI — the Sessions tab is server-side only.
The user concluded the session was lost.  It wasn't: the chunk WAVs were
fully intact, but there was no discoverable path to them.

On TUI launch, ``install_recovery_scan`` scans the local recordings roots
(in a worker thread, off the UI path) for sessions that never reached the
server — interrupted recordings, orphaned recorders, unfinished uploads
(see ``vezir.client.recovery``) — and pushes this modal when any are
found.  One salvage action per session does the right thing for its
state: stop an orphaned recorder (SIGINT finalizes the WAV), stitch the
chunks, compress, upload with the journal tracking every step.

Disabled in tests via VEZIR_TUI_DISABLE_RECOVERY_SCAN=1.
"""
from __future__ import annotations

import logging

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, OptionList, Static
from textual.widgets.option_list import Option

from ..recovery import RecoverableSession, scan_interrupted

log = logging.getLogger("vezir.client.tui.recovery")

_MINUTE_BYTES = 64000 * 60  # pcm_s16le 16kHz stereo


def _fmt_size(nbytes: int) -> str:
    if nbytes >= 1024 * 1024 * 1024:
        return f"{nbytes / (1024 ** 3):.1f} GiB"
    if nbytes >= 1024 * 1024:
        return f"{nbytes / (1024 ** 2):.1f} MiB"
    return f"{nbytes / 1024:.1f} KiB"


def _describe(rec: RecoverableSession) -> str:
    size = _fmt_size(rec.total_bytes)
    if rec.kind == "pending_upload":
        state = rec.detail
    elif rec.kind == "orphaned":
        state = f"⚠ {rec.detail}"
    else:
        minutes = rec.total_bytes / _MINUTE_BYTES
        state = f"~{minutes:.0f} min, {rec.detail}"
    started = (rec.started_at or "")[:16].replace("T", " ")
    stamp = f"{started}  " if started else ""
    return f"{rec.session_dir.name}  [{rec.team_id}]  {stamp}{size} — {state}"


class RecoveryScreen(ModalScreen[None]):
    """Lists salvageable local sessions; uploads them on demand."""

    DEFAULT_CSS = """
    RecoveryScreen {
        align: center middle;
    }
    #recovery-box {
        width: 84%;
        height: auto;
        max-height: 85%;
        border: round $primary;
        padding: 1 2;
        background: $surface;
    }
    #recovery-title {
        height: 1;
        margin-bottom: 1;
        text-style: bold;
    }
    #recovery-list {
        height: auto;
        max-height: 10;
        margin-bottom: 1;
    }
    #recovery-title-input {
        margin-bottom: 1;
    }
    #recovery-status {
        height: auto;
        color: $text-muted;
        margin-bottom: 1;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss_all", "Close"),
        Binding("r", "salvage", "Salvage & upload"),
        Binding("o", "open_folder", "Open folder"),
        Binding("t", "pick_team", "Team"),
        Binding("p", "toggle_personal", "Personal"),
        Binding("s", "toggle_sync", "Sync"),
        Binding("a", "toggle_auto_label", "Auto-label"),
    ]

    def __init__(self, sessions: list[RecoverableSession]) -> None:
        super().__init__()
        # Done rows stay visible but inert.  ``team`` is the destination,
        # defaulting to where the recording lives (0.24.0: changeable).
        self._rows: list[dict] = [
            {"rec": s, "done": False, "busy": False, "team": s.team_id}
            for s in sessions
        ]
        # Upload options default to the saved preferences, like the Record
        # tab (0.23.x hard-wired auto-label + sync on).
        try:
            from ..config import load_client_prefs

            prefs = load_client_prefs()
        except Exception:
            prefs = {}
        self._sync = bool(prefs.get("sync", True))
        self._auto_label = bool(prefs.get("auto_label", True))
        self._personal = False

    def compose(self) -> ComposeResult:
        with Vertical(id="recovery-box"):
            yield Static(
                "Recovered recordings — never reached the server",
                id="recovery-title",
            )
            yield OptionList(
                *[Option(_describe(r["rec"]), id=str(i))
                  for i, r in enumerate(self._rows)],
                id="recovery-list",
            )
            yield Input(
                placeholder="Title for the upload (optional)",
                id="recovery-title-input",
            )
            yield Static("", id="recovery-target")
            yield Static(
                "r salvage & upload · t team · p personal · s sync · "
                "a auto-label · o open folder · esc close",
                id="recovery-status",
            )
            with Horizontal(id="recovery-actions"):
                yield Button("⬆ Salvage & upload", id="recovery-go", variant="primary")
                yield Button("Open folder", id="recovery-open")
                yield Button("Close", id="recovery-close")

    def on_mount(self) -> None:
        try:
            self.query_one("#recovery-list", OptionList).focus()
        except Exception:
            pass
        self._prefill_title()
        self._render_target()

    # ── selection helpers ──

    def _selected_row(self) -> dict | None:
        try:
            idx = self.query_one("#recovery-list", OptionList).highlighted
        except Exception:
            return None
        if idx is None or not (0 <= idx < len(self._rows)):
            return None
        return self._rows[idx]

    def _prefill_title(self) -> None:
        row = self._selected_row()
        if not row:
            return
        hint = row["rec"].title_hint or ""
        try:
            self.query_one("#recovery-title-input", Input).value = hint
        except Exception:
            pass

    def on_option_list_option_highlighted(
        self, event: OptionList.OptionHighlighted
    ) -> None:
        self._prefill_title()
        self._render_target()

    def _set_status(self, text: str) -> None:
        try:
            self.query_one("#recovery-status", Static).update(text)
        except Exception:
            pass

    # ── actions ──

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "recovery-go":
            self.action_salvage()
        elif event.button.id == "recovery-open":
            self.action_open_folder()
        elif event.button.id == "recovery-close":
            self.dismiss(None)

    def action_dismiss_all(self) -> None:
        self.dismiss(None)

    def action_open_folder(self) -> None:
        row = self._selected_row()
        if not row:
            return
        import subprocess

        try:
            subprocess.Popen(
                ["xdg-open", str(row["rec"].session_dir)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            self._set_status(f"could not open folder: {exc}")

    def action_salvage(self) -> None:
        row = self._selected_row()
        if not row or row["done"] or row["busy"]:
            return
        row["busy"] = True
        try:
            title = self.query_one("#recovery-title-input", Input).value.strip() or None
        except Exception:
            title = None
        self._salvage_worker(row, title)

    # ── destination + options (0.24.0) ──

    def _render_target(self) -> None:
        """Show where the selected row will go — before anything is sent."""
        row = self._selected_row()
        if not row:
            return
        moved = (
            f"  (moves from {row['rec'].team_id})"
            if row["team"] != row["rec"].team_id else ""
        )
        text = (
            f"→ [b]{row['team']}[/b]{moved} · "
            f"sync {'on' if self._sync and not self._personal else 'off'} · "
            f"auto-label {'on' if self._auto_label else 'off'}"
            + (" · [b]personal[/b]" if self._personal else "")
        )
        try:
            self.query_one("#recovery-target", Static).update(text)
        except Exception:
            pass

    def _team_choices(self) -> list[str]:
        teams: set[str] = set()
        try:
            teams.update(t["slug"] for t in self.app.all_teams())
        except Exception:
            pass
        try:
            from ..local import known_teams

            teams.update(known_teams())
        except Exception:
            pass
        return sorted(teams)

    def action_pick_team(self) -> None:
        row = self._selected_row()
        if not row or row["done"] or row["busy"]:
            return

        def _after(team: str | None) -> None:
            if team:
                row["team"] = team
                self._render_target()

        self.app.push_screen(TeamPickScreen(self._team_choices(), row["team"]), _after)

    def action_toggle_personal(self) -> None:
        self._personal = not self._personal
        self._render_target()

    def action_toggle_sync(self) -> None:
        self._sync = not self._sync
        self._render_target()

    def action_toggle_auto_label(self) -> None:
        self._auto_label = not self._auto_label
        self._render_target()

    def _credentials_for(self, team: str) -> tuple[str, str]:
        """Server + token for *team*: a fresh same-identity token from
        teams.json when configured (0.24.0), else the running app's."""
        try:
            from ..config import freshest_credentials

            t_id, url, token = freshest_credentials(team)
            if t_id and url and token:
                return url, token
        except Exception:
            pass
        return self.app.server_url, self.app.token or ""

    @work(thread=True, group="recovery-salvage")
    def _salvage_worker(self, row: dict, title: str | None) -> None:
        from ..recovery import salvage

        rec: RecoverableSession = row["rec"]
        team = row["team"]
        server_url, token = self._credentials_for(team)

        def refresh_cb() -> str | None:
            from ..api import refresh_session

            new = refresh_session(server_url, None, team)
            if new and team == getattr(self.app, "active_team_id", None):
                self.app.token = new
                if getattr(self.app, "api", None) is not None:
                    self.app.api.token = new
            return new

        def on_status(msg: str) -> None:
            self.app.call_from_thread(self._set_status, msg)

        try:
            session_id = salvage(
                rec,
                server_url=server_url,
                token=token,
                team=team,
                title=title,
                auto_label=self._auto_label,
                sync=self._sync,
                personal=self._personal,
                refresh_cb=refresh_cb,
                on_status=on_status,
            )
            row["done"] = True
            self.app.call_from_thread(
                self._set_status,
                f"✔ {rec.session_dir.name} uploaded to {team} as "
                f"{session_id or '(no session id)'}",
            )
            self.app.call_from_thread(self._mark_row_done, row)
        except Exception as exc:
            log.warning("salvage of %s failed: %s", rec.session_dir, exc)
            self.app.call_from_thread(
                self._set_status, f"✖ {rec.session_dir.name}: {exc}"
            )
        finally:
            row["busy"] = False

    def _mark_row_done(self, row: dict) -> None:
        try:
            idx = self._rows.index(row)
            lst = self.query_one("#recovery-list", OptionList)
            lst.replace_option_prompt(
                str(idx), f"✔ {_describe(row['rec'])}"
            )
        except Exception:
            pass


class TeamPickScreen(ModalScreen["str | None"]):
    """Pick the destination team for a salvaged recording."""

    DEFAULT_CSS = """
    TeamPickScreen {
        align: center middle;
    }
    #team-pick-box {
        width: 50;
        height: auto;
        max-height: 80%;
        border: round $primary;
        padding: 1 2;
        background: $surface;
    }
    """

    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    def __init__(self, teams: list[str], current: str) -> None:
        super().__init__()
        self._teams = teams
        self._current = current

    def compose(self) -> ComposeResult:
        with Vertical(id="team-pick-box"):
            yield Static("Upload to which team?")
            yield OptionList(
                *[Option(("● " if t == self._current else "  ") + t, id=t)
                  for t in self._teams],
                id="team-pick-list",
            )

    def on_mount(self) -> None:
        lst = self.query_one("#team-pick-list", OptionList)
        lst.focus()
        if self._current in self._teams:
            lst.highlighted = self._teams.index(self._current)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(str(event.option.id))


def install_recovery_scan(host) -> None:
    """Scan recordings roots once at startup; push RecoveryScreen if needed.

    ``host`` is whatever on_mount called us (the MainScreen) — the scan
    worker hangs off it, but push_screen/call_from_thread belong to the
    App.  Runs in a worker thread (filesystem + /proc walk) so startup
    isn't blocked; the modal appears a beat after the main screen.
    """
    app = host.app

    def _scan() -> None:
        try:
            sessions = scan_interrupted()
        except Exception as exc:
            log.warning("recovery scan failed: %s", exc)
            return
        if not sessions:
            return
        log.info(
            "recovery scan found %d salvageable session(s): %s",
            len(sessions),
            [s.session_dir.name for s in sessions],
        )
        app.call_from_thread(app.push_screen, RecoveryScreen(sessions))

    host.run_worker(_scan, thread=True, name="recovery-scan")
