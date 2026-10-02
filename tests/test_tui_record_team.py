"""Record tab: the destination team belongs to the recording (0.25.0).

Incident 2026-10-02: a meeting recorded under `blink` had to go to
`twentyone`.  Stop uploaded to the team the recording started in, Escape
on the attachment prompt uploaded too, and switching teams was refused
while paused.  These tests pin the fix end to end with a fake recorder:
team selector → journal at start → change while recording → review on Stop
→ folder moved + upload to the chosen team, or Keep local.
"""
from __future__ import annotations

import json
import wave
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

MEMBERSHIPS = [
    {"team_id": "uuid-blink", "slug": "blink", "role": "member", "team_name": "Blink"},
    {"team_id": "uuid-21", "slug": "twentyone", "role": "member", "team_name": "21"},
]


@pytest.fixture
def server(monkeypatch):
    seen: dict = {"session_headers": []}

    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p == "/api/me":
            return httpx.Response(200, json={
                "github": "tester", "is_admin": False,
                "memberships": MEMBERSHIPS, "alternate_urls": [],
            })
        if p == "/api/sessions":
            return httpx.Response(200, json={"sessions": []})
        if p.startswith("/api/sessions/"):
            seen["session_headers"].append(request.headers.get("x-team-id"))
            return httpx.Response(200, json={"id": p.split("/")[-1], "status": "queued"})
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    import vezir.client.api as api_mod
    orig = api_mod.httpx.Client

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return orig(*args, **kwargs)

    api_mod.httpx.Client = factory
    yield seen
    api_mod.httpx.Client = orig


@dataclass
class _Status:
    elapsed_seconds: float = 1.0
    file_size_bytes: int = 8192
    paused: bool = False
    failed: bool = False
    fail_reason: str | None = None
    is_alive: bool = True


