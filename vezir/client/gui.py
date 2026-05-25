"""Vezir scribe GUI — Tkinter-based recording widget.

A small always-on-top window that wraps `vezir scribe`'s flow:

  ┌─────────────────────────────────┐
  │ [vezir logo]         alice ✕   │   header (identity, close)
  ├─────────────────────────────────┤
  │ Title: [_____________________ ] │   meeting title
  │                                 │
  │   ┌───────┐                     │
  │   │ ● REC │   00:00:00  0 B    │   record/stop button + timer
  │   └───────┘                     │
  │                                 │
  │ Status:  ready                  │   server-side status badge
  │                                 │
  │ [ Open dashboard ]              │   action button (after upload)
  └─────────────────────────────────┘

The GUI does not perform transcription itself — it shells out to
`millet record` (via millet-record) for capture, then uploads to
the configured vezir server, then polls /api/sessions/<id> for status.

Configuration:
  VEZIR_URL    server URL (e.g. http://muscle:8000)
  VEZIR_TOKEN  bearer token

If either is missing, a settings dialog prompts on first launch and
persists to ~/.config/vezir/client.json.

This module imports tkinter at top level. If tkinter is missing
(Debian/Ubuntu may need `apt install python3-tk`), the import error
is reraised by the CLI subcommand wrapper with a friendly message.
"""
from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path

import tkinter as tk
from tkinter import messagebox

from .. import config

_PRESET_LABEL_TO_ID = {
    "High Quality \u2014 Sonnet 4.6": "high-quality",
    "Confidential \u2014 DeepSeek V4 Pro (TEE)": "confidential",
    "Alternative \u2014 Kimi K2.6": "alternative",
}


def _load_logo_image() -> "tk.PhotoImage | None":
    """Load the vezir brand-lockup PNG for the GUI header.

    Returns None if the asset cannot be located or Tk fails to decode it
    (e.g. running from a checkout where the PNG has not been generated).
    Caller falls back to a textual label.

    Sizing: the 88x28 variant is chosen to roughly match the Record
    button's height so the header stays compact and the lockup reads as
    a piece of UI chrome rather than a hero element.  Larger 176/400/800
    variants are also shipped (package-data) for places that want a
    bigger lockup (e.g. an About dialog) — pick the variant by filename.

    The PNG is shipped as package-data; we resolve it through
    importlib.resources so it works the same in a wheel install, an
    editable install, and a zipapp.
    """
    try:
        from importlib.resources import files as _pkg_files, as_file
        resource = _pkg_files("vezir.client.assets") / "vezir-logo-88.png"
        # importlib.resources may return a MultiplexedPath / Traversable
        # that is not a plain filesystem path.  as_file() yields a real
        # path even when the resource is shipped inside a zip.
        with as_file(resource) as p:
            return tk.PhotoImage(file=str(p))
    except Exception:
        return None


# ─── Persistent client config ────────────────────────────────────────────────


def _client_config_path() -> Path:
    return Path.home() / ".config" / "vezir" / "client.json"


def _load_client_config() -> dict:
    p = _client_config_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_client_config(data: dict) -> None:
    p = _client_config_path()
    config.secure_write_text(p, json.dumps(data, indent=2))


