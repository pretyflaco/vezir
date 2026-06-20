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
      [ctrl+d] delete session (admin or uploader; confirm dialog)
      [escape] back
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Select,
    Static,
)

from ..api import Session

log = logging.getLogger("vezir.client.tui.detail")


_PRESETS = [
    ("High Quality (Sonnet 4.6)", "high-quality"),
    ("Confidential (TEE)", "confidential"),
    ("Alternative (Kimi)", "alternative"),
]

# Summary languages with localized section headers in millet.  "auto" keeps
# the transcript's detected language (rewrites the primary summary); any other
# choice generates an ADDITIONAL <name>.summary.<lang>.md alongside it.
_SUMMARY_LANGUAGES = [
    ("Auto (detected)", "auto"),
    ("English", "en"),
    ("German", "de"),
    ("French", "fr"),
    ("Spanish", "es"),
    ("Turkish", "tr"),
    ("Persian (Farsi)", "fa"),
]


def _slugify(text: str) -> str:
    """Client-side folder slug, mirroring server ``config.sync_slug``.

    Lowercase, non-alphanumerics collapsed to hyphens, trimmed, capped at 60.
    The server re-slugifies (and validates) the value, so this is just a UX
    pre-fill / preview.
    """
    import re
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", (text or "").strip()).strip("-").lower()
    return slug[:60]


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


@dataclass
class SessionDeleted(Message):
    ok: bool
    detail: str = ""
    warning: str | None = None