class FakeSession:
    """Stands in for millet_record's RecordingSession."""

    def __init__(self, output_dir: str):
        stamp = "meeting-20261002-123316"
        self.output_dir = Path(output_dir) / stamp
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.output_file = self.output_dir / f"{stamp}.wav"
        self._paused = False
        self._alive = False

    def start(self):
        self._alive = True

    def status(self):
        return _Status(paused=self._paused, is_alive=self._alive)

    def pause(self):
        self._paused = True

    def resume(self):
        self._paused = False

    def stop(self):
        self._alive = False
        with wave.open(str(self.output_file), "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(b"\x00" * 64)
        return self.output_file


@pytest.fixture
def app(server, monkeypatch, tmp_path):
    monkeypatch.setenv("VEZIR_URL", "http://test")
    monkeypatch.setenv("VEZIR_TOKEN", "vzr_" + "x" * 43)
    monkeypatch.setenv("VEZIR_TEAM_ID", "blink")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("VEZIR_RECORD_DIR", str(tmp_path / "rec"))
    monkeypatch.setenv("VEZIR_ATTACHMENTS_DIR", str(tmp_path / "staging"))
    monkeypatch.setenv("VEZIR_TUI_DISABLE_NOTIFY_POLL", "1")
    monkeypatch.setenv("VEZIR_TUI_DISABLE_UPDATE_CHECK", "1")
    monkeypatch.setenv("VEZIR_TUI_DISABLE_RECOVERY_SCAN", "1")
    import millet_record.capture as capture

    monkeypatch.setattr(capture, "check_prerequisites", lambda: [])
    monkeypatch.setattr(capture, "create_session", lambda output_dir: FakeSession(output_dir))
    from vezir.client.tui.app import VezirTuiApp
    return VezirTuiApp()


@pytest.fixture
def uploads(monkeypatch):
    from vezir.client import uploader

    sent: list = []

    def compress(path, keep_wav=False):
        ogg = path.with_suffix(".ogg")
        ogg.write_bytes(b"OggS")
        return ogg

    def up(server_url, token, audio_path, **kw):
        sent.append({**kw, "audio_path": audio_path})
        return {"session_id": "01NEW"}

    monkeypatch.setattr(uploader, "compress_wav_for_upload", compress)
    monkeypatch.setattr(uploader, "server_supports_resumable", lambda *a, **k: True)
    monkeypatch.setattr(uploader, "upload_resumable", up)
    return sent


async def _wait(pilot, cond, n=100):
    for _ in range(n):
        await pilot.pause(0.05)
        if cond():
            return True
    return False


def _body(app):
    from vezir.client.tui.record_screen import RecordBody

    return app.screen.query_one(RecordBody) if app.screen.__class__.__name__ == "MainScreen" \
        else app.screen_stack[0].query_one(RecordBody)


async def _start(app, pilot):
    from textual.widgets import Select

    body = _body(app)
    assert await _wait(pilot, lambda: "twentyone" in body._team_opts)
    assert body.query_one("#team-select", Select).value == "blink"
    body.action_toggle_record()
    assert await _wait(pilot, lambda: body.is_recording)
    return body


async def test_selector_defaults_to_active_team_and_follows_idle_switch(app, uploads):
    from textual.widgets import Select

    async with app.run_test(size=(120, 40)) as pilot:
        body = _body(app)
        await _wait(pilot, lambda: "twentyone" in body._team_opts)
        assert body.destination_team == "blink"
        app.switch_to_team("twentyone")
        await pilot.pause()
        assert body.destination_team == "twentyone"
        assert body.query_one("#team-select", Select).value == "twentyone"


async def test_journal_carries_team_from_start_and_follows_changes(app, uploads, tmp_path):
    from textual.widgets import Select

    from vezir.client import recovery, upload_journal

    async with app.run_test(size=(120, 40)) as pilot:
        body = await _start(app, pilot)
        rec_dir = tmp_path / "rec" / "blink" / "meeting-20261002-123316"
        assert upload_journal.read(rec_dir) == {
            **upload_journal.read(rec_dir), "status": "recording", "team_id": "blink",
        }
        body.action_toggle_pause()
        body.query_one("#team-select", Select).value = "twentyone"
        await pilot.pause()
        # Destination is journaled; the folder is NOT moved under a recorder.
        assert upload_journal.read(rec_dir)["team_id"] == "twentyone"
        assert rec_dir.is_dir()
        # A crash now would be salvaged to the right team.
        assert recovery.resolve_team(rec_dir) == "twentyone"


async def test_stop_review_upload_moves_folder_and_targets_chosen_team(
    app, uploads, server, tmp_path,
):
    from textual.widgets import Select

    from vezir.client import upload_journal

    async with app.run_test(size=(120, 40)) as pilot:
        body = await _start(app, pilot)
        body.query_one("#team-select", Select).value = "twentyone"
        await pilot.pause()
        body.action_toggle_record()  # Stop
        assert await _wait(pilot, lambda: app.screen.__class__.__name__ == "UploadReviewScreen")
        assert await _wait(
            pilot, lambda: app.screen.query_one("#review-team", Select).value == "twentyone",
        )
        await pilot.pause()
        app.screen.action_upload()
        assert await _wait(pilot, lambda: bool(uploads))
        await _wait(pilot, lambda: bool(server["session_headers"]))

    new_dir = tmp_path / "rec" / "twentyone" / "meeting-20261002-123316"
    assert not (tmp_path / "rec" / "blink" / "meeting-20261002-123316").exists()
    assert uploads[0]["team_id"] == "twentyone"
    assert uploads[0]["audio_path"].parent == new_dir
    assert upload_journal.read(new_dir)["status"] == "done"
    assert json.loads((new_dir / "session.json").read_text())["team_id"] == "twentyone"
    # Status polling asks the session's team, not the app-wide active one.
    assert server["session_headers"][0] == "twentyone"


async def test_stop_then_escape_keeps_local_with_attachments(app, uploads, tmp_path):
    from vezir.client import local, upload_journal

    staging = tmp_path / "staging"
    async with app.run_test(size=(120, 40)) as pilot:
        body = await _start(app, pilot)
        staging.mkdir(exist_ok=True)
        (staging / "slides.pdf").write_bytes(b"deck")
        body.action_toggle_record()
        assert await _wait(pilot, lambda: app.screen.__class__.__name__ == "UploadReviewScreen")
        await pilot.press("escape")
        await pilot.pause(0.2)
        assert "kept local" in body.status_text

    rec_dir = tmp_path / "rec" / "blink" / "meeting-20261002-123316"
    assert uploads == []
    j = upload_journal.read(rec_dir)
    assert j["status"] == "held" and j["team_id"] == "blink"
    assert j["pending_attachments"] is True
    assert (rec_dir / "attachments" / "slides.pdf").exists()
    assert not list(staging.iterdir())  # won't ride along with the next meeting
    [rec] = local.scan()
    assert rec.state == "held"


async def test_global_switch_allowed_while_recording_keeps_destination(app, uploads):
    async with app.run_test(size=(120, 40)) as pilot:
        body = await _start(app, pilot)
        assert app.switch_to_team("twentyone") is True
        await pilot.pause()
        assert app.active_team_id == "twentyone"
        assert body.destination_team == "blink"  # the recording keeps its team


async def test_crash_during_review_is_pending(app, uploads, tmp_path):
    """Journaled pending before the review opens: a crash there is offered."""
    from vezir.client import upload_journal

    async with app.run_test(size=(120, 40)) as pilot:
        body = await _start(app, pilot)
        body.action_toggle_record()
        assert await _wait(pilot, lambda: app.screen.__class__.__name__ == "UploadReviewScreen")
        rec_dir = tmp_path / "rec" / "blink" / "meeting-20261002-123316"
        assert upload_journal.read(rec_dir)["status"] == "pending"
