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
import math
import os
import shutil
import struct
import subprocess
import sys
import threading
from dataclasses import dataclass
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


# ─── Real-time audio level metering (v0.7.0+) ────────────────────────────────
#
# Shared by TUI, GUI, and scribe-widget for the "spectrometer" waveform
# display during recording.  Reads raw PCM from the in-progress WAV
# chunk file that millet-record's ffmpeg writes with -flush_packets 1.
#
# Cross-platform contract: the same data format (AudioLevelSample) and
# signal thresholds are used by vezir-android (Kotlin AudioRecord).


@dataclass(slots=True)
class AudioLevelSample:
    """A single audio level measurement for both channels.

    Values are 0.0-1.0 normalized.  The cross-platform contract
    shared with vezir-android.
    """
    mic_rms: float = 0.0
    sys_rms: float = 0.0
    mic_peak: float = 0.0
    sys_peak: float = 0.0


# Signal detection thresholds (cross-platform, same on Android).
SIGNAL_MIC_THRESHOLD = 0.005    # ~-46 dBFS; speech is well above
SIGNAL_SYS_THRESHOLD = 0.001    # system audio is typically louder
SILENCE_DEBOUNCE_SECS = 3.0     # seconds before declaring "no signal"

# WAV format constants for millet-record's output.
_WAV_HEADER_BYTES = 44
_BYTES_PER_STEREO_SAMPLE = 4    # 2ch × 16-bit
_SAMPLE_RATE = 16000
_LEVEL_WINDOW_MS = 100          # read last 100ms of audio
_LEVEL_WINDOW_BYTES = int(
    _SAMPLE_RATE * _BYTES_PER_STEREO_SAMPLE * _LEVEL_WINDOW_MS / 1000
)  # 6400 bytes = 1600 stereo samples = 100ms at 16kHz

# Unicode block elements for waveform rendering (9 levels: silence + 8 bars).
LEVEL_BARS = " ▁▂▃▄▅▆▇█"


def read_chunk_levels(chunk_path: "str | Path") -> AudioLevelSample:
    """Read the last ~100ms of audio from a WAV chunk and compute levels.

    The chunk is a standard WAV (pcm_s16le, 16kHz, stereo) written by
    millet-record's ffmpeg with ``-flush_packets 1``.  We seek to the
    tail and parse raw PCM — no numpy needed.

    Safe to call concurrently with ffmpeg writing (append-only file,
    POSIX read semantics).  Returns a zero-level sample if the file is
    too small or unreadable.

    Cross-platform: this function works identically on Linux and macOS.
    On Android, the equivalent computation is done on the raw
    ``AudioRecord`` buffer in Kotlin.
    """
    try:
        with open(chunk_path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            if size < _WAV_HEADER_BYTES + _BYTES_PER_STEREO_SAMPLE:
                return AudioLevelSample()
            read_from = max(_WAV_HEADER_BYTES, size - _LEVEL_WINDOW_BYTES)
            f.seek(read_from)
            raw = f.read()
    except (OSError, IOError):
        return AudioLevelSample()

    # Align to stereo sample boundary (4 bytes per stereo sample).
    n_bytes = len(raw) - (len(raw) % _BYTES_PER_STEREO_SAMPLE)
    if n_bytes < _BYTES_PER_STEREO_SAMPLE:
        return AudioLevelSample()

    n_samples = n_bytes // 2  # total int16 samples (both channels)
    try:
        samples = struct.unpack(f"<{n_samples}h", raw[:n_bytes])
    except struct.error:
        return AudioLevelSample()

    # Deinterleave: even indices = mic (L), odd = system (R).
    mic = samples[0::2]
    sys_ = samples[1::2]

    if not mic or not sys_:
        return AudioLevelSample()

    mic_sq_sum = sum(s * s for s in mic)
    sys_sq_sum = sum(s * s for s in sys_)
    n = len(mic)

    return AudioLevelSample(
        mic_rms=math.sqrt(mic_sq_sum / n) / 32768.0,
        sys_rms=math.sqrt(sys_sq_sum / n) / 32768.0,
        mic_peak=max(abs(s) for s in mic) / 32768.0,
        sys_peak=max(abs(s) for s in sys_) / 32768.0,
    )


def render_level_bars(
    history: "list[float] | tuple[float, ...]",
    *,
    scale: float = 80.0,
) -> str:
    """Convert a sequence of RMS values (0.0-1.0) to Unicode block bars.

    ``scale`` maps the RMS range to bar height.  Default 80 means an
    RMS of 0.1 (typical speech) fills the bars to ~full height.

    Returns a string of ``len(history)`` characters from :data:`LEVEL_BARS`.
    """
    return "".join(
        LEVEL_BARS[min(8, int(v * scale))]
        for v in history
    )
