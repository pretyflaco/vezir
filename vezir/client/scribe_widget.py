"""Floating always-on-top recording widget (Tkinter).

The hybrid of the v0.3 plan: the TUI handles sessions / detail /
artifacts / labeling, this Tkinter window is purely the *recording*
affordance with the always-on-top property your terminal can't give
you.  It's intentionally small -- title, record/pause/stop, personal
toggle, status, and a button that launches ``vezir tui`` in a fresh
terminal for everything else.

Slimmed down from ``vezir/client/gui.py`` (744 lines).  Key differences:

* Drops the settings dialog -- credentials come from env / client.json
  exactly like the TUI.  If they're missing, the widget complains in
  the status bar and stays usable for "open TUI to configure".
* Drops the preset dropdown -- preset is a per-session decision better
  served from the TUI's record screen.  The widget records with the
  preset persisted in ``client.json``, or ``high-quality`` if unset.
* Drops the auto-label / sync checkboxes -- same reason; defaults from
  prefs are good enough for a quick-record affordance.
* KEEPS the personal toggle -- it's the one per-recording decision
  that the user must make consciously every time.
* Uses ``meet_record.capture`` library directly (pause/resume).
  The current ``vezir gui`` shells out to ``millet record`` via Popen
  which loses pause/resume.
* Uses ``vezir.client.api.VezirClient`` for status polling instead of
  inline httpx calls.

CLI: ``vezir scribe-widget``.

Tkinter ships with Python on most distros; on Debian/Ubuntu minimal
the user needs ``apt install python3-tk``.  Same caveat as the legacy
``vezir gui``.
"""
from __future__ import annotations

import logging
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path

from .. import config
from .api import VezirClient
from .config import load_client_prefs

log = logging.getLogger("vezir.client.scribe_widget")


# ─── State ───────────────────────────────────────────────────────────────────


