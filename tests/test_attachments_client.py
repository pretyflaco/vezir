"""Attachment surfaces in the client: API methods, pull, and the TUI rows.

Part D and E of issue #16.  The TUI screens are exercised at the level of
their populate/dispatch logic — driving Textual itself is out of scope here.
"""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest

# ── VezirClient methods ─────────────────────────────────────────────────────


@pytest.fixture
def make_client(monkeypatch):
    """VezirClient wired to a MockTransport (same shape as test_client_api)."""
    import vezir.client.api as api_mod

    def _make(handler):
        transport = httpx.MockTransport(handler)
        orig = api_mod.httpx.Client

        def factory(*args, **kwargs):
            kwargs["transport"] = transport
            return orig(*args, **kwargs)

        monkeypatch.setattr(api_mod.httpx, "Client", factory)
        return api_mod.VezirClient(
            "https://vezir.example", "vzr_token", team_id="team-uuid",
        )

    return _make


def test_list_attachments_returns_items(make_client):
    def handler(request):
        assert request.url.path == "/api/sessions/01S/attachments"
        return httpx.Response(200, json={"attachments": [
            {"name": "slides.pdf", "size": 4, "content_type": "application/pdf"},
        ]})

    result = make_client(handler).list_attachments("01S")
    assert result.is_ok()
    assert result.ok[0]["name"] == "slides.pdf"


def test_list_attachments_on_old_server_is_not_ok(make_client):
    """A server without the route 404s; callers treat that as 'none'."""
    api = make_client(lambda r: httpx.Response(404, text="nope"))
    result = api.list_attachments("01S")
    assert not result.is_ok()


def test_download_attachment_returns_bytes(make_client):
    def handler(request):
        assert request.url.path == "/api/sessions/01S/attachments/slides.pdf"
        return httpx.Response(200, content=b"%PDF deck")

    result = make_client(handler).download_attachment("01S", "slides.pdf")
    assert result.is_ok()
    assert result.ok == b"%PDF deck"


def test_save_attachment_writes_the_file(tmp_path, make_client):
    api = make_client(lambda r: httpx.Response(200, content=b"deck"))
    dest = tmp_path / "sub" / "slides.pdf"
    result = api.save_attachment("01S", "slides.pdf", dest)
    assert result.is_ok()
    assert dest.read_bytes() == b"deck"


# ── pull / download_session_artifacts ───────────────────────────────────────


class _Result:
    def __init__(self, ok=None, failed=False):
        self.ok = ok
        self._failed = failed

    def is_ok(self):
        return not self._failed

    def error_message(self):
        return "boom"


class _Session:
    id = "01PULL"
    title = "Weekly"
    status = "done"
    github = "alice"
    created_at = "2026-08-15T10:00:00Z"
    team_id = "blink"
    artifacts = {"summary": "01PULL.summary.md"}


class _Api:
    def __init__(self, attachments, fail_download=False):
        self._attachments = attachments
        self._fail_download = fail_download
        self.saved: list[str] = []

    def save_artifact(self, sid, name, dest):
        Path(dest).write_text("artifact")
        return _Result()

    def list_attachments(self, sid):
        if self._attachments is None:
            return _Result(failed=True)
        return _Result(ok=self._attachments)

    def save_attachment(self, sid, name, dest):
        if self._fail_download:
            return _Result(failed=True)
        self.saved.append(name)
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_text(f"body of {name}")
        return _Result(ok=Path(dest))


def test_pull_fetches_attachments_into_subdir(tmp_path):
    from vezir.client.artifacts import download_session_artifacts

    api = _Api([{"name": "slides.pdf"}, {"name": "photo of board.png"}])
    saved = download_session_artifacts(api, _Session(), tmp_path)

    adir = tmp_path / "attachments"
    assert (adir / "slides.pdf").read_text() == "body of slides.pdf"
    # Names are kept verbatim, spaces and all — unlike pipeline artifacts,
    # which are renamed to summary.md / transcript.txt.
    assert (adir / "photo of board.png").exists()
    assert adir / "slides.pdf" in saved


def test_pull_skips_existing_attachment_unless_overwrite(tmp_path):
    from vezir.client.artifacts import download_session_artifacts

    adir = tmp_path / "attachments"
    adir.mkdir()
    (adir / "slides.pdf").write_text("local edit")

    api = _Api([{"name": "slides.pdf"}])
    download_session_artifacts(api, _Session(), tmp_path)
    assert (adir / "slides.pdf").read_text() == "local edit"
    assert api.saved == []

    download_session_artifacts(api, _Session(), tmp_path, overwrite=True)
    assert (adir / "slides.pdf").read_text() == "body of slides.pdf"


