"""Tests for vezir.client.recovery (0.23.0).

Pins the startup-recovery scan contract built on millet-record 0.6.0
markers + the upload journal:

* interrupted (chunks, recorder dead) / orphaned (recorder alive, owner
  dead) / pending_upload (journal) classification
* in-progress recordings (owner alive) are left alone
* legacy pre-marker dirs get a /proc cmdline fallback for live ffmpeg
* stop_orphaned SIGINTs the recorder and flips the session to
  interrupted; recover() stitches via millet_record.recover_session
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from vezir.client import recovery, upload_journal


@pytest.fixture
def roots(monkeypatch, tmp_path):
    """Two fake recordings roots: startups/ (used) and default/ (empty)."""
    startups = tmp_path / "startups"
    startups.mkdir()
    default = tmp_path / "default"
    default.mkdir()
    monkeypatch.setattr(
        recovery, "recordings_roots",
        lambda: [("startups", startups), ("default", default)],
    )
    return {"startups": startups, "default": default}


def _chunk_dir(root: Path, name: str) -> Path:
    d = root / name
    d.mkdir()
    (d / f"{name}.chunk-000.wav").write_bytes(b"\x00" * 2048)
    return d


def _write_marker(d: Path, stem: str, pid: int, owner_pid: int) -> None:
    (d / f"{stem}.recorder.json").write_text(json.dumps({
        "pid": pid,
        "pid_start_ticks": None,
        "owner_pid": owner_pid,
        "owner_start_ticks": None,
        "backend": "ffmpeg",
        "chunk": f"{stem}.chunk-000.wav",
        "started_at": "2026-09-28T13:59:48",
    }))


def _dead_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


# ── classification ────────────────────────────────────────────────────────────


def test_interrupted_when_recorder_dead(roots):
    d = _chunk_dir(roots["startups"], "meeting-20260928-135948")
    found = recovery.scan_interrupted()
    assert len(found) == 1
    rec = found[0]
    assert rec.session_dir == d
    assert rec.team_id == "startups"
    assert rec.kind == "interrupted"
    assert rec.recorder_pid is None


def test_orphaned_when_recorder_alive_owner_dead(roots):
    d = _chunk_dir(roots["startups"], "meeting-20260928-135948")
    proc = subprocess.Popen(["sleep", "30"])
    try:
        _write_marker(d, d.name, pid=proc.pid, owner_pid=_dead_pid())
        found = recovery.scan_interrupted()
        assert len(found) == 1
        assert found[0].kind == "orphaned"
        assert found[0].recorder_pid == proc.pid
        assert "still running" in found[0].detail
    finally:
        proc.kill()
        proc.wait()


def test_in_progress_recording_is_left_alone(roots):
    d = _chunk_dir(roots["startups"], "meeting-20260928-135948")
    proc = subprocess.Popen(["sleep", "30"])
    try:
        # owner = this very (live) test process
        _write_marker(d, d.name, pid=proc.pid, owner_pid=os.getpid())
        assert recovery.scan_interrupted() == []
    finally:
        proc.kill()
        proc.wait()


def test_pending_upload_from_journal(roots):
    d = roots["startups"] / "meeting-20260928-151929_CITRUSRATE"
    d.mkdir()
    (d / f"{d.name}.ogg").write_bytes(b"audio")
    upload_journal.mark_pending(d, title="citrusrate", team_id="startups")

    found = recovery.scan_interrupted()
    assert len(found) == 1
    rec = found[0]
    assert rec.kind == "pending_upload"
    assert rec.title_hint == "citrusrate"
    assert rec.audio_path and rec.audio_path.suffix == ".ogg"
    assert rec.team_id == "startups"


def test_scan_covers_all_roots(roots):
    _chunk_dir(roots["startups"], "meeting-20260928-135948")
    _chunk_dir(roots["default"], "meeting-20260927-100000")
    found = recovery.scan_interrupted()
    assert {f.team_id for f in found} == {"startups", "default"}


def test_scan_newest_first(roots):
    old = _chunk_dir(roots["startups"], "meeting-20260901-100000")
    new = _chunk_dir(roots["startups"], "meeting-20260928-135948")
    os.utime(old, (1000000000, 1000000000))
    found = recovery.scan_interrupted()
    assert found[0].session_dir == new


# ── legacy /proc fallback ─────────────────────────────────────────────────────


def test_legacy_proc_fallback_matches_ffmpeg_argv(tmp_path):
    session_dir = tmp_path / "meeting-20260928-135948"
    session_dir.mkdir()
    proc_root = tmp_path / "proc"
    (proc_root / "4321").mkdir(parents=True)
    (proc_root / "4321" / "cmdline").write_bytes(
        b"ffmpeg\0-y\0-f\0pulse\0-i\0default\0"
        + os.fsencode(str(session_dir / "meeting-20260928-135948.chunk-000.wav")) + b"\0"
    )
    # Non-ffmpeg process touching the same dir must not match.
    (proc_root / "4322").mkdir()
    (proc_root / "4322" / "cmdline").write_bytes(
        b"python3\0" + os.fsencode(str(session_dir)) + b"\0"
    )
    # Non-numeric entries are skipped.
    (proc_root / "self").mkdir()

    assert recovery._legacy_live_recorder(session_dir, str(proc_root)) == (4321, None)


def test_legacy_proc_fallback_no_match(tmp_path):
    (tmp_path / "proc").mkdir()
    assert recovery._legacy_live_recorder(tmp_path, str(tmp_path / "proc")) is None


def test_legacy_dir_with_live_owner_is_left_alone(roots, monkeypatch):
    """A pre-marker recording whose ffmpeg still has a LIVE parent (e.g.
    an old-version TUI recording right now) is in progress, not orphaned
    — mislabeling it would invite the user to kill an active recording."""
    _chunk_dir(roots["startups"], "meeting-20260928-160248")
    monkeypatch.setattr(
        recovery, "_legacy_live_recorder",
        lambda session_dir: (99999, os.getpid()),  # live parent = us
    )
    assert recovery.scan_interrupted() == []


def test_legacy_dir_with_reparented_recorder_is_orphaned(roots, monkeypatch):
    """ppid 1 (reparented to init) = the owner is gone = orphaned."""
    _chunk_dir(roots["startups"], "meeting-20260928-135948")
    monkeypatch.setattr(
        recovery, "_legacy_live_recorder",
        lambda session_dir: (99999, 1),
    )
    found = recovery.scan_interrupted()
    assert len(found) == 1
    assert found[0].kind == "orphaned"
    assert found[0].recorder_pid == 99999


# ── stop_orphaned / recover ───────────────────────────────────────────────────


def test_stop_orphaned_sigints_and_flips_state(roots):
    d = _chunk_dir(roots["startups"], "meeting-20260928-135948")
    proc = subprocess.Popen(["sleep", "30"])
    rec = recovery.RecoverableSession(
        session_dir=d, team_id="startups", kind="orphaned",
        started_at=None, total_bytes=2048, recorder_pid=proc.pid,
        audio_path=None, title_hint=None, detail="recorder still running",
    )
    try:
        assert recovery.stop_orphaned(rec, timeout=5.0)
        assert proc.poll() is not None
        assert rec.kind == "interrupted"
        assert rec.recorder_pid is None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_stop_orphaned_already_dead(roots):
    d = _chunk_dir(roots["startups"], "meeting-20260928-135948")
    rec = recovery.RecoverableSession(
        session_dir=d, team_id="startups", kind="orphaned",
        started_at=None, total_bytes=2048, recorder_pid=_dead_pid(),
        audio_path=None, title_hint=None, detail="",
    )
    assert recovery.stop_orphaned(rec)


def test_recover_stitches_chunks(roots):
    pytest.importorskip("millet_record.capture")
    d = _chunk_dir(roots["startups"], "meeting-20260928-135948")
    rec = recovery.RecoverableSession(
        session_dir=d, team_id="startups", kind="interrupted",
        started_at=None, total_bytes=2048, recorder_pid=None,
        audio_path=None, title_hint=None, detail="",
    )
    out = recovery.recover(rec)
    assert out == d / f"{d.name}.wav"
    assert out.exists()
    assert rec.audio_path == out


# ── recordings_roots ──────────────────────────────────────────────────────────


def test_recordings_roots_enumerates_disk(monkeypatch, tmp_path):
    """The scan roots come from what's ON DISK under the recordings base,
    not from teams.json — recordings of long-removed teams still count."""
    monkeypatch.setenv("VEZIR_RECORD_DIR", str(tmp_path))
    (tmp_path / "startups").mkdir()
    (tmp_path / "default").mkdir()
    (tmp_path / ".trash").mkdir()  # dotdirs are skipped
    (tmp_path / "stray-file.txt").write_text("x")  # files are skipped

    roots = recovery.recordings_roots()
    by_team = dict(roots)
    assert by_team == {
        "default": tmp_path / "default",
        "startups": tmp_path / "startups",
    }


# ── 0.24.0: team resolution + shared salvage pipeline ───────────────────────


def test_resolve_team_precedence(tmp_path):
    d = tmp_path / "blink" / "meeting-20261002-123316"
    d.mkdir(parents=True)
    assert recovery.resolve_team(d) == "blink"  # folder
    (d / "meeting-20261002-123316.session.json").write_text(
        json.dumps({"vezir_team": "twentyone"})
    )
    assert recovery.resolve_team(d, "blink") == "twentyone"  # meta beats folder
    upload_journal.mark_pending(d, title=None, team_id="startups")
    assert recovery.resolve_team(d, "blink") == "startups"  # journal beats meta


def _stub_uploader(monkeypatch, sent):
    from vezir.client import uploader

    monkeypatch.setattr(uploader, "server_supports_resumable", lambda *a, **k: True)

    def up(server_url, token, audio_path, **kw):
        sent.update(kw, audio_path=audio_path)
        return {"session_id": "01S"}

    monkeypatch.setattr(uploader, "upload_resumable", up)


def test_salvage_pending_upload_to_other_team(monkeypatch, tmp_path):
    """Changing the team moves the folder AND re-roots the audio path."""
    base = tmp_path / "rec"
    src = base / "blink" / "meeting-1"
    src.mkdir(parents=True)
    (src / "meeting-1.ogg").write_bytes(b"OggS")
    monkeypatch.setenv("VEZIR_RECORD_DIR", str(base))
    sent: dict = {}
    _stub_uploader(monkeypatch, sent)
    rec = recovery.RecoverableSession(
        session_dir=src, team_id="blink", kind="pending_upload", started_at=None,
        total_bytes=4, recorder_pid=None, audio_path=src / "meeting-1.ogg",
        title_hint=None, detail="",
    )
    statuses: list = []
    sid = recovery.salvage(
        rec, server_url="https://srv", token="t", team="twentyone",
        auto_label=False, sync=True, personal=True, on_status=statuses.append,
    )
    dest = base / "twentyone" / "meeting-1"
    assert sid == "01S"
    assert sent["audio_path"] == dest / "meeting-1.ogg"
    assert sent["team_id"] == "twentyone"
    assert sent["auto_label"] is False
    assert sent["sync"] is False  # personal forces it off
    assert any("moving" in s for s in statuses)
    assert upload_journal.read(dest)["status"] == "done"
    assert json.loads((dest / "session.json").read_text())["team_id"] == "twentyone"


def test_salvage_refuses_move_before_stopping_orphan(monkeypatch, tmp_path):
    """An orphan that won't die must not have its folder moved under it."""
    base = tmp_path / "rec"
    src = base / "blink" / "meeting-1"
    src.mkdir(parents=True)
    monkeypatch.setenv("VEZIR_RECORD_DIR", str(base))
    monkeypatch.setattr(recovery, "stop_orphaned", lambda rec: False)
    rec = recovery.RecoverableSession(
        session_dir=src, team_id="blink", kind="orphaned", started_at=None,
        total_bytes=0, recorder_pid=12345, audio_path=None,
        title_hint=None, detail="",
    )
    with pytest.raises(RuntimeError, match="did not exit"):
        recovery.salvage(rec, server_url="https://srv", token="t", team="twentyone")
    assert src.exists()
