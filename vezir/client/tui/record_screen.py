"""Record screen: start/stop/pause/resume + upload.

Calls into ``meet_record.capture`` directly (the library exposes
``pause()`` / ``resume()`` as of millet-record 0.3.0; we verified
this in the spike at the start of the v0.2.0-tui work).  No subprocess
invocation of ``millet record`` -- that path doesn't expose pause.

State machine (mirrors RecordingState in gui.py):

  idle      no recording in flight
  recording RecordingSession is active and not paused
  paused    RecordingSession.pause() was called; can resume or stop
  draining  user pressed stop; ffmpeg/sidecar flushing remaining samples
  compressing WAV -> OGG re-encoding (only when --compress)
  uploading multipart upload in flight
  queued    server acknowledged upload
  done      server reached terminal status (also: needs_labeling, error)

The screen does work() chains:

  * ``_record_worker`` -- builds a RecordingSession, calls .start(),
    yields each second to update the timer label until stopped.
  * ``_upload_worker`` -- compresses (optionally) then uploads via
    vezir.client.uploader.upload(), reporting progress as a posted
    message back to the screen.
  * ``_poll_worker`` -- after upload, polls /api/sessions/{id} until
    terminal status, surfacing transitions in the status bar.

Worker threads (Textual @work(thread=True)) are used everywhere
blocking I/O happens so the event loop stays responsive.
"""
from __future__ import annotations

import logging
import re
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DirectoryTree,
    Input,
    Label,
    OptionList,
    Select,
    Static,
)
from textual.widgets.option_list import Option

from ... import config
from ..config import load_client_prefs, save_client_prefs


def _vezir_version() -> str:
    """Return the running vezir client version (lazy to avoid import cycles)."""
    try:
        from vezir import __version__
        return __version__
    except Exception:
        return "?"

log = logging.getLogger("vezir.client.tui.record")


_PRESET_OPTIONS = [
    ("High Quality", "high-quality"),
    ("Confidential", "confidential"),
    ("Alternative", "alternative"),
]


