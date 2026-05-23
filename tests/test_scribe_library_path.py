"""Tests for the library-direct recording path in vezir/client/scribe.py.

Confirms:

  * ``_record_via_library`` returns None (falls back) when:
      * meet_record library not importable, OR
      * caller passed extra_record_args we don't translate yet.
  * ``_record_via_subprocess`` (the fallback path) honors
    extra_record_args and calls the legacy ``meet record`` binary.
  * ``run_scribe`` automatically falls back to subprocess when the
    library path returns None.

We don't actually drive the meet_record library here -- that would
require working audio devices.  Patching the library symbols lets us
verify the dispatch and contract.
"""
from __future__ import annotations

import subprocess as _real_subprocess
from pathlib import Path

import pytest


def test_library_path_returns_none_when_extra_args_present(monkeypatch, tmp_path):
    from vezir.client import scribe
    # Even if meet_record is importable, the presence of extras forces
    # the subprocess fallback (we don't translate args yet).
    result = scribe._record_via_library(tmp_path, ["--virtual-sink"])
    assert result is None


def test_library_path_returns_none_when_meet_record_missing(monkeypatch, tmp_path):
    """Simulate a deployment where meetscribe-record is not installed."""
    from vezir.client import scribe
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("meet_record"):
            raise ImportError("simulated missing meet_record")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    result = scribe._record_via_library(tmp_path, None)
    assert result is None


def test_subprocess_fallback_invokes_meet_record(monkeypatch, tmp_path):
    """The subprocess fallback should spawn `meet record -o <dir>` and
    return the produced audio path on success."""
    from vezir.client import scribe

    invocations: list[list[str]] = []

    class _FakeProc:
        returncode = 0

        def wait(self, timeout=None):
            return 0

        def send_signal(self, sig):
            pass

        def kill(self):
            pass

    def fake_popen(cmd, *args, **kwargs):
        invocations.append(list(cmd))
        return _FakeProc()

    monkeypatch.setattr(scribe.subprocess, "Popen", fake_popen)

    # Simulate the session dir + audio file that meet record would
    # have produced.
    sdir = tmp_path / "meeting-fake"
    sdir.mkdir()
    audio = sdir / "meeting-fake.wav"
    audio.write_bytes(b"RIFFsomewav")
    monkeypatch.setattr(scribe, "_find_latest_session", lambda *a, **k: sdir)

    out = scribe._record_via_subprocess("/bin/meet", tmp_path, ["--virtual-sink"])
    assert out == audio
    assert invocations == [["/bin/meet", "record", "-o", str(tmp_path), "--virtual-sink"]]


def test_subprocess_fallback_returns_none_when_no_audio_emitted(monkeypatch, tmp_path):
    from vezir.client import scribe

    class _FakeProc:
        returncode = 0
        def wait(self, timeout=None): return 0
        def send_signal(self, sig): pass
        def kill(self): pass

    monkeypatch.setattr(scribe.subprocess, "Popen", lambda *a, **k: _FakeProc())
    monkeypatch.setattr(scribe, "_find_latest_session", lambda *a, **k: None)
    out = scribe._record_via_subprocess("/bin/meet", tmp_path, None)
    assert out is None


def test_run_scribe_falls_back_to_subprocess_when_library_returns_none(
    monkeypatch, tmp_path,
):
    """End-to-end: library path returns None -> subprocess path runs."""
    from vezir.client import scribe as scribe_mod

    monkeypatch.setattr(
        scribe_mod, "_record_via_library", lambda *a, **k: None,
    )
    audio = tmp_path / "fallback.wav"
    audio.write_bytes(b"RIFFsomewav")
    monkeypatch.setattr(
        scribe_mod, "_record_via_subprocess",
        lambda *a, **k: audio,
    )
    monkeypatch.setattr(scribe_mod, "_meet_bin", lambda: "/bin/meet")
    monkeypatch.setattr(scribe_mod, "_check_meet_prerequisites", lambda *_: None)
    monkeypatch.setattr(
        scribe_mod.uploader, "upload",
        lambda *a, **k: {"session_id": "01X", "dashboard_url": "http://x"},
    )

    result = scribe_mod.run_scribe(
        server_url="http://x",
        token="vzr_" + "x" * 43,
        compress=False,
        wait=False,
    )
    assert result["session_id"] == "01X"
