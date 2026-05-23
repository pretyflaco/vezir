"""Headless smoke tests for the Textual TUI.

These don't snapshot the rendered output (that would tie us to a
specific textual version's rendering quirks).  They confirm:

* The app starts without exception.
* Each screen mounts and its compose() doesn't blow up.
* Keyboard bindings dispatch to the right actions.
* Worker plumbing (API calls) reaches the screens via posted messages.

The HTTP layer is stubbed by injecting a MockTransport into
vezir.client.api.httpx.Client (same pattern as test_client_api.py).
"""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest


@pytest.fixture
def mock_server(monkeypatch):
    """Wire a deterministic httpx transport into VezirClient.

    Default behavior: empty session list, no team, all POSTs return
    {"ok": true}.  Individual tests override by re-monkeypatching the
    handler.
    """
    state: dict = {
        "sessions": [],
        "team": [],
        "labels": {},
    }

    # Per-session-id label info, populated by tests that need it.
    state["label_info"] = {}

    def default_handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p == "/api/sessions":
            return httpx.Response(200, json={"sessions": state["sessions"]})
        if p == "/api/team":
            return httpx.Response(200, json={"team": state["team"]})
        if p.startswith("/api/sessions/"):
            sid = p.split("/")[-1]
            for s in state["sessions"]:
                if s["id"] == sid:
                    return httpx.Response(200, json=s)
            return httpx.Response(404, text="not found")
        if p.startswith("/api/label/"):
            sid = p.split("/")[-1]
            info = state["label_info"].get(sid)
            if info is not None:
                return httpx.Response(200, json=info)
            return httpx.Response(404, text="not found")
        if p == "/health":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(default_handler)

    import vezir.client.api as api_mod
    orig = api_mod.httpx.Client

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return orig(*args, **kwargs)

    api_mod.httpx.Client = factory
    yield state
    api_mod.httpx.Client = orig


@pytest.fixture
def app(mock_server, monkeypatch, tmp_path):
    """Boot a VezirTuiApp with stubbed credentials and config dir.

    Note: textual apps need an asyncio loop -- pilot.run_test() handles
    that.  We just return an instance here; the test drives it.

    The labeling-needed background poll is disabled in tests so its
    set_interval timer doesn't keep firing after teardown and isn't
    counted toward the per-test pause budget.
    """
    monkeypatch.setenv("VEZIR_URL", "http://test")
    monkeypatch.setenv("VEZIR_TOKEN", "vzr_" + "x" * 43)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("VEZIR_TUI_DISABLE_NOTIFY_POLL", "1")
    from vezir.client.tui.app import VezirTuiApp
    return VezirTuiApp()


# ─── start-up + screen mounting ──────────────────────────────────────────────


async def test_app_starts_and_mounts_main_screen(app, mock_server):
    async with app.run_test() as pilot:
        assert app.screen.__class__.__name__ == "MainScreen"


async def test_global_binding_switches_to_sessions(app, mock_server):
    from textual.widgets import TabbedContent
    async with app.run_test() as pilot:
        await pilot.press("ctrl+s")
        await pilot.pause()
        tabs = app.screen.query_one(TabbedContent)
        assert tabs.active == "sessions"


async def test_global_binding_switches_back_to_record(app, mock_server):
    from textual.widgets import TabbedContent
    async with app.run_test() as pilot:
        await pilot.press("ctrl+s")
        await pilot.pause()
        await pilot.press("ctrl+r")
        await pilot.pause()
        tabs = app.screen.query_one(TabbedContent)
        assert tabs.active == "record"


async def test_help_screen_opens_and_closes(app, mock_server):
    async with app.run_test() as pilot:
        await pilot.press("f1")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "HelpScreen"
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen.__class__.__name__ != "HelpScreen"


# ─── sessions screen ──────────────────────────────────────────────────────────


async def test_sessions_screen_renders_empty_state(app, mock_server):
    async with app.run_test() as pilot:
        await pilot.press("ctrl+s")
        # Wait for the refresh worker to land.
        await pilot.pause(0.5)
        from textual.widgets import Static
        empty = app.screen.query_one("#empty-state", Static)
        assert "No sessions" in str(empty.render())


async def test_sessions_screen_lists_rows(app, mock_server):
    mock_server["sessions"] = [
        {"id": "01A", "status": "done", "title": "first", "github": "alice"},
        {"id": "01B", "status": "needs_labeling", "title": "second", "github": "bob"},
    ]
    async with app.run_test() as pilot:
        await pilot.press("ctrl+s")
        await pilot.pause(0.5)
        from textual.widgets import DataTable
        table = app.screen.query_one("#sessions-table", DataTable)
        assert table.row_count == 2


