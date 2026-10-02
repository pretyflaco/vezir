"""`vezir local` (0.24.0): list / upload / move / discard local recordings.

Incident 2026-10-02: a paused recording started in the wrong team had to
be salvaged into another team by hand (quit TUI → mv folder → stitch via a
python one-liner → `vezir upload` with a borrowed token).  These tests pin
the one-command path: `vezir local upload <ref> --team <other>`.
"""
from __future__ import annotations

import json
import os
import wave
from pathlib import Path

import pytest
from click.testing import CliRunner

from vezir.cli import main
from vezir.client import local, recovery, upload_journal


@pytest.fixture
def base(tmp_path, monkeypatch):
    """Isolated HOME + recordings base with blink/ and twentyone/ roots."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    b = tmp_path / "rec"
    (b / "blink").mkdir(parents=True)
    (b / "twentyone").mkdir()
    monkeypatch.setenv("VEZIR_RECORD_DIR", str(b))
    monkeypatch.setenv("VEZIR_URL", "https://srv")
    monkeypatch.setenv("VEZIR_TOKEN", "vzr_tok")
    monkeypatch.delenv("VEZIR_TEAM_ID", raising=False)
    return b


def _wav(path: Path, frames: int = 1600) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x01\x00" * 2 * frames)
    return path


def _paused_recording(root: Path, name: str, *, owner_pid: int | None = None) -> Path:
    """What a paused (or crashed) TUI recording leaves: chunk + meta."""
    d = root / name
    d.mkdir()
    _wav(d / f"{name}.chunk-000.wav")
    meta = {
        "started_at": "2026-10-02T12:33:16",
        "output_file": str(d / f"{name}.wav"),
        "status": "recording",
    }
    if owner_pid is not None:
        meta["owner_pid"] = owner_pid
    (d / f"{name}.session.json").write_text(json.dumps(meta))
    return d


def _uploaded(root: Path, name: str, sid: str) -> Path:
    d = root / name
    d.mkdir()
    (d / f"{name}.ogg").write_bytes(b"OggS" + b"\x00" * 64)
    (d / "session.json").write_text(json.dumps({"session_id": sid}))
    return d


def _local_only(root: Path, name: str) -> Path:
    d = root / name
    d.mkdir()
    (d / f"{name}.ogg").write_bytes(b"OggS" + b"\x00" * 64)
    return d


@pytest.fixture
def fake_upload(monkeypatch):
    """Stub compression + upload; capture what would have been sent."""
    from vezir.client import uploader

    sent: dict = {}

    def compress(path, keep_wav=False):
        ogg = path.with_suffix(".ogg")
        ogg.write_bytes(b"OggS" + b"\x00" * 64)
        if not keep_wav:
            path.unlink()
        return ogg

    def upload_resumable(server_url, token, audio_path, **kw):
        sent.update(kw, server_url=server_url, token=token, audio_path=audio_path)
        return {"session_id": "01NEW", "bytes": 68}

    monkeypatch.setattr(uploader, "compress_wav_for_upload", compress)
    monkeypatch.setattr(uploader, "server_supports_resumable", lambda *a, **k: True)
    monkeypatch.setattr(uploader, "upload_resumable", upload_resumable)
    return sent


def _run(*args, input=None):
    return CliRunner().invoke(main, ["local", *args], input=input)


# ── list ──────────────────────────────────────────────────────────────────────


def test_list_shows_outbox_by_default(base):
    _paused_recording(base / "blink", "meeting-20261002-123316")
    _uploaded(base / "blink", "meeting-20261001-110259_BOUNTY", "01OLD")
    _local_only(base / "twentyone", "meeting-20260101-000000")

    res = _run("list")
    assert res.exit_code == 0, res.output
    assert "meeting-20261002-123316" in res.output
    assert "interrupted" in res.output
    assert "01OLD" not in res.output
    assert "meeting-20260101-000000" not in res.output

    res = _run("list", "--all")
    assert "01OLD" in res.output
    assert "local-only" in res.output


def test_list_json_and_team_filter(base):
    _paused_recording(base / "blink", "meeting-20261002-123316")
    _local_only(base / "twentyone", "meeting-20260101-000000")
    res = _run("list", "--all", "--json", "--team", "twentyone")
    data = json.loads(res.output)
    assert [r["name"] for r in data] == ["meeting-20260101-000000"]
    assert data[0]["state"] == "local-only"
    assert data[0]["team"] == "twentyone"


def test_list_in_progress_when_owner_alive(base):
    _paused_recording(base / "blink", "meeting-20261002-140104", owner_pid=os.getpid())
    [rec] = local.scan()
    assert rec.state == "in-progress"


def test_list_empty_message(base):
    res = _run("list")
    assert res.exit_code == 0
    assert "Nothing local" in res.output


def test_journal_states(base):
    d = _local_only(base / "blink", "meeting-20260601-000000")
    upload_journal.mark_pending(d, title="T", team_id="blink")
    upload_journal.mark_failed(d, "boom")
    [rec] = local.scan()
    assert (rec.state, rec.detail, rec.title) == ("failed", "boom", "T")


# ── ref resolution ────────────────────────────────────────────────────────────


def test_ref_prefix_and_ambiguity(base):
    _local_only(base / "blink", "meeting-20261002-123316")
    _local_only(base / "blink", "meeting-20261002-140000")
    recs = local.scan(include_all=True)
    assert local.resolve_ref("meeting-20261002-1233", recs).name == "meeting-20261002-123316"
    with pytest.raises(local.RefError, match="ambiguous"):
        local.resolve_ref("meeting-20261002", recs)
    with pytest.raises(local.RefError, match="no local recording"):
        local.resolve_ref("nope", recs)
    path_ref = str(base / "blink" / "meeting-20261002-140000")
    assert local.resolve_ref(path_ref, recs).name == "meeting-20261002-140000"


# ── upload ────────────────────────────────────────────────────────────────────


def test_upload_to_other_team_moves_stitches_and_links(base, fake_upload):
    """The incident, as one command."""
    _paused_recording(base / "blink", "meeting-20261002-123316")

    res = _run("upload", "meeting-20261002-1233", "--team", "twentyone",
               "--title", "Private", "--yes")
    assert res.exit_code == 0, res.output
    assert "uploaded as session 01NEW [twentyone]" in res.output

    assert not (base / "blink" / "meeting-20261002-123316").exists()
    d = base / "twentyone" / "meeting-20261002-123316"
    assert fake_upload["team_id"] == "twentyone"
    assert fake_upload["title"] == "Private"
    assert fake_upload["audio_path"] == d / "meeting-20261002-123316.ogg"
    assert not list(d.glob("*.chunk-*.wav"))  # stitched
    assert upload_journal.read(d)["status"] == "done"
    assert upload_journal.read(d)["team_id"] == "twentyone"
    assert json.loads((d / "session.json").read_text())["session_id"] == "01NEW"
    meta = json.loads((d / "meeting-20261002-123316.session.json").read_text())
    assert meta["vezir_team"] == "twentyone"
    assert meta["output_file"].startswith(str(d))
    # Now it's "uploaded", not in the outbox.
    assert local.scan() == []


def test_upload_flags_are_per_upload_not_persisted(base, fake_upload):
    from vezir.client.config import load_client_prefs

    _local_only(base / "blink", "meeting-20260601-000000")
    res = _run("upload", "meeting-20260601-000000", "--no-sync", "--no-auto-label", "-y")
    assert res.exit_code == 0, res.output
    assert fake_upload["sync"] is False
    assert fake_upload["auto_label"] is False
    assert "sync" not in load_client_prefs()


def test_upload_personal_forces_sync_off(base, fake_upload):
    _local_only(base / "blink", "meeting-20260601-000000")
    res = _run("upload", "meeting-20260601-000000", "--personal", "--sync", "-y")
    assert res.exit_code == 0, res.output
    assert fake_upload["personal"] is True
    assert fake_upload["sync"] is False


def test_upload_confirmation_can_abort(base, fake_upload):
    _local_only(base / "blink", "meeting-20260601-000000")
    res = _run("upload", "meeting-20260601-000000", input="n\n")
    assert res.exit_code == 1
    assert fake_upload == {}


def test_upload_refuses_in_progress(base, fake_upload):
    _paused_recording(base / "blink", "meeting-20261002-140104", owner_pid=os.getpid())
    res = _run("upload", "meeting-20261002-140104", "-y")
    assert res.exit_code == 2
    assert "still recording" in res.output
    assert fake_upload == {}


def test_upload_refuses_already_uploaded(base, fake_upload):
    _uploaded(base / "blink", "meeting-20261001-110259", "01OLD")
    res = _run("upload", "meeting-20261001-110259", "-y")
    assert res.exit_code == 2
    assert "already uploaded as 01OLD" in res.output


def test_upload_unknown_team_refused(base, fake_upload):
    _local_only(base / "blink", "meeting-20260601-000000")
    res = _run("upload", "meeting-20260601-000000", "--team", "typo", "-y")
    assert res.exit_code == 2
    assert "unknown team 'typo'" in res.output
    assert (base / "blink" / "meeting-20260601-000000").exists()


def test_upload_failure_marks_journal_failed(base, monkeypatch, fake_upload):
    from vezir.client import uploader

    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(uploader, "upload_resumable", boom)
    d = _local_only(base / "blink", "meeting-20260601-000000")
    res = _run("upload", "meeting-20260601-000000", "-y")
    assert res.exit_code == 1
    assert upload_journal.read(d)["status"] == "failed"
    [rec] = local.scan()
    assert rec.state == "failed"


# ── move ──────────────────────────────────────────────────────────────────────


def test_move_rehomes_and_updates_metadata(base):
    d = _paused_recording(base / "blink", "meeting-20261002-123316")
    res = _run("move", "meeting-20261002-123316", "--team", "twentyone")
    assert res.exit_code == 0, res.output
    assert not d.exists()
    [rec] = local.scan()
    assert rec.team == "twentyone"
    assert rec.session_dir.parent == base / "twentyone"
    # The startup recovery dialog agrees.
    [r] = recovery.scan_interrupted()
    assert r.team_id == "twentyone"


def test_move_updates_journal_team(base):
    d = _local_only(base / "blink", "meeting-20260601-000000")
    upload_journal.mark_pending(d, title=None, team_id="blink")
    _run("move", "meeting-20260601-000000", "--team", "twentyone")
    new = base / "twentyone" / "meeting-20260601-000000"
    assert upload_journal.read(new)["team_id"] == "twentyone"


def test_move_refusals(base):
    _paused_recording(base / "blink", "meeting-20261002-140104", owner_pid=os.getpid())
    _uploaded(base / "blink", "meeting-20261001-110259", "01OLD")
    _local_only(base / "blink", "meeting-20260601-000000")

    res = _run("move", "meeting-20261002-140104", "--team", "twentyone")
    assert res.exit_code == 2 and "still recording" in res.output
    res = _run("move", "meeting-20261001-110259", "--team", "twentyone")
    assert res.exit_code == 2 and "already on the server" in res.output
    res = _run("move", "meeting-20260601-000000", "--team", "typo")
    assert res.exit_code == 2 and "unknown team" in res.output
    res = _run("move", "meeting-20260601-000000", "--team", "blink")
    assert res.exit_code == 0 and "nothing to do" in res.output


# ── discard ───────────────────────────────────────────────────────────────────


def test_discard_moves_to_trash_and_hides(base):
    d = _local_only(base / "blink", "meeting-20260601-000000")
    res = _run("discard", "meeting-20260601-000000", "-y")
    assert res.exit_code == 0, res.output
    assert not d.exists()
    assert (base / ".trash" / "blink" / "meeting-20260601-000000").is_dir()
    assert local.scan(include_all=True) == []


def test_discard_purge(base):
    d = _local_only(base / "blink", "meeting-20260601-000000")
    res = _run("discard", "meeting-20260601-000000", "--purge", "-y")
    assert res.exit_code == 0, res.output
    assert not d.exists()
    assert not (base / ".trash").exists()


def test_discard_warns_when_not_uploaded_and_can_abort(base):
    d = _local_only(base / "blink", "meeting-20260601-000000")
    res = _run("discard", "meeting-20260601-000000", input="n\n")
    assert "NOT reached the server" in res.output
    assert res.exit_code == 1
    assert d.exists()


def test_discard_refuses_in_progress(base):
    d = _paused_recording(base / "blink", "meeting-20261002-140104", owner_pid=os.getpid())
    res = _run("discard", "meeting-20261002-140104", "-y")
    assert res.exit_code == 2
    assert d.exists()


# ── 0.25.0: held + recording journal states ─────────────────────────────────


def test_held_with_chunks_is_held_but_still_stitched(base, fake_upload):
    d = _paused_recording(base / "blink", "meeting-20261002-123316")
    upload_journal.mark_held(
        d, title="Kept", team_id="blink",
        options={"sync": False, "auto_label": False, "personal": True},
    )
    [rec] = local.scan()
    assert rec.state == "held" and rec.needs_stitch
    assert rec.to_recoverable().kind == "interrupted"
    # Upload defaults come from the held choice.
    res = _run("upload", "meeting-20261002-123316", "-y")
    assert res.exit_code == 0, res.output
    assert fake_upload["sync"] is False
    assert fake_upload["auto_label"] is False
    assert fake_upload["personal"] is True
    assert fake_upload["title"] == "Kept"


def test_recording_journal_with_final_audio_is_pending(base):
    d = _local_only(base / "twentyone", "meeting-20261002-140104")
    upload_journal.mark_recording(d, team_id="twentyone")
    [rec] = local.scan()
    assert rec.state == "pending"
    assert rec.team == "twentyone"


def test_held_attachments_go_out_with_the_upload(base, fake_upload, monkeypatch):
    from vezir.client import uploader

    d = _local_only(base / "blink", "meeting-20260601-000000")
    (d / "attachments").mkdir()
    (d / "attachments" / "slides.pdf").write_bytes(b"deck")
    upload_journal.mark_held(d, title=None, team_id="blink", pending_attachments=True)
    sent = {}

    def fake_att(server_url, token, sid, paths, team_id=None):
        sent.update(sid=sid, names=[p.name for p in paths], team=team_id)
        return [{"name": "slides.pdf"}]

    monkeypatch.setattr(uploader, "upload_attachments", fake_att)
    res = _run("upload", "meeting-20260601-000000", "--team", "twentyone", "-y")
    assert res.exit_code == 0, res.output
    assert sent == {"sid": "01NEW", "names": ["slides.pdf"], "team": "twentyone"}
    new = base / "twentyone" / "meeting-20260601-000000"
    assert "pending_attachments" not in upload_journal.read(new)


def test_doctor_does_not_report_held(base):
    from vezir.doctor import _check_recordings_health, _Results

    d = _paused_recording(base / "blink", "meeting-20261002-123316")
    upload_journal.mark_held(d, title=None, team_id="blink")
    r = _Results()
    _check_recordings_health(r)
    assert not any("123316" in msg for _sev, msg in r.rows)
