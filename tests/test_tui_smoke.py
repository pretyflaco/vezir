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


# ─── PR4 regression guards: freeze fixes ─────────────────────────────────────


async def test_no_enter_binding_on_detail_screen(app, mock_server):
    """DetailScreen must NOT bind `enter` -- DataTable already handles it,
    and a screen-level binding causes a double-push of ArtifactScreen.

    Background: the muscle smoke on 2026-05-23 froze the TUI on PDF
    click.  Root cause was the DataTable's built-in
    `enter -> select_cursor` firing alongside our screen-level
    `Binding('enter', 'open_selected_artifact')`, pushing
    ArtifactScreen twice.  This test fails (in the negative sense)
    if we ever re-add the binding.
    """
    from vezir.client.tui.detail_screen import DetailScreen
    keys = [b.key for b in DetailScreen.BINDINGS]
    assert "enter" not in keys, (
        f"DetailScreen has an `enter` binding ({keys}); "
        "DataTable already handles enter natively."
    )


async def test_no_enter_binding_on_sessions_body(app, mock_server):
    """Same regression guard for SessionsBody."""
    from vezir.client.tui.sessions_screen import SessionsBody
    keys = [b.key for b in SessionsBody.BINDINGS]
    assert "enter" not in keys, (
        f"SessionsBody has an `enter` binding ({keys}); "
        "DataTable already handles enter natively."
    )


async def test_force_quit_binding_exists(app, mock_server):
    """ctrl+shift+q must be wired as the priority=True emergency exit.

    This is the user's escape hatch when a screen wedges.  Without it,
    a hung screen leaves the user with no way out short of `kill`.

    The key changed from ctrl+c to ctrl+shift+q in PR6 because the
    PR4 ctrl+c binding shadowed TextArea's built-in copy-selection
    binding and Textual's app-level selection-aware copy convention.
    A three-key chord avoids both collisions.
    """
    from vezir.client.tui.app import VezirTuiApp
    by_key = {b.key: b for b in VezirTuiApp.BINDINGS}
    assert "ctrl+shift+q" in by_key
    assert by_key["ctrl+shift+q"].priority is True
    assert by_key["ctrl+shift+q"].action == "force_quit"


async def test_ctrl_c_is_NOT_force_quit_anymore(app, mock_server):
    """Regression guard for the PR4 -> PR6 swap.

    If anyone re-adds a priority=True ctrl+c binding on the app,
    they will silently break the native ctrl+c copy semantics inside
    TextArea / Input widgets.  This test fails first.
    """
    from vezir.client.tui.app import VezirTuiApp
    for b in VezirTuiApp.BINDINGS:
        assert not (b.key == "ctrl+c" and b.action == "force_quit"), (
            "VezirTuiApp has ctrl+c bound to force_quit -- this shadows "
            "TextArea's built-in copy-selection.  Use ctrl+shift+q instead."
        )


async def test_copy_selection_binding_exists(app, mock_server):
    """ctrl+shift+c must route to Screen.action_copy_text so users can
    copy a mouse-selected region (selection-aware copy)."""
    from vezir.client.tui.app import VezirTuiApp
    by_key = {b.key: b for b in VezirTuiApp.BINDINGS}
    assert "ctrl+shift+c" in by_key
    assert "copy_text" in by_key["ctrl+shift+c"].action


async def test_artifact_screen_c_copies_body(app, mock_server, monkeypatch):
    """Pressing `c` on ArtifactScreen with a loaded text artifact calls
    app.copy_to_clipboard with the full body."""
    from vezir.client.tui.artifact_screen import ArtifactScreen
    copied: list[str] = []
    monkeypatch.setattr(
        app, "copy_to_clipboard", lambda text: copied.append(text),
    )
    screen = ArtifactScreen("01X", "summary.md")
    screen._body = "# Summary\n\nLorem ipsum dolor sit amet."
    # Mount a tiny fake app context for self.notify.  Easier path:
    # invoke the action directly; notify is best-effort so we just
    # patch it to a no-op on the screen.
    monkeypatch.setattr(screen, "notify", lambda *a, **k: None)
    # Also stub self.app on the un-mounted screen so action's
    # self.app.copy_to_clipboard goes to our list.
    monkeypatch.setattr(type(screen), "app", property(lambda self: app))
    screen.action_copy_artifact()
    assert copied == ["# Summary\n\nLorem ipsum dolor sit amet."]


