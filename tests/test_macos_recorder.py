from __future__ import annotations

import subprocess

import pytest

from vezir.client import macos_recorder


FFMPEG_DEVICES = """
[AVFoundation indev @ 0x123] AVFoundation video devices:
[AVFoundation indev @ 0x123] [0] FaceTime HD Camera
[AVFoundation indev @ 0x123] AVFoundation audio devices:
[AVFoundation indev @ 0x123] [0] MacBook Pro Microphone
[AVFoundation indev @ 0x123] [1] BlackHole 2ch
[AVFoundation indev @ 0x123] [2] External USB Mic
"""


def test_parse_audio_devices_ignores_video_devices():
    assert macos_recorder.parse_audio_devices(FFMPEG_DEVICES) == [
        macos_recorder.AudioDevice(index="0", name="MacBook Pro Microphone"),
        macos_recorder.AudioDevice(index="1", name="BlackHole 2ch"),
        macos_recorder.AudioDevice(index="2", name="External USB Mic"),
    ]


def test_resolve_mic_device_uses_default_without_visible_devices():
    assert macos_recorder.resolve_mic_device(None, []) == "default"


def test_resolve_named_mic_device_by_substring():
    devices = macos_recorder.parse_audio_devices(FFMPEG_DEVICES)

    assert macos_recorder.resolve_mic_device("external", devices) == "2"


def test_resolve_output_device_auto_detects_loopback():
    devices = macos_recorder.parse_audio_devices(FFMPEG_DEVICES)

    assert macos_recorder.resolve_output_device(
        None,
        devices,
        require_output=True,
    ) == "1"


def test_resolve_output_device_can_allow_mic_only():
    devices = [
        macos_recorder.AudioDevice(index="0", name="MacBook Pro Microphone"),
    ]

    assert macos_recorder.resolve_output_device(
        None,
        devices,
        require_output=False,
    ) is None


def test_resolve_output_device_requires_loopback_by_default():
    devices = [
        macos_recorder.AudioDevice(index="0", name="MacBook Pro Microphone"),
    ]

    with pytest.raises(RuntimeError, match="system-output capture device"):
        macos_recorder.resolve_output_device(None, devices, require_output=True)


def test_build_ffmpeg_command_mixes_mic_and_output(tmp_path):
    audio = tmp_path / "recording.wav"

    cmd = macos_recorder.build_ffmpeg_command(
        ffmpeg_bin="/opt/homebrew/bin/ffmpeg",
        mic_device="default",
        output_device="1",
        audio_path=audio,
        duration=2,
    )

    assert cmd[:10] == [
        "/opt/homebrew/bin/ffmpeg",
        "-hide_banner",
        "-y",
        "-t",
        "2",
        "-f",
        "avfoundation",
        "-i",
        ":default",
        "-f",
    ]
    assert ":1" in cmd
    assert "-filter_complex" in cmd
    assert cmd[-1] == str(audio)


def test_build_ffmpeg_command_supports_mic_only(tmp_path):
    audio = tmp_path / "recording.wav"

    cmd = macos_recorder.build_ffmpeg_command(
        ffmpeg_bin="ffmpeg",
        mic_device="default",
        output_device=None,
        audio_path=audio,
    )

    assert cmd.count("-f") == 1
    assert "-filter_complex" not in cmd
    assert cmd[-1] == str(audio)


def test_list_audio_devices_parses_ffmpeg_stderr(monkeypatch):
    class Completed:
        stdout = ""
        stderr = FFMPEG_DEVICES

    def fake_run(args, capture_output, text, timeout):
        assert args[:2] == ["ffmpeg", "-hide_banner"]
        assert capture_output is True
        assert text is True
        assert timeout == 10
        return Completed()

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert macos_recorder.list_audio_devices("ffmpeg")[1].name == "BlackHole 2ch"


def test_prepare_recording_fails_clearly_when_no_devices(monkeypatch, tmp_path):
    monkeypatch.setattr(macos_recorder, "ensure_macos", lambda: None)
    monkeypatch.setattr(macos_recorder, "list_audio_devices", lambda ffmpeg: [])

    with pytest.raises(RuntimeError, match="no AVFoundation audio devices"):
        macos_recorder.prepare_recording(
            macos_recorder.MacOSRecorderConfig(
                output_dir=tmp_path,
                ffmpeg_bin="ffmpeg",
                require_output=False,
            )
        )


def test_cli_record_lists_macos_devices(monkeypatch):
    from click.testing import CliRunner
    from vezir import cli

    monkeypatch.setattr(
        macos_recorder,
        "list_audio_devices",
        lambda: [macos_recorder.AudioDevice(index="1", name="BlackHole 2ch")],
    )

    result = CliRunner().invoke(
        cli.main,
        ["record", "--recorder", "macos-light", "--list-devices"],
    )

    assert result.exit_code == 0, result.output
    assert "1: BlackHole 2ch" in result.output
