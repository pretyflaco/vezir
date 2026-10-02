"""Outbox tab: every local recording that hasn't reached the server (0.25.0).

The TUI face of ``vezir local`` (0.24.0) and the successor of the 0.23
startup recovery dialog.  The dialog only appeared at launch, only knew
three states, and could not hold, move or discard; recordings kept local
on purpose ("Keep local" after Stop, 0.25.0) need a place to live that is
always one keystroke away (``^o``).

Rows come from :func:`vezir.client.local.scan`.  Actions on the selected
row: ``u`` review + upload (any team), ``t`` move to another team, ``h``
hold (stop the startup nudge), ``d`` discard to the trash, ``o`` open the
folder, ``a`` show all (uploaded + historical), ``ctrl+l`` refresh.

On launch :func:`install_outbox_check` scans once and, when something
needs attention (anything but held / in-progress), switches to this tab
and says so — loudly for an orphaned recorder, which is still capturing.
Disabled in tests via ``VEZIR_TUI_DISABLE_RECOVERY_SCAN=1``.
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
from textual.widgets import Button, DataTable, Label, Static

from .. import local, upload_journal

log = logging.getLogger("vezir.client.tui.outbox")

# States that make the launch check speak up.
ATTENTION_STATES = frozenset({"orphaned", "interrupted", "failed", "uploading", "pending"})


def _fmt_size(n: int) -> str:
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.1f} GiB"
    if n >= 1024 ** 2:
        return f"{n / 1024 ** 2:.1f} MiB"
    return f"{n / 1024:.0f} KiB"


@dataclass
class OutboxScanned(Message):
    recordings: list


class ConfirmScreen(ModalScreen[bool]):
    """Yes/no; Escape and the default button say no."""

    BINDINGS = [Binding("escape", "dismiss(False)", "Cancel")]

    DEFAULT_CSS = """
    ConfirmScreen { align: center middle; }
    #confirm-box {
        width: 70;
        max-width: 90%;
        height: auto;
        border: round $warning;
        padding: 1 2;
        background: $surface;
    }
    #confirm-box Horizontal { height: 3; margin-top: 1; }
    #confirm-box Button { margin-right: 1; }
    """

    def __init__(self, question: str, detail: str, confirm: str) -> None:
        super().__init__()
        self._question = question
        self._detail = detail
        self._confirm = confirm

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Label(f"[b]{self._question}[/b]")
            yield Label(self._detail)
            with Horizontal():
                yield Button("Cancel", id="confirm-no", variant="primary")
                yield Button(self._confirm, id="confirm-yes", variant="warning")

    def on_mount(self) -> None:
        self.query_one("#confirm-no", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-yes")


class OutboxBody(Vertical):
    """Local recordings not (yet) on the server."""

    BINDINGS = [
        Binding("u", "upload", "Upload…"),
        Binding("t", "move_team", "Team…"),
        Binding("h", "hold", "Hold"),
        Binding("d", "discard", "Discard"),
        Binding("o", "open_folder", "Open folder"),
        Binding("a", "toggle_all", "All"),
    ]

    DEFAULT_CSS = """
    OutboxBody { padding: 1 2; }
    #outbox-help { color: $text-muted; height: 1; margin-bottom: 1; }
    #outbox-table { height: 1fr; }
    #outbox-status { height: auto; margin-top: 1; color: $text-muted; }
    """

    def __init__(self) -> None:
        super().__init__()
        self._recs: list[local.LocalRecording] = []
        self._show_all = False
        self._busy: set[str] = set()

    @classmethod
    def body_widget(cls) -> OutboxBody:
        return cls()

    def compose(self) -> ComposeResult:
        yield Static(
            "u upload… · t team… · h hold · d discard · o open folder · "
            "a show all · ^l refresh",
            id="outbox-help",
        )
        table: DataTable = DataTable(id="outbox-table", cursor_type="row", zebra_stripes=True)
        table.add_columns("recording", "team", "state", "size", "detail")
        yield table
        yield Static("", id="outbox-status")

    def on_mount(self) -> None:
        self.action_refresh()
        self.set_interval(15.0, self.action_refresh, name="outbox-refresh")

    # ── scanning ──

    def action_refresh(self) -> None:
        self._scan_worker(self._show_all)

    @work(thread=True, exclusive=True, group="outbox-scan")
    def _scan_worker(self, show_all: bool) -> None:
        try:
            recs = local.scan(include_all=show_all)
        except Exception as exc:  # pragma: no cover - scan never raises
            log.warning("outbox scan failed: %s", exc)
            return
        self.post_message(OutboxScanned(recs))

    def on_outbox_scanned(self, message: OutboxScanned) -> None:
        keep = self._selected()  # before the rows change under the cursor
        self._recs = message.recordings
        table = self.query_one("#outbox-table", DataTable)
        table.clear()
        for r in self._recs:
            state = r.state + (" …" if r.name in self._busy else "")
            table.add_row(r.name, r.team, state, _fmt_size(r.total_bytes), r.detail, key=r.name)
        if keep is not None:
            names = [r.name for r in self._recs]
            if keep.name in names:
                table.move_cursor(row=names.index(keep.name))
        self._update_tab_label()
        if not self._recs:
            self._set_status(
                "Nothing local is waiting for the server."
                + ("" if self._show_all else "  (a: show uploaded/historical)")
            )

    def _update_tab_label(self) -> None:
        n = sum(1 for r in self._recs if r.state in ATTENTION_STATES | {"held"})
        try:
            from textual.widgets import TabbedContent

            tabs = self.screen.query_one(TabbedContent)
            tabs.get_tab("outbox").label = f"Outbox ({n})" if n else "Outbox"
        except Exception:
            pass

    # ── helpers ──

    def _selected(self) -> local.LocalRecording | None:
        try:
            table = self.query_one("#outbox-table", DataTable)
        except Exception:
            return None
        if not self._recs or table.cursor_row is None:
            return None
        if not (0 <= table.cursor_row < len(self._recs)):
            return None
        return self._recs[table.cursor_row]

    def _set_status(self, text: str) -> None:
        try:
            self.query_one("#outbox-status", Static).update(text)
        except Exception:
            pass

    def _actionable(self, verb: str) -> local.LocalRecording | None:
        rec = self._selected()
        if rec is None:
            return None
        if rec.name in self._busy:
            self._set_status(f"{rec.name}: already working on it")
            return None
        if rec.state == "in-progress":
            self._set_status(f"{rec.name} is still recording — stop it on the Record tab first")
            return None
        if rec.state == "uploaded" and verb != "discard":
            self._set_status(f"{rec.name} is already on the server as {rec.session_id}")
            return None
        return rec

    # ── actions ──

    def action_upload(self) -> None:
        rec = self._actionable("upload")
        if rec is None:
            return
        from ..config import load_client_prefs
        from .review_screen import UploadReviewScreen, team_choices

        opts = {**load_client_prefs(), **(rec.options or {})}

        def _after(result) -> None:
            if result is None or result.action == "cancel":
                return
            if result.action == "hold":
                self._hold(rec, result.team, result.title, result.options)
                return
            self._busy.add(rec.name)
            self._salvage_worker(rec, result)
            self.action_refresh()

        self.app.push_screen(UploadReviewScreen(
            name=rec.name,
            audio_path=rec.audio_path,
            team=rec.team,
            teams=team_choices(self.app, rec.team),
            title=rec.title,
            auto_label=bool(opts.get("auto_label", True)),
            sync=bool(opts.get("sync", True)),
            personal=bool(opts.get("personal", False)),
            hold_label="Keep local",
            show_attachments=False,
        ), _after)

    @work(thread=True, group="outbox-salvage")
    def _salvage_worker(self, rec: local.LocalRecording, review) -> None:
        from ..recovery import salvage
        from .review_screen import upload_credentials

        def status(msg: str) -> None:
            self.app.call_from_thread(self._set_status, f"{rec.name}: {msg}")

        server_url, token, refresh_cb = upload_credentials(self.app, review.team)
        try:
            sid = salvage(
                rec.to_recoverable(),
                server_url=server_url, token=token, team=review.team,
                title=review.title, auto_label=review.auto_label,
                sync=review.sync, personal=review.personal,
                refresh_cb=refresh_cb, on_status=status,
            )
            status(f"✔ uploaded to {review.team} as {sid}")
            self.app.call_from_thread(
                self.app.notify, f"{rec.name} uploaded to {review.team}", timeout=5,
            )
        except Exception as exc:
            log.warning("outbox upload of %s failed: %s", rec.name, exc)
            status(f"✖ {exc}")
        finally:
            self._busy.discard(rec.name)
            self.app.call_from_thread(self.action_refresh)

    def action_move_team(self) -> None:
        rec = self._actionable("move")
        if rec is None:
            return
        from .review_screen import TeamPickScreen, team_choices

        def _after(team: str | None) -> None:
            if not team or team == rec.team:
                return
            try:
                local.move(rec, team)
            except Exception as exc:
                self._set_status(f"✖ {exc}")
                return
            self._set_status(f"moved {rec.name}: {rec.team} → {team}")
            self.action_refresh()

        self.app.push_screen(TeamPickScreen(team_choices(self.app, rec.team), rec.team), _after)

    def _hold(self, rec, team: str, title: str | None, options: dict | None) -> None:
        if rec.state == "orphaned":
            self._set_status(
                f"{rec.name}: its recorder is still running — upload it (u) to "
                "stop and save it; holding would leave it capturing"
            )
            return
        if team != rec.team:
            try:
                local.move(rec, team)
                rec = next(
                    r for r in local.scan(include_all=True) if r.name == rec.name
                )
            except Exception as exc:
                self._set_status(f"✖ {exc}")
                return
        upload_journal.mark_held(
            rec.session_dir, title=title, team_id=team, options=options or rec.options,
            pending_attachments=bool(upload_journal.read(rec.session_dir).get(
                "pending_attachments")),
        )
        self._set_status(f"holding {rec.name} in {team} — it won't be offered at launch")
        self.action_refresh()

    def action_hold(self) -> None:
        rec = self._actionable("hold")
        if rec is None or rec.state == "held":
            return
        self._hold(rec, rec.team, rec.title, rec.options)

    def action_discard(self) -> None:
        rec = self._actionable("discard")
        if rec is None:
            return
        if rec.state == "orphaned":
            self._set_status(f"{rec.name}: its recorder is still running; upload or stop it first")
            return
        on_server = rec.state == "uploaded"
        detail = (
            f"{rec.name} [{rec.team}, {_fmt_size(rec.total_bytes)}]\n"
            + ("The server session is unaffected." if on_server
               else "[b]This recording has NOT reached the server.[/b]")
            + f"\nIt moves to {local.trash_dir()} (recoverable)."
        )

        def _after(ok: bool | None) -> None:
            if not ok:
                return
            try:
                dest = local.discard(rec)
            except Exception as exc:
                self._set_status(f"✖ {exc}")
                return
            self._set_status(f"moved {rec.name} to {dest}")
            self.action_refresh()

        self.app.push_screen(ConfirmScreen("Discard this recording?", detail, "Discard"), _after)

    def action_open_folder(self) -> None:
        rec = self._selected()
        if rec is None:
            return
        import subprocess

        try:
            subprocess.Popen(
                ["xdg-open", str(rec.session_dir)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            self._set_status(f"could not open folder: {exc}")

    def action_toggle_all(self) -> None:
        self._show_all = not self._show_all
        self._set_status("showing all local recordings" if self._show_all else "showing the outbox")
        self.action_refresh()


def install_outbox_check(host) -> None:
    """Scan once at launch; switch to the Outbox when something needs you.

    Held and in-progress recordings never trigger it.  Runs in a worker
    thread (filesystem + /proc walk) so startup isn't blocked.
    """
    app = host.app

    def _scan() -> None:
        try:
            recs = local.scan()
        except Exception as exc:
            log.warning("outbox launch scan failed: %s", exc)
            return
        attention = [r for r in recs if r.state in ATTENTION_STATES]
        if not attention:
            return
        log.info("outbox: %d recording(s) need attention: %s",
                 len(attention), [r.name for r in attention])
        orphans = [r for r in attention if r.state == "orphaned"]

        def _show() -> None:
            try:
                host.action_show_tab("outbox")
            except Exception:
                pass
            if orphans:
                app.notify(
                    f"{len(orphans)} recorder(s) still capturing with no vezir "
                    "attached — upload (u) to stop and save.",
                    severity="error", timeout=15,
                )
            app.notify(
                f"{len(attention)} recording(s) never reached the server — "
                "see the Outbox tab.",
                severity="warning", timeout=10,
            )

        app.call_from_thread(_show)

    host.run_worker(_scan, thread=True, name="outbox-launch-scan")