async def test_artifact_screen_c_copies_path_for_binary(app, mock_server, monkeypatch):
    """For binary artifacts (no body, only tmp_path), `c` copies the path."""
    from pathlib import Path
    from vezir.client.tui.artifact_screen import ArtifactScreen
    copied: list[str] = []
    monkeypatch.setattr(
        app, "copy_to_clipboard", lambda text: copied.append(text),
    )
    screen = ArtifactScreen("01X", "report.pdf")
    screen._tmp_path = Path("/tmp/vezir-artifact-xyz.pdf")
    monkeypatch.setattr(screen, "notify", lambda *a, **k: None)
    monkeypatch.setattr(type(screen), "app", property(lambda self: app))
    screen.action_copy_artifact()
    assert copied == ["/tmp/vezir-artifact-xyz.pdf"]


async def test_artifact_screen_c_noop_when_nothing_loaded(app, mock_server, monkeypatch):
    """No body and no tmp_path -> warning notify, no clipboard write."""
    from vezir.client.tui.artifact_screen import ArtifactScreen
    copied: list[str] = []
    notified: list[tuple] = []
    monkeypatch.setattr(
        app, "copy_to_clipboard", lambda text: copied.append(text),
    )
    screen = ArtifactScreen("01X", "x.md")
    monkeypatch.setattr(screen, "notify",
                        lambda *a, **k: notified.append((a, k)))
    monkeypatch.setattr(type(screen), "app", property(lambda self: app))
    screen.action_copy_artifact()
    assert copied == []
    assert notified and "Nothing loaded" in notified[0][0][0]


async def test_detail_screen_c_copies_session_id(app, mock_server, monkeypatch):
    """Pressing `c` on DetailScreen copies the session id."""
    from vezir.client.tui.detail_screen import DetailScreen
    copied: list[str] = []
    monkeypatch.setattr(
        app, "copy_to_clipboard", lambda text: copied.append(text),
    )
    screen = DetailScreen(session_id="01KSBABCDEF")
    monkeypatch.setattr(screen, "notify", lambda *a, **k: None)
    monkeypatch.setattr(type(screen), "app", property(lambda self: app))
    screen.action_copy_session_id()
    assert copied == ["01KSBABCDEF"]


async def test_sessions_body_c_copies_selected_id(app, mock_server, monkeypatch):
    """Pressing `c` on SessionsBody copies the cursor row's session id.

    Exercised through a real app.run_test so the DataTable cursor /
    row_index is actually populated -- the action reads from
    ``self._table.cursor_coordinate`` which requires a mounted widget.
    """
    mock_server["sessions"] = [
        {"id": "01ROW0", "status": "done", "title": "row 0", "github": "alice"},
        {"id": "01ROW1", "status": "done", "title": "row 1", "github": "bob"},
    ]
    copied: list[str] = []
    monkeypatch.setattr(
        app, "copy_to_clipboard", lambda text: copied.append(text),
    )
    async with app.run_test() as pilot:
        await pilot.press("ctrl+s")  # switch to sessions tab
        await pilot.pause(0.5)
        # Cursor lands on row 0 by default.
        from textual.widgets import TabbedContent
        tabs = app.screen.query_one(TabbedContent)
        from vezir.client.tui.sessions_screen import SessionsBody
        sess_body = tabs.active_pane.query_one(SessionsBody)
        # Don't pop notifications -- patch notify into a no-op.
        monkeypatch.setattr(sess_body, "notify", lambda *a, **k: None)
        sess_body.action_copy_selected_id()
        assert copied == ["01ROW0"], (
            f"expected ['01ROW0'], got {copied}"
        )


