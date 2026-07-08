"""Empty-recording gate (v0.11.1 regression).

A session whose transcript has zero speech segments (an accidental tap, a
dead mic, or silence WhisperX reports as "no active speech") must NOT be
synced to the team repo.  Previously ``_has_unresolved_speakers`` returned
False for a zero-*speaker* transcript, so the pipeline skipped
``needs_labeling`` and pushed empty/stub artifacts, landing ``done``.

The pipeline now routes such sessions to the terminal ``empty`` status and
skips sync entirely.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from vezir.server import queue, worker


@pytest.fixture
def tmp_data(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("VEZIR_DATA", d)
        yield Path(d)


def _write_transcript(sd: Path, sid: str, *, segments, speakers) -> None:
    sd.mkdir(parents=True, exist_ok=True)
    (sd / f"{sid}.json").write_text(
        json.dumps(
            {
                "audio_file": f"{sid}.ogg",
                "language": "en",
                "duration": 3.2,
                "speakers": speakers,
                "segments": segments,
            }
        ),
        encoding="utf-8",
    )
    # Minimal sibling artifacts so _find_artifacts has something.
    (sd / f"{sid}.txt").write_text("", encoding="utf-8")


# ── _is_empty_transcript unit behavior ──


def test_is_empty_transcript_true_for_zero_segments(tmp_path):
    sd = tmp_path / "01SID"
    _write_transcript(sd, "01SID", segments=[], speakers=[])
    assert worker._is_empty_transcript(sd) is True


def test_is_empty_transcript_false_for_content(tmp_path):
    sd = tmp_path / "01SID"
    _write_transcript(
        sd, "01SID",
        segments=[{"start": 0.0, "end": 2.0, "speaker": "SPEAKER_00", "text": "hi"}],
        speakers=[{"id": "SPEAKER_00", "label": None}],
    )
    assert worker._is_empty_transcript(sd) is False


def test_is_empty_transcript_false_when_missing(tmp_path):
    sd = tmp_path / "01SID"
    sd.mkdir()
    # No <sid>.json -> treat as not-empty (a transcription failure, handled
    # elsewhere as `error`, not `empty`).
    assert worker._is_empty_transcript(sd) is False


def test_is_empty_transcript_false_on_bad_json(tmp_path):
    sd = tmp_path / "01SID"
    sd.mkdir()
    (sd / "01SID.json").write_text("{not json", encoding="utf-8")
    assert worker._is_empty_transcript(sd) is False


# ── full pipeline routing: empty -> status=empty, sync NEVER called ──


def _make_job(tmp_data, sid: str) -> dict:
    queue.create_team("blink", "Blink (test)")
    team = queue.get_team("blink")
    team_id = team["id"]
    queue.enqueue(sid, github="alice", team_id=team_id, title="oops")
    return queue.get(sid)


def test_empty_session_routes_to_empty_and_skips_sync(tmp_data, monkeypatch):
    sid = "01KWYAH6QZF1JM7TPECNQCMRRQ"
    job = _make_job(tmp_data, sid)

    calls = {"sync": 0, "label_auto": 0}

    def fake_transcribe(session_dir, job_id, team_id, log_path, **kw):
        _write_transcript(session_dir, job_id, segments=[], speakers=[])
        return 0

    def fake_label_auto(session_dir, job_id, team_id, log_path):
        calls["label_auto"] += 1
        return 0

    def fake_sync(*a, **k):
        calls["sync"] += 1
        return 0

    monkeypatch.setattr(worker.meet_runner, "transcribe", fake_transcribe)
    monkeypatch.setattr(worker.meet_runner, "label_auto", fake_label_auto)
    monkeypatch.setattr(worker.meet_runner, "sync", fake_sync)
    monkeypatch.setattr(worker.meet_runner, "team_has_sync_target", lambda t: True)
    monkeypatch.setattr(worker.meet_runner, "cleanup_home_shim", lambda j: None)

    worker.process_one(job)

    row = queue.get(sid)
    assert row["status"] == "empty"
    assert calls["sync"] == 0, "empty session must never be synced"


def test_nonempty_session_still_syncs(tmp_data, monkeypatch):
    sid = "01KWYE8DN414ZJGPBNZJEMSV47"
    job = _make_job(tmp_data, sid)

    calls = {"sync": 0}

    def fake_transcribe(session_dir, job_id, team_id, log_path, **kw):
        _write_transcript(
            session_dir, job_id,
            segments=[{"start": 0.0, "end": 300.0, "speaker": "alice", "text": "x"}],
            speakers=[{"id": "alice", "label": "alice"}],
        )
        return 0

    def fake_sync(session_dir, job_id, team_id, log_path, **kw):
        calls["sync"] += 1
        # Simulate a successful push so the log-scan gate passes.
        Path(log_path).write_text("millet sync\n  Pushed 5 file(s).\n", encoding="utf-8")
        return 0

    monkeypatch.setattr(worker.meet_runner, "transcribe", fake_transcribe)
    monkeypatch.setattr(worker.meet_runner, "label_auto", lambda *a, **k: 0)
    monkeypatch.setattr(worker.meet_runner, "sync", fake_sync)
    monkeypatch.setattr(worker.meet_runner, "team_has_sync_target", lambda t: True)
    monkeypatch.setattr(worker.meet_runner, "cleanup_home_shim", lambda j: None)

    worker.process_one(job)

    row = queue.get(sid)
    assert calls["sync"] == 1
    assert row["status"] in ("done", "sync_failed")
    assert row["status"] != "empty"