def _fmt_elapsed(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def _fmt_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    return f"{n / (1024 * 1024 * 1024):.1f} GB"


# ─── Messages posted from workers back to the screen ─────────────────────────
#
# v0.7.0: every message carries a ``gen`` (generation) field.  When the
# user starts a new recording, the generation counter increments.
# Message handlers discard messages whose gen < self._gen, preventing
# stale poll/upload threads from clobbering state of a newer session.


@dataclass
class TimerTick(Message):
    elapsed: float
    bytes: int
    paused: bool
    gen: int = 0


@dataclass
class RecorderFailed(Message):
    reason: str
    gen: int = 0


@dataclass
class RecorderFinished(Message):
    audio_path: Path
    gen: int = 0


@dataclass
class UploadProgress(Message):
    sent: int
    total: int
    rate: float
    gen: int = 0


@dataclass
class UploadFinished(Message):
    session_id: str
    gen: int = 0


@dataclass
class UploadFailed(Message):
    error: str
    audio_path: Path | None
    gen: int = 0


@dataclass
class ServerStatus(Message):
    status: str
    extra: str = ""
    gen: int = 0


@dataclass
class AudioLevel(Message):
    """Real-time audio levels from the recording chunk (v0.7.0+)."""
    mic_rms: float
    sys_rms: float
    mic_peak: float
    sys_peak: float
    gen: int = 0


@dataclass
class SessionUploadComplete(Message):
    """Posted by ``_poll_worker`` when the server reaches a terminal
    status for a freshly uploaded session.  Bubbles up to MainScreen
    so it can refresh the Sessions tab and toast the user.

    Background: PR9 (2026-05-24) — user reported that a session
    recorded in the TUI didn't appear in the Sessions list until the
    TUI was restarted.  Root cause: SessionsBody only refreshed on
    its own ``on_mount``, never reacting to upload completion.
    """
    session_id: str
    status: str  # "done" | "error" | "needs_labeling"


# ─── Import file picker (v0.5.0+) ───────────────────────────────────────────


_AUDIO_EXTS = {".wav", ".ogg"}

_MEETING_TS_RE = re.compile(r"meeting-(\d{8})-(\d{6})")


def _recordings_base() -> Path:
    """The base dir that holds all teams' recordings: ``~/vezir-meetings/``.

    ``config.recordings_dir()`` returns ``<base>/<team>``; its parent is the
    base that contains every team's recordings.  Honors ``VEZIR_RECORD_DIR``.
    """
    try:
        return config.recordings_dir().parent
    except Exception:
        return Path.home() / "vezir-meetings"


def _recording_sort_key(path: Path) -> tuple[float, float]:
    """Newest-first sort key for a recording file.

    Prefer the ``meeting-YYYYMMDD-HHMMSS`` timestamp parsed from the session
    directory name (stable, matches the displayed date); fall back to the
    file mtime when the name can't be parsed.
    """
    for part in (path.parent.name, path.name):
        m = _MEETING_TS_RE.search(part)
        if m:
            try:
                dt = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
                return (dt.timestamp(), dt.timestamp())
            except ValueError:
                pass
    try:
        mt = path.stat().st_mtime
    except OSError:
        mt = 0.0
    return (mt, mt)


def _recording_label(path: Path, base: Path) -> str:
    """Human row label: ``<team>/<session>  ·  <size>  ·  <date>``."""
    try:
        rel = path.relative_to(base)
        # rel is usually <team>/<session-dir>/<file>; show team/session.
        parts = rel.parts
        descr = "/".join(parts[:-1]) if len(parts) > 1 else rel.name
    except ValueError:
        descr = path.parent.name
    try:
        size = _fmt_bytes(path.stat().st_size)
    except OSError:
        size = "?"
    when = ""
    m = _MEETING_TS_RE.search(path.parent.name) or _MEETING_TS_RE.search(path.name)
    if m:
        try:
            dt = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
            when = dt.strftime("%Y-%m-%d %H:%M")
        except ValueError:
            when = ""
    bits = [descr or path.name, size]
    if when:
        bits.append(when)
    return "  ·  ".join(bits)


def _scan_recordings(base: Path) -> list[Path]:
    """All ``.wav``/``.ogg`` recordings under *base*, newest-first.

    Recurses (teams → session dirs → audio), skips dot-directories, dedupes.
    Resilient to a missing base (returns []).
    """
    if not base.is_dir():
        return []
    found: set[Path] = set()
    for ext in _AUDIO_EXTS:
        for p in base.rglob(f"*{ext}"):
            if any(part.startswith(".") for part in p.relative_to(base).parts):
                continue
            if p.is_file():
                found.add(p)
    return sorted(found, key=_recording_sort_key, reverse=True)


class _AudioOnlyDirectoryTree(DirectoryTree):
    """DirectoryTree that hides non-audio non-directory entries.

    Shows directories so the user can navigate; filters files to
    .wav and .ogg only so the picker is uncluttered.  Hidden files
    (dot-prefixed) are also hidden, which matches typical OS file
    pickers and prevents the tree from being dominated by .cache /
    .config / .git noise.
    """

    def filter_paths(self, paths):  # type: ignore[override]
        out = []
        for p in paths:
            try:
                name = p.name
            except Exception:
                continue
            if name.startswith("."):
                continue
            if p.is_dir():
                out.append(p)
                continue
            if p.suffix.lower() in _AUDIO_EXTS:
                out.append(p)
        return out


class ImportScreen(ModalScreen["Path | None"]):
    """Modal picker for selecting an audio file (.wav/.ogg) to upload.

    Default view: a flat, scrollable, newest-first list of *every* recording
    under ``~/vezir-meetings/`` (all teams) — so the user can see and pick any
    of their recordings, not just one folder.  A "Browse files…" fallback
    (``b``) opens a directory tree rooted at ``~`` for importing an arbitrary
    ``.wav``/``.ogg`` from elsewhere.

    Dismisses with the selected ``Path`` on confirmation, or ``None`` on
    cancel.  Only ``.wav`` and ``.ogg`` are accepted; an invalid browse
    selection keeps the modal open with an inline hint.
    """

    DEFAULT_CSS = """
    ImportScreen {
        align: center middle;
    }
    #picker-box {
        width: 80%;
        height: 80%;
        border: round $primary;
        padding: 1 2;
        background: $surface;
    }
    #picker-title {
        height: 1;
        margin-bottom: 1;
        text-style: bold;
    }
    #picker-list {
        height: 1fr;
    }
    #picker-tree {
        height: 1fr;
    }
    #picker-empty {
        height: 1fr;
        color: $text-muted;
    }
    #picker-hint {
        height: 1;
        margin-top: 1;
        color: $text-muted;
    }
    #picker-hint.error {
        color: $error;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "select", "Select"),
        Binding("b", "toggle_browse", "Browse files"),
    ]

    def __init__(self, browse_start: Path | None = None) -> None:
        super().__init__()
        self._browse_start = browse_start or Path.home()
        self._recordings: list[Path] = _scan_recordings(_recordings_base())
        self._browsing = False

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-box"):
            yield Static("", id="picker-title")
            if self._recordings:
                ol = OptionList(id="picker-list")
                base = _recordings_base()
                for p in self._recordings:
                    ol.add_option(Option(_recording_label(p, base), id=str(p)))
                yield ol
            else:
                yield Static(
                    "No recordings found under ~/vezir-meetings/.\n\n"
                    "Press 'b' to browse the filesystem for a .wav/.ogg file.",
                    id="picker-empty",
                )
            yield Static("", id="picker-hint")

    def on_mount(self) -> None:
        self._refresh_title()
        if self._recordings:
            try:
                ol = self.query_one("#picker-list", OptionList)
                # Highlight the first (newest) option so Enter works immediately
                # without requiring an initial arrow-key press.
                if ol.option_count and ol.highlighted is None:
                    ol.highlighted = 0
                ol.focus()
            except Exception:
                pass

    def _refresh_title(self) -> None:
        try:
            title = self.query_one("#picker-title", Static)
        except Exception:
            return
        if self._browsing:
            title.update(
                "Browse for audio (.wav/.ogg)  —  Enter: select  ·  "
                "b: back to recordings  ·  Esc: cancel"
            )
        else:
            n = len(self._recordings)
            title.update(
                f"Import a recording ({n})  —  Enter: select  ·  "
                "b: browse files  ·  Esc: cancel"
            )

    # ── flat recordings list ──
    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        if event.option.id:
            self.dismiss(Path(event.option.id))

    # ── browse-mode directory tree ──
    def action_toggle_browse(self) -> None:
        box = self.query_one("#picker-box", Vertical)
        if not self._browsing:
            self._browsing = True
            for wid in ("#picker-list", "#picker-empty"):
                try:
                    self.query_one(wid).remove()
                except Exception:
                    pass
            tree = _AudioOnlyDirectoryTree(str(self._browse_start), id="picker-tree")
            box.mount(tree, after=self.query_one("#picker-title", Static))
            tree.focus()
        else:
            self._browsing = False
            try:
                self.query_one("#picker-tree", _AudioOnlyDirectoryTree).remove()
            except Exception:
                pass
            base = _recordings_base()
            if self._recordings:
                ol = OptionList(id="picker-list")
                for p in self._recordings:
                    ol.add_option(Option(_recording_label(p, base), id=str(p)))
                box.mount(ol, after=self.query_one("#picker-title", Static))
                if ol.option_count and ol.highlighted is None:
                    ol.highlighted = 0
                ol.focus()
            else:
                box.mount(
                    Static(
                        "No recordings found under ~/vezir-meetings/.\n\n"
                        "Press 'b' to browse the filesystem for a .wav/.ogg file.",
                        id="picker-empty",
                    ),
                    after=self.query_one("#picker-title", Static),
                )
        self._refresh_title()

    def on_directory_tree_file_selected(
        self, event: DirectoryTree.FileSelected
    ) -> None:
        path = Path(event.path)
        if path.suffix.lower() not in _AUDIO_EXTS:
            hint = self.query_one("#picker-hint", Static)
            hint.add_class("error")
            hint.update(
                f"unsupported file type {path.suffix or '(none)'}; "
                f"expected .wav or .ogg"
            )
            return
        self.dismiss(path)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_select(self) -> None:
        """Enter handling for whichever pane is active."""
        if self._browsing:
            try:
                tree = self.query_one("#picker-tree", _AudioOnlyDirectoryTree)
            except Exception:
                return
            node = tree.cursor_node
            if node is None or node.data is None:
                return
            path = Path(node.data.path)
            if path.is_file() and path.suffix.lower() in _AUDIO_EXTS:
                self.dismiss(path)
            # directory: let the tree's own Enter expand it.
            return
        # flat list: select the highlighted recording.
        try:
            ol = self.query_one("#picker-list", OptionList)
        except Exception:
            return
        idx = ol.highlighted
        if idx is None:
            return
        opt = ol.get_option_at_index(idx)
        if opt and opt.id:
            self.dismiss(Path(opt.id))


# ─── Screen ──────────────────────────────────────────────────────────────────


class RecordBody(Vertical):
    """The record-and-upload pane (used inside MainScreen's TabbedContent)."""

    BINDINGS = [
        Binding("ctrl+space", "toggle_record", "Start/Stop"),
        Binding("ctrl+p", "toggle_pause", "Pause/Resume"),
        Binding("ctrl+x", "toggle_personal", "Personal"),
        Binding("ctrl+u", "import_file", "Import"),
    ]

    DEFAULT_CSS = """
    RecordBody {
        padding: 1 2;
    }

    /* ── title row: full-width input, no label ── */
    #title-row {
        height: 3;
        margin-bottom: 1;
    }
    #title-row Input {
        width: 1fr;
    }

    /* ── shared 4-column grid for toggles + controls rows ── */
    #toggles-row,
    #controls-row {
        height: 3;
        margin-bottom: 1;
        align: left middle;
    }
    #toggles-row > *,
    #controls-row > * {
        width: 1fr;
        height: 3;
        margin-right: 1;
        border: round $primary;
        content-align: center middle;
    }
    #toggles-row > :last-child,
    #controls-row > :last-child {
        margin-right: 0;
    }

    /* ── preset Select: strip our outer border so its inner SelectCurrent
       chrome (which has its own border + the ▼ glyph) renders the value
       text without double-bordering collapse.  v0.4.2 forced height: 3
       on every cell, but Select's outer border + inner SelectCurrent
       border consumed 4 rows of chrome on a 3-row cell, hiding the text
       (issue reported in vezir-data/errors/tui_0.4.2.png). */
    #toggles-row Select {
        border: none;
        padding: 0;
    }

    /* ── toggle buttons: green when on, default when off ── */
    .toggle-on {
        background: $success;
        color: $text;
        border: round $success;
    }
    /* Personal uses warning color when on (privacy-mode indicator) */
    .toggle-personal-on {
        background: $warning;
        color: $text;
        border: round $warning;
    }

    /* ── timer label: read-only display cell ── */
    #timer-label {
        text-style: bold;
        color: $accent;
        padding: 0 1;
    }

    /* ── recording / paused state overrides ── */
    Button.recording {
        background: $error;
        color: $text;
        border: round $error;
    }
    Button.paused {
        background: $warning;
        color: $text;
        border: round $warning;
    }

    #level-row {
        height: 1;
        margin-bottom: 1;
        color: $text-muted;
    }
    #status-line {
        height: 1;
        margin-bottom: 1;
        color: $text-muted;
    }
    #error-line {
        color: $error;
        height: auto;
        min-height: 0;
    }
    #version-line {
        height: 1;
        color: $text-muted;
        text-style: italic;
        text-align: right;
    }
    """

    # ── reactive state ──
    # init=False so watchers don't fire before compose() mounts the
    # backing widgets.  Default values are still respected.
    is_recording: reactive[bool] = reactive(False, init=False)
    is_paused: reactive[bool] = reactive(False, init=False)
    is_uploading: reactive[bool] = reactive(False, init=False)
    elapsed_seconds: reactive[float] = reactive(0.0, init=False)
    file_bytes: reactive[int] = reactive(0, init=False)
    status_text: reactive[str] = reactive("ready", init=False)
    error_text: reactive[str] = reactive("", init=False)

    def __init__(self) -> None:
        super().__init__()
        # Library-level session; None when not recording.
        self._session = None  # type: ignore[assignment]
        # Path of the last finished recording (used by upload).
        self._last_audio_path: Path | None = None
        # Last upload session_id (for poll worker).
        self._last_session_id: str | None = None
        # Persisted preferences cache.
        self._prefs = load_client_prefs()
        # v0.7.0: session generation counter.  Incremented on each
        # _start_recording(); message handlers discard stale messages.
        self._gen: int = 0
        # v0.7.0: audio level spectrometer state.
        self._mic_history: deque[float] = deque([0.0] * 12, maxlen=12)
        self._sys_history: deque[float] = deque([0.0] * 12, maxlen=12)
        self._silence_since: float = 0.0

    @classmethod
    def body_widget(cls) -> RecordBody:
        """Factory used by MainScreen's TabbedContent."""
        return cls()

    # ── compose ──

    def compose(self) -> ComposeResult:
        with Horizontal(id="title-row"):
            yield Input(placeholder="optional meeting title", id="title-input")

        with Horizontal(id="toggles-row"):
            yield Button("Auto-label", id="auto-label-btn")
            yield Button("Sync", id="sync-btn")
            yield Button("Personal", id="personal-btn")
            yield Select(
                options=_PRESET_OPTIONS,
                value=self._prefs.get("preset", "high-quality"),
                allow_blank=False,
                id="preset",
            )

        with Horizontal(id="controls-row"):
            yield Button("● Record", id="record-btn", variant="error")
            yield Button("⏸ Pause", id="pause-btn", disabled=True)
            yield Label(
                "00:00:00",
                id="timer-label",
                classes="timer",
            )
            yield Button("⬆ Upload", id="upload-btn")

        yield Static(
            "[dim]🎤 ▁▁▁▁▁▁▁▁▁▁▁▁  🔊 ▁▁▁▁▁▁▁▁▁▁▁▁  ― idle[/]",
            id="level-row",
        )
        yield Static(f"Status: {self.status_text}", id="status-line")
        yield Static("", id="error-line", classes="error")
        yield Static(f"v{_vezir_version()}", id="version-line")

    # ── mount: apply initial toggle states from prefs ──

    def on_mount(self) -> None:
        """Style toggle-buttons to match persisted preferences."""
        al = self.query_one("#auto-label-btn", Button)
        self._style_toggle(al, bool(self._prefs.get("auto_label", True)))
        sy = self.query_one("#sync-btn", Button)
        self._style_toggle(sy, bool(self._prefs.get("sync", True)))
        # Personal always starts off (not persisted).
        pe = self.query_one("#personal-btn", Button)
        self._style_toggle(pe, False, personal=True)

    # ── reactive watchers ──

    def watch_status_text(self, value: str) -> None:
        try:
            line = self.query_one("#status-line", Static)
        except Exception:
            return  # not mounted yet
        line.update(f"Status: {value}")

    def watch_error_text(self, value: str) -> None:
        try:
            line = self.query_one("#error-line", Static)
        except Exception:
            return
        line.update(value)

    def watch_is_recording(self, value: bool) -> None:
        try:
            btn = self.query_one("#record-btn", Button)
            pause_btn = self.query_one("#pause-btn", Button)
        except Exception:
            return
        if value:
            btn.label = "■ Stop"
            btn.variant = "warning"
            btn.add_class("recording")
            pause_btn.disabled = False
        else:
            btn.label = "● Record"
            btn.variant = "error"
            btn.remove_class("recording")
            pause_btn.disabled = True
            pause_btn.label = "⏸ Pause"

    def watch_is_paused(self, value: bool) -> None:
        try:
            pause_btn = self.query_one("#pause-btn", Button)
        except Exception:
            return
        if value:
            pause_btn.label = "▶ Resume"
            pause_btn.add_class("paused")
        else:
            pause_btn.label = "⏸ Pause"
            pause_btn.remove_class("paused")

    def watch_elapsed_seconds(self, value: float) -> None:
        self._refresh_timer()

    def watch_file_bytes(self, value: int) -> None:
        self._refresh_timer()

    def _refresh_timer(self) -> None:
        try:
            lbl = self.query_one("#timer-label", Label)
        except Exception:
            return
        suffix = "  (paused)" if self.is_paused else ""
        elapsed_str = _fmt_elapsed(self.elapsed_seconds)
        # Hide byte counter until real audio data has accumulated.
        # WAV header alone is ~44 B; show only once we exceed 4 KB.
        if self.file_bytes >= 4096:
            bytes_str = f"  {_fmt_bytes(self.file_bytes)}"
        else:
            bytes_str = ""
        lbl.update(f"{elapsed_str}{bytes_str}{suffix}")

    # ── button + binding handlers ──

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "record-btn":
            self.action_toggle_record()
        elif bid == "pause-btn":
            self.action_toggle_pause()
        elif bid == "upload-btn":
            self.action_import_file()
        elif bid == "auto-label-btn":
            self._toggle_pref_button(event.button, "auto_label")
            event.stop()
        elif bid == "sync-btn":
            self._toggle_pref_button(event.button, "sync")
            event.stop()
        elif bid == "personal-btn":
            self.action_toggle_personal()
            event.stop()

    # ── toggle-button helpers ──

    def _toggle_pref_button(self, btn: Button, pref_key: str) -> None:
        """Flip a preference toggle-button on/off, persist, and restyle."""
        current = bool(self._prefs.get(pref_key, True))
        new_val = not current
        self._prefs[pref_key] = new_val
        save_client_prefs(self._prefs)
        self._style_toggle(btn, new_val)

    @staticmethod
    def _style_toggle(btn: Button, is_on: bool, *, personal: bool = False) -> None:
        """Apply visual on/off styling to a toggle-button."""
        if personal:
            if is_on:
                btn.variant = "warning"
                btn.add_class("toggle-personal-on")
                btn.remove_class("toggle-on")
            else:
                btn.variant = "default"
                btn.remove_class("toggle-personal-on")
                btn.remove_class("toggle-on")
        else:
            if is_on:
                btn.variant = "success"
                btn.add_class("toggle-on")
            else:
                btn.variant = "default"
                btn.remove_class("toggle-on")

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "preset":
            self._prefs["preset"] = str(event.value)
            save_client_prefs(self._prefs)

    def action_toggle_record(self) -> None:
        if self.is_recording:
            self._stop_recording()
        else:
            self._start_recording()

    def action_toggle_pause(self) -> None:
        if not self.is_recording or self._session is None:
            return
        try:
            if self.is_paused:
                self._session.resume()
                self.is_paused = False
                self.status_text = "recording"
            else:
                self._session.pause()
                self.is_paused = True
                self.status_text = "paused"
        except Exception as exc:
            self.error_text = f"pause/resume failed: {exc}"

    def action_toggle_personal(self) -> None:
        """Toggle personal mode.  When personal is on, force sync off
        and disable the sync button (server enforces this anyway; we
        keep the UI honest).  When personal is off, restore the
        persisted sync preference."""
        btn = self.query_one("#personal-btn", Button)
        is_on = "toggle-personal-on" not in btn.classes
        self._style_toggle(btn, is_on, personal=True)

        sync_btn = self.query_one("#sync-btn", Button)
        if is_on:
            # Force sync off and disable.
            self._prefs["sync"] = False
            save_client_prefs(self._prefs)
            self._style_toggle(sync_btn, False)
            sync_btn.disabled = True
        else:
            # Restore persisted sync pref and re-enable.
            sync_btn.disabled = False
            restored = bool(self._prefs.get("sync", True))
            self._style_toggle(sync_btn, restored)

    def action_import_file(self) -> None:
        """Open the import picker to select an existing .wav/.ogg recording
        and upload it through the same pipeline used by in-TUI recordings.

        v0.8.7: the picker shows a flat, newest-first list of every recording
        under ``~/vezir-meetings/`` (all teams) so the user can pick any of
        their recordings, plus a "browse files" fallback (``b``) for an
        arbitrary path.  (Earlier versions rooted a directory tree at the last
        imported folder — a leaf session dir with one file and no way to
        navigate out.)  Auto-upload on Stop is unchanged.  CLI alternative:
        ``vezir upload <path>``.
        """
        if self.is_uploading:
            self.error_text = "upload in progress; wait for it to finish"
            return
        if self.is_recording:
            self.error_text = "stop the current recording before importing"
            return

        # Browse fallback starts at the last browsed dir (if still valid), else
        # the recordings base, else home.  Never a leaf session dir as the
        # primary view — the flat list always covers all recordings.
        browse = self._prefs.get("last_import_dir")
        browse_start = Path(browse) if browse else _recordings_base()
        if not browse_start.is_dir():
            browse_start = Path.home()

        def _after_pick(picked: Path | None) -> None:
            if picked is None:
                return  # silent cancel
            # Remember the browsed directory only (so "Browse" reopens there);
            # the flat list is always rebuilt from the recordings base.
            try:
                self._prefs["last_import_dir"] = str(picked.parent)
                save_client_prefs(self._prefs)
            except Exception:
                pass  # non-fatal
            self.error_text = ""
            self.status_text = f"importing {picked.name}"
            self._kick_upload(picked)

        self.app.push_screen(ImportScreen(browse_start), _after_pick)

    # ── recording lifecycle ──

    def _start_recording(self) -> None:
        # Lazy-import millet-record so a system that only wants to
        # list sessions doesn't pay for the pulseaudio / sidecar import.
        try:
            from millet_record.capture import check_prerequisites, create_session
        except ImportError as exc:
            self.error_text = (
                f"millet-record not installed: {exc}. "
                f"pip install millet-record."
            )
            return

        issues = check_prerequisites()
        if issues:
            self.error_text = " | ".join(issues)
            return

        try:
            output_dir = config.recordings_dir()
            output_dir.mkdir(parents=True, exist_ok=True)
            self._session = create_session(output_dir=str(output_dir))
        except Exception as exc:
            self.error_text = f"could not create session: {exc}"
            return

        # v0.7.0: bump generation so stale messages from the previous
        # session's upload/poll workers are discarded.
        self._gen += 1
        self._mic_history = deque([0.0] * 12, maxlen=12)
        self._sys_history = deque([0.0] * 12, maxlen=12)
        self._silence_since = 0.0
        self.error_text = ""
        self.elapsed_seconds = 0.0
        self.file_bytes = 0
        self.status_text = "starting recorder"
        self._record_worker(self._session, self._gen)
        self._level_worker(self._session, self._gen)

    def _stop_recording(self) -> None:
        if self._session is None:
            return
        self.status_text = "draining (flushing audio buffer)"
        self._stop_worker(self._session, self._gen)

    @work(thread=True, exclusive=True, group="recorder")
    def _record_worker(self, session, gen: int) -> None:
        """Drive the recorder: start, then poll status every second."""
        from textual.worker import get_current_worker
        worker = get_current_worker()
        try:
            session.start()
        except Exception as exc:
            self.post_message(RecorderFailed(reason=str(exc), gen=gen))
            return
        # Notify the UI to flip into recording mode.
        self.post_message(ServerStatus(status="recording", gen=gen))
        # Tick loop: poll the session's own status() and emit TimerTick
        # messages.  Stops when the session is no longer alive (i.e.
        # _stop_worker has finalized it).
        while not worker.is_cancelled:
            try:
                st = session.status()
            except Exception as exc:
                self.post_message(RecorderFailed(reason=f"status() raised: {exc}", gen=gen))
                return
            self.post_message(TimerTick(
                elapsed=st.elapsed_seconds,
                bytes=st.file_size_bytes,
                paused=st.paused,
                gen=gen,
            ))
            if st.failed:
                self.post_message(RecorderFailed(
                    reason=st.fail_reason or "unknown recorder error",
                    gen=gen,
                ))
                return
            if not st.is_alive and not st.paused:
                pass
            worker.cancelled_event.wait(1.0)

    @work(thread=True, exclusive=True, group="recorder-stop")
    def _stop_worker(self, session, gen: int) -> None:
        try:
            out = session.stop()
        except Exception as exc:
            self.post_message(RecorderFailed(reason=f"stop() raised: {exc}", gen=gen))
            return
        self.post_message(RecorderFinished(audio_path=out, gen=gen))

    # ── audio level spectrometer (v0.7.0+) ──

    @work(thread=True, exclusive=True, group="level")
    def _level_worker(self, session, gen: int) -> None:
        """Read audio levels from the recording chunk at ~15 FPS."""
        from textual.worker import get_current_worker

        from ..audio import read_chunk_levels

        worker = get_current_worker()

        # Wait for session.start() to be called by the parallel
        # _record_worker.  The level worker may be scheduled first;
        # without this spin-wait it would see is_alive=False and exit
        # immediately (the v0.7.0 spectrometer-shows-idle bug).
        for _ in range(50):  # up to ~3.3 seconds
            if gen < self._gen or worker.is_cancelled:
                return
            try:
                st = session.status()
                if st.is_alive:
                    break
            except Exception:
                pass
            worker.cancelled_event.wait(0.066)

        while not worker.is_cancelled:
            if gen < self._gen:
                return
            try:
                st = session.status()
            except Exception:
                return
            if not st.is_alive and not st.paused:
                return
            chunk = getattr(session, "_current_chunk", None)
            if chunk is None or st.paused:
                worker.cancelled_event.wait(0.066)
                continue
            try:
                lvl = read_chunk_levels(chunk)
                self.post_message(AudioLevel(
                    mic_rms=lvl.mic_rms,
                    sys_rms=lvl.sys_rms,
                    mic_peak=lvl.mic_peak,
                    sys_peak=lvl.sys_peak,
                    gen=gen,
                ))
            except Exception:
                pass
            worker.cancelled_event.wait(0.066)  # ~15 FPS

    # ── upload lifecycle ──

    def _kick_upload(self, audio_path: Path) -> None:
        title_widget = self.query_one("#title-input", Input)
        auto_label = "toggle-on" in self.query_one("#auto-label-btn", Button).classes
        sync = "toggle-on" in self.query_one("#sync-btn", Button).classes
        personal = "toggle-personal-on" in self.query_one("#personal-btn", Button).classes
        preset = str(self.query_one("#preset", Select).value)
        title = (title_widget.value or "").strip() or None

        if personal:
            sync = False  # match server-side enforcement

        self.is_uploading = True
        self.status_text = "compressing" if audio_path.suffix.lower() == ".wav" else "uploading"
        self.error_text = ""
        self._upload_worker(audio_path, title, preset, auto_label, sync, personal, self._gen)

    @work(thread=True, exclusive=True, group="upload")
    def _upload_worker(
        self,
        audio_path: Path,
        title: str | None,
        preset: str,
        auto_label: bool,
        sync: bool,
        personal: bool,
        gen: int,
    ) -> None:
        from .. import uploader

        try:
            if audio_path.suffix.lower() == ".wav":
                # keep_wav=False: drop the raw PCM once compressed.  The OGG
                # (opus 48k, transparent for speech) is the local audio
                # archive and the upload artifact; the WAV is never reused.
                audio_path = uploader.compress_wav_for_upload(
                    audio_path, keep_wav=False,
                )
        except Exception as exc:
            self.post_message(UploadFailed(
                error=f"compression failed: {exc}",
                audio_path=audio_path,
                gen=gen,
            ))
            return

        def on_progress(sent: int, total: int, elapsed: float) -> None:
            rate = sent / elapsed if elapsed > 0 else 0.0
            self.post_message(UploadProgress(sent=sent, total=total, rate=rate, gen=gen))

        def on_retry(attempt: int, retries: int, exc: Exception) -> None:
            self.post_message(ServerStatus(
                status="uploading",
                extra=f"attempt {attempt}/{retries} failed; retrying",
                gen=gen,
            ))

        server_url = self.app.server_url
        token = self.app.token or ""
        team_id = getattr(self.app, "active_team_id", None)
        upload_kwargs = dict(
            title=title,
            summary_preset=preset,
            auto_label=auto_label,
            sync=sync,
            personal=personal,
            progress=on_progress,
            on_retry=on_retry,
            team_id=team_id,
        )
        try:
            # Prefer resumable; fall back to one-shot on older servers.
            if uploader.server_supports_resumable(
                server_url, token, team_id=team_id
            ):
                result = uploader.upload_resumable(
                    server_url, token, audio_path, **upload_kwargs
                )
            else:
                result = uploader.upload(
                    server_url, token, audio_path, **upload_kwargs
                )
        except Exception as exc:
            self.post_message(UploadFailed(
                error=f"upload failed: {exc}",
                audio_path=audio_path,
                gen=gen,
            ))
            return

        session_id = result.get("session_id", "")
        # Bridge the local recording dir to the server session immediately:
        # write a minimal session.json so a later "open folder" (which calls
        # find_local_session_dir) reuses THIS folder instead of pulling the
        # artifacts into a new, differently-timestamped duplicate folder.
        if session_id:
            try:
                from ..pull import record_uploaded_session
                record_uploaded_session(
                    audio_path.parent, session_id, title=title,
                    team_id=team_id,
                )
            except Exception as exc:
                log.warning("could not write upload session.json: %s", exc)

        self.post_message(UploadFinished(
            session_id=session_id,
            gen=gen,
        ))

    @work(thread=True, exclusive=True, group="poll")
    def _poll_worker(self, session_id: str, gen: int) -> None:
        """Poll /api/sessions/{id} until terminal status.

        Terminal statuses include ``needs_labeling`` because the
        server is then waiting on the human; there's nothing more
        for the poll worker to track.  Previously this kept polling
        forever for any session whose auto-label didn't reach
        confident voiceprint matches (PR9 latent bug).
        """
        import time as _t

        from textual.worker import get_current_worker

        worker = get_current_worker()
        terminal = {"done", "error", "needs_labeling", "sync_failed"}
        last_status = ""
        deadline = _t.time() + 600
        while _t.time() < deadline and not worker.is_cancelled:
            # v0.7.0: bail early if the generation has moved on (user
            # started a new recording and that recording also uploaded).
            if gen < self._gen:
                return
            result = self.app.api.get_session(session_id)
            if not result.is_ok():
                worker.cancelled_event.wait(5)
                continue
            session = result.ok
            if session.status != last_status:
                last_status = session.status
                self.post_message(ServerStatus(
                    status=session.status,
                    extra=session.error or session.summary_error or "",
                    gen=gen,
                ))
            if session.status in terminal:
                # v0.7.0: auto-download artifacts into the local
                # recording directory when the server finishes.
                if session.status == "done" and self._last_audio_path:
                    try:
                        from ..artifacts import download_session_artifacts
                        dest = self._last_audio_path.parent
                        saved = download_session_artifacts(
                            self.app.api, session, dest,
                        )
                        if saved:
                            self.post_message(ServerStatus(
                                status="done",
                                extra=f"artifacts saved ({len(saved)} files)",
                                gen=gen,
                            ))
                    except Exception as exc:
                        log.warning("artifact download failed: %s", exc)
                # PR9: notify the rest of the UI that this session is
                # now displayable (done) or failed (error) so the
                # Sessions tab can refresh and the user gets a toast.
                self.post_message(SessionUploadComplete(
                    session_id=session_id,
                    status=session.status,
                ))
                return
            worker.cancelled_event.wait(5)

    # ── message handlers ──
    #
    # v0.7.0: all handlers check ``message.gen`` against ``self._gen``.
    # Messages from a stale generation (old recording's upload/poll
    # workers) are silently discarded so they don't clobber the current
    # recording's UI state.

    def on_timer_tick(self, message: TimerTick) -> None:
        if message.gen < self._gen:
            return
        self.elapsed_seconds = message.elapsed
        self.file_bytes = message.bytes
        if message.paused != self.is_paused:
            self.is_paused = message.paused

    def on_audio_level(self, message: AudioLevel) -> None:
        if message.gen < self._gen:
            return
        from ..audio import (
            SIGNAL_MIC_THRESHOLD,
            SIGNAL_SYS_THRESHOLD,
            SILENCE_DEBOUNCE_SECS,
            render_level_bars,
        )

        self._mic_history.append(message.mic_rms)
        self._sys_history.append(message.sys_rms)

        mic_bars = render_level_bars(self._mic_history)
        sys_bars = render_level_bars(self._sys_history)

        has_mic = message.mic_rms > SIGNAL_MIC_THRESHOLD
        has_sys = message.sys_rms > SIGNAL_SYS_THRESHOLD
        now = time.time()

        if has_mic or has_sys:
            self._silence_since = 0.0

        if has_mic and has_sys:
            signal = "[green]✓ signal[/]"
        elif has_mic:
            signal = "[green]✓ mic[/]  [yellow]⚠ no system audio[/]"
        elif has_sys:
            signal = "[green]✓ system[/]  [yellow]⚠ no mic[/]"
        else:
            if self._silence_since == 0.0:
                self._silence_since = now
            if now - self._silence_since >= SILENCE_DEBOUNCE_SECS:
                signal = "[red]✗ no signal[/]"
            else:
                signal = "[dim]…[/]"

        silence_timeout = (
            self._silence_since and now - self._silence_since >= SILENCE_DEBOUNCE_SECS
        )
        mic_color = "green" if has_mic else ("red" if silence_timeout else "dim")
        sys_color = "green" if has_sys else ("red" if silence_timeout else "dim")

        text = (
            f"[{mic_color}]🎤 {mic_bars}[/]  "
            f"[{sys_color}]🔊 {sys_bars}[/]  "
            f"{signal}"
        )
        try:
            self.query_one("#level-row", Static).update(text)
        except Exception:
            pass

    def on_recorder_failed(self, message: RecorderFailed) -> None:
        if message.gen < self._gen:
            return
        self.is_recording = False
        self.is_paused = False
        self.status_text = "recorder failed"
        self.error_text = message.reason

    def on_recorder_finished(self, message: RecorderFinished) -> None:
        if message.gen < self._gen:
            return
        self.is_recording = False
        self.is_paused = False
        # v0.7.0: rename session dir with title suffix.
        audio_path = message.audio_path
        try:
            title_widget = self.query_one("#title-input", Input)
            title = (title_widget.value or "").strip() or None
            session_dir = config.rename_session_dir_with_title(
                audio_path.parent, title,
            )
            audio_path = session_dir / audio_path.name
        except Exception:
            pass  # non-fatal; use original path
        self._last_audio_path = audio_path
        self._session = None  # release reference
        # Reset level display to idle.
        try:
            self.query_one("#level-row", Static).update(
                "[dim]🎤 ▁▁▁▁▁▁▁▁▁▁▁▁  🔊 ▁▁▁▁▁▁▁▁▁▁▁▁  ― idle[/]"
            )
        except Exception:
            pass
        # (Upload button no longer toggles on/off based on _last_audio_path
        # in v0.5.0+; it's always enabled and opens the Import picker.
        # Auto-upload of the just-finished recording still happens below.)
        size = audio_path.stat().st_size if audio_path.exists() else 0
        self.file_bytes = size
        self.status_text = (
            f"recording finished ({_fmt_bytes(size)}); uploading..."
        )
        # Auto-kick upload to match gui.py's behavior.
        self._kick_upload(audio_path)

    def on_server_status(self, message: ServerStatus) -> None:
        if message.gen < self._gen:
            return
        # v0.7.0: DECOUPLED from is_recording.  ServerStatus messages
        # from poll/upload workers should NEVER flip the recording flag.
        # Only _record_worker posts status="recording", and that is
        # handled separately via the initial is_recording=True set in
        # action_toggle_record / _start_recording flow.
        if message.status == "recording":
            self.is_recording = True
        # For non-recording statuses, only update the status text --
        # do NOT reset is_recording (which caused the back-to-back bug).
        text = message.status
        if message.extra:
            text = f"{message.status}  ({message.extra})"
        self.status_text = text

    def on_upload_progress(self, message: UploadProgress) -> None:
        if message.gen < self._gen:
            return
        if message.total > 0:
            pct = message.sent / message.total * 100
            self.status_text = (
                f"uploading {pct:5.1f}% "
                f"({_fmt_bytes(message.sent)} / {_fmt_bytes(message.total)} "
                f"@ {_fmt_bytes(int(message.rate))}/s)"
            )

    def on_upload_finished(self, message: UploadFinished) -> None:
        if message.gen < self._gen:
            return
        self.is_uploading = False
        self._last_session_id = message.session_id
        self.status_text = f"uploaded as {message.session_id}; polling status"
        self.error_text = ""
        self._poll_worker(message.session_id, message.gen)

    def on_upload_failed(self, message: UploadFailed) -> None:
        if message.gen < self._gen:
            return
        self.is_uploading = False
        self.status_text = "upload failed"
        suffix = ""
        if message.audio_path is not None:
            suffix = (
                f"\nRetry with: vezir upload {message.audio_path}"
            )
        self.error_text = message.error + suffix