async def test_personal_checkbox_disables_sync(app, mock_server):
    """RecordBody: flipping personal greys out sync and forces it false."""
    from textual.widgets import Checkbox
    async with app.run_test() as pilot:
        # Default tab is record.
        personal = app.screen.query_one("#personal", Checkbox)
        sync = app.screen.query_one("#sync", Checkbox)
        assert not personal.value
        assert not sync.disabled
        personal.value = True
        await pilot.pause()
        assert sync.disabled
        assert not sync.value
        personal.value = False
        await pilot.pause()
        assert not sync.disabled


# ─── detail screen: render-pipeline regression guard ─────────────────────────
#
# Background: PR2 shipped DetailScreen and LabelScreen with a helper named
# ``_render(self) -> None`` that *shadowed* ``textual.widget.Widget._render``,
# Textual's internal that produces the Visual consumed by ``to_strips()``.
# The override returned None -> every render of the screen on a real terminal
# crashed with ``AttributeError: 'NoneType' object has no attribute
# 'render_strips'``.
#
# The PR2 version of this test was *rewritten* to skip the render path after
# the same crash showed up in the pilot harness.  That rewrite removed the
# very signal that would have caught a real production bug.  Lesson: when a
# test fails with a render-pipeline NoneType, suspect API shadowing BEFORE
# blaming the test harness.
#
# These tests now drive the actual render pipeline so any future shadowing
# regression (or any other widget that emits a None visual on push) fails
# in CI rather than in the user's terminal.
#
# Note: we set VEZIR_TUI_CRASH_ON_ERROR=1 so VezirTuiApp._handle_exception
# falls through to Textual's default (re-raise) instead of swallowing the
# error as a notification.  Without this, the test would pass even with
# the bug present.


async def test_detail_screen_renders_without_crash(app, mock_server, monkeypatch):
    """Push DetailScreen and force a compositor pass.

    Before the v0.2.0-tui PR3 fix, this raised
    ``AttributeError: 'NoneType' object has no attribute 'render_strips'``
    on the first ``pilot.pause()`` after the screen mounts.  After the
    ``_render`` -> ``_refresh_view`` rename, the screen renders cleanly
    and we can assert the metadata Static actually contains the loaded
    title.
    """
    monkeypatch.setenv("VEZIR_TUI_CRASH_ON_ERROR", "1")
    mock_server["sessions"] = [
        {
            "id": "01XYZ",
            "status": "done",
            "title": "detail render test",
            "github": "alice",
            "summary_preset": "high-quality",
            "summary_error": None,
            "sync_error": None,
            "personal": 0,
            "artifacts": {"summary": "summary.md", "transcript": "t.txt"},
        },
    ]
    async with app.run_test() as pilot:
        from vezir.client.tui.detail_screen import DetailScreen
        await app.push_screen(DetailScreen(session_id="01XYZ"))
        # First pause: compositor pass that previously crashed.
        await pilot.pause(0.1)
        assert app.screen.__class__.__name__ == "DetailScreen"
        # Worker runs in a thread (textual @work(thread=True)).  Poll the
        # rendered meta block until the loading placeholder is replaced
        # with the loaded title -- up to 2 seconds total.
        from textual.widgets import Static, DataTable
        meta = app.screen.query_one("#meta", Static)
        for _ in range(20):
            await pilot.pause(0.1)
            meta_text = str(getattr(meta, "content", getattr(meta, "renderable", "")))
            if "detail render test" in meta_text:
                break
        else:
            raise AssertionError(
                f"meta block never loaded; final value: {meta_text!r}"
            )
        assert "high-quality" in meta_text
        # Artifact table populated.
        table = app.screen.query_one("#artifacts-table", DataTable)
        assert table.row_count == 2


async def test_label_screen_renders_without_crash(app, mock_server, monkeypatch):
    """Same regression guard for LabelScreen.

    LabelScreen had the identical ``_render`` shadow bug in PR2.  This
    test drives the render pipeline so a future shadow regression fails
    here rather than in production.
    """
    monkeypatch.setenv("VEZIR_TUI_CRASH_ON_ERROR", "1")
    mock_server["label_info"]["01LABEL"] = {
        "session_id": "01LABEL",
        "status": "needs_labeling",
        "speakers": [
            {"id": "SPEAKER_00", "channel": "mic",
             "sample_text": "Hello team"},
            {"id": "SPEAKER_01", "channel": "system",
             "sample_text": "Yes I agree"},
        ],
        "team": ["alice", "bob", "kasita"],
        "audio_available": True,
    }
    async with app.run_test() as pilot:
        from vezir.client.tui.label_screen import LabelScreen
        await app.push_screen(LabelScreen(session_id="01LABEL"))
        # Compositor pass that previously crashed.
        await pilot.pause(0.1)
        assert app.screen.__class__.__name__ == "LabelScreen"
        # Worker runs in a thread; poll the rendered speaker count
        # until the two Inputs land, up to 2 seconds.
        from textual.widgets import Input
        for _ in range(20):
            await pilot.pause(0.1)
            inputs = list(app.screen.query(Input))
            if len(inputs) == 2:
                break
        else:
            raise AssertionError(
                f"speaker rows never populated; final count: {len(inputs)}"
            )
