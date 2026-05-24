"""Cross-platform audio clip playback for the desktop thin client.

Mirrors vezir-android's ``net/AudioClipPlayer.kt`` (MediaPlayer over
HTTPS-cached WAVs) with the desktop equivalent: shell out to ffplay
(bundled with ffmpeg, already a millet-record dep) to play a
locally-cached WAV.  The TUI's label screen passes one of these
players around so play/stop interactions stay snappy without blocking
the event loop on subprocess startup.

Why ffplay and not e.g. simpleaudio / pyaudio:

* Already on every machine that runs millet-record (ffmpeg is a
  required dep on Linux + macOS).  Zero new pip deps.
* Decodes everything ffmpeg can decode -- no need to worry about WAV
  vs OGG vs MP3 vs whatever the server hands us next.
* The ``-nodisp -autoexit -loglevel quiet`` invocation is a true
  background process: no window, no extra threads in the Python
  process, no shutdown choreography needed beyond .terminate().

The class is intentionally tiny and synchronous-from-the-outside; the
Textual TUI dispatches play()/stop() from worker threads so the
event loop stays responsive even if ffplay launch is slow.

Resolution order for ffplay:
  1. ``VEZIR_FFPLAY_BIN`` env var (test/manual override).
  2. ``ffplay`` on PATH.

If ffplay is not available, ``AudioPlayer.play()`` raises
``FfplayNotFound``.  Callers should surface the error in the UI
without crashing -- labeling without audio still works (Android did
this when audio_available=False).
"""
from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
from pathlib import Path

log = logging.getLogger("vezir.client.audio")


class FfplayNotFound(RuntimeError):
    """Raised when ``ffplay`` cannot be located on PATH."""


def _resolve_ffplay() -> str | None:
    explicit = os.environ.get("VEZIR_FFPLAY_BIN")
    if explicit:
        if Path(explicit).is_file():
            return explicit
        log.warning("VEZIR_FFPLAY_BIN=%r is not a file; ignoring", explicit)
    found = shutil.which("ffplay")
    return found


def ffplay_available() -> bool:
    """True iff an ffplay binary is resolvable.

    Cheap enough to call at TUI startup to decide whether to grey out
    the play buttons before the user clicks something that would fail.
    """
    return _resolve_ffplay() is not None


class AudioPlayer:
    """Plays one WAV at a time. Stops any in-flight playback on new play().

    Thread-safe in the sense that concurrent calls to play()/stop()
    from different threads will serialize on an internal lock; the
    underlying ffplay process is always owned by the most recent
    play() call.

    Not safe to use from multiple TUI screens simultaneously (you
    probably want one instance per LabelScreen).
    """

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._current_path: Path | None = None

    @property
    def is_playing(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    @property
    def current_path(self) -> Path | None:
        with self._lock:
            return self._current_path

    def play(self, path: Path) -> None:
        """Play ``path`` from the beginning; stop any currently playing clip.

        Raises:
            FfplayNotFound: if ffplay is not on PATH.
            FileNotFoundError: if ``path`` does not exist.
        """
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(path)
        bin_path = _resolve_ffplay()
        if not bin_path:
            raise FfplayNotFound(
                "ffplay not found. Install ffmpeg (Linux: apt install ffmpeg; "
                "Mac: brew install ffmpeg) or set VEZIR_FFPLAY_BIN."
            )

        with self._lock:
            self._stop_locked()
            cmd = [
                bin_path,
                "-nodisp",
                "-autoexit",
                "-loglevel",
                "quiet",
                str(path),
            ]
            # start_new_session so a parent Ctrl+C (delivered to the
            # whole tty process group) doesn't double-up into ffplay
            # and race the explicit terminate() we do on stop().
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            self._current_path = path

    def stop(self) -> None:
        """Stop any currently-playing clip; idempotent."""
        with self._lock:
            self._stop_locked()
            self._current_path = None

    def _stop_locked(self) -> None:
        """Internal: caller must hold ``self._lock``."""
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        if proc.poll() is not None:
            return  # already exited
        try:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=1)
        except OSError:
            pass  # process already gone

    def close(self) -> None:
        """Alias for stop(); shaped so callers can use AudioPlayer as a contextmanager-adjacent."""
        self.stop()

    def __del__(self) -> None:
        try:
            self.stop()
        except Exception:
            pass


# ─── Desktop notifications (best-effort, no hard dep) ─────────────────────────


def notify_desktop(title: str, body: str) -> bool:
    """Best-effort desktop notification.

    Returns True if a notifier was successfully invoked, False otherwise.
    Never raises -- the TUI uses this from a background poll loop and a
    failed notification must not crash the app.

    Resolution order:
      Linux: notify-send (libnotify, usually pre-installed)
      macOS: osascript -e 'display notification "..." with title "..."'
      Else:  no-op, return False.
    """
    try:
        if sys.platform == "darwin":
            # AppleScript escaping: replace " with '
            safe_title = title.replace('"', "'")
            safe_body = body.replace('"', "'")
            cmd = [
                "osascript",
                "-e",
                f'display notification "{safe_body}" with title "{safe_title}"',
            ]
            subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
            return True

        if sys.platform.startswith("linux"):
            bin_path = shutil.which("notify-send")
            if not bin_path:
                return False
            subprocess.run(
                [bin_path, "--app-name=vezir", title, body],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
            return True
    except Exception as exc:
        log.debug("notify_desktop failed: %s", exc)
    return False
