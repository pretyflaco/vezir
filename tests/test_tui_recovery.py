"""Startup recovery dialog (0.23.0): TUI offers to salvage recordings
that never reached the server.

Regression guard for the 2026-09-28 incident: an 87-minute recording
survived a TUI crash on disk but was invisible in the UI (the Sessions
tab is server-side only), so the user concluded it was lost.  The TUI
now scans the recordings roots at launch and pushes a RecoveryScreen
when salvageable sessions exist.
"""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest


@pytest.fixture
def mock_server(monkeypatch):
    """Minimal httpx stub: enough for the app to boot."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/sessions":
            return httpx.Response(200, json={"sessions": []})
        if request.url.path == "/api/me":
            return httpx.Response(200, json={
                "github": "tester", "is_admin": False,
                "memberships": [], "alternate_urls": [],
            })
        if request.url.path == "/api/team":
            return httpx.Response(200, json={"team": []})
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    import vezir.client.api as api_mod
    orig = api_mod.httpx.Client

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return orig(*args, **kwargs)

    api_mod.httpx.Client = factory
    yield
    api_mod.httpx.Client = orig


@pytest.fixture
def app(mock_server, monkeypatch, tmp_path):
    monkeypatch.setenv("VEZIR_URL", "http://test")
    monkeypatch.setenv("VEZIR_TOKEN", "vzr_" + "x" * 43)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("VEZIR_TUI_DISABLE_NOTIFY_POLL", "1")
    monkeypatch.setenv("VEZIR_TUI_DISABLE_UPDATE_CHECK", "1")
    from vezir.client.tui.app import VezirTuiApp
    return VezirTuiApp()


def _make_interrupted(home: Path) -> Path:
    d = home / "vezir-meetings" / "startups" / "meeting-20260928-135948"
    d.mkdir(parents=True)
    (d / "meeting-20260928-135948.chunk-000.wav").write_bytes(b"\x00" * 2048)
    return d


async def test_recovery_dialog_appears_for_interrupted_session(app, tmp_path):
    _make_interrupted(tmp_path)
    async with app.run_test() as pilot:
        # The scan runs in a thread worker; give it a few beats.
        for _ in range(20):
            await pilot.pause(0.1)
            if app.screen.__class__.__name__ == "RecoveryScreen":
                break
        assert app.screen.__class__.__name__ == "RecoveryScreen"


async def test_no_recovery_dialog_when_clean(app):
    async with app.run_test() as pilot:
        for _ in range(10):
            await pilot.pause(0.1)
        assert app.screen.__class__.__name__ == "MainScreen"


async def test_recovery_dialog_dismisses_on_escape(app, tmp_path):
    _make_interrupted(tmp_path)
    async with app.run_test() as pilot:
        for _ in range(20):
            await pilot.pause(0.1)
            if app.screen.__class__.__name__ == "RecoveryScreen":
                break
        assert app.screen.__class__.__name__ == "RecoveryScreen"
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "MainScreen"


async def test_recovery_dialog_describes_session(app, tmp_path):
    from textual.widgets import OptionList

    _make_interrupted(tmp_path)
    async with app.run_test() as pilot:
        for _ in range(20):
            await pilot.pause(0.1)
            if app.screen.__class__.__name__ == "RecoveryScreen":
                break
        screen = app.screen
        options = screen.query_one("#recovery-list", OptionList)
        prompt = str(options.get_option_at_index(0).prompt)
        assert "meeting-20260928-135948" in prompt
        assert "startups" in prompt
        assert "interrupted" in prompt or "audio on disk" in prompt
