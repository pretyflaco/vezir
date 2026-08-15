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
    from vezir.client import scribe

    (staging / "slides.pdf").write_bytes(b"deck")
    (staging / "agenda.md").write_text("# agenda")
    (staging / ".DS_Store").write_bytes(b"junk")
    (staging / "subdir").mkdir()

    assert [p.name for p in scribe._staged_attachments()] == [
        "agenda.md", "slides.pdf",
    ]


def test_staged_attachments_empty_when_folder_missing(tmp_path, monkeypatch):
    from vezir.client import scribe

    monkeypatch.setenv("VEZIR_ATTACHMENTS_DIR", str(tmp_path / "nope"))
    assert scribe._staged_attachments() == []


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

    monkeypatch.setattr(scribe.uploader, "upload_attachments", fake_upload)
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
    from vezir.client import scribe

    def boom(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("no upload should happen")

    monkeypatch.setattr(scribe.uploader, "upload_attachments", boom)
    scribe._send_attachments("u", "t", "01S", tmp_path, None)


def test_send_attachments_keeps_files_on_failure(staging, tmp_path, monkeypatch, capsys):
    """The meeting itself is already uploaded, so a failed attachment POST
    warns and leaves the staging folder untouched instead of raising."""
    from vezir.client import scribe

    (staging / "slides.pdf").write_bytes(b"deck")
    session_dir = tmp_path / "meeting"
    session_dir.mkdir()

    def boom(*a, **k):
        raise RuntimeError("server said no")

    monkeypatch.setattr(scribe.uploader, "upload_attachments", boom)
    scribe._send_attachments("u", "t", "01S", session_dir, None)

    assert [p.name for p in staging.iterdir()] == ["slides.pdf"]
    assert not (session_dir / "attachments").exists()
    assert "server said no" in capsys.readouterr().err


def test_move_does_not_clobber_an_existing_file(staging, tmp_path, monkeypatch):
    from vezir.client import scribe

    (staging / "shot.png").write_bytes(b"new")
    session_dir = tmp_path / "meeting"
    (session_dir / "attachments").mkdir(parents=True)
    (session_dir / "attachments" / "shot.png").write_bytes(b"old")

    monkeypatch.setattr(
        scribe.uploader, "upload_attachments", lambda *a, **k: [{"name": "shot.png"}],
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
