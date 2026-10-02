"""Outbox tab (0.25.0): local recordings that never reached the server.

Successor of the 0.23 startup recovery dialog (regression guard for the
2026-09-28 incident: an 87-minute recording survived a TUI crash on disk
but was invisible in the UI).  At launch the TUI scans the recordings roots
and, when something needs attention, switches to the Outbox and says so.
From there every recording can be uploaded to any team, moved, held or
discarded (2026-10-02 incident: a recording in the wrong team).
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
            # Team pickers offer memberships only (0.26.1), never folder names.
            return httpx.Response(200, json={
                "github": "tester", "is_admin": False,
                "memberships": [
                    {"team_id": "u1", "slug": "startups", "role": "member"},
                    {"team_id": "u2", "slug": "twentyone", "role": "member"},
                ],
                "alternate_urls": [],
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
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("VEZIR_RECORD_DIR", raising=False)
    monkeypatch.setenv("VEZIR_TUI_DISABLE_NOTIFY_POLL", "1")
    monkeypatch.setenv("VEZIR_TUI_DISABLE_UPDATE_CHECK", "1")
    from vezir.client.tui.app import VezirTuiApp
    return VezirTuiApp()


def _make_interrupted(home: Path, team: str = "startups") -> Path:
    d = home / "vezir-meetings" / team / "meeting-20260928-135948"
    d.mkdir(parents=True)
    (d / "meeting-20260928-135948.chunk-000.wav").write_bytes(b"\x00" * 2048)
    (home / "vezir-meetings" / "twentyone").mkdir(exist_ok=True)
    return d


def _active_tab(app) -> str:
    from textual.widgets import TabbedContent

    return app.screen_stack[1].query_one(TabbedContent).active


def _outbox(app):
    from vezir.client.tui.outbox_screen import OutboxBody

    return app.screen_stack[1].query_one(OutboxBody)


async def _wait(pilot, cond, n=60):
    for _ in range(n):
        await pilot.pause(0.05)
        if cond():
            return True
    return False


async def test_launch_switches_to_outbox_for_interrupted_session(app, tmp_path):
    _make_interrupted(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        assert await _wait(pilot, lambda: _active_tab(app) == "outbox")
        body = _outbox(app)
        assert await _wait(pilot, lambda: bool(body._recs))
        rec = body._recs[0]
        assert (rec.name, rec.team, rec.state) == (
            "meeting-20260928-135948", "startups", "interrupted")


async def test_launch_stays_put_when_clean(app):
    async with app.run_test() as pilot:
        for _ in range(10):
            await pilot.pause(0.05)
        assert app.screen.__class__.__name__ == "MainScreen"
        assert _active_tab(app) == "record"


async def test_held_recordings_do_not_nag_at_launch(app, tmp_path):
    from vezir.client import upload_journal

    d = _make_interrupted(tmp_path)
    upload_journal.mark_held(d, title=None, team_id="startups")
    async with app.run_test(size=(120, 40)) as pilot:
        body = _outbox(app)
        assert await _wait(pilot, lambda: bool(body._recs))
        assert body._recs[0].state == "held"
        assert _active_tab(app) == "record"


async def test_outbox_upload_to_other_team(app, tmp_path, monkeypatch):
    """u → review (pick twentyone) → salvage gets the chosen team + options."""
    from textual.widgets import Select

    _make_interrupted(tmp_path)
    sent: dict = {}

    def fake_salvage(rec, **kw):
        sent.update(kw, kind=rec.kind)
        return "01X"

    monkeypatch.setattr("vezir.client.recovery.salvage", fake_salvage)
    async with app.run_test(size=(120, 40)) as pilot:
        body = _outbox(app)
        assert await _wait(pilot, lambda: bool(body._recs) and bool(app.memberships))
        body.action_upload()
        assert await _wait(pilot, lambda: app.screen.__class__.__name__ == "UploadReviewScreen")
        app.screen.query_one("#review-team", Select).value = "twentyone"
        await pilot.pause()
        app.screen.query_one("#review-sync").press()
        await pilot.pause()
        app.screen.action_upload()
        assert await _wait(pilot, lambda: bool(sent))
    assert sent["team"] == "twentyone"
    assert sent["sync"] is False
    assert sent["kind"] == "interrupted"  # chunks get stitched


async def test_outbox_move_hold_discard(app, tmp_path):
    from vezir.client import upload_journal

    d = _make_interrupted(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        body = _outbox(app)
        assert await _wait(pilot, lambda: bool(body._recs) and bool(app.memberships))

        # t → pick twentyone → folder moves.
        body.action_move_team()
        assert await _wait(pilot, lambda: app.screen.__class__.__name__ == "TeamPickScreen")
        from textual.widgets import OptionList

        lst = app.screen.query_one("#team-pick-list", OptionList)
        ids = [lst.get_option_at_index(i).id for i in range(lst.option_count)]
        lst.highlighted = ids.index("twentyone")
        await pilot.press("enter")
        moved = tmp_path / "vezir-meetings" / "twentyone" / d.name
        assert await _wait(pilot, lambda: moved.is_dir())
        assert await _wait(pilot, lambda: body._recs and body._recs[0].team == "twentyone")

        # h → held; no longer offered at launch.
        body.action_hold()
        assert upload_journal.read(moved)["status"] == "held"
        assert await _wait(pilot, lambda: body._recs and body._recs[0].state == "held")

        # d → confirm → trash.
        body.action_discard()
        assert await _wait(pilot, lambda: app.screen.__class__.__name__ == "ConfirmScreen")
        app.screen.query_one("#confirm-yes").press()
        assert await _wait(pilot, lambda: not moved.exists())
        assert (tmp_path / "vezir-meetings" / ".trash" / "twentyone" / d.name).is_dir()


async def test_outbox_discard_defaults_to_no(app, tmp_path):
    d = _make_interrupted(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        body = _outbox(app)
        assert await _wait(pilot, lambda: bool(body._recs))
        body.action_discard()
        assert await _wait(pilot, lambda: app.screen.__class__.__name__ == "ConfirmScreen")
        await pilot.press("enter")  # focused button is Cancel
        await pilot.pause()
        assert d.exists()


async def test_stray_session_folder_is_not_a_team(app, tmp_path):
    """0.26.1: a session folder sitting directly in ~/vezir-meetings/ (an old
    pull that couldn't resolve its team) was offered as a "team" in every
    picker.  Pickers now list memberships only, and the folder is not a
    recordings root."""
    from vezir.client import local, recovery
    from vezir.client.tui.review_screen import team_choices

    stray = tmp_path / "vezir-meetings" / "meeting-20260620-162219_DANIEL_BIP110"
    stray.mkdir(parents=True)
    (stray / "session.json").write_text('{"session_id": "01OLD"}')
    (tmp_path / "vezir-meetings" / "oldteam").mkdir()
    assert [n for n, _ in recovery.recordings_roots()] == ["oldteam"]
    assert "meeting-20260620-162219_DANIEL_BIP110" not in local.known_teams()
    async with app.run_test(size=(120, 40)) as pilot:
        assert await _wait(pilot, lambda: bool(app.memberships))
        choices = team_choices(app)
        assert choices == ["startups", "twentyone"]  # not oldteam, not the stray
        from textual.widgets import Select

        from vezir.client.tui.record_screen import RecordBody
        body = app.screen_stack[1].query_one(RecordBody)
        sel = body.query_one("#team-select", Select)
        await _wait(pilot, lambda: body._team_opts == ["startups", "twentyone"])
        labels = [str(o[0]) for o in sel._options]
        assert not any("meeting-" in lbl or lbl == "team" for lbl in labels)
