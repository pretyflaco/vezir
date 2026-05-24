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
import time
from dataclasses import dataclass, field
from pathlib import Path

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import Button, Checkbox, Input, Label, Select, Static

from .. import config as _client_config_mod
from ..config import load_client_prefs, save_client_prefs

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


@dataclass
class TimerTick(Message):
    elapsed: float
    bytes: int
    paused: bool


@dataclass
class RecorderFailed(Message):
    reason: str


@dataclass
class RecorderFinished(Message):
    audio_path: Path


@dataclass
class UploadProgress(Message):
    sent: int
    total: int
    rate: float


@dataclass
class UploadFinished(Message):
    session_id: str
    dashboard_url: str | None


@dataclass
class UploadFailed(Message):
    error: str
    audio_path: Path | None


@dataclass
class ServerStatus(Message):
    status: str
    extra: str = ""


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


# ─── Screen ──────────────────────────────────────────────────────────────────


class RecordBody(Vertical):
    """The record-and-upload pane (used inside MainScreen's TabbedContent)."""

    BINDINGS = [
        Binding("ctrl+space", "toggle_record", "Start/Stop"),
        Binding("ctrl+p", "toggle_pause", "Pause/Resume"),
        Binding("ctrl+x", "toggle_personal", "Personal"),
        Binding("ctrl+u", "upload_last", "Upload last"),
    ]

    DEFAULT_CSS = """
    RecordBody {
        padding: 1 2;
    }

    /* ── title row ── */
    #title-row {
        height: 3;
        margin-bottom: 1;
        align: left middle;
    }
    #title-row Label {
        width: 10;
    }
    #title-row Input {
        width: 1fr;
    }

    /* ── toggles row ── */
    #toggles-row {
        height: 3;
        margin-bottom: 1;
        align: left middle;
    }
    #toggles-row Checkbox,
    #toggles-row Select {
        width: 1fr;
        margin-right: 1;
    }
    #toggles-row > :last-child {
        margin-right: 0;
    }

    /* ── controls row ── */
    #controls-row {
        height: 3;
        margin-bottom: 1;
        align: left middle;
    }
    #controls-row Button {
        margin-right: 1;
    }
    #controls-row #upload-btn {
        margin-right: 0;
        margin-left: 1;
    }
    #timer-label {
        width: 1fr;
        content-align: center middle;
        border: round $primary;
        padding: 0 1;
        height: 3;
        text-style: bold;
        color: $accent;
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
    Button.recording {
        background: $error;
        color: $text;
    }
    Button.paused {
        background: $warning;
        color: $text;
    }
    Checkbox.personal-on > .toggle--label {
        color: $warning;
        text-style: bold;
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

    @classmethod
    def body_widget(cls) -> "RecordBody":
        """Factory used by MainScreen's TabbedContent."""
        return cls()

    # ── compose ──

    def compose(self) -> ComposeResult:
        with Horizontal(id="title-row"):
            yield Label("Title:", classes="muted")
            yield Input(placeholder="optional meeting title", id="title-input")

        with Horizontal(id="toggles-row"):
            yield Checkbox(
                "Auto-label",
                value=bool(self._prefs.get("auto_label", True)),
                id="auto-label",
            )
            yield Checkbox(
                "Sync",
                value=bool(self._prefs.get("sync", True)),
                id="sync",
            )
            yield Checkbox("Personal", value=False, id="personal")
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
            yield Button("⬆ Upload last", id="upload-btn", disabled=True)

        yield Static(f"Status: {self.status_text}", id="status-line")
        yield Static("", id="error-line", classes="error")

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
            self.action_upload_last()

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        cid = event.checkbox.id
        if cid == "auto-label":
            self._prefs["auto_label"] = bool(event.value)
            save_client_prefs(self._prefs)
        elif cid == "sync":
            self._prefs["sync"] = bool(event.value)
            save_client_prefs(self._prefs)
        elif cid == "personal":
            # Per the gui.py pattern: when personal flips on, force sync
            # off (server will enforce this anyway; we just keep the UI
            # honest).  When it flips off, restore the persisted pref.
            sync_cb = self.query_one("#sync", Checkbox)
            if event.value:
                sync_cb.value = False
                sync_cb.disabled = True
                event.checkbox.add_class("personal-on")
            else:
                sync_cb.disabled = False
                sync_cb.value = bool(self._prefs.get("sync", True))
                event.checkbox.remove_class("personal-on")

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
        cb = self.query_one("#personal", Checkbox)
        cb.value = not cb.value

    def action_upload_last(self) -> None:
        if self.is_uploading:
            return
        if self._last_audio_path is None or not self._last_audio_path.exists():
            self.error_text = "no recording to upload yet"
            return
        self._kick_upload(self._last_audio_path)

    # ── recording lifecycle ──

    def _start_recording(self) -> None:
        # Lazy-import millet-record so a system that only wants to
        # list sessions doesn't pay for the pulseaudio / sidecar import.
        try:
            from millet_record.capture import create_session, check_prerequisites
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
            self._session = create_session()
        except Exception as exc:
            self.error_text = f"could not create session: {exc}"
            return

        self.error_text = ""
        self.status_text = "starting recorder"
        self._record_worker(self._session)

    def _stop_recording(self) -> None:
        if self._session is None:
            return
        self.status_text = "draining (flushing audio buffer)"
        self._stop_worker(self._session)

    @work(thread=True, exclusive=True, group="recorder")
    def _record_worker(self, session) -> None:
        """Drive the recorder: start, then poll status every second."""
        try:
            session.start()
        except Exception as exc:
            self.post_message(RecorderFailed(reason=str(exc)))
            return
        # Notify the UI to flip into recording mode.
        self.post_message(ServerStatus(status="recording"))
        # Tick loop: poll the session's own status() and emit TimerTick
        # messages.  Stops when the session is no longer alive (i.e.
        # _stop_worker has finalized it).
        while True:
            try:
                st = session.status()
            except Exception as exc:
                self.post_message(RecorderFailed(reason=f"status() raised: {exc}"))
                return
            self.post_message(TimerTick(
                elapsed=st.elapsed_seconds,
                bytes=st.file_size_bytes,
                paused=st.paused,
            ))
            if st.failed:
                self.post_message(RecorderFailed(
                    reason=st.fail_reason or "unknown recorder error",
                ))
                return
            if not st.is_alive and not st.paused:
                # Either we haven't started yet (let the watchdog catch
                # that) or we've finalized.  Loop again briefly and let
                # the stop worker complete; we exit through the explicit
                # return in _stop_worker via session attribute mutation.
                pass
            time.sleep(1.0)

    @work(thread=True, exclusive=True, group="recorder-stop")
    def _stop_worker(self, session) -> None:
        try:
            out = session.stop()
        except Exception as exc:
            self.post_message(RecorderFailed(reason=f"stop() raised: {exc}"))
            return
        self.post_message(RecorderFinished(audio_path=out))

    # ── upload lifecycle ──

    def _kick_upload(self, audio_path: Path) -> None:
        title_widget = self.query_one("#title-input", Input)
        auto_label = bool(self.query_one("#auto-label", Checkbox).value)
        sync = bool(self.query_one("#sync", Checkbox).value)
        personal = bool(self.query_one("#personal", Checkbox).value)
        preset = str(self.query_one("#preset", Select).value)
        title = (title_widget.value or "").strip() or None

        if personal:
            sync = False  # match server-side enforcement

        self.is_uploading = True
        self.status_text = "compressing" if audio_path.suffix.lower() == ".wav" else "uploading"
        self.error_text = ""
        self._upload_worker(audio_path, title, preset, auto_label, sync, personal)

    @work(thread=True, exclusive=True, group="upload")
    def _upload_worker(
        self,
        audio_path: Path,
        title: str | None,
        preset: str,
        auto_label: bool,
        sync: bool,
        personal: bool,
    ) -> None:
        from .. import uploader

        try:
            if audio_path.suffix.lower() == ".wav":
                audio_path = uploader.compress_wav_for_upload(
                    audio_path, keep_wav=True,
                )
        except Exception as exc:
            self.post_message(UploadFailed(
                error=f"compression failed: {exc}",
                audio_path=audio_path,
            ))
            return

        def on_progress(sent: int, total: int, elapsed: float) -> None:
            rate = sent / elapsed if elapsed > 0 else 0.0
            self.post_message(UploadProgress(sent=sent, total=total, rate=rate))

        def on_retry(attempt: int, retries: int, exc: Exception) -> None:
            self.post_message(ServerStatus(
                status="uploading",
                extra=f"attempt {attempt}/{retries} failed; retrying",
            ))

        try:
            result = uploader.upload(
                self.app.server_url,
                self.app.token or "",
                audio_path,
                title=title,
                summary_preset=preset,
                auto_label=auto_label,
                sync=sync,
                personal=personal,
                progress=on_progress,
                on_retry=on_retry,
            )
        except Exception as exc:
            self.post_message(UploadFailed(
                error=f"upload failed: {exc}",
                audio_path=audio_path,
            ))
            return

        self.post_message(UploadFinished(
            session_id=result.get("session_id", ""),
            dashboard_url=result.get("dashboard_url")
                          or result.get("dashboard_login_url"),
        ))

    @work(thread=True, exclusive=True, group="poll")
    def _poll_worker(self, session_id: str) -> None:
        """Poll /api/sessions/{id} until terminal status.

        Terminal statuses include ``needs_labeling`` because the
        server is then waiting on the human; there's nothing more
        for the poll worker to track.  Previously this kept polling
        forever for any session whose auto-label didn't reach
        confident voiceprint matches (PR9 latent bug).
        """
        import time as _t

        terminal = {"done", "error", "needs_labeling"}
        last_status = ""
        deadline = _t.time() + 600
        while _t.time() < deadline:
            result = self.app.api.get_session(session_id)
            if not result.is_ok():
                _t.sleep(5)
                continue
            session = result.ok
            if session.status != last_status:
                last_status = session.status
                self.post_message(ServerStatus(
                    status=session.status,
                    extra=session.error or session.summary_error or "",
                ))
            if session.status in terminal:
                # PR9: notify the rest of the UI that this session is
                # now displayable (done) or failed (error) so the
                # Sessions tab can refresh and the user gets a toast.
                self.post_message(SessionUploadComplete(
                    session_id=session_id,
                    status=session.status,
                ))
                return
            _t.sleep(5)

    # ── message handlers ──

    def on_timer_tick(self, message: TimerTick) -> None:
        self.elapsed_seconds = message.elapsed
        self.file_bytes = message.bytes
        if message.paused != self.is_paused:
            self.is_paused = message.paused

    def on_recorder_failed(self, message: RecorderFailed) -> None:
        self.is_recording = False
        self.is_paused = False
        self.status_text = "recorder failed"
        self.error_text = message.reason

    def on_recorder_finished(self, message: RecorderFinished) -> None:
        self.is_recording = False
        self.is_paused = False
        self._last_audio_path = message.audio_path
        self.query_one("#upload-btn", Button).disabled = False
        size = message.audio_path.stat().st_size if message.audio_path.exists() else 0
        self.file_bytes = size
        self.status_text = (
            f"recording finished ({_fmt_bytes(size)}); press u to upload"
        )
        # Auto-kick upload to match gui.py's behavior.
        self._kick_upload(message.audio_path)

    def on_server_status(self, message: ServerStatus) -> None:
        self.is_recording = message.status == "recording"
        text = message.status
        if message.extra:
            text = f"{message.status}  ({message.extra})"
        self.status_text = text

    def on_upload_progress(self, message: UploadProgress) -> None:
        if message.total > 0:
            pct = message.sent / message.total * 100
            self.status_text = (
                f"uploading {pct:5.1f}% "
                f"({_fmt_bytes(message.sent)} / {_fmt_bytes(message.total)} "
                f"@ {_fmt_bytes(int(message.rate))}/s)"
            )

    def on_upload_finished(self, message: UploadFinished) -> None:
        self.is_uploading = False
        self._last_session_id = message.session_id
        self.status_text = f"uploaded as {message.session_id}; polling status"
        self.error_text = ""
        self._poll_worker(message.session_id)

    def on_upload_failed(self, message: UploadFailed) -> None:
        self.is_uploading = False
        self.status_text = "upload failed"
        suffix = ""
        if message.audio_path is not None:
            suffix = (
                f"\nRetry with: vezir upload {message.audio_path}"
            )
        self.error_text = message.error + suffix
