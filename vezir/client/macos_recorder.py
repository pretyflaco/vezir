"""macOS lightweight recorder built on ffmpeg/AVFoundation.

This recorder avoids the full meetscribe client path. It records locally to
the same `~/meet-recordings/meeting-*` layout that `vezir scribe` already
uploads from, but it uses only ffmpeg for capture.

macOS does not expose speaker output as a normal input device. To capture
system output with AVFoundation, route output through a loopback-capable
device such as BlackHole, Loopback, Background Music, or Soundflower, then
select that device with VEZIR_MACOS_OUTPUT_DEVICE or --output-device.
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .. import config


_DEVICE_RE = re.compile(r"\[AVFoundation indev @ [^\]]+\] \[(\d+)\] (.+)")
_LOOPBACK_HINTS = (
    "blackhole",
    "loopback",
    "background music",
    "soundflower",
    "existentialaudio",
    "vb-cable",
)


@dataclass(frozen=True)
class AudioDevice:
    """One AVFoundation audio device as printed by ffmpeg."""

    index: str
    name: str


@dataclass(frozen=True)
class MacOSRecorderConfig:
    output_dir: Path
    title: str | None = None
    mic_device: str = "default"
    output_device: str = "auto"
    require_output: bool = True
    duration: float | None = None
    ffmpeg_bin: str | None = None
    sample_rate: int = 16000
    channels: int = 1


def ffmpeg_binary() -> str:
    explicit = os.environ.get("VEZIR_FFMPEG_BIN")
    if explicit:
        return explicit
    found = shutil.which("ffmpeg")
    if not found:
        raise RuntimeError(
            "ffmpeg not found in PATH. Install it with `brew install ffmpeg` "
            "or set VEZIR_FFMPEG_BIN."
        )
    return found


def ensure_macos() -> None:
    if platform.system() != "Darwin":
        raise RuntimeError("the macOS light recorder only runs on macOS")


def list_audio_devices(ffmpeg_bin: str | None = None) -> list[AudioDevice]:
    """Return AVFoundation audio devices visible to ffmpeg.

    ffmpeg exits non-zero for the device-listing probe; stderr still contains
    the useful list, so this function parses both stdout and stderr and ignores
    the exit code.
    """
    ffmpeg = ffmpeg_bin or ffmpeg_binary()
    proc = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-f",
            "avfoundation",
            "-list_devices",
            "true",
            "-i",
            "",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return parse_audio_devices("\n".join([proc.stdout, proc.stderr]))


def parse_audio_devices(ffmpeg_output: str) -> list[AudioDevice]:
    devices: list[AudioDevice] = []
    in_audio = False
    for line in ffmpeg_output.splitlines():
        if "AVFoundation audio devices:" in line:
            in_audio = True
            continue
        if "AVFoundation video devices:" in line:
            in_audio = False
            continue
        if not in_audio:
            continue
        match = _DEVICE_RE.search(line)
        if match:
            devices.append(AudioDevice(index=match.group(1), name=match.group(2).strip()))
    return devices


def _matches_device(requested: str, device: AudioDevice) -> bool:
    req = requested.casefold()
    return req == device.index.casefold() or req == device.name.casefold()


def _contains_device(requested: str, device: AudioDevice) -> bool:
    return requested.casefold() in device.name.casefold()


def _resolve_named_device(requested: str, devices: list[AudioDevice]) -> str:
    for device in devices:
        if _matches_device(requested, device):
            return device.index
    for device in devices:
        if _contains_device(requested, device):
            return device.index
    available = ", ".join(f"{d.index}:{d.name}" for d in devices) or "(none visible)"
    raise RuntimeError(f"macOS audio device {requested!r} not found. Visible devices: {available}")


def resolve_mic_device(requested: str | None, devices: list[AudioDevice]) -> str:
    requested = requested or os.environ.get("VEZIR_MACOS_MIC_DEVICE") or "default"
    if requested == "default":
        # ffmpeg's AVFoundation input accepts `:default` for the current
        # default input on normal macOS desktops.
        return "default"
    return _resolve_named_device(requested, devices)


def resolve_output_device(
    requested: str | None,
    devices: list[AudioDevice],
    *,
    require_output: bool,
) -> str | None:
    requested = requested or os.environ.get("VEZIR_MACOS_OUTPUT_DEVICE") or "auto"
    if requested.lower() in {"none", "off", "mic-only"}:
        if require_output:
            raise RuntimeError("--output-device none requires --allow-mic-only")
        return None
    if requested != "auto":
        return _resolve_named_device(requested, devices)

    for device in devices:
        lowered = device.name.casefold()
        if any(hint in lowered for hint in _LOOPBACK_HINTS):
            return device.index

    if require_output:
        available = ", ".join(f"{d.index}:{d.name}" for d in devices) or "(none visible)"
        raise RuntimeError(
            "could not auto-detect a macOS system-output capture device. "
            "Install/configure a loopback device such as BlackHole, Loopback, "
            "Background Music, or Soundflower, then pass --output-device NAME "
            "or set VEZIR_MACOS_OUTPUT_DEVICE. Visible devices: "
            f"{available}"
        )
    return None


def _session_slug(title: str | None) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    if not title:
        return f"meeting-{stamp}"
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", title.strip()).strip("-").lower()
    return f"meeting-{stamp}-{slug[:48]}" if slug else f"meeting-{stamp}"


def create_session_dir(output_dir: Path, title: str | None = None) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    session_dir = output_dir / _session_slug(title)
    counter = 1
    while session_dir.exists():
        counter += 1
        session_dir = output_dir / f"{_session_slug(title)}-{counter}"
    session_dir.mkdir(parents=True)
    return session_dir


def build_ffmpeg_command(
    *,
    ffmpeg_bin: str,
    mic_device: str,
    output_device: str | None,
    audio_path: Path,
    duration: float | None = None,
    sample_rate: int = 16000,
    channels: int = 1,
) -> list[str]:
    cmd = [ffmpeg_bin, "-hide_banner", "-y"]
    if duration is not None:
        cmd.extend(["-t", f"{duration:g}"])
    cmd.extend(["-f", "avfoundation", "-i", f":{mic_device}"])

    if output_device is not None:
        cmd.extend(["-f", "avfoundation", "-i", f":{output_device}"])
        cmd.extend(
            [
                "-filter_complex",
                (
                    "[0:a]aresample=48000,aformat=channel_layouts=stereo[mic];"
                    "[1:a]aresample=48000,aformat=channel_layouts=stereo[sys];"
                    "[mic][sys]amix=inputs=2:duration=longest:"
                    "dropout_transition=0:normalize=0[a]"
                ),
                "-map",
                "[a]",
            ]
        )
    else:
        cmd.extend(["-map", "0:a"])

    cmd.extend(
        [
            "-ac",
            str(channels),
            "-ar",
            str(sample_rate),
            "-c:a",
            "pcm_s16le",
            str(audio_path),
        ]
    )
    return cmd


def prepare_recording(config_: MacOSRecorderConfig) -> tuple[Path, Path, list[str]]:
    ensure_macos()
    ffmpeg = config_.ffmpeg_bin or ffmpeg_binary()
    devices = list_audio_devices(ffmpeg)
    if not devices and (config_.mic_device or "default") == "default":
        raise RuntimeError(
            "no AVFoundation audio devices are visible to ffmpeg. Grant the "
            "terminal app microphone permission in macOS Privacy & Security, "
            "then re-run `vezir record --recorder macos-light --list-devices`."
        )
    mic_device = resolve_mic_device(config_.mic_device, devices)
    output_device = resolve_output_device(
        config_.output_device,
        devices,
        require_output=config_.require_output,
    )
    session_dir = create_session_dir(config_.output_dir, config_.title)
    audio_path = session_dir / f"{session_dir.name}.wav"
    cmd = build_ffmpeg_command(
        ffmpeg_bin=ffmpeg,
        mic_device=mic_device,
        output_device=output_device,
        audio_path=audio_path,
        duration=config_.duration,
        sample_rate=config_.sample_rate,
        channels=config_.channels,
    )
    return session_dir, audio_path, cmd


def record(config_: MacOSRecorderConfig) -> Path:
    """Record until Ctrl+C or until config.duration elapses. Returns WAV path."""
    session_dir, audio_path, cmd = prepare_recording(config_)
    log_path = session_dir / "ffmpeg.log"
    with log_path.open("ab") as log:
        config.secure_chmod_file(log_path)
        log.write(f"--- {' '.join(cmd)}\n".encode("utf-8"))
        log.flush()
        proc = subprocess.Popen(cmd, stdout=log, stderr=log)
        try:
            proc.wait()
        except KeyboardInterrupt:
            try:
                proc.send_signal(signal.SIGINT)
            except ProcessLookupError:
                pass
            proc.wait(timeout=30)

    if proc.returncode not in (0, 255, -signal.SIGINT):
        raise RuntimeError(
            f"ffmpeg recording failed with exit code {proc.returncode}; see {log_path}"
        )
    if not audio_path.exists() or audio_path.stat().st_size == 0:
        raise RuntimeError(f"ffmpeg did not create a non-empty WAV at {audio_path}")
    config.secure_chmod_file(audio_path)
    return audio_path
