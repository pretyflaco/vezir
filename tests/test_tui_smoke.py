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
        # v0.7.6: /api/me memberships for the Teams tab tests.
        "memberships": [],
    }

    # Per-session-id label info, populated by tests that need it.
    state["label_info"] = {}

    def default_handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p == "/api/sessions":
            return httpx.Response(200, json={"sessions": state["sessions"]})
        if p == "/api/me":
            return httpx.Response(200, json={
                "github": "tester",
                "is_admin": False,
                "memberships": state["memberships"],
                "alternate_urls": [],
            })
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
    async with app.run_test():
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


# ─── team identity / switching (v0.7.4 slug↔UUID regression) ─────────────────


def _mock_api_me(monkeypatch, memberships):
    """Patch httpx.get (imported locally inside _refresh_identity) to
    return a /api/me payload with the given memberships."""

    def fake_get(url, *args, **kwargs):
        return httpx.Response(
            200,
            json={
                "github": "pretyflaco",
                "is_admin": True,
                "memberships": memberships,
                "alternate_urls": [],
            },
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx, "get", fake_get)


async def test_refresh_identity_slug_matches_uuid_membership(
    app, mock_server, monkeypatch
):
    """A slug-configured active team must resolve against UUID-keyed
    memberships WITHOUT being rewritten to the UUID (the v0.7.4 bug
    that broke the TUI team switcher)."""
    _mock_api_me(monkeypatch, [
        {"team_id": "5e0d4eecd0e24b2a8dbc517adb486199", "slug": "blink",
         "role": "scribe", "team_name": "Blink"},
    ])
    async with app.run_test():
        app.active_team_id = "blink"  # slug, as configured in teams.json
        # v0.12.1: _refresh_identity is now a thread worker; wait for it.
        app._refresh_identity()
        await app.workers.wait_for_complete()
        # active_team_id stays the slug, not the UUID.
        assert app.active_team_id == "blink"
        # Label resolves to the human team name.
        assert app.team_label == "Blink"


async def test_refresh_identity_fallback_keeps_slug_not_uuid(
    app, mock_server, monkeypatch
):
    """When the configured team isn't matched and we fall back to the
    first membership, adopt its SLUG (so next_team_id's slug cycle
    still works), never the bare UUID."""
    _mock_api_me(monkeypatch, [
        {"team_id": "8ec688fd1a1a4b94b9ce7dbafb6e330c", "slug": "startups",
         "role": "admin", "team_name": "startups"},
    ])
    async with app.run_test():
        app.active_team_id = "not-a-configured-team"
        app._refresh_identity()
        await app.workers.wait_for_complete()
        assert app.active_team_id == "startups"  # slug, not the UUID
        assert app.team_label == "startups"


# ─── Teams tab + auto-discovery (v0.7.6) ─────────────────────────────────────


_MEMBERSHIPS_3 = [
    {"team_id": "uuid-blink", "slug": "blink", "role": "scribe",
     "team_name": "Blink"},
    {"team_id": "uuid-21", "slug": "twentyone", "role": "admin",
     "team_name": "Twentyone"},
    {"team_id": "uuid-abct", "slug": "abcapetown", "role": "admin",
     "team_name": "AB Cape Town"},
]


async def test_all_teams_merges_discovered_memberships(app, mock_server):
    """all_teams() surfaces every /api/me membership even with an empty
    teams.json — the core of feature (1)."""
    async with app.run_test():
        app.memberships = list(_MEMBERSHIPS_3)
        teams = app.all_teams()
        slugs = [t["slug"] for t in teams]
        assert slugs == ["abcapetown", "blink", "twentyone"]  # slug-sorted
        # All discovered, all inherit the current token.
        assert all(t["source"] == "discovered" for t in teams)
        assert all(t["token"] == app.token for t in teams)
        # Labels come from team_name; roles preserved.
        blink = next(t for t in teams if t["slug"] == "blink")
        assert blink["label"] == "Blink"
        assert blink["role"] == "scribe"