async def test_binary_artifact_does_not_block_event_loop(
    app, mock_server, monkeypatch,
):
    """Pushing ArtifactScreen for a binary artifact must not block.

    PR2 inlined subprocess.Popen in on_binary_ready.  If the launched
    process takes the controlling terminal or stalls, the TUI freezes.
    PR4 moves the launch into a worker so the message handler returns
    immediately.

    We stub the OS opener path (_os_opener_cmd) and a fake Popen so
    no real process actually starts -- the test only verifies that
    on_binary_ready returns control to the event loop quickly.
    """
    monkeypatch.setenv("VEZIR_TUI_CRASH_ON_ERROR", "1")
    mock_server["sessions"] = [
        {
            "id": "01BIN",
            "status": "done",
            "title": "binary test",
            "github": "alice",
            "artifacts": {"pdf": "report.pdf"},
        },
    ]

    # Make the artifact endpoint return a small PDF-like blob.
    import httpx
    import vezir.client.api as api_mod
    orig_factory = api_mod.httpx.Client
    inner_orig = api_mod.httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p.startswith("/artifact/"):
            return httpx.Response(200, content=b"%PDF-1.4\n%fake pdf body")
        if p == "/api/sessions":
            return httpx.Response(200, json={"sessions": mock_server["sessions"]})
        if p.startswith("/api/sessions/"):
            sid = p.split("/")[-1]
            for s in mock_server["sessions"]:
                if s["id"] == sid:
                    return httpx.Response(200, json=s)
            return httpx.Response(404, text="nf")
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return inner_orig(*args, **kwargs)

    api_mod.httpx.Client = factory

    # Stub the opener worker's Popen so no real process spawns and so
    # we can assert that Popen WAS called -- proving the worker fires.
    from vezir.client.tui import artifact_screen as art_mod
    popen_calls: list[list[str]] = []

    class _FakeProc:
        def __init__(self, *a, **k):
            self._dead = False
            popen_calls.append(list(a[0]) if a else [])

        def poll(self):
            return 0 if self._dead else None

    monkeypatch.setattr(art_mod, "_os_opener_cmd", lambda: ["true"])
    monkeypatch.setattr(art_mod.subprocess, "Popen", _FakeProc)

    try:
        async with app.run_test() as pilot:
            from vezir.client.tui.artifact_screen import ArtifactScreen
            await app.push_screen(ArtifactScreen("01BIN", "report.pdf"))
            # Pause to let workers fire.  Critical: the test must
            # CONTINUE TO RESPOND TO EVENTS after the binary handler
            # ran -- if the TUI froze, additional pilot operations
            # would hang.  We probe by issuing pilot.press("escape").
            for _ in range(20):
                await pilot.pause(0.1)
                if popen_calls:
                    break
            assert popen_calls, "opener worker never invoked Popen"
            # Now prove the event loop is alive: escape pops the
            # ArtifactScreen.  If we froze, this would hang the test.
            await pilot.press("escape")
            await pilot.pause(0.2)
            assert app.screen.__class__.__name__ != "ArtifactScreen", (
                "escape did not pop ArtifactScreen -- TUI appears frozen"
            )
    finally:
        api_mod.httpx.Client = orig_factory


async def test_opener_failed_message_dispatches_to_handler(app, mock_server):
    """Sanity: OpenerFailed message class must have a non-underscored
    name so its handler_name matches the screen's `on_opener_failed`.

    Belt-and-braces for the leading-underscore Message bug we hit in
    PR2.  If anyone ever re-prefixes the class with `_`, this test
    fails before the next manual smoke does.
    """
    from vezir.client.tui.artifact_screen import OpenerLaunched, OpenerFailed
    assert OpenerLaunched.handler_name == "on_opener_launched"
    assert OpenerFailed.handler_name == "on_opener_failed"