def _resolve_url_and_token() -> tuple[str | None, str | None]:
    """Resolve server URL and token: env > teams.json > client.json > None."""
    url = os.environ.get("VEZIR_URL")
    token = os.environ.get("VEZIR_TOKEN")
    if url and token:
        return url, token
    # v0.7.0: honor teams.json for the GUI (same precedence as TUI/CLI).
    try:
        from .config import resolve_credentials
        rc_url, rc_token, _src = resolve_credentials()
        if rc_url and rc_token:
            return rc_url, rc_token
    except Exception:
        pass
    cfg = _load_client_config()
    return url or cfg.get("url"), token or cfg.get("token")


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _fmt_elapsed(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def _fmt_size(nbytes: int) -> str:
    if nbytes < 1024:
        return f"{nbytes} B"
    if nbytes < 1024 * 1024:
        return f"{nbytes / 1024:.1f} KB"
    if nbytes < 1024 * 1024 * 1024:
        return f"{nbytes / (1024 * 1024):.1f} MB"
    return f"{nbytes / (1024 * 1024 * 1024):.1f} GB"


def _meet_bin() -> str:
    """Locate the `millet` (or legacy `meet`) binary."""
    explicit = os.environ.get("VEZIR_MILLET_BIN") or os.environ.get("VEZIR_MEET_BIN")
    if explicit:
        return explicit
    import shutil
    for name in ("millet", "meet"):
        found = shutil.which(name)
        if found:
            return found
    raise RuntimeError(
        "`millet` binary not found in PATH. Install millet-record "
        "(pip install millet-record)."
    )


def _default_output_dir() -> Path:
    """Recording output directory: ~/vezir-meetings/<active-team>/.

    v0.7.0: standardized on per-team subdirectory under ~/vezir-meetings/.
    """
    return config.recordings_dir()


def _check_meet_prerequisites(meet_bin: str) -> list[str]:
    """Run ``millet check`` and return any issues as a list of strings.

    On macOS this triggers the TCC permission dialog on first use.
    Returns an empty list when all prerequisites pass.
    """
    try:
        result = subprocess.run(
            [meet_bin, "check"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []  # best-effort; don't block the GUI

    if result.returncode != 0:
        return [
            line.strip()
            for line in result.stderr.strip().splitlines()
            if line.strip()
        ]
    return []


# ─── State machine ──────────────────────────────────────────────────────────


@dataclass
class RecordingState:
    """Tracked client-side state."""
    status: str = "ready"   # ready, recording, draining, compressing, uploading, queued,
                            # transcribing, syncing, done, error, needs_labeling
    started_at: float = 0.0
    wav_path: Path | None = None
    session_id: str | None = None
    dashboard_url: str | None = None
    # Browser-targeted URL that hands off the bearer token through /login
    # so the browser picks up a session cookie. Distinct from dashboard_url
    # which is the canonical (header-auth) path.
    dashboard_login_url: str | None = None
    error_message: str = ""


# ─── Settings dialog ────────────────────────────────────────────────────────


def _prompt_settings(parent: tk.Tk, current: dict) -> dict | None:
    """Show a modal settings dialog. Returns updated dict or None on cancel."""
    dlg = tk.Toplevel(parent)
    dlg.title("vezir — settings")
    dlg.transient(parent)
    dlg.grab_set()

    tk.Label(dlg, text="Server URL").grid(row=0, column=0, sticky="w", padx=8, pady=4)
    url_var = tk.StringVar(value=current.get("url", ""))
    tk.Entry(dlg, textvariable=url_var, width=42).grid(row=0, column=1, padx=8, pady=4)

    tk.Label(dlg, text="Token").grid(row=1, column=0, sticky="w", padx=8, pady=4)
    tok_var = tk.StringVar(value=current.get("token", ""))
    tk.Entry(dlg, textvariable=tok_var, width=42, show="•").grid(row=1, column=1, padx=8, pady=4)

    result: dict | None = None

    def ok():
        nonlocal result
        result = {"url": url_var.get().strip(), "token": tok_var.get().strip()}
        dlg.destroy()

    def cancel():
        dlg.destroy()

    btn_frame = tk.Frame(dlg)
    btn_frame.grid(row=2, column=0, columnspan=2, pady=8)
    tk.Button(btn_frame, text="Cancel", command=cancel).pack(side="left", padx=4)
    tk.Button(btn_frame, text="Save", command=ok).pack(side="left", padx=4)

    parent.wait_window(dlg)
    return result


# ─── Main window ────────────────────────────────────────────────────────────


class ScribeWindow:
    """Always-on-top recording widget."""

    POLL_INTERVAL_MS = 500          # GUI tick (timer + queue drain)
    STATUS_POLL_INTERVAL_MS = 5000  # remote status poll while in non-terminal state
    TERMINAL_STATUSES = {"done", "error"}

    def __init__(self, root: tk.Tk):
        self.root = root
        self.state = RecordingState()
        self.url, self.token = _resolve_url_and_token()

        self._proc: subprocess.Popen | None = None
        self._upload_thread: threading.Thread | None = None
        self._poll_thread: threading.Thread | None = None
        self._gui_queue: "queue.Queue[tuple[str, object]]" = queue.Queue()

        root.title("vezir")
        root.attributes("-topmost", True)
        root.minsize(360, 220)

        # ─── Header ─────────
        header = tk.Frame(root)
        header.pack(fill="x", padx=8, pady=(8, 4))
        # Brand lockup PNG, pre-rendered from assets/logo/vezir-logo.svg
        # and shipped as package-data inside vezir/client/assets/.  Falls
        # back to a textual label if the asset is missing (e.g. running
        # from a dev checkout where the PNG hasn't been regenerated).
        self._logo_img = _load_logo_image()
        if self._logo_img is not None:
            tk.Label(header, image=self._logo_img, bd=0).pack(side="left")
        else:
            tk.Label(header, text="vezir", font=("Sans", 11, "bold")).pack(side="left")
        self.identity_lbl = tk.Label(header, text="", fg="#666", font=("Mono", 9))
        self.identity_lbl.pack(side="right")
        tk.Button(header, text="⚙", width=2, command=self._open_settings, relief="flat").pack(side="right", padx=4)

        # ─── Title input ─────
        title_frame = tk.Frame(root)
        title_frame.pack(fill="x", padx=8, pady=4)
        tk.Label(title_frame, text="Title:").pack(side="left")
        self.title_var = tk.StringVar()
        self.title_entry = tk.Entry(title_frame, textvariable=self.title_var)
        self.title_entry.pack(side="left", fill="x", expand=True, padx=(4, 0))

        # ─── Summarization preset dropdown ─────
        preset_frame = tk.Frame(root)
        preset_frame.pack(fill="x", padx=8, pady=(2, 4))
        tk.Label(preset_frame, text="Summarization:", anchor="w").pack(side="left")
        self._preset_var = tk.StringVar(value="High Quality \u2014 Sonnet 4.6")
        _PRESET_LABELS = [
            "High Quality \u2014 Sonnet 4.6",
            "Confidential \u2014 DeepSeek V4 Pro (TEE)",
            "Alternative \u2014 Kimi K2.6",
        ]
        import tkinter.ttk as ttk
        preset_combo = ttk.Combobox(preset_frame, textvariable=self._preset_var,
                                    values=_PRESET_LABELS, state="readonly", width=38)
        preset_combo.pack(side="left", padx=(8, 0), fill="x", expand=True)

        # ─── Auto-label + sync opt-out checkboxes ─────
        # Load sticky prefs so the user's last choice survives relaunches.
        # Default True on missing keys preserves pre-opt-out behavior.
        from .config import load_client_prefs, save_client_prefs
        self._client_prefs_load = load_client_prefs
        self._client_prefs_save = save_client_prefs
        prefs = load_client_prefs()
        self._auto_label_var = tk.BooleanVar(
            value=bool(prefs.get("auto_label", True))
        )
        self._sync_var = tk.BooleanVar(
            value=bool(prefs.get("sync", True))
        )
        # Personal is a per-recording decision, NOT persisted across
        # launches.  Matches vezir-android 0.2.0+ UX and prevents the
        # "I forgot it was on last meeting" footgun for sync-disabling
        # behavior.  Default off on every relaunch.
        self._personal_var = tk.BooleanVar(value=False)

        def _persist_auto_label(*_a):
            p = self._client_prefs_load()
            p["auto_label"] = bool(self._auto_label_var.get())
            self._client_prefs_save(p)

        def _persist_sync(*_a):
            p = self._client_prefs_load()
            p["sync"] = bool(self._sync_var.get())
            self._client_prefs_save(p)

        def _on_personal_toggle(*_a):
            # When personal flips ON, force-disable sync visually so the
            # user sees that personal sessions can't be synced.  Matches
            # server-side enforcement in vezir/server/queue.py::enqueue.
            # When personal flips OFF, restore the persisted sync pref.
            if self._personal_var.get():
                self._sync_var.set(False)
                self._sync_checkbox.configure(state="disabled")
            else:
                p = self._client_prefs_load()
                self._sync_var.set(bool(p.get("sync", True)))
                self._sync_checkbox.configure(state="normal")

        toggles_frame = tk.Frame(root)
        toggles_frame.pack(fill="x", padx=8, pady=(2, 4))
        tk.Checkbutton(
            toggles_frame,
            text="Auto-label speakers",
            variable=self._auto_label_var,
            command=_persist_auto_label,
            anchor="w",
        ).pack(side="left", padx=(0, 16))
        self._sync_checkbox = tk.Checkbutton(
            toggles_frame,
            text="Sync to git",
            variable=self._sync_var,
            command=_persist_sync,
            anchor="w",
        )
        self._sync_checkbox.pack(side="left", padx=(0, 16))
        tk.Checkbutton(
            toggles_frame,
            text="Personal",
            variable=self._personal_var,
            command=_on_personal_toggle,
            anchor="w",
        ).pack(side="left")

        # ─── Recorder row ────
        rec_frame = tk.Frame(root)
        rec_frame.pack(fill="x", padx=8, pady=8)
        self.rec_btn = tk.Button(
            rec_frame, text="● Record",
            command=self._toggle_record,
            font=("Sans", 11, "bold"),
            width=12, bg="#e0e0e0",
        )
        self.rec_btn.pack(side="left")
        self.timer_lbl = tk.Label(rec_frame, text="00:00:00  0 B", font=("Mono", 11))
        self.timer_lbl.pack(side="left", padx=12)

        # ─── Status row ──────
        status_frame = tk.Frame(root)
        status_frame.pack(fill="x", padx=8, pady=4)
        tk.Label(status_frame, text="Status:").pack(side="left")
        self.status_lbl = tk.Label(status_frame, text="ready", fg="#444",
                                    font=("Mono", 10), padx=6, pady=2,
                                    relief="solid", borderwidth=1, bg="#f0f0f0")
        self.status_lbl.pack(side="left", padx=(4, 0))

        # ─── Action button ───
        self.action_btn = tk.Button(root, text="Open dashboard",
                                     command=self._open_dashboard,
                                     state="disabled")
        self.action_btn.pack(fill="x", padx=8, pady=(4, 8))

        # ─── Optional error display ──
        self.err_lbl = tk.Label(root, text="", fg="#c00", wraplength=340,
                                 justify="left", font=("Sans", 9))
        self.err_lbl.pack(fill="x", padx=8)

        # First-launch settings prompt
        if not self.url or not self.token:
            self.root.after(100, self._first_launch_prompt)
        else:
            self._update_identity()

        # GUI tick
        self.root.after(self.POLL_INTERVAL_MS, self._tick)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── identity / config UX ──

    def _update_identity(self):
        if self.url:
            short = self.url.replace("http://", "").replace("https://", "")
            self.identity_lbl.config(text=short)
        else:
            self.identity_lbl.config(text="(unconfigured)")

    def _first_launch_prompt(self):
        messagebox.showinfo(
            "vezir — first launch",
            "Server URL and token are not configured.\n"
            "You will be prompted to enter them now.",
            parent=self.root,
        )
        self._open_settings()

    def _open_settings(self):
        cfg = _load_client_config()
        if self.url:
            cfg["url"] = self.url
        if self.token:
            cfg["token"] = self.token
        result = _prompt_settings(self.root, cfg)
        if result is None:
            return
        self.url = result.get("url") or None
        self.token = result.get("token") or None
        if self.url and self.token:
            _save_client_config({"url": self.url, "token": self.token})
            self._update_identity()
            self._set_status("ready")

    # ── recording flow ──

    def _toggle_record(self):
        if self.state.status == "recording":
            self._stop_recording()
        elif self.state.status in ("ready", "done", "error", "needs_labeling"):
            self._start_recording()
        # Other states (uploading/transcribing/...) -> button is disabled

    def _start_recording(self):
        if not self.url or not self.token:
            messagebox.showwarning(
                "Configuration missing",
                "Set server URL and token via the ⚙ settings button before recording.",
                parent=self.root,
            )
            return
        try:
            meet_bin = _meet_bin()
        except RuntimeError as exc:
            messagebox.showerror("meet binary not found", str(exc), parent=self.root)
            return

        prereq_issues = _check_meet_prerequisites(meet_bin)
        if prereq_issues:
            messagebox.showerror(
                "Recording prerequisites not met",
                "\n".join(prereq_issues),
                parent=self.root,
            )
            return

        outdir = _default_output_dir()
        outdir.mkdir(parents=True, exist_ok=True)

        # Reset state for a fresh recording
        self.state = RecordingState(status="recording", started_at=time.time())
        self.err_lbl.config(text="")
        self.action_btn.config(state="disabled")
        self._set_status("recording")
        self.rec_btn.config(text="■ Stop", bg="#ffd0d0")
        self.title_entry.config(state="disabled")

        self._proc = subprocess.Popen(
            [meet_bin, "record", "-o", str(outdir)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _stop_recording(self):
        """Send Ctrl+C to millet record; spawn upload thread once it exits."""
        if not self._proc:
            return
        self._set_status("draining")
        self.rec_btn.config(state="disabled")

        def _waiter():
            try:
                self._proc.send_signal(signal.SIGINT)
            except ProcessLookupError:
                pass
            try:
                self._proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
            # locate the produced wav and queue an upload
            outdir = _default_output_dir()
            sdir = self._find_latest_session(outdir, self.state.started_at)
            if sdir is None:
                self._gui_queue.put(("error", "no session directory found after recording"))
                return
            # v0.7.0: rename session dir with title suffix
            title = self.title_var.get().strip() or None
            sdir = config.rename_session_dir_with_title(sdir, title)
            audio_files = sorted(sdir.glob("*.wav")) or sorted(sdir.glob("*.ogg"))
            if not audio_files:
                self._gui_queue.put(("error", f"no audio file in {sdir}"))
                return
            self._gui_queue.put(("recorded", audio_files[0]))

        threading.Thread(target=_waiter, daemon=True).start()

    @staticmethod
    def _find_latest_session(output_dir: Path, after: float) -> Path | None:
        if not output_dir.exists():
            return None
        candidates = []
        for p in output_dir.iterdir():
            if not p.is_dir():
                continue
            try:
                mtime = p.stat().st_mtime
            except OSError:
                continue
            if mtime >= after - 1:
                candidates.append((mtime, p))
        if not candidates:
            return None
        candidates.sort()
        return candidates[-1][1]

    def _start_upload(self, wav_path: Path):
        self.state.wav_path = wav_path
        self._set_status("compressing" if wav_path.suffix.lower() == ".wav" else "uploading")

        def _upload():
            try:
                from .uploader import compress_wav_for_upload, upload as do_upload
                title = self.title_var.get().strip() or None
                audio_path = wav_path
                if audio_path.suffix.lower() == ".wav":
                    before = audio_path.stat().st_size
                    audio_path = compress_wav_for_upload(audio_path, keep_wav=True)
                    after = audio_path.stat().st_size
                    ratio = before / after if after else 0
                    self._gui_queue.put((
                        "status",
                        f"compressed {_fmt_size(before)} -> {_fmt_size(after)} ({ratio:.1f}x)",
                    ))

                def progress(sent: int, total: int, elapsed: float) -> None:
                    pct = (sent / total * 100) if total else 0.0
                    self._gui_queue.put(("upload_progress", pct))

                def on_retry(attempt: int, retries: int, exc: Exception) -> None:
                    self._gui_queue.put((
                        "status",
                        f"upload attempt {attempt}/{retries} failed; retrying from byte 0",
                    ))

                preset_id = _PRESET_LABEL_TO_ID.get(self._preset_var.get(), "high-quality")
                # Snapshot the toggles at upload time so any in-flight
                # checkbox flips after we've started uploading don't
                # affect this session's behavior.
                auto_label = bool(self._auto_label_var.get())
                sync = bool(self._sync_var.get())
                personal = bool(self._personal_var.get())
                # Mirror the server's hard rule that personal => never
                # synced (vezir/server/queue.py::enqueue).  The checkbox
                # state machine already greys out sync when personal is
                # on, but the data model has the final say.
                if personal:
                    sync = False
                self._gui_queue.put(("status", "uploading"))
                result = do_upload(
                    self.url,
                    self.token,
                    audio_path,
                    title=title,
                    summary_preset=preset_id,
                    auto_label=auto_label,
                    sync=sync,
                    personal=personal,
                    progress=progress,
                    on_retry=on_retry,
                )
                self._gui_queue.put(("uploaded", result))
            except Exception as exc:
                hint = f"upload failed: {exc}"
                if audio_path and audio_path.exists():
                    hint += (
                        f"\n\nCheck VPN tunnel, then retry manually:\n"
                        f"  vezir upload {audio_path}"
                    )
                self._gui_queue.put(("error", hint))

        self._upload_thread = threading.Thread(target=_upload, daemon=True)
        self._upload_thread.start()

    # ── server status polling ──

    def _start_status_polling(self):
        def _poll():
            try:
                import httpx
            except ImportError:
                self._gui_queue.put(("error", "httpx is required for status polling"))
                return
            url = f"{self.url.rstrip('/')}/api/sessions/{self.state.session_id}"
            hdr = {"Authorization": f"Bearer {self.token}"}
            while True:
                try:
                    r = httpx.get(url, headers=hdr, timeout=10)
                    if r.status_code != 200:
                        self._gui_queue.put(("status", f"poll {r.status_code}"))
                        time.sleep(self.STATUS_POLL_INTERVAL_MS / 1000)
                        continue
                    data = r.json()
                    self._gui_queue.put(("server_status", data))
                    if data.get("status") in self.TERMINAL_STATUSES:
                        return
                except Exception as exc:
                    self._gui_queue.put(("status", f"poll error: {exc}"))
                time.sleep(self.STATUS_POLL_INTERVAL_MS / 1000)

        self._poll_thread = threading.Thread(target=_poll, daemon=True)
        self._poll_thread.start()

    # ── artifact download (v0.7.0) ──

    def _download_artifacts(self):
        """Download meeting artifacts into the local recording directory."""
        if not self.state.session_id or not self.state.wav_path:
            return

        session_id = self.state.session_id
        dest_dir = self.state.wav_path.parent

        def _worker():
            try:
                from .api import VezirClient
                from .artifacts import download_session_artifacts
                api = VezirClient(self.url, self.token)
                result = api.get_session(session_id)
                if not result.is_ok():
                    return
                saved = download_session_artifacts(api, result.ok, dest_dir)
                if saved:
                    self._gui_queue.put((
                        "status",
                        f"artifacts saved ({len(saved)} files)",
                    ))
            except Exception as exc:
                import logging
                logging.getLogger("vezir.client.gui").warning(
                    "artifact download failed: %s", exc,
                )

        threading.Thread(target=_worker, daemon=True).start()

    # ── GUI event loop ──

    def _tick(self):
        # 1. drain async events from worker threads
        try:
            while True:
                kind, payload = self._gui_queue.get_nowait()
                self._handle_async(kind, payload)
        except queue.Empty:
            pass

        # 2. update timer/size during recording or draining
        if self.state.status in ("recording", "draining") and self.state.started_at:
            elapsed = time.time() - self.state.started_at
            size = 0
            if self.state.wav_path is None:
                # peek at the in-progress wav under output dir
                outdir = _default_output_dir()
                sdir = self._find_latest_session(outdir, self.state.started_at)
                if sdir:
                    wavs = sorted(sdir.glob("*.wav"))
                    if wavs:
                        try:
                            size = wavs[0].stat().st_size
                        except OSError:
                            size = 0
            else:
                try:
                    size = self.state.wav_path.stat().st_size
                except OSError:
                    size = 0
            self.timer_lbl.config(text=f"{_fmt_elapsed(elapsed)}  {_fmt_size(size)}")

        self.root.after(self.POLL_INTERVAL_MS, self._tick)

    def _handle_async(self, kind: str, payload):
        if kind == "recorded":
            wav_path: Path = payload
            self._start_upload(wav_path)
        elif kind == "uploaded":
            data = payload
            self.state.session_id = data.get("session_id")
            self.state.dashboard_url = data.get("dashboard_url")
            # Prefer the /login hand-off URL when the server provides it
            # (vezir 0.0.2+); fall back to bare dashboard_url for older
            # servers (browser then prompts for token via /login form).
            self.state.dashboard_login_url = (
                data.get("dashboard_login_url") or data.get("dashboard_url")
            )
            self._set_status("queued")
            self.action_btn.config(state="normal")
            self._start_status_polling()
        elif kind == "server_status":
            data = payload
            new_status = data.get("status", "?")
            self._set_status(new_status)
            err = data.get("error")
            if err:
                self.err_lbl.config(text=str(err).splitlines()[0][:200])
            if new_status in self.TERMINAL_STATUSES:
                self.rec_btn.config(state="normal", text="● Record", bg="#e0e0e0")
                self.title_entry.config(state="normal")
                # v0.7.0: auto-download artifacts when done.
                if new_status == "done" and self.state.wav_path:
                    self._download_artifacts()
            elif new_status == "needs_labeling":
                self.rec_btn.config(state="normal", text="● Record", bg="#e0e0e0")
                self.title_entry.config(state="normal")
                self.err_lbl.config(
                    text="Some speakers need labeling — click Open dashboard.",
                    fg="#aa6600",
                )
        elif kind == "error":
            self.state.error_message = str(payload)
            self._set_status("error")
            self.err_lbl.config(text=self.state.error_message)
            self.rec_btn.config(state="normal", text="● Record", bg="#e0e0e0")
            self.title_entry.config(state="normal")
        elif kind == "status":
            msg = str(payload)
            if msg in {"uploading", "compressing"}:
                self._set_status(msg)
            else:
                self.err_lbl.config(text=msg, fg="#666")
        elif kind == "upload_progress":
            self._set_status(f"uploading {float(payload):.0f}%")

    def _set_status(self, status: str):
        self.state.status = status
        colour = {
            "ready": "#444",
            "recording": "#117733",
            "draining": "#876600",
            "compressing": "#1a4488",
            "uploading": "#1a4488",
            "queued": "#666666",
            "transcribing": "#876600",
            "syncing": "#1a4488",
            "needs_labeling": "#cc7a00",
            "done": "#117733",
            "error": "#c00",
        }.get(status, "#444")
        self.status_lbl.config(text=status, fg=colour)

    def _open_dashboard(self):
        # Prefer the /login hand-off URL so the browser picks up the cookie
        # and lands on a clean URL. Falls back to the canonical dashboard
        # URL if the server didn't provide a login URL.
        url = self.state.dashboard_login_url or self.state.dashboard_url
        if url:
            webbrowser.open_new_tab(url)

    def _on_close(self):
        # Kill any in-flight recording before closing.
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.send_signal(signal.SIGINT)
                self._proc.wait(timeout=15)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
        self.root.destroy()


def launch() -> int:
    """Launch the scribe GUI. Returns 0 on clean exit, 1 on failure."""
    root = tk.Tk()
    try:
        ScribeWindow(root)
    except Exception as exc:
        messagebox.showerror("vezir failed to launch", str(exc))
        return 1
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(launch())