async def test_team_slug_for_maps_uuid_to_slug(app, mock_server):
    """team_slug_for() resolves a server UUID to the on-disk team slug so
    local-folder lookups hit ~/vezir-meetings/<slug>/ (regression: open
    folder said 'No artifacts' because it looked under the UUID dir)."""
    async with app.run_test():
        app.memberships = list(_MEMBERSHIPS_3)
        # UUID -> slug.
        assert app.team_slug_for("uuid-blink") == "blink"
        assert app.team_slug_for("uuid-21") == "twentyone"
        # Already a slug -> unchanged.
        assert app.team_slug_for("blink") == "blink"
        # Unknown id -> returned as-is (global-scan fallback covers it).
        assert app.team_slug_for("uuid-unknown") == "uuid-unknown"
        # None -> None.
        assert app.team_slug_for(None) is None


async def test_all_teams_config_overrides_discovered(app, mock_server, monkeypatch):
    """A teams.json entry (own server/token) takes precedence over the
    discovered membership of the same slug."""
    from vezir.client import config as cfg_mod
    async with app.run_test():
        app.memberships = list(_MEMBERSHIPS_3)
        monkeypatch.setattr(cfg_mod, "load_teams_config", lambda: {
            "teams": [{"id": "blink", "url": "https://other",
                       "token": "vzr_other", "label": "Blink HQ"}],
            "active": "blink",
        })
        # app.all_teams reads load_teams_config via its own import site.
        monkeypatch.setattr(
            "vezir.client.tui.app.load_teams_config",
            cfg_mod.load_teams_config,
        )
        teams = app.all_teams()
        blink = next(t for t in teams if t["slug"] == "blink")
        assert blink["source"] == "config"
        assert blink["url"] == "https://other"
        assert blink["token"] == "vzr_other"
        assert blink["label"] == "Blink HQ"
        # Role still enriched from the discovered membership.
        assert blink["role"] == "scribe"


async def test_switch_to_discovered_team_preserves_token(app, mock_server):
    """Switching to a discovered-only team keeps the same bearer token
    and only changes the team scope (X-Team-Id)."""
    async with app.run_test():
        app.memberships = list(_MEMBERSHIPS_3)
        original_token = app.token
        ok = app.switch_to_team("twentyone")
        assert ok is True
        assert app.active_team_id == "twentyone"
        assert app.token == original_token       # token unchanged
        assert app.api.token == original_token
        assert app.api.team_id == "twentyone"     # X-Team-Id updated
        # Discovered-only selection is in-memory, not persisted.
        assert app._discovered_active == "twentyone"
        assert app.cred_source == "discovered:twentyone"


async def test_ctrl_t_cycles_all_discovered_teams(app, mock_server):
    """^t cycles through ALL discovered teams without teams.json — the
    fix for the laptop showing only manually-added teams."""
    async with app.run_test() as pilot:
        app.memberships = list(_MEMBERSHIPS_3)
        app.active_team_id = "abcapetown"  # first in slug-sorted order
        visited = []
        for _ in range(3):
            await pilot.press("ctrl+t")
            await pilot.pause()
            visited.append(app.active_team_id)
        # slug-sorted: abcapetown -> blink -> twentyone -> abcapetown
        assert visited == ["blink", "twentyone", "abcapetown"]


async def test_teams_tab_lists_memberships(app, mock_server):
    """The Teams tab DataTable lists every membership from /api/me."""
    from textual.widgets import DataTable

    from vezir.client.tui.teams_screen import TeamsBody
    mock_server["memberships"] = list(_MEMBERSHIPS_3)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+e")
        await pilot.pause(0.5)
        body = app.screen.query_one(TeamsBody)
        table = body.query_one(DataTable)
        assert table.row_count == 3


async def test_teams_tab_single_membership_still_lists(app, mock_server):
    from textual.widgets import DataTable

    from vezir.client.tui.teams_screen import TeamsBody
    mock_server["memberships"] = [_MEMBERSHIPS_3[0]]
    async with app.run_test() as pilot:
        await pilot.press("ctrl+e")
        await pilot.pause(0.5)
        body = app.screen.query_one(TeamsBody)
        table = body.query_one(DataTable)
        assert table.row_count == 1


async def test_ctrl_t_single_team_is_noop(app, mock_server):
    async with app.run_test() as pilot:
        app.memberships = [_MEMBERSHIPS_3[0]]
        app.active_team_id = "blink"
        await pilot.press("ctrl+t")
        await pilot.pause()
        assert app.active_team_id == "blink"  # unchanged


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