class PresetPickerScreen(ModalScreen[tuple[str, str] | None]):
    """Modal: pick a summary preset + language for retry-summary, or cancel.

    Dismisses with ``(preset, language)`` on confirm, or ``None`` on cancel.
    ``language`` is ``"auto"`` (use the transcript's detected language and
    rewrite the primary summary) or a language code (generate an ADDITIONAL
    ``*.summary.<lang>.md`` artifact).
    """

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
    #preset-box Label { margin-top: 1; }
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
            yield Label("Summary language:")
            yield Select(
                options=_SUMMARY_LANGUAGES,
                value="auto",
                allow_blank=False,
                id="language-select",
            )
            with Horizontal():
                yield Button("Cancel", id="cancel-btn")
                yield Button("Retry", id="confirm-btn", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel-btn":
            self.dismiss(None)
        elif event.button.id == "confirm-btn":
            preset = self.query_one("#preset-select", Select).value
            language = self.query_one("#language-select", Select).value
            if preset is None:
                self.dismiss(None)
                return
            self.dismiss((str(preset), str(language or "auto")))


class SyncAsScreen(ModalScreen[str | None | object]):
    """Modal: choose the target folder for a "sync as" override, or cancel.

    Dismisses with:
      * ``""`` — auto-detect (current behavior: schedule/title detection),
      * ``"<slug>"`` — force this folder,
      * ``None`` — cancel (no sync).

    The free-text input is pre-filled with the session's title slug.  A
    ``Select`` offers quick choices: "Auto-detect" plus the title slug.  (The
    client can't see the team's full schedule list, so existing scheduled
    folders aren't enumerated here; the operator can type any slug.)
    """

    # Sentinel distinct from both "" (auto) and None (cancel) is unnecessary:
    # "" means auto, None means cancel.
    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    CSS = """
    SyncAsScreen { align: center middle; }
    #syncas-box {
        width: 64;
        max-width: 90%;
        height: auto;
        border: solid $accent;
        padding: 1 2;
        background: $surface;
    }
    #syncas-box Label { margin-top: 1; }
    #syncas-box Input { margin-top: 1; }
    """

    _AUTO = "\x00auto"  # Select value standing in for auto-detect ("")

    def __init__(self, title: str | None) -> None:
        super().__init__()
        self._title_slug = _slugify(title or "")

    def compose(self) -> ComposeResult:
        options = [("Auto-detect (schedule / title)", self._AUTO)]
        if self._title_slug:
            options.append((f"Title: {self._title_slug}", self._title_slug))
        with Vertical(id="syncas-box"):
            yield Label("[b]Sync to which folder?[/b]")
            yield Select(
                options=options,
                value=self._AUTO,
                allow_blank=False,
                id="syncas-select",
            )
            yield Label("Or type a custom folder slug:")
            yield Input(
                value="",
                placeholder=self._title_slug or "e.g. weekly-sync",
                id="syncas-input",
            )
            with Horizontal():
                yield Button("Cancel", id="cancel-btn")
                yield Button("Sync", id="confirm-btn", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel-btn":
            self.dismiss(None)
            return
        if event.button.id != "confirm-btn":
            return
        # A non-empty custom input wins over the Select.
        custom = self.query_one("#syncas-input", Input).value.strip()
        if custom:
            self.dismiss(_slugify(custom))
            return
        sel = self.query_one("#syncas-select", Select).value
        if sel is None or sel == self._AUTO:
            self.dismiss("")  # auto-detect
        else:
            self.dismiss(str(sel))


class ConfirmDeleteScreen(ModalScreen[bool]):
    """Modal: confirm a destructive, irreversible session deletion.

    Dismisses with ``True`` on confirm, ``False`` on cancel.  ``escape``
    cancels.  The confirm button is the non-default so an accidental Enter
    doesn't delete.
    """

    BINDINGS = [Binding("escape", "dismiss(False)", "Cancel")]

    CSS = """
    ConfirmDeleteScreen { align: center middle; }
    #confirm-del-box {
        width: 64;
        max-width: 90%;
        height: auto;
        border: solid $error;
        padding: 1 2;
        background: $surface;
    }
    #confirm-del-box Label { margin-top: 1; }
    """

    def __init__(self, session_id: str, title: str | None) -> None:
        super().__init__()
        self._session_id = session_id
        self._title = title

    def compose(self) -> ComposeResult:
        name = self._title or self._session_id
        with Vertical(id="confirm-del-box"):
            yield Label("[b]Delete this session?[/b]")
            yield Label(f"  {name}")
            yield Label(f"  id: {self._session_id}")
            yield Label(
                "[red]This permanently removes the session and its "
                "artifacts. This cannot be undone.[/red]"
            )
            with Horizontal():
                yield Button("Cancel", id="cancel-btn", variant="primary")
                yield Button("Delete", id="confirm-btn", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm-btn":
            self.dismiss(True)
        else:
            self.dismiss(False)


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
        Binding("f", "open_folder", "Open folder"),
        Binding("d", "copy_path", "Copy path"),
        Binding("ctrl+d", "delete_session", "Delete"),
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
            yield Button("[^d] Delete", id="delete-btn", variant="error")
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

    def _on_preset_picked(self, choice: tuple[str, str] | None) -> None:
        if choice is None:
            return
        preset, language = choice
        self._action_worker(
            "retry summary", "retry_summary",
            preset=preset, language=language,
        )

    def action_sync_now(self) -> None:
        title = self.session.title if self.session else None
        self.app.push_screen(
            SyncAsScreen(title),
            self._on_sync_as_picked,
        )

    def _on_sync_as_picked(self, choice: str | None | object) -> None:
        # None = cancelled; "" = auto-detect; "<slug>" = explicit folder.
        if choice is None:
            return
        meeting_type = str(choice)
        if meeting_type:
            self._action_worker(
                "sync", "sync_now", meeting_type=meeting_type,
            )
        else:
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

    def action_delete_session(self) -> None:
        title = self.session.title if self.session else None
        self.app.push_screen(
            ConfirmDeleteScreen(self.session_id, title),
            self._on_delete_confirmed,
        )

    def _on_delete_confirmed(self, confirmed: bool | None) -> None:
        if not confirmed:
            return
        self.notify("Deleting session...", severity="information", timeout=3)
        self._delete_worker()

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

    def _resolve_local_dir(self) -> Path | None:
        """Resolve the local recording/pull directory for this session.

        ``session.team_id`` is the server **UUID**, but recordings live on
        disk under the team **slug** (``~/vezir-meetings/<slug>/``).  Map
        UUID -> slug via the app's memberships so the fast path hits the
        right team dir; ``find_local_session_dir`` also has a global scan
        fallback that covers cases where the mapping is unavailable.
        """
        from ..pull import find_local_session_dir
        team_id = self.session.team_id if self.session else None
        try:
            team_id = self.app.team_slug_for(team_id)
        except Exception:
            pass
        return find_local_session_dir(self.session_id, team_id)

    def action_open_folder(self) -> None:
        """Open the local meeting artifacts folder in the OS file manager.

        If the folder exists but is missing artifacts the server has (e.g. a
        local recording whose auto-download never completed because the TUI
        was closed mid-processing), download them first so the folder opens
        complete.  This makes "open folder" self-healing again — the
        upload-time session.json bridge (0.7.18) otherwise let an
        artifact-less recording folder be "found" and opened as-is.
        """
        local = self._resolve_local_dir()
        if local is None:
            self._offer_pull()
            return
        # Self-heal: fetch any artifacts the server has but the folder lacks,
        # then open.  Done in a worker so the UI doesn't block on the network.
        if self.session is not None:
            from ..pull import missing_server_artifacts
            try:
                missing = missing_server_artifacts(self.session, local)
            except Exception:
                missing = []
            if missing:
                self.notify(
                    f"Folder incomplete — downloading {len(missing)} "
                    "artifact(s)...",
                    severity="information", timeout=4,
                )
                self._heal_and_open_worker(local)
                return
        self._open_in_file_manager(local)

    def _open_in_file_manager(self, local: Path) -> None:
        import shutil
        import subprocess
        import sys
        if sys.platform == "darwin":
            cmd = ["open", str(local)]
        elif shutil.which("xdg-open"):
            cmd = ["xdg-open", str(local)]
        else:
            self.notify(f"No file manager found.  Path: {local}", severity="warning", timeout=8)
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

    @work(thread=True, exclusive=True, group="pull")
    def _heal_and_open_worker(self, local: Path) -> None:
        """Download missing artifacts into an existing folder, then open it."""
        from ..artifacts import download_session_artifacts
        try:
            download_session_artifacts(self.app.api, self.session, local)
        except Exception as exc:
            self.app.call_from_thread(
                self.notify, f"Artifact download failed: {exc}",
                severity="error",
            )
            return
        self.app.call_from_thread(self._open_in_file_manager, local)

    def action_copy_path(self) -> None:
        """Copy the local meeting artifacts path to the clipboard."""
        local = self._resolve_local_dir()
        if local is None:
            self._offer_pull()
            return
        try:
            self.app.copy_to_clipboard(str(local))
        except Exception as exc:
            self.notify(f"Copy failed: {exc}", severity="error")
            return
        self.notify(f"Copied: {local}", severity="information", timeout=4)

    def _offer_pull(self) -> None:
        """Pull the session artifacts when no local directory exists."""
        self.notify(
            "No local folder — pulling artifacts...",
            severity="information",
            timeout=4,
        )
        self._pull_worker(self.session_id)

    @work(thread=True, exclusive=True, group="pull")
    def _pull_worker(self, session_id: str) -> None:
        """Pull a single session's artifacts in the background."""
        from ..pull import pull_team_sessions
        try:
            pulled = pull_team_sessions(
                self.app.api,
                session_id=session_id,
            )
            if pulled > 0:
                self.app.call_from_thread(
                    self.notify,
                    "Artifacts pulled.  Press [d] to copy path or [f] to open.",
                    severity="information",
                    timeout=6,
                )
            else:
                self.app.call_from_thread(
                    self.notify,
                    "No artifacts available for this session.",
                    severity="warning",
                    timeout=6,
                )
        except Exception as exc:
            self.app.call_from_thread(
                self.notify,
                f"Pull failed: {exc}",
                severity="error",
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
        elif bid == "delete-btn":
            self.action_delete_session()

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

    @work(thread=True, exclusive=True, group="detail-delete")
    def _delete_worker(self) -> None:
        result = self.app.api.delete_session(self.session_id)
        if result.is_ok():
            warning = None
            if isinstance(result.ok, dict):
                warning = result.ok.get("warning")
            self.post_message(SessionDeleted(ok=True, warning=warning))
        else:
            self.post_message(SessionDeleted(
                ok=False, detail=result.error_message(),
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

    def on_session_deleted(self, message: SessionDeleted) -> None:
        if message.ok:
            self.notify("Session deleted.", severity="information")
            if message.warning:
                self.notify(message.warning, severity="warning", timeout=10)
            # The session no longer exists: leave the detail screen and let
            # the sessions list refresh on its own.
            self.app.pop_screen()
        else:
            self.notify(
                f"Delete failed: {message.detail}",
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
            "needs_labeling", "done", "error", "sync_failed",
        )
