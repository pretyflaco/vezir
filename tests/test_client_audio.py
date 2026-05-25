"""Tests for vezir/client/audio.py (ffplay wrapper + desktop notify).

Subprocess invocations are stubbed so the suite stays hermetic and
doesn't require ffplay or notify-send on the test runner.
"""
from __future__ import annotations


import pytest


def test_ffplay_available_finds_path(monkeypatch):
    from vezir.client import audio

    monkeypatch.setattr(audio.shutil, "which", lambda b: "/usr/bin/ffplay")
    monkeypatch.delenv("VEZIR_FFPLAY_BIN", raising=False)
    assert audio.ffplay_available() is True


def test_ffplay_available_returns_false_when_missing(monkeypatch):
    from vezir.client import audio

    monkeypatch.setattr(audio.shutil, "which", lambda b: None)
    monkeypatch.delenv("VEZIR_FFPLAY_BIN", raising=False)
    assert audio.ffplay_available() is False


def test_env_override_takes_precedence(monkeypatch, tmp_path):
    from vezir.client import audio

    fake_bin = tmp_path / "ffplay-mock"
    fake_bin.write_text("#!/bin/sh\nsleep 0\n")
    fake_bin.chmod(0o755)
    monkeypatch.setenv("VEZIR_FFPLAY_BIN", str(fake_bin))
    monkeypatch.setattr(audio.shutil, "which", lambda b: None)
    assert audio._resolve_ffplay() == str(fake_bin)


def test_play_raises_filenotfound_for_missing_clip(monkeypatch, tmp_path):
    from vezir.client import audio

    monkeypatch.setattr(audio.shutil, "which", lambda b: "/usr/bin/ffplay")
    player = audio.AudioPlayer()
    with pytest.raises(FileNotFoundError):
        player.play(tmp_path / "missing.wav")


def test_play_raises_ffplaynotfound_when_no_binary(monkeypatch, tmp_path):
    from vezir.client import audio

    monkeypatch.setattr(audio.shutil, "which", lambda b: None)
    monkeypatch.delenv("VEZIR_FFPLAY_BIN", raising=False)
    clip = tmp_path / "x.wav"
    clip.write_bytes(b"RIFF")
    player = audio.AudioPlayer()
    with pytest.raises(audio.FfplayNotFound):
        player.play(clip)


def test_play_invokes_ffplay_with_expected_args(monkeypatch, tmp_path):
    from vezir.client import audio

    monkeypatch.setattr(audio.shutil, "which", lambda b: "/usr/bin/ffplay")
    monkeypatch.delenv("VEZIR_FFPLAY_BIN", raising=False)
    clip = tmp_path / "x.wav"
    clip.write_bytes(b"RIFF")

    captured: dict = {}

    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            captured["cmd"] = list(cmd)
            captured["kwargs"] = kwargs

        def poll(self):
            return None  # still running

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr(audio.subprocess, "Popen", _FakePopen)
    player = audio.AudioPlayer()
    player.play(clip)
    assert captured["cmd"][0] == "/usr/bin/ffplay"
    assert "-nodisp" in captured["cmd"]
    assert "-autoexit" in captured["cmd"]
    assert captured["cmd"][-1] == str(clip)
    assert captured["kwargs"].get("start_new_session") is True
    # Cleanup so __del__ doesn't try to terminate the fake process
    # with stale state in another test.
    player.stop()


def test_second_play_stops_first(monkeypatch, tmp_path):
    """Playing a new clip should terminate the currently-playing one."""
    from vezir.client import audio

    monkeypatch.setattr(audio.shutil, "which", lambda b: "/usr/bin/ffplay")
    clip1 = tmp_path / "a.wav"
    clip2 = tmp_path / "b.wav"
    for c in (clip1, clip2):
        c.write_bytes(b"RIFF")

    terminated: list = []

    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            self._cmd = cmd
            self._dead = False

        def poll(self):
            return 0 if self._dead else None

        def terminate(self):
            self._dead = True
            terminated.append(self._cmd[-1])

        def wait(self, timeout=None):
            return 0

        def kill(self):
            self._dead = True

    monkeypatch.setattr(audio.subprocess, "Popen", _FakePopen)
    player = audio.AudioPlayer()
    player.play(clip1)
    assert player.current_path == clip1
    player.play(clip2)
    # The first clip must have been terminated before the second started.
    assert str(clip1) in terminated
    assert player.current_path == clip2
    player.stop()


def test_stop_idempotent(monkeypatch, tmp_path):
    from vezir.client import audio

    player = audio.AudioPlayer()
    player.stop()  # no current proc
    player.stop()
    assert not player.is_playing


# ─── notify_desktop ──────────────────────────────────────────────────────────


def test_notify_desktop_linux_calls_notify_send(monkeypatch):
    from vezir.client import audio

    monkeypatch.setattr(audio.sys, "platform", "linux")
    monkeypatch.setattr(audio.shutil, "which", lambda b: "/usr/bin/notify-send")

    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd

        class R:
            returncode = 0

        return R()

    monkeypatch.setattr(audio.subprocess, "run", fake_run)
    ok = audio.notify_desktop("title", "body")
    assert ok is True
    assert "notify-send" in captured["cmd"][0]
    assert "title" in captured["cmd"]
    assert "body" in captured["cmd"]


def test_notify_desktop_linux_returns_false_without_notify_send(monkeypatch):
    from vezir.client import audio

    monkeypatch.setattr(audio.sys, "platform", "linux")
    monkeypatch.setattr(audio.shutil, "which", lambda b: None)
    assert audio.notify_desktop("t", "b") is False


def test_notify_desktop_mac_uses_osascript(monkeypatch):
    from vezir.client import audio

    monkeypatch.setattr(audio.sys, "platform", "darwin")
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd

        class R:
            returncode = 0

        return R()

    monkeypatch.setattr(audio.subprocess, "run", fake_run)
    ok = audio.notify_desktop('quote " in title', "body")
    assert ok is True
    assert captured["cmd"][0] == "osascript"
    # Quote in title must be escaped to single quote so osascript parses.
    apple_script = captured["cmd"][2]
    assert '"' not in apple_script.split('with title "')[1].rstrip('"')


def test_notify_desktop_unknown_platform_returns_false(monkeypatch):
    from vezir.client import audio

    monkeypatch.setattr(audio.sys, "platform", "freebsd13")
    assert audio.notify_desktop("t", "b") is False


def test_notify_desktop_swallows_exceptions(monkeypatch):
    from vezir.client import audio

    monkeypatch.setattr(audio.sys, "platform", "linux")
    monkeypatch.setattr(audio.shutil, "which", lambda b: "/bin/notify-send")

    def boom(*a, **k):
        raise OSError("simulated permission denied")

    monkeypatch.setattr(audio.subprocess, "run", boom)
    # Must not raise even when the notifier crashes.
    assert audio.notify_desktop("t", "b") is False