# ─── PR9 regression guards: sessions auto-refresh ─────────────────────────────
#
# Background: dogfood report 2026-05-24 -- a session recorded in the
# TUI didn't appear in the Sessions list until the TUI was restarted.
# Root cause: SessionsBody refreshed only on its own ``on_mount``;
# nothing reacted to tab switches or upload completion.  PR9 added
# ``MainScreen.on_tabbed_content_tab_activated`` and
# ``MainScreen.on_session_upload_complete`` to fix both paths.


async def _wait_for_row_count(app, pilot, *, target: int, attempts: int = 30):
    """Poll until DataTable.row_count == target, or fail after attempts."""
    from textual.widgets import DataTable
    for _ in range(attempts):
        await pilot.pause(0.1)
        try:
            table = app.screen.query_one("#sessions-table", DataTable)
        except Exception:
            continue
        if table.row_count == target:
            return table
    raise AssertionError(
        f"row_count never reached {target}; last seen "
        f"{table.row_count if 'table' in dir() else 'no table'}"
    )


async def test_sessions_refreshes_on_tab_activation(app, mock_server):
    """PR9: switching to Sessions tab must re-fetch /api/sessions.

    Without this, a session added after the TUI's first mount stays
    invisible until the user restarts the TUI (the exact dogfood
    report from 2026-05-24).
    """
    mock_server["sessions"] = [
        {"id": "01A", "status": "done", "title": "first", "github": "alice"},
    ]
    async with app.run_test() as pilot:
        # Initial mount triggers a refresh that lands 1 row.
        await pilot.press("ctrl+s")
        await _wait_for_row_count(app, pilot, target=1)

        # Simulate "a new session arrived on the server while the user
        # was on the Record tab".
        await pilot.press("ctrl+r")  # back to Record
        await pilot.pause(0.1)
        mock_server["sessions"].append({
            "id": "01B", "status": "done",
            "title": "fresh", "github": "alice",
        })

        # Switch back to Sessions -> PR9's TabActivated handler
        # should fire action_refresh and the new row should appear.
        await pilot.press("ctrl+s")
        await _wait_for_row_count(app, pilot, target=2)


async def test_sessions_refreshes_on_upload_complete(app, mock_server):
    """PR9: when _poll_worker reports terminal status, SessionsBody
    must refresh -- even if the Sessions tab isn't currently active.

    Simulates the message arriving by posting SessionUploadComplete
    directly to the screen; this is the contract between RecordBody
    and MainScreen.
    """
    mock_server["sessions"] = []
    async with app.run_test() as pilot:
        # On Record tab initially.  Pre-populate the server so the
        # refresh has something to land.
        mock_server["sessions"] = [
            {"id": "01FRESH", "status": "done",
             "title": "just uploaded", "github": "alice"},
        ]

        # Post the completion message; bubbles to MainScreen's handler.
        from vezir.client.tui.record_screen import SessionUploadComplete
        app.screen.post_message(SessionUploadComplete(
            session_id="01FRESH",
            status="done",
        ))
        await pilot.pause(0.2)

        # Now switch to Sessions -- the refresh should already have
        # landed (or be in flight).  Wait up to 3s.
        await pilot.press("ctrl+s")
        await _wait_for_row_count(app, pilot, target=1)


async def test_session_upload_complete_toasts_user(app, mock_server, monkeypatch):
    """PR9: a terminal-status completion should produce a notification
    so the user knows their session is ready without having to
    eyeball the Sessions tab.
    """
    captured: list[tuple[str, str]] = []

    from vezir.client.tui.app import VezirTuiApp
    orig_notify = VezirTuiApp.notify

    def fake_notify(self, message, *, severity="information", **kw):
        captured.append((severity, str(message)))
        return orig_notify(self, message, severity=severity, **kw)

    monkeypatch.setattr(VezirTuiApp, "notify", fake_notify)

    async with app.run_test() as pilot:
        from vezir.client.tui.record_screen import SessionUploadComplete
        app.screen.post_message(SessionUploadComplete(
            session_id="01ABCDEFGHIJ",
            status="done",
        ))
        await pilot.pause(0.2)

        upload_toasts = [
            (sev, msg) for sev, msg in captured if "01ABCDEF" in msg
        ]
        assert upload_toasts, (
            f"no upload-completion toast captured; all: {captured}"
        )
        sev, msg = upload_toasts[0]
        assert sev == "information"
        assert "ready" in msg


