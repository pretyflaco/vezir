"""Tests for vezir.client.upload_journal (0.23.0).

The journal is the crash-safe record of "this recording still needs
uploading", covering the post-stop window (compress + upload) that the
millet-record markers don't see.  Pins the state machine and the
pending-scan filtering rules (stub wins, audio must exist, only
journal-touched dirs are ever nagged about).
"""
from __future__ import annotations

import json
from pathlib import Path

from vezir.client import upload_journal


def _dir_with_audio(root: Path, name: str, suffix: str = ".ogg") -> Path:
    d = root / name
    d.mkdir()
    (d / f"{name}{suffix}").write_bytes(b"audio")
    return d


# ── state machine ─────────────────────────────────────────────────────────────


def test_mark_pending_writes_marker(tmp_path):
    d = _dir_with_audio(tmp_path, "meeting-20260928-140000")
    upload_journal.mark_pending(d, title="Standup", team_id="startups")

    state = upload_journal.read(d)
    assert state["status"] == "pending"
    assert state["title"] == "Standup"
    assert state["team_id"] == "startups"
    assert "created_at" in state and "updated_at" in state


def test_full_lifecycle_pending_uploading_done(tmp_path):
    d = _dir_with_audio(tmp_path, "meeting-20260928-140000")
    upload_journal.mark_pending(d, title=None, team_id="startups")
    upload_journal.mark_uploading(d)
    assert upload_journal.read(d)["status"] == "uploading"
    upload_journal.mark_done(d, "01M3M05SG4ZQ373YK9R763MFCX")

    state = upload_journal.read(d)
    assert state["status"] == "done"
    assert state["session_id"] == "01M3M05SG4ZQ373YK9R763MFCX"
    assert "error" not in state


def test_mark_failed_records_error(tmp_path):
    d = _dir_with_audio(tmp_path, "meeting-20260928-140000")
    upload_journal.mark_pending(d, title=None, team_id=None)
    upload_journal.mark_failed(d, "upload failed: connection refused")

    state = upload_journal.read(d)
    assert state["status"] == "failed"
    assert "connection refused" in state["error"]


def test_done_clears_prior_error(tmp_path):
    d = _dir_with_audio(tmp_path, "meeting-20260928-140000")
    upload_journal.mark_pending(d, title=None, team_id=None)
    upload_journal.mark_failed(d, "boom")
    upload_journal.mark_done(d, "SID")
    assert "error" not in upload_journal.read(d)


def test_transitions_without_pending_marker_are_noops(tmp_path):
    d = _dir_with_audio(tmp_path, "meeting-20260928-140000")
    upload_journal.mark_uploading(d)
    upload_journal.mark_done(d, "SID")
    upload_journal.mark_failed(d, "boom")
    assert upload_journal.read(d) == {}


def test_read_missing_or_corrupt_returns_empty(tmp_path):
    d = _dir_with_audio(tmp_path, "meeting-20260928-140000")
    assert upload_journal.read(d) == {}
    (d / ".upload.json").write_text("not json {")
    assert upload_journal.read(d) == {}


def test_mark_pending_preserves_created_at(tmp_path):
    d = _dir_with_audio(tmp_path, "meeting-20260928-140000")
    upload_journal.mark_pending(d, title="first", team_id=None)
    created = upload_journal.read(d)["created_at"]
    upload_journal.mark_pending(d, title="second", team_id=None)
    state = upload_journal.read(d)
    assert state["created_at"] == created
    assert state["title"] == "second"


# ── pending_in_root scan ──────────────────────────────────────────────────────


def test_pending_in_root_finds_pending_and_failed(tmp_path):
    d1 = _dir_with_audio(tmp_path, "meeting-20260928-140000")
    d2 = _dir_with_audio(tmp_path, "meeting-20260928-150000")
    upload_journal.mark_pending(d1, title="A", team_id="startups")
    upload_journal.mark_pending(d2, title="B", team_id=None)
    upload_journal.mark_failed(d2, "boom")

    pending = upload_journal.pending_in_root(tmp_path)
    by_dir = {p.session_dir.name: p for p in pending}
    assert set(by_dir) == {"meeting-20260928-140000", "meeting-20260928-150000"}
    assert by_dir["meeting-20260928-140000"].title == "A"
    assert by_dir["meeting-20260928-150000"].status == "failed"
    assert by_dir["meeting-20260928-150000"].error == "boom"


def test_pending_in_root_skips_done(tmp_path):
    d = _dir_with_audio(tmp_path, "meeting-20260928-140000")
    upload_journal.mark_pending(d, title=None, team_id=None)
    upload_journal.mark_done(d, "SID")
    assert upload_journal.pending_in_root(tmp_path) == []


def test_pending_in_root_stub_wins_over_marker(tmp_path):
    """session.json (written on successful upload) outranks a stale marker."""
    d = _dir_with_audio(tmp_path, "meeting-20260928-140000")
    upload_journal.mark_pending(d, title=None, team_id=None)
    (d / "session.json").write_text(json.dumps({"session_id": "SID"}))
    assert upload_journal.pending_in_root(tmp_path) == []


def test_pending_in_root_skips_when_audio_gone(tmp_path):
    d = tmp_path / "meeting-20260928-140000"
    d.mkdir()
    upload_journal.mark_pending(d, title=None, team_id=None)
    assert upload_journal.pending_in_root(tmp_path) == []


def test_pending_in_root_never_nags_untracked_dirs(tmp_path):
    """Historical/local-only dirs have no journal marker — leave them be."""
    _dir_with_audio(tmp_path, "meeting-20200101-120000")
    assert upload_journal.pending_in_root(tmp_path) == []


def test_pending_in_root_prefers_ogg_over_wav(tmp_path):
    d = tmp_path / "meeting-20260928-140000"
    d.mkdir()
    (d / "meeting-20260928-140000.wav").write_bytes(b"raw")
    (d / "meeting-20260928-140000.ogg").write_bytes(b"compressed")
    upload_journal.mark_pending(d, title=None, team_id=None)

    pending = upload_journal.pending_in_root(tmp_path)
    assert pending[0].audio_path and pending[0].audio_path.suffix == ".ogg"


def test_pending_in_root_ignores_chunk_files(tmp_path):
    d = tmp_path / "meeting-20260928-140000"
    d.mkdir()
    (d / "meeting-20260928-140000.chunk-000.wav").write_bytes(b"chunk")
    upload_journal.mark_pending(d, title=None, team_id=None)
    # Chunks are not uploadable audio (that's recovery's job, not the
    # journal's) — so this dir is not a pending upload.
    assert upload_journal.pending_in_root(tmp_path) == []


def test_pending_in_root_missing_root(tmp_path):
    assert upload_journal.pending_in_root(tmp_path / "nope") == []