def test_pull_rejects_a_path_in_an_attachment_name(tmp_path):
    """The server sanitizes names, but this writes to the user's disk."""
    from vezir.client.artifacts import download_session_artifacts

    api = _Api([{"name": "../../escaped.txt"}, {"name": "ok.txt"}])
    download_session_artifacts(api, _Session(), tmp_path)

    assert api.saved == ["ok.txt"]
    assert not (tmp_path.parent / "escaped.txt").exists()


def test_pull_survives_a_server_without_attachments(tmp_path):
    from vezir.client.artifacts import _friendly_name, download_session_artifacts

    saved = download_session_artifacts(_Api(None), _Session(), tmp_path)
    assert (tmp_path / _friendly_name(_Session(), "01PULL.summary.md")).exists()
    assert not (tmp_path / "attachments").exists()
    assert saved


def test_pull_survives_a_failed_attachment_download(tmp_path):
    from vezir.client.artifacts import _friendly_name, download_session_artifacts

    api = _Api([{"name": "slides.pdf"}], fail_download=True)
    saved = download_session_artifacts(api, _Session(), tmp_path)
    assert (tmp_path / _friendly_name(_Session(), "01PULL.summary.md")).exists()
    assert not (tmp_path / "attachments" / "slides.pdf").exists()
    assert saved


# ── TUI wiring ──────────────────────────────────────────────────────────────


def test_artifact_screen_picks_the_attachment_endpoint():
    from vezir.client.tui.artifact_screen import ArtifactScreen

    calls = []

    class _App:
        class api:
            @staticmethod
            def download_artifact(sid, name):
                calls.append(("artifact", sid, name))
                return _Result(ok=b"")

            @staticmethod
            def download_attachment(sid, name):
                calls.append(("attachment", sid, name))
                return _Result(ok=b"")

    screen = ArtifactScreen("01S", "slides.pdf", is_attachment=True)
    # Screen.app is a read-only property fed by the running Textual app;
    # swap it for the duration of the call.
    type(screen).app = property(lambda self: _App())

    try:
        screen._download()
        plain = ArtifactScreen("01S", "transcript.txt")
        plain._download()
    finally:
        del type(screen).app

    assert calls == [
        ("attachment", "01S", "slides.pdf"),
        ("artifact", "01S", "transcript.txt"),
    ]


def _detail_screen(attachments, artifacts):
    """A DetailScreen with the Textual plumbing stubbed out.

    ``_refresh_view`` (the real one) is what we want to exercise; everything
    it touches besides the table is a widget lookup.
    """
    from vezir.client.api import Session
    from vezir.client.tui.detail_screen import DetailScreen

    rows: list = []

    class _RowKey:
        def __init__(self, value):
            self.value = value

    class _Table:
        def clear(self):
            rows.clear()

        def add_row(self, *cells, key=None):
            rows.append((cells, key))
            return _RowKey(key)

    class _Widget:
        disabled = False

        def update(self, *_a, **_k):
            return None

    screen = DetailScreen.__new__(DetailScreen)
    screen.session_id = "01S"
    screen._artifact_index = {}
    screen._attachment_keys = set()
    screen._attachments = attachments
    screen._table = _Table()
    screen.session = Session(id="01S", status="done", artifacts=artifacts)
    screen.query_one = lambda *_a, **_k: _Widget()
    return screen, rows


def test_detail_screen_marks_attachment_rows():
    """Attachment rows land in the same table but open through their own
    endpoint, so they must be keyed apart from the pipeline artifacts."""
    screen, rows = _detail_screen(
        [{"name": "slides.pdf"}, {"name": ""}],
        {"summary": "01S.summary.md"},
    )
    screen._refresh_view()

    assert rows[0][0] == ("summary", "01S.summary.md")
    assert rows[1][0] == ("attachment", "slides.pdf")
    # The nameless entry is dropped rather than producing an unopenable row.
    assert len(rows) == 2
    assert screen._attachment_keys == {"attachment:slides.pdf"}


def test_detail_screen_opens_each_row_through_the_right_endpoint():
    screen, _rows = _detail_screen(
        [{"name": "slides.pdf"}], {"summary": "01S.summary.md"},
    )
    screen._refresh_view()

    opened = []
    screen._open_artifact = lambda name, *, is_attachment=False: opened.append(
        (name, is_attachment)
    )
    screen._open_row("summary")
    screen._open_row("attachment:slides.pdf")
    screen._open_row("nonexistent")
    assert opened == [("01S.summary.md", False), ("slides.pdf", True)]


def test_detail_screen_without_attachments_is_unchanged():
    screen, rows = _detail_screen([], {"summary": "01S.summary.md"})
    screen._refresh_view()

    assert [r[0] for r in rows] == [("summary", "01S.summary.md")]
    assert screen._attachment_keys == set()