async def test_personal_toggle_disables_sync(app, mock_server):
    """RecordBody: flipping personal greys out sync and forces it false.

    v0.4.2 replaced Checkboxes with toggle-Buttons.  The personal
    toggle uses CSS class ``toggle-personal-on`` (variant=warning)
    and the sync toggle uses ``toggle-on`` (variant=success).  When
    personal is on, the sync button must be disabled + off.  When
    personal is turned off again, sync must re-enable.
    """
    from textual.widgets import Button
    async with app.run_test() as pilot:
        # Default tab is record.
        personal = app.screen.query_one("#personal-btn", Button)
        sync = app.screen.query_one("#sync-btn", Button)
        # Default: personal off, sync enabled.
        assert "toggle-personal-on" not in personal.classes
        assert not sync.disabled
        # Turn personal on via the action (simulates button press).
        app.screen.query_one("RecordBody").action_toggle_personal()
        await pilot.pause()
        assert "toggle-personal-on" in personal.classes
        assert sync.disabled
        assert "toggle-on" not in sync.classes
        # Turn personal off again.
        app.screen.query_one("RecordBody").action_toggle_personal()
        await pilot.pause()
        assert "toggle-personal-on" not in personal.classes
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
        from textual.widgets import DataTable, Static
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


async def test_label_screen_inputs_have_visible_text_area(
    app, mock_server, monkeypatch
):
    """PR8 regression: LabelScreen.speaker-row must be tall enough
    to contain Textual's default Input (height: 3 = border-top +
    content + border-bottom).

    Previously the CSS had ``height: 3`` AND ``padding: 0 0 1 0`` on
    the row, leaving only 2 rows of usable content space.  The
    Input's content row (where typed text appears) was clipped
    entirely so users saw no text while typing, and the Button's
    top + bottom borders consumed both visible rows, rendering the
    "▶ Play" label nowhere ("all black box" bug from the 2026-05-23
    dogfood).
    """
    monkeypatch.setenv("VEZIR_TUI_CRASH_ON_ERROR", "1")
    mock_server["label_info"]["01LABEL2"] = {
        "session_id": "01LABEL2",
        "status": "needs_labeling",
        "speakers": [
            {"id": "SPEAKER_00", "channel": "mic",
             "sample_text": "Hello team"},
        ],
        "team": ["alice", "bob", "kasita"],
        "audio_available": True,
    }
    async with app.run_test(size=(120, 40)) as pilot:
        from textual.widgets import Button, Input

        from vezir.client.tui.label_screen import LabelScreen
        await app.push_screen(LabelScreen(session_id="01LABEL2"))
        # Poll until the speaker row mounts (worker thread).
        for _ in range(20):
            await pilot.pause(0.1)
            inputs = list(app.screen.query(Input))
            if inputs:
                break
        else:
            raise AssertionError("speaker Input never mounted")

        inp = inputs[0]
        # Layout assertion: Input needs at least 3 rows (its default).
        # Earlier (height:3 row + padding-bottom 1), Input was clipped
        # to 2 rows -- content row hidden.
        assert inp.region.height >= 3, (
            f"Input clipped to height {inp.region.height}; "
            "speaker-row CSS too short for Input's default height: 3."
        )
        # The play button must also have a non-trivial visible region.
        btn = app.screen.query_one(Button)
        assert btn.region.height >= 3, (
            f"Play button clipped to height {btn.region.height}; "
            "Button needs 3 rows (border-top + label + border-bottom)."
        )

        # Functional assertion: typing into the Input must update
        # its .value reactive.  This is the user-observable bug --
        # 'I type but see nothing'.
        inp.focus()
        await pilot.pause()
        await pilot.press(*"alice")
        await pilot.pause()
        assert inp.value == "alice", (
            f"Input did not receive typed text; value={inp.value!r}"
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
    from vezir.client.tui.artifact_screen import OpenerFailed, OpenerLaunched
    assert OpenerLaunched.handler_name == "on_opener_launched"
    assert OpenerFailed.handler_name == "on_opener_failed"


# ─── PR7: clipboard dual-write regression guards ─────────────────────────────


async def test_copy_dual_writes_to_xclip_on_linux_x11(app, mock_server, monkeypatch):
    """On a Linux X11 box (xclip on PATH, no WAYLAND_DISPLAY), copy
    should call BOTH the OSC 52 super() and subprocess.run with xclip.

    Background: PR6 shipped OSC 52 via Textual's default
    copy_to_clipboard.  That works in Ghostty but is disabled by
    default in gnome-terminal / VTE.  PR7 adds a subprocess fallback
    so the system clipboard gets populated regardless.
    """
    import shutil as _sh
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(
        _sh, "which",
        lambda name: "/usr/bin/xclip" if name == "xclip" else None,
    )
    # Clear the discovery cache so this test gets a fresh probe.
    if hasattr(app, "_clipboard_cmd_cache"):
        delattr(app, "_clipboard_cmd_cache")

    # Capture subprocess.run.
    import subprocess as _sp
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["input"] = kwargs.get("input")

        class _R:
            returncode = 0

        return _R()

    monkeypatch.setattr(_sp, "run", fake_run)

    # Also stub the Textual driver write so OSC 52 doesn't write to
    # the real stdout.  We can't easily inspect _driver.write from
    # outside (App._driver is None outside a run_test).
    async with app.run_test() as pilot:
        app.copy_to_clipboard("hello-world")
        await pilot.pause(0.1)

    assert captured["cmd"] == ["xclip", "-selection", "clipboard"]
    assert captured["input"] == b"hello-world"


async def test_copy_prefers_wl_copy_on_wayland(app, mock_server, monkeypatch):
    """When WAYLAND_DISPLAY is set AND wl-copy is on PATH, wl-copy
    must win over xclip (Wayland-first ordering)."""
    import shutil as _sh
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setattr(
        _sh, "which",
        lambda name: {
            "wl-copy": "/usr/bin/wl-copy",
            "xclip": "/usr/bin/xclip",
        }.get(name),
    )
    if hasattr(app, "_clipboard_cmd_cache"):
        delattr(app, "_clipboard_cmd_cache")

    import subprocess as _sp
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["input"] = kwargs.get("input")

        class _R:
            returncode = 0

        return _R()

    monkeypatch.setattr(_sp, "run", fake_run)
    async with app.run_test() as pilot:
        app.copy_to_clipboard("hello-wayland")
        await pilot.pause(0.1)

    assert captured["cmd"] == ["wl-copy"]
    assert captured["input"] == b"hello-wayland"


async def test_copy_falls_through_to_pbcopy_on_mac(app, mock_server, monkeypatch):
    """No wl-copy, no xclip, but pbcopy present -> pbcopy used.

    Simulated by patching shutil.which; we don't actually need to be
    on macOS for this test.
    """
    import shutil as _sh
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(
        _sh, "which",
        lambda name: "/usr/bin/pbcopy" if name == "pbcopy" else None,
    )
    if hasattr(app, "_clipboard_cmd_cache"):
        delattr(app, "_clipboard_cmd_cache")

    import subprocess as _sp
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["input"] = kwargs.get("input")

        class _R:
            returncode = 0

        return _R()

    monkeypatch.setattr(_sp, "run", fake_run)
    async with app.run_test() as pilot:
        app.copy_to_clipboard("hello-mac")
        await pilot.pause(0.1)

    assert captured["cmd"] == ["pbcopy"]


async def test_copy_no_utility_does_not_crash(app, mock_server, monkeypatch):
    """All clipboard utilities missing -> no subprocess call, no
    exception.  OSC 52 still attempted via super().
    """
    import shutil as _sh
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(_sh, "which", lambda name: None)
    if hasattr(app, "_clipboard_cmd_cache"):
        delattr(app, "_clipboard_cmd_cache")

    import subprocess as _sp
    calls: list = []
    monkeypatch.setattr(_sp, "run", lambda *a, **k: calls.append((a, k)))
    async with app.run_test() as pilot:
        app.copy_to_clipboard("nowhere-to-go")
        await pilot.pause(0.1)
    assert calls == [], "subprocess.run should NOT have been called"


async def test_copy_empty_payload_does_not_clear_clipboard(
    app, mock_server, monkeypatch,
):
    """copy_to_clipboard('') must NOT shell out -- xclip with empty
    stdin would clear the existing clipboard, which is hostile to
    users who accidentally trigger a copy on an empty selection.
    """
    import shutil as _sh
    monkeypatch.setattr(_sh, "which", lambda name: "/usr/bin/xclip")
    if hasattr(app, "_clipboard_cmd_cache"):
        delattr(app, "_clipboard_cmd_cache")

    import subprocess as _sp
    calls: list = []
    monkeypatch.setattr(_sp, "run", lambda *a, **k: calls.append((a, k)))
    async with app.run_test() as pilot:
        app.copy_to_clipboard("")
        await pilot.pause(0.1)
    assert calls == [], (
        "empty payload should NOT call xclip (would clear clipboard)"
    )


async def test_copy_subprocess_timeout_is_swallowed(
    app, mock_server, monkeypatch,
):
    """If xclip hangs, copy_to_clipboard must not raise -- the toast
    already fired and OSC 52 may have worked.  Log at debug, return.
    """
    import shutil as _sh
    import subprocess as _sp
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(
        _sh, "which",
        lambda name: "/usr/bin/xclip" if name == "xclip" else None,
    )
    if hasattr(app, "_clipboard_cmd_cache"):
        delattr(app, "_clipboard_cmd_cache")

    def hang(*a, **k):
        raise _sp.TimeoutExpired(cmd="xclip", timeout=2)

    monkeypatch.setattr(_sp, "run", hang)
    async with app.run_test() as pilot:
        # Should not raise.
        app.copy_to_clipboard("payload that times out")
        await pilot.pause(0.1)


async def test_discover_clipboard_cmd_caches_result(app, mock_server, monkeypatch):
    """The discovery probe should run at most once per app instance --
    shutil.which is cheap but not free, and we copy frequently.
    """
    import shutil as _sh
    probe_count = {"n": 0}

    def counting_which(name):
        probe_count["n"] += 1
        return "/usr/bin/xclip" if name == "xclip" else None

    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(_sh, "which", counting_which)
    if hasattr(app, "_clipboard_cmd_cache"):
        delattr(app, "_clipboard_cmd_cache")

    # Two consecutive copies -> probe runs once.
    cmd1 = app._discover_clipboard_cmd()
    n_after_first = probe_count["n"]
    cmd2 = app._discover_clipboard_cmd()
    assert cmd1 == cmd2
    assert probe_count["n"] == n_after_first, (
        f"discovery probed {probe_count['n']} times across 2 calls; "
        "expected exactly one round."
    )


# PR10 (open-in-browser) tests removed in v0.7.0 along with the
# dashboard.  See test_detail_screen.py / test_sessions_screen.py
# (no replacements yet; folder-open + copy-id bindings are still
# covered by the broader smoke tests in this file).


# ─── PR11 regression guard: LabelScreen enter-to-submit ──────────────────────


async def test_label_screen_enter_submits(app, mock_server, monkeypatch):
    """PR11: pressing enter while typing in a github-handle input
    submits all labels.  The dogfood pattern is: type the last
    handle, hit enter, move on -- no mouse needed.
    """
    monkeypatch.setenv("VEZIR_TUI_CRASH_ON_ERROR", "1")
    mock_server["label_info"]["01ENTER"] = {
        "session_id": "01ENTER",
        "status": "needs_labeling",
        "speakers": [
            {"id": "SPEAKER_00", "channel": "mic",
             "sample_text": "Hello team"},
        ],
        "team": ["alice", "bob"],
        "audio_available": True,
    }

    submitted: list[dict] = []
    # Patch the API client BEFORE pushing the screen (the screen
    # captures self.app.api which is the same VezirClient instance).
    def fake_submit(session_id, labels):
        submitted.append({"id": session_id, "labels": labels})
        from vezir.client.api import ApiResult
        return ApiResult.success({"ok": True})
    monkeypatch.setattr(app.api, "submit_labels", fake_submit)

    async with app.run_test(size=(120, 40)) as pilot:
        from textual.widgets import Input

        from vezir.client.tui.label_screen import LabelScreen
        await app.push_screen(LabelScreen(session_id="01ENTER"))
        # Wait for speaker row to mount.
        for _ in range(20):
            await pilot.pause(0.1)
            inputs = list(app.screen.query(Input))
            if inputs:
                break
        else:
            raise AssertionError("speaker Input never mounted")

        inp = inputs[0]
        inp.focus()
        await pilot.pause()
        await pilot.press(*"alice")
        await pilot.press("enter")
        # Submit worker is threaded; give it a moment.
        for _ in range(20):
            await pilot.pause(0.1)
            if submitted:
                break
        assert submitted, "expected a submit call after pressing enter"
        assert submitted[0]["id"] == "01ENTER"
        assert submitted[0]["labels"] == {"SPEAKER_00": "alice"}


# ─── v0.7.12 regression: named speakers (spaces) must not crash the screen ────
#
# Background: once voiceprint auto-labeling persists matched names into the
# transcript (millet 0.12.1), the labeling screen receives real names like
# "Juan Pablo" instead of placeholder ids.  The old code built widget ids as
# f"play-{sid}" / f"input-{sid}"; a space made these invalid Textual
# identifiers and the screen crashed on mount with
# ``textual.dom.BadIdentifier``.  Widget ids are now derived from the row
# index, with a _row_sid map recovering the real id on click.


async def test_label_screen_named_speaker_with_space_mounts(
    app, mock_server, monkeypatch
):
    """A speaker named "Juan Pablo" must render without BadIdentifier.

    With VEZIR_TUI_CRASH_ON_ERROR=1 a mount crash propagates and fails the
    test; pre-fix this raised on the first compositor pass.
    """
    monkeypatch.setenv("VEZIR_TUI_CRASH_ON_ERROR", "1")
    mock_server["label_info"]["01NAMED"] = {
        "session_id": "01NAMED",
        "status": "needs_labeling",
        "speakers": [
            {"id": "Juan Pablo", "channel": "system",
             "sample_text": "Hola equipo"},
            {"id": "SPEAKER_08", "channel": "system",
             "sample_text": "still raw"},
        ],
        "team": ["alice", "bob"],
        "audio_available": True,
    }
    async with app.run_test(size=(120, 40)) as pilot:
        from textual.widgets import Input

        from vezir.client.tui.label_screen import LabelScreen
        await app.push_screen(LabelScreen(session_id="01NAMED"))
        for _ in range(20):
            await pilot.pause(0.1)
            inputs = list(app.screen.query(Input))
            if len(inputs) == 2:
                break
        else:
            raise AssertionError(
                f"speaker rows never populated; final count: {len(inputs)}"
            )
        screen = app.screen
        # Widget ids are index-based and valid; the named speaker's prefill
        # holds the real name.
        assert screen._row_sid == {"0": "Juan Pablo", "1": "SPEAKER_08"}
        named_input = screen._inputs["Juan Pablo"]
        assert named_input.value == "Juan Pablo"
        # The raw speaker starts empty (unresolved, no suggestion).
        assert screen._inputs["SPEAKER_08"].value == ""


async def test_label_screen_play_button_resolves_named_speaker(
    app, mock_server, monkeypatch
):
    """Clicking Play on a named speaker resolves the index-based button id
    back to the real speaker id and fetches that speaker's clip."""
    monkeypatch.setenv("VEZIR_TUI_CRASH_ON_ERROR", "1")
    # Reproduce headless CI deterministically: no ffplay on PATH, so the Play
    # button renders disabled.  The test invokes the handler directly, so it
    # must still resolve the speaker id regardless of the disabled state.
    monkeypatch.setattr(
        "vezir.client.tui.label_screen.ffplay_available", lambda: False
    )
    mock_server["label_info"]["01PLAY"] = {
        "session_id": "01PLAY",
        "status": "needs_labeling",
        "speakers": [
            {"id": "Juan Pablo", "channel": "system",
             "sample_text": "Hola equipo"},
        ],
        "team": ["alice"],
        "audio_available": True,
    }
    async with app.run_test(size=(120, 40)) as pilot:
        from textual.widgets import Button, Input

        from vezir.client.tui.label_screen import LabelScreen
        await app.push_screen(LabelScreen(session_id="01PLAY"))
        for _ in range(20):
            await pilot.pause(0.1)
            if list(app.screen.query(Input)):
                break
        else:
            raise AssertionError("speaker Input never mounted")

        screen = app.screen
        requested: list[str] = []
        # Intercept the clip worker so no real audio/ffplay is needed; assert
        # it receives the resolved name, not the index token.
        monkeypatch.setattr(
            screen, "_clip_worker", lambda sid: requested.append(sid)
        )
        play_btn = app.screen.query_one("#play-0", Button)
        assert play_btn.id == "play-0"  # index-based, valid identifier
        # Invoke the handler directly rather than via pilot.click(): in
        # headless CI there is no ffplay on PATH, so the Play button renders
        # disabled and a real click is a no-op.  We're testing the
        # button-id -> speaker-id resolution, not click geometry / ffplay.
        screen.on_button_pressed(Button.Pressed(play_btn))
        await pilot.pause(0.1)
        assert requested == ["Juan Pablo"]


# ─── retry-summary preset + language picker (additional-language summaries) ──


async def test_preset_picker_returns_preset_and_language(app, mock_server):
    """PresetPickerScreen confirms with (preset, language); the language
    Select offers Auto + the 6 localized languages."""
    from textual.widgets import Button, Select

    from vezir.client.tui.detail_screen import (
        _SUMMARY_LANGUAGES,
        PresetPickerScreen,
    )

    # The curated language set (Auto + 6 with localized section headers).
    assert [code for _, code in _SUMMARY_LANGUAGES] == [
        "auto", "en", "de", "fr", "es", "tr", "fa",
    ]

    result: dict = {}
    async with app.run_test() as pilot:
        def _capture(value):
            result["value"] = value
        await app.push_screen(PresetPickerScreen("high-quality"), _capture)
        await pilot.pause(0.1)
        screen = app.screen
        screen.query_one("#language-select", Select).value = "de"
        await pilot.pause(0.1)
        screen.query_one("#confirm-btn", Button).press()
        await pilot.pause(0.1)
    assert result["value"] == ("high-quality", "de")


async def test_preset_picker_cancel_returns_none(app, mock_server):
    from textual.widgets import Button

    from vezir.client.tui.detail_screen import PresetPickerScreen

    result: dict = {}
    async with app.run_test() as pilot:
        def _capture(value):
            result["value"] = value
        await app.push_screen(PresetPickerScreen("high-quality"), _capture)
        await pilot.pause(0.1)
        app.screen.query_one("#cancel-btn", Button).press()
        await pilot.pause(0.1)
    assert result["value"] is None


# ─── "sync as" folder-override dialog (v0.7.16) ──────────────────────────────


async def test_sync_as_default_returns_empty_auto(app, mock_server):
    """Confirming with the prefilled selection (Auto) returns '' (auto-detect)."""
    from textual.widgets import Button

    from vezir.client.tui.detail_screen import SyncAsScreen

    result: dict = {}
    async with app.run_test() as pilot:
        def _capture(value):
            result["value"] = value
        await app.push_screen(SyncAsScreen("Post Scrum"), _capture)
        await pilot.pause(0.1)
        # Don't touch the input → Select stays on Auto-detect.
        app.screen.query_one("#confirm-btn", Button).press()
        await pilot.pause(0.1)
    assert result["value"] == ""


async def test_sync_as_custom_input_returns_slug(app, mock_server):
    """A typed custom folder is slugified and returned, overriding the Select."""
    from textual.widgets import Button, Input

    from vezir.client.tui.detail_screen import SyncAsScreen

    result: dict = {}
    async with app.run_test() as pilot:
        def _capture(value):
            result["value"] = value
        await app.push_screen(SyncAsScreen("Post Scrum"), _capture)
        await pilot.pause(0.1)
        app.screen.query_one("#syncas-input", Input).value = "Weekly Sync"
        await pilot.pause(0.1)
        app.screen.query_one("#confirm-btn", Button).press()
        await pilot.pause(0.1)
    assert result["value"] == "weekly-sync"


async def test_sync_as_select_title_returns_title_slug(app, mock_server):
    """Choosing the Title option (no custom input) returns the title slug."""
    from textual.widgets import Button, Select

    from vezir.client.tui.detail_screen import SyncAsScreen

    result: dict = {}
    async with app.run_test() as pilot:
        def _capture(value):
            result["value"] = value
        await app.push_screen(SyncAsScreen("Post Scrum"), _capture)
        await pilot.pause(0.1)
        app.screen.query_one("#syncas-select", Select).value = "post-scrum"
        await pilot.pause(0.1)
        app.screen.query_one("#confirm-btn", Button).press()
        await pilot.pause(0.1)
    assert result["value"] == "post-scrum"


async def test_sync_as_cancel_returns_none(app, mock_server):
    from textual.widgets import Button

    from vezir.client.tui.detail_screen import SyncAsScreen

    result: dict = {}
    async with app.run_test() as pilot:
        def _capture(value):
            result["value"] = value
        await app.push_screen(SyncAsScreen("Post Scrum"), _capture)
        await pilot.pause(0.1)
        app.screen.query_one("#cancel-btn", Button).press()
        await pilot.pause(0.1)
    assert result["value"] is None
