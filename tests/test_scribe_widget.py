"""Tests for the slimmed-down Tkinter scribe widget.

Most assertions only run when tkinter is available; on CI / server
boxes without Tk we just verify the module imports cleanly and the
CLI gracefully reports the missing dependency.
"""
from __future__ import annotations

import subprocess

import pytest


def _tk_available() -> bool:
    try:
        import tkinter  # noqa: F401
        return True
    except ImportError:
        return False


def test_cli_reports_missing_tk_gracefully(monkeypatch):
    """vezir scribe-widget should print a friendly error without traceback
    when tkinter is missing, not blow up with an ImportError stacktrace."""
    if _tk_available():
        pytest.skip("tkinter available; can't simulate missing-Tk path")
    result = subprocess.run(
        ["python3", "-m", "vezir.cli", "scribe-widget"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "Tkinter" in result.stderr
    assert "python3-tk" in result.stderr
    assert "vezir tui" in result.stderr  # points to the alternative


@pytest.mark.skipif(not _tk_available(), reason="tkinter not installed")
def test_widget_helpers():
    from vezir.client.scribe_widget import _fmt_elapsed, _fmt_size
    assert _fmt_elapsed(0) == "00:00:00"
    assert _fmt_elapsed(61) == "00:01:01"
    assert _fmt_elapsed(3725) == "01:02:05"
    assert _fmt_size(500) == "500 B"
    assert _fmt_size(2048) == "2.0 KB"
    assert _fmt_size(5 * 1024 * 1024) == "5.0 MB"


@pytest.mark.skipif(not _tk_available(), reason="tkinter not installed")
def test_launch_tui_in_terminal_falls_back_gracefully(monkeypatch):
    """If no known terminal emulator is on PATH, returns False without raising."""
    from vezir.client import scribe_widget

    def fake_popen(*args, **kwargs):
        raise FileNotFoundError("no such terminal")

    monkeypatch.setattr(scribe_widget.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(scribe_widget.sys, "platform", "linux")
    assert scribe_widget._launch_tui_in_terminal() is False
