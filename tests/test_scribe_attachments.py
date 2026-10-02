"""Client-side attachment workflow in ``vezir scribe`` (issue #16).

The recording path itself is not driven here (that needs audio devices);
these tests cover the pieces around it:

  * the staging folder and what counts as an attachment
  * the post-recording pause, and when it is skipped
  * upload-then-move, including failure handling and name collisions
  * ``uploader.upload_attachments`` request shape
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def staging(tmp_path, monkeypatch) -> Path:
    """Point the fixed staging folder at a temp dir."""
    d = tmp_path / "staging"
    d.mkdir()
    monkeypatch.setenv("VEZIR_ATTACHMENTS_DIR", str(d))
    return d


# ── staging folder ──────────────────────────────────────────────────────────


def test_attachments_dir_honors_env(tmp_path, monkeypatch):
    from vezir.client import config as client_config

    monkeypatch.setenv("VEZIR_ATTACHMENTS_DIR", str(tmp_path / "elsewhere"))
    assert client_config.attachments_dir() == tmp_path / "elsewhere"

    monkeypatch.delenv("VEZIR_ATTACHMENTS_DIR")
    assert client_config.attachments_dir() == Path.home() / "vezir-attachments"


def test_staged_attachments_skips_dotfiles_and_dirs(staging):
    from vezir.client import attachments

    (staging / "slides.pdf").write_bytes(b"deck")
    (staging / "agenda.md").write_text("# agenda")
    (staging / ".DS_Store").write_bytes(b"junk")
    (staging / "subdir").mkdir()

    assert [p.name for p in attachments.staged_attachments()] == [
        "agenda.md", "slides.pdf",
    ]


def test_staged_attachments_empty_when_folder_missing(tmp_path, monkeypatch):
    from vezir.client import attachments

    monkeypatch.setenv("VEZIR_ATTACHMENTS_DIR", str(tmp_path / "nope"))
    assert attachments.staged_attachments() == []


def test_announce_creates_the_folder(tmp_path, monkeypatch, capsys):
    from vezir.client import scribe

    target = tmp_path / "made-on-demand"
    monkeypatch.setenv("VEZIR_ATTACHMENTS_DIR", str(target))
    scribe._announce_attachments_folder()

    assert target.is_dir()
    assert str(target) in capsys.readouterr().out


# ── the pause ───────────────────────────────────────────────────────────────


def _fake_tty(monkeypatch, is_tty: bool, calls: list):
    import sys

    class _Stdin:
        def isatty(self):
            return is_tty

    monkeypatch.setattr(sys, "stdin", _Stdin())
    monkeypatch.setattr("builtins.input", lambda *a: calls.append(a))


def test_pause_waits_for_enter_on_a_tty(staging, monkeypatch, capsys):
    from vezir.client import scribe

    (staging / "slides.pdf").write_bytes(b"deck")
    calls: list = []
    _fake_tty(monkeypatch, True, calls)

    scribe._attachment_pause(no_pause=False)
    assert len(calls) == 1
    out = capsys.readouterr().out
    assert "found 1 attachment(s)" in out
    assert "slides.pdf" in out


def test_pause_skipped_without_a_tty(staging, monkeypatch, capsys):
    """scribe is documented for headless/ssh/scripted use — a blocking
    prompt there would hang the run forever."""
    from vezir.client import scribe

    calls: list = []
    _fake_tty(monkeypatch, False, calls)

    scribe._attachment_pause(no_pause=False)
    assert calls == []
    assert "no attachments staged" in capsys.readouterr().out


def test_pause_skipped_with_no_pause_flag(staging, monkeypatch):
    from vezir.client import scribe

    calls: list = []
    _fake_tty(monkeypatch, True, calls)

    scribe._attachment_pause(no_pause=True)
    assert calls == []


def test_pause_survives_ctrl_c_and_eof(staging, monkeypatch):
    """A stray Ctrl-C at the prompt must not throw away a recorded
    meeting — it continues into the upload."""
    import sys

    from vezir.client import scribe

    class _Stdin:
        def isatty(self):
            return True

    monkeypatch.setattr(sys, "stdin", _Stdin())

    for exc in (KeyboardInterrupt, EOFError):
        def _raise(*_a, _exc=exc):
            raise _exc()

        monkeypatch.setattr("builtins.input", _raise)
        scribe._attachment_pause(no_pause=False)  # must not raise


# ── upload + move ───────────────────────────────────────────────────────────


def test_send_attachments_uploads_then_moves(staging, tmp_path, monkeypatch, capsys):
    from vezir.client import attachments as attachments_mod
    from vezir.client import scribe

    (staging / "slides.pdf").write_bytes(b"deck")
    (staging / "agenda.md").write_text("# agenda")
    session_dir = tmp_path / "meeting-20260815-100000"
    session_dir.mkdir()

    seen = {}

    def fake_upload(server_url, token, session_id, paths, **kwargs):
        seen["args"] = (server_url, token, session_id, [p.name for p in paths])
        seen["team_id"] = kwargs.get("team_id")
        return [{"name": p.name} for p in paths]

    monkeypatch.setattr(attachments_mod.uploader, "upload_attachments", fake_upload)
    scribe._send_attachments(
        "https://vezir.example", "tok", "01SESSION", session_dir, "team-uuid",
    )

    assert seen["args"] == (
        "https://vezir.example", "tok", "01SESSION", ["agenda.md", "slides.pdf"],
    )
    assert seen["team_id"] == "team-uuid"
    # Staging folder is emptied; the files live next to the recording.
    assert list(staging.iterdir()) == []
    assert sorted(p.name for p in (session_dir / "attachments").iterdir()) == [
        "agenda.md", "slides.pdf",
    ]
    assert (session_dir / "attachments" / "slides.pdf").read_bytes() == b"deck"


def test_send_attachments_noop_when_nothing_staged(staging, tmp_path, monkeypatch):
    from vezir.client import attachments as attachments_mod
    from vezir.client import scribe

    def boom(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("no upload should happen")

    monkeypatch.setattr(attachments_mod.uploader, "upload_attachments", boom)
    scribe._send_attachments("u", "t", "01S", tmp_path, None)


def test_send_attachments_keeps_files_on_failure(staging, tmp_path, monkeypatch, capsys):
    """The meeting itself is already uploaded, so a failed attachment POST
    warns and leaves the staging folder untouched instead of raising."""
    from vezir.client import attachments as attachments_mod
    from vezir.client import scribe

    (staging / "slides.pdf").write_bytes(b"deck")
    session_dir = tmp_path / "meeting"
    session_dir.mkdir()

    def boom(*a, **k):
        raise RuntimeError("server said no")

    monkeypatch.setattr(attachments_mod.uploader, "upload_attachments", boom)
    scribe._send_attachments("u", "t", "01S", session_dir, None)

    assert [p.name for p in staging.iterdir()] == ["slides.pdf"]
    assert not (session_dir / "attachments").exists()
    assert "server said no" in capsys.readouterr().err


def test_identical_file_at_destination_is_not_duplicated(
    staging, tmp_path, monkeypatch
):
    """Same-box deployment: with VEZIR_RECORD_DIR pointing into the server's
    own sessions dir, the copy the server just stored IS the move target.
    Dropping the staged file beats writing a ``_2`` duplicate."""
    from vezir.client import attachments as attachments_mod
    from vezir.client import scribe

    (staging / "slides.pdf").write_bytes(b"deck")
    session_dir = tmp_path / "meeting"
    (session_dir / "attachments").mkdir(parents=True)
    (session_dir / "attachments" / "slides.pdf").write_bytes(b"deck")

    monkeypatch.setattr(
        attachments_mod.uploader, "upload_attachments", lambda *a, **k: [{"name": "slides.pdf"}],
    )
    scribe._send_attachments("u", "t", "01S", session_dir, None)

    adir = session_dir / "attachments"
    assert sorted(p.name for p in adir.iterdir()) == ["slides.pdf"]
    assert list(staging.iterdir()) == []


def test_move_does_not_clobber_an_existing_file(staging, tmp_path, monkeypatch):
    from vezir.client import attachments as attachments_mod
    from vezir.client import scribe

    (staging / "shot.png").write_bytes(b"new")
    session_dir = tmp_path / "meeting"
    (session_dir / "attachments").mkdir(parents=True)
    (session_dir / "attachments" / "shot.png").write_bytes(b"old")

    monkeypatch.setattr(
        attachments_mod.uploader, "upload_attachments", lambda *a, **k: [{"name": "shot.png"}],
    )
    scribe._send_attachments("u", "t", "01S", session_dir, None)

    adir = session_dir / "attachments"
    assert (adir / "shot.png").read_bytes() == b"old"
    assert (adir / "shot_2.png").read_bytes() == b"new"


# ── uploader request shape ──────────────────────────────────────────────────


def test_upload_attachments_posts_multipart(tmp_path, monkeypatch):
    import httpx

    from vezir.client import uploader

    a = tmp_path / "slides.pdf"
    a.write_bytes(b"deck")
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["team"] = request.headers.get("x-team-id")
        captured["body"] = request.content
        return httpx.Response(200, json={"attachments": [{"name": "slides.pdf"}]})

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs.pop("verify", None)
        return real_client(*args, transport=transport, **kwargs)

    monkeypatch.setattr(uploader.httpx, "Client", fake_client)

    out = uploader.upload_attachments(
        "https://vezir.example/", "tok", "01SESSION", [a], team_id="team-uuid",
    )

    assert out == [{"name": "slides.pdf"}]
    assert captured["url"] == (
        "https://vezir.example/api/sessions/01SESSION/attachments"
    )
    assert captured["auth"] == "Bearer tok"
    assert captured["team"] == "team-uuid"
    assert b'name="files"' in captured["body"]
    assert b"slides.pdf" in captured["body"]


def test_upload_attachments_empty_list_makes_no_request(monkeypatch):
    from vezir.client import uploader

    def boom(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("no request should be made")

    monkeypatch.setattr(uploader.httpx, "Client", boom)
    assert uploader.upload_attachments("u", "t", "01S", []) == []


def test_upload_attachments_explains_a_404(tmp_path, monkeypatch):
    import httpx

    from vezir.client import uploader

    a = tmp_path / "slides.pdf"
    a.write_bytes(b"deck")
    transport = httpx.MockTransport(lambda r: httpx.Response(404, text="nope"))
    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs.pop("verify", None)
        return real_client(*args, transport=transport, **kwargs)

    monkeypatch.setattr(uploader.httpx, "Client", fake_client)

    with pytest.raises(RuntimeError, match="another team"):
        uploader.upload_attachments("https://x", "tok", "01S", [a])


# ── TUI record screen (issue #16 review follow-up) ──────────────────────────


class _ReviewHost:
    """Minimal Textual app hosting one UploadReviewScreen."""

    @staticmethod
    def make(**kw):
        from textual.app import App

        from vezir.client.tui.review_screen import UploadReviewScreen

        results: list = []
        params = dict(
            name="meeting-1", audio_path=None, team="blink",
            teams=["blink", "twentyone"], title=None,
            auto_label=True, sync=True, personal=False,
        )
        params.update(kw)

        class Host(App):
            def on_mount(self):
                self.push_screen(UploadReviewScreen(**params), results.append)

        return Host(), results


async def test_review_screen_lists_staged_files(staging):
    """The review modal is the TUI's equivalent of scribe's Enter prompt."""
    from textual.widgets import Static

    (staging / "slides.pdf").write_bytes(b"deck")
    app, _results = _ReviewHost.make()
    async with app.run_test() as pilot:
        await pilot.pause()
        line = str(app.screen.query_one("#review-attachments", Static).render())
        assert "1 staged" in line and "slides.pdf" in line
        (staging / "slides.pdf").unlink()
        await pilot.press("ctrl+r")
        await pilot.pause()
        line = str(app.screen.query_one("#review-attachments", Static).render())
        assert "none" in line


async def test_review_screen_escape_keeps_local(staging):
    """0.25.0 reverses 0.23's "dismissing always uploads": an accidental
    Escape must share nothing.  The recording is held (journaled, listed in
    the Outbox), not dropped."""
    app, results = _ReviewHost.make()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
    assert results[0].action == "hold"
    assert results[0].team == "blink"


async def test_review_screen_upload_carries_choices(staging):
    from textual.widgets import Input, Select

    app, results = _ReviewHost.make()
    async with app.run_test() as pilot:  # default 80x24 must fit
        await pilot.pause()
        app.screen.query_one("#review-team", Select).value = "twentyone"
        app.screen.query_one("#review-title", Input).value = "Bounty"
        await pilot.pause()
        await pilot.click("#review-personal")
        await pilot.click("#review-auto-label")
        await pilot.pause()
        await pilot.click("#review-upload")
        await pilot.pause()
    r = results[0]
    assert (r.action, r.team, r.title) == ("upload", "twentyone", "Bounty")
    assert r.personal is True and r.sync is False and r.auto_label is False


async def test_review_screen_import_escape_cancels(staging):
    app, results = _ReviewHost.make(hold_label="Cancel")
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
    assert results[0].action == "cancel"


def test_hold_staged_parks_attachments_with_recording(staging, tmp_path):
    from vezir.client import attachments

    (staging / "slides.pdf").write_bytes(b"deck")
    rec = tmp_path / "meeting-1"
    rec.mkdir()
    assert attachments.hold_staged(rec) == 1
    assert (rec / "attachments" / "slides.pdf").read_bytes() == b"deck"
    assert attachments.staged_attachments() == []  # next meeting starts empty


def test_send_held_attachments(staging, tmp_path, monkeypatch):
    from vezir.client import attachments, uploader

    rec = tmp_path / "meeting-1"
    (rec / "attachments").mkdir(parents=True)
    (rec / "attachments" / "slides.pdf").write_bytes(b"deck")
    sent = {}

    def fake(server_url, token, sid, paths, team_id=None):
        sent.update(sid=sid, names=[p.name for p in paths], team=team_id)
        return [{"name": "slides.pdf"}]

    monkeypatch.setattr(uploader, "upload_attachments", fake)
    out = attachments.send_held_attachments("u", "t", "01S", rec, "twentyone")
    assert out == [{"name": "slides.pdf"}]
    assert sent == {"sid": "01S", "names": ["slides.pdf"], "team": "twentyone"}


def test_record_screen_line_shows_folder_and_count(staging, monkeypatch):
    from vezir.client.tui.record_screen import RecordBody

    (staging / "slides.pdf").write_bytes(b"deck")
    body = RecordBody.__new__(RecordBody)
    shown: list = []

    class _Static:
        def update(self, text):
            shown.append(text)

    body.query_one = lambda *_a, **_k: _Static()
    body._refresh_attachments_line()
    assert str(staging) in shown[0]
    assert "1 file(s) staged" in shown[0]