@dataclass
class WidgetState:
    """In-memory state of the floating recorder."""

    status: str = "ready"   # ready, recording, paused, draining, uploading,
                             # queued, transcribing, summarizing,
                             # needs_labeling, done, error
    audio_path: Path | None = None
    session_id: str | None = None
    error_message: str = ""
    paused: bool = False


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _fmt_elapsed(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    return f"{n / (1024 * 1024 * 1024):.1f} GB"


def _launch_tui_in_terminal() -> bool:
    """Open `vezir tui` in a fresh terminal emulator.

    Tries x-terminal-emulator (Debian/Ubuntu), gnome-terminal, konsole,
    xterm, and on macOS the Terminal.app via `open -a Terminal`.
    Returns True if something launched, False if we couldn't find a
    terminal emulator.  Failure is non-fatal; the user can run
    ``vezir tui`` themselves.
    """
    cmd_vezir = "vezir tui"
    if sys.platform == "darwin":
        try:
            subprocess.Popen([
                "osascript", "-e",
                f'tell application "Terminal" to do script "{cmd_vezir}"',
            ])
            return True
        except OSError:
            return False
    # Linux: probe common terminal emulators in order of likelihood.
    for emu in ("x-terminal-emulator", "gnome-terminal", "konsole",
                "alacritty", "kitty", "xterm"):
        try:
            if emu == "gnome-terminal":
                subprocess.Popen([emu, "--", "bash", "-c", f"{cmd_vezir}; exec bash"])
            elif emu == "konsole":
                subprocess.Popen([emu, "-e", "bash", "-c", f"{cmd_vezir}; exec bash"])
            else:
                subprocess.Popen([emu, "-e", "bash", "-c", f"{cmd_vezir}; exec bash"])
            return True
        except (FileNotFoundError, OSError):
            continue
    return False


# ─── Main widget ─────────────────────────────────────────────────────────────


class ScribeWidget:
    """Compact always-on-top recording controller.

    Owns one ``meet_record.capture.RecordingSession`` at a time and
    delegates uploads + status polling to a worker thread that
    communicates back via ``self._queue``.
    """

    POLL_INTERVAL_MS = 500       # GUI tick (timer + queue drain)
    STATUS_POLL_INTERVAL_MS = 5000   # server-side status poll

    TERMINAL_STATUSES = {"done", "error"}

    def __init__(self, root: tk.Tk):
        self.root = root
        self.state = WidgetState()
        self._session = None  # RecordingSession when active
        self._queue: queue.Queue = queue.Queue()
        self._upload_thread: threading.Thread | None = None
        self._poll_thread: threading.Thread | None = None
        self._prefs = load_client_prefs()

        # Credentials resolved once at startup (matches TUI behavior).
        self.url = (
            os.environ.get("VEZIR_URL")
            or self._prefs.get("url")
        )
        self.token = (
            os.environ.get("VEZIR_TOKEN")
            or self._prefs.get("token")
        )
        # Lazy api client -- not constructed until upload time so a
        # missing token doesn't fail widget startup.
        self._api: VezirClient | None = None

        self._build_ui()

        # Periodic tick for elapsed-time updates + queue drain.
        self.root.after(self.POLL_INTERVAL_MS, self._tick)

        # If credentials missing, surface that immediately.
        if not self.url or not self.token:
            self._set_status(
                "ready (no credentials -- click 'Open TUI' to enroll)"
            )

    # ── UI ──

    def _build_ui(self) -> None:
        r = self.root
        r.title("vezir")
        r.attributes("-topmost", True)
        r.minsize(360, 200)

        # Header
        header = tk.Frame(r)
        header.pack(fill="x", padx=8, pady=(8, 4))
        tk.Label(header, text="vezir", font=("Sans", 11, "bold")).pack(side="left")
        tk.Button(
            header, text="Open TUI", relief="flat",
            command=self._open_tui,
        ).pack(side="right")

        # Title input
        title_frame = tk.Frame(r)
        title_frame.pack(fill="x", padx=8, pady=4)
        tk.Label(title_frame, text="Title:").pack(side="left")
        self.title_var = tk.StringVar()
        tk.Entry(title_frame, textvariable=self.title_var).pack(
            side="left", fill="x", expand=True, padx=(4, 0),
        )

        # Personal toggle (per-recording, not persisted)
        toggles = tk.Frame(r)
        toggles.pack(fill="x", padx=8, pady=(2, 4))
        self._personal_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            toggles, text="Personal (private, never synced)",
            variable=self._personal_var, anchor="w",
        ).pack(side="left")

        # Recorder controls
        rec = tk.Frame(r)
        rec.pack(fill="x", padx=8, pady=6)
        self.rec_btn = tk.Button(
            rec, text="● Record", font=("Sans", 11, "bold"),
            width=11, bg="#e0e0e0", command=self._on_record_button,
        )
        self.rec_btn.pack(side="left")
        self.pause_btn = tk.Button(
            rec, text="⏸ Pause", width=8, command=self._on_pause_button,
            state="disabled",
        )
        self.pause_btn.pack(side="left", padx=4)
        self.timer_lbl = tk.Label(rec, text="00:00:00  0 B", font=("Mono", 11))
        self.timer_lbl.pack(side="left", padx=10)

        # Audio level row (v0.7.0)
        self.level_lbl = tk.Label(
            r,
            text="🎤 ▁▁▁▁▁▁▁▁  🔊 ▁▁▁▁▁▁▁▁",
            fg="#999", font=("Mono", 10), anchor="w",
        )
        self.level_lbl.pack(fill="x", padx=8, pady=(0, 4))
        self._level_timer_id: str | None = None

        # Status badge
        status_frame = tk.Frame(r)
        status_frame.pack(fill="x", padx=8, pady=4)
        tk.Label(status_frame, text="Status:").pack(side="left")
        self.status_lbl = tk.Label(
            status_frame, text="ready", fg="#444",
            font=("Mono", 10), padx=6, pady=2,
            relief="solid", borderwidth=1, bg="#f0f0f0",
        )
        self.status_lbl.pack(side="left", padx=(4, 0))

        # Error / hint line
        self.err_lbl = tk.Label(
            r, text="", fg="#c00", wraplength=340,
            justify="left", font=("Sans", 9),
        )
        self.err_lbl.pack(fill="x", padx=8)

    # ── Button handlers ──

    def _on_record_button(self) -> None:
        if self.state.status == "recording" or self.state.paused:
            self._stop_recording()
        elif self.state.status in ("ready", *self.TERMINAL_STATUSES):
            self._start_recording()
        # Other statuses (uploading, transcribing, ...): ignore -- the
        # user shouldn't be able to start a new recording while one is
        # in flight.  Could grey the button instead; keeping the noop
        # for now is fine.

    def _on_pause_button(self) -> None:
        if self._session is None:
            return
        try:
            if self.state.paused:
                self._session.resume()
                self.state.paused = False
                self.pause_btn.configure(text="⏸ Pause")
                self._set_status("recording")
            else:
                self._session.pause()
                self.state.paused = True
                self.pause_btn.configure(text="▶ Resume")
                self._set_status("paused")
        except Exception as exc:
            self._set_error(f"pause/resume failed: {exc}")

    def _open_tui(self) -> None:
        if _launch_tui_in_terminal():
            self._set_status("TUI launched in new terminal")
        else:
            self._set_error(
                "Could not find a terminal emulator to launch `vezir tui`.\n"
                "Run it manually from a shell.",
            )

    # ── Recording lifecycle (library-direct) ──

    def _start_recording(self) -> None:
        if not self.url or not self.token:
            self._set_error(
                "No VEZIR_URL/VEZIR_TOKEN set; cannot upload.  "
                "Click 'Open TUI' to enroll, or set env vars.",
            )
            return
        try:
            from millet_record.capture import check_prerequisites, create_session
        except ImportError as exc:
            self._set_error(
                f"millet-record not installed: {exc}.  "
                f"pip install millet-record"
            )
            return

        issues = check_prerequisites()
        if issues:
            self._set_error(" | ".join(issues))
            return

        try:
            output_dir = config.recordings_dir()
            output_dir.mkdir(parents=True, exist_ok=True)
            self._session = create_session(output_dir=str(output_dir))
            self._session.start()
        except Exception as exc:
            self._set_error(f"could not start recorder: {exc}")
            self._session = None
            return

        self.state.status = "recording"
        self.state.paused = False
        self.state.error_message = ""
        self.err_lbl.configure(text="")
        self.rec_btn.configure(text="■ Stop", bg="#f0c0c0")
        self.pause_btn.configure(state="normal", text="⏸ Pause")
        self._set_status("recording")
        self._start_level_polling()

    def _stop_recording(self) -> None:
        if self._session is None:
            return
        self._stop_level_polling()
        self._set_status("draining (flushing audio buffer)")
        try:
            out = self._session.stop()
        except Exception as exc:
            self._set_error(f"stop failed: {exc}")
            self._session = None
            return
        self._session = None
        # v0.7.0: rename session dir with title suffix.
        title = (self.title_var.get() or "").strip() or None
        session_dir = config.rename_session_dir_with_title(out.parent, title)
        out = session_dir / out.name
        self.state.audio_path = out
        self.state.paused = False
        self.rec_btn.configure(text="● Record", bg="#e0e0e0")
        self.pause_btn.configure(state="disabled", text="⏸ Pause")

        if not out.exists() or out.stat().st_size == 0:
            self._set_error("recording produced no audio")
            return

        self._kick_upload(out)

    # ── audio level spectrometer (v0.7.0) ──

    def _start_level_polling(self) -> None:
        from collections import deque
        self._mic_hist: deque = deque([0.0] * 12, maxlen=12)
        self._sys_hist: deque = deque([0.0] * 12, maxlen=12)
        self._silence_since_w: float = 0.0
        self._poll_levels_widget()

    def _stop_level_polling(self) -> None:
        if self._level_timer_id is not None:
            self.root.after_cancel(self._level_timer_id)
            self._level_timer_id = None
        self.level_lbl.configure(
            text="🎤 ▁▁▁▁▁▁▁▁▁▁▁▁  🔊 ▁▁▁▁▁▁▁▁▁▁▁▁",
            fg="#999",
        )

    def _poll_levels_widget(self) -> None:
        if self._session is None or self.state.paused:
            self._level_timer_id = self.root.after(66, self._poll_levels_widget)
            return
        import time as _t

        from .audio import (
            SIGNAL_MIC_THRESHOLD,
            SIGNAL_SYS_THRESHOLD,
            SILENCE_DEBOUNCE_SECS,
            read_chunk_levels,
            render_level_bars,
        )

        chunk = getattr(self._session, "_current_chunk", None)
        if chunk is not None:
            lvl = read_chunk_levels(chunk)
            self._mic_hist.append(lvl.mic_rms)
            self._sys_hist.append(lvl.sys_rms)

            has_mic = lvl.mic_rms > SIGNAL_MIC_THRESHOLD
            has_sys = lvl.sys_rms > SIGNAL_SYS_THRESHOLD
            now = _t.time()
            if has_mic or has_sys:
                self._silence_since_w = 0.0

            if has_mic and has_sys:
                sig = "✓"
                color = "#117733"
            elif has_mic or has_sys:
                sig = "⚠"
                color = "#aa6600"
            else:
                if self._silence_since_w == 0.0:
                    self._silence_since_w = now
                if now - self._silence_since_w >= SILENCE_DEBOUNCE_SECS:
                    sig = "✗"
                    color = "#c00"
                else:
                    sig = "…"
                    color = "#999"

            mic_bars = render_level_bars(self._mic_hist)
            sys_bars = render_level_bars(self._sys_hist)
            self.level_lbl.configure(
                text=f"🎤 {mic_bars}  🔊 {sys_bars}  {sig}",
                fg=color,
            )

        self._level_timer_id = self.root.after(66, self._poll_levels_widget)

    def _kick_upload(self, audio_path: Path) -> None:
        self._set_status("uploading")
        title = (self.title_var.get() or "").strip() or None
        personal = bool(self._personal_var.get())
        auto_label = bool(self._prefs.get("auto_label", True))
        sync = bool(self._prefs.get("sync", True))
        if personal:
            sync = False
        preset = self._prefs.get("preset", "high-quality")

        def _worker() -> None:
            from . import uploader
            try:
                # Compress WAV -> OGG before upload to match scribe.py.
                if audio_path.suffix.lower() == ".wav":
                    self._queue.put(("status", "compressing"))
                    compressed = uploader.compress_wav_for_upload(
                        audio_path, keep_wav=True,
                    )
                else:
                    compressed = audio_path
                self._queue.put(("status", "uploading"))
                result = uploader.upload(
                    self.url, self.token, compressed,
                    title=title,
                    summary_preset=preset,
                    auto_label=auto_label,
                    sync=sync,
                    personal=personal,
                )
                self._queue.put(("uploaded", result))
            except Exception as exc:
                self._queue.put(("error", f"upload failed: {exc}"))

        self._upload_thread = threading.Thread(target=_worker, daemon=True)
        self._upload_thread.start()

    def _kick_status_poll(self, session_id: str) -> None:
        api = self._get_api()
        if api is None:
            return

        def _worker() -> None:
            import time as _t
            last_status = ""
            deadline = _t.time() + 600
            while _t.time() < deadline:
                result = api.get_session(session_id)
                if result.is_ok():
                    status = result.ok.status
                    if status != last_status:
                        last_status = status
                        self._queue.put(("status", status))
                    if status in self.TERMINAL_STATUSES:
                        return
                _t.sleep(self.STATUS_POLL_INTERVAL_MS / 1000.0)

        self._poll_thread = threading.Thread(target=_worker, daemon=True)
        self._poll_thread.start()

    def _get_api(self) -> VezirClient | None:
        if not self.url or not self.token:
            return None
        if self._api is None:
            self._api = VezirClient(self.url, self.token)
        return self._api

    # ── Periodic tick ──

    def _tick(self) -> None:
        # Drain worker messages.
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                self._handle_message(kind, payload)
        except queue.Empty:
            pass

        # Refresh elapsed timer while recording (or paused).
        if self._session is not None:
            try:
                st = self._session.status()
                self.timer_lbl.configure(
                    text=(
                        f"{_fmt_elapsed(st.elapsed_seconds)}  "
                        f"{_fmt_size(st.file_size_bytes)}"
                        + ("  (paused)" if st.paused else "")
                    ),
                )
            except Exception:
                pass  # don't let a transient status() error spam the log

        self.root.after(self.POLL_INTERVAL_MS, self._tick)

    def _handle_message(self, kind: str, payload) -> None:
        if kind == "status":
            self._set_status(str(payload))
        elif kind == "uploaded":
            result = payload or {}
            sid = result.get("session_id")
            self.state.session_id = sid
            if sid:
                self._set_status(f"uploaded as {sid}")
                self._kick_status_poll(sid)
        elif kind == "error":
            self._set_error(str(payload))
        else:
            log.debug("unhandled queue msg: %s %r", kind, payload)

    # ── UI mutators (always on the main thread) ──

    def _set_status(self, text: str) -> None:
        self.state.status = text
        self.status_lbl.configure(text=text)

    def _set_error(self, text: str) -> None:
        self.state.error_message = text
        self.err_lbl.configure(text=text)
        self._set_status("error")


# ─── Entry point ─────────────────────────────────────────────────────────────


def launch() -> int:
    """Run the widget.  Called by ``vezir scribe-widget``."""
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(
            f"vezir scribe-widget: Tk could not start: {exc}\n"
            "  On Debian/Ubuntu: sudo apt install python3-tk\n"
            "  Or use `vezir tui` instead (terminal-only, no Tk).",
            file=sys.stderr,
        )
        return 1
    ScribeWidget(root)
    root.mainloop()
    return 0
