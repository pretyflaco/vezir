"""Upload review: the last stop before anything leaves the machine (0.25.0).

Incident 2026-10-02: a recording started in the wrong team could not be
redirected — Stop uploaded to the team the recording began in, and even
Escape on the attachment prompt uploaded.  This modal replaces that
prompt.  It states the destination and options in full, lets every one of
them change, and offers **Keep local**, which is also what Escape does: an
accidental dismissal shares nothing.  A held recording is journaled
(``held``), parked with its attachments, and listed in the Outbox tab.

Also home of the helpers the Record and Outbox tabs share:
:class:`TeamPickScreen`, :func:`team_choices`, :func:`upload_credentials`.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, OptionList, Select, Static
from textual.widgets.option_list import Option

from .. import attachments


@dataclass
class ReviewResult:
    action: str  # "upload" | "hold" | "cancel"
    team: str
    title: str | None
    auto_label: bool
    sync: bool
    personal: bool

    @property
    def options(self) -> dict:
        return {"auto_label": self.auto_label, "sync": self.sync, "personal": self.personal}


def team_choices(app, *extra: str | None) -> list[str]:
    """Every team the user can target: memberships + teams.json + local roots."""
    teams: set[str] = {t for t in extra if t}
    try:
        teams.update(t["slug"] for t in app.all_teams())
    except Exception:
        pass
    try:
        from ..local import known_teams

        teams.update(known_teams())
    except Exception:
        pass
    return sorted(teams)


def upload_credentials(app, team: str):
    """``(server_url, token, refresh_cb)`` for uploading to *team*.

    Prefers a fresh same-identity token from teams.json (0.24.0
    ``freshest_credentials``), falls back to the running app's.  The
    refresh callback rotates *team*'s entry (fanned out to the identity)
    and rebinds the app's in-memory token when it is the same login.
    """
    server_url, token = app.server_url, app.token or ""
    try:
        from ..config import freshest_credentials

        t_id, url, tok = freshest_credentials(team)
        if t_id and url and tok:
            server_url, token = url, tok
    except Exception:
        pass

    def refresh_cb() -> str | None:
        from ..api import refresh_session

        new = refresh_session(server_url, None, team)
        if new and server_url == app.server_url:
            app.token = new
            if getattr(app, "api", None) is not None:
                app.api.token = new
        return new

    return server_url, token, refresh_cb


class TeamPickScreen(ModalScreen["str | None"]):
    """Pick a destination team."""

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

    def __init__(self, teams: list[str], current: str | None) -> None:
        super().__init__()
        self._teams = teams
        self._current = current

    def compose(self) -> ComposeResult:
        with Vertical(id="team-pick-box"):
            yield Static("Which team?")
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


def _fmt_size(n: int) -> str:
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.1f} GiB"
    if n >= 1024 ** 2:
        return f"{n / 1024 ** 2:.1f} MiB"
    return f"{n / 1024:.0f} KiB"


class UploadReviewScreen(ModalScreen["ReviewResult | None"]):
    """Confirm destination + options; Upload or Keep local.

    ``hold_label`` names the non-upload choice: "Keep local" for a fresh
    recording or an Outbox item, "Cancel" for an import (an arbitrary file
    has no recording folder to hold it in).  Escape picks it.
    """

    DEFAULT_CSS = """
    UploadReviewScreen {
        align: center middle;
    }
    #review-box {
        width: 76%;
        height: auto;
        max-height: 90%;
        border: round $primary;
        padding: 1 2;
        background: $surface;
    }
    #review-heading {
        text-style: bold;
        margin-bottom: 1;
    }
    .review-row {
        height: 3;
    }
    .review-row > Static {
        width: 12;
        height: 3;
        content-align: left middle;
    }
    #review-team, #review-title {
        width: 1fr;
    }
    #review-options > Button {
        width: 1fr;
        margin-right: 1;
    }
    #review-attachments {
        height: auto;
        color: $text-muted;
        margin: 1 0;
    }
    #review-actions > Button {
        margin-right: 1;
    }
    .toggle-on {
        background: $success;
        color: $text;
        border: round $success;
    }
    .toggle-personal-on {
        background: $warning;
        color: $text;
        border: round $warning;
    }
    """

    BINDINGS = [
        Binding("escape", "hold", "Keep local"),
        Binding("ctrl+r", "rescan", "Rescan attachments"),
    ]

    def __init__(
        self,
        *,
        name: str,
        audio_path: Path | None,
        team: str,
        teams: list[str],
        title: str | None,
        auto_label: bool,
        sync: bool,
        personal: bool,
        hold_label: str = "Keep local",
        show_attachments: bool = True,
    ) -> None:
        super().__init__()
        self._name = name
        self._audio_path = audio_path
        self._team = team
        self._teams = teams if team in teams else sorted({*teams, team})
        self._title = title or ""
        self._auto_label = auto_label
        self._sync = sync
        self._personal = personal
        self._hold_label = hold_label
        self._show_attachments = show_attachments

    def compose(self) -> ComposeResult:
        size = ""
        try:
            if self._audio_path is not None:
                size = f" ({_fmt_size(self._audio_path.stat().st_size)})"
        except OSError:
            pass
        with Vertical(id="review-box"):
            yield Static(f"Ready to upload — {self._name}{size}", id="review-heading")
            with Horizontal(classes="review-row"):
                yield Static("Team")
                yield Select(
                    [(t, t) for t in self._teams], value=self._team,
                    allow_blank=False, id="review-team",
                )
            with Horizontal(classes="review-row"):
                yield Static("Title")
                yield Input(value=self._title, placeholder="optional", id="review-title")
            with Horizontal(classes="review-row", id="review-options"):
                yield Button("Auto-label", id="review-auto-label")
                yield Button("Sync", id="review-sync")
                yield Button("Personal", id="review-personal")
            yield Static("", id="review-attachments")
            with Horizontal(classes="review-row", id="review-actions"):
                yield Button("", id="review-upload", variant="primary")
                yield Button(self._hold_label, id="review-hold")

    def on_mount(self) -> None:
        self._restyle()
        self._refresh_attachments()
        self.query_one("#review-upload", Button).focus()

    # ── rendering ──

    def _restyle(self) -> None:
        def style(btn_id: str, on: bool, cls: str = "toggle-on") -> None:
            # Same look as the Record tab's toggles (variant carries the colour).
            btn = self.query_one(btn_id, Button)
            btn.set_class(on, cls)
            if on:
                btn.variant = "warning" if cls == "toggle-personal-on" else "success"
            else:
                btn.variant = "default"

        style("#review-auto-label", self._auto_label)
        style("#review-sync", self._sync and not self._personal)
        style("#review-personal", self._personal, "toggle-personal-on")
        self.query_one("#review-sync", Button).disabled = self._personal
        self.query_one("#review-upload", Button).label = f"⬆ Upload to {self._team}"

    def _refresh_attachments(self) -> None:
        line = self.query_one("#review-attachments", Static)
        if not self._show_attachments:
            line.display = False
            return
        staged = attachments.staged_attachments()
        if staged:
            names = ", ".join(p.name for p in staged)
            line.update(f"Attachments: {len(staged)} staged ({names}) · ^r rescan")
        else:
            line.update(
                f"[dim]Attachments: none — drop files into "
                f"{attachments.staging_dir()} · ^r rescan[/]"
            )

    # ── events ──

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "review-team" and event.value is not Select.BLANK:
            self._team = str(event.value)
            self._restyle()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.action_upload()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "review-auto-label":
            self._auto_label = not self._auto_label
        elif bid == "review-sync":
            self._sync = not self._sync
        elif bid == "review-personal":
            self._personal = not self._personal
        elif bid == "review-upload":
            self.action_upload()
            return
        elif bid == "review-hold":
            self.action_hold()
            return
        self._restyle()

    def action_rescan(self) -> None:
        self._refresh_attachments()

    def _result(self, action: str) -> ReviewResult:
        title = self.query_one("#review-title", Input).value.strip() or None
        return ReviewResult(
            action=action, team=self._team, title=title,
            auto_label=self._auto_label,
            sync=self._sync and not self._personal,
            personal=self._personal,
        )

    def action_upload(self) -> None:
        self.dismiss(self._result("upload"))

    def action_hold(self) -> None:
        self.dismiss(self._result("hold" if self._hold_label == "Keep local" else "cancel"))
