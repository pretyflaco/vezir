"""Summary-fallback provenance (v0.14.0).

When millet's requested summary preset fails and millet falls back down the
backend chain (opt-in MILLET_SUMMARY_PRESET_FALLBACK, millet-pipeline >=
0.16.0), the ``.summary.meta.json`` sidecar records ``fallback_used: true``
plus the actual backend/model.  The worker surfaces that as
``jobs.summary_fallback = "<backend>/<model>"`` so the UI can show the
summary was NOT produced by the requested preset — loud, never silent.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from vezir.server import migrations, queue, worker


@pytest.fixture
def tmp_data(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("VEZIR_DATA", d)
        yield Path(d)


def _write_meta(sd: Path, sid: str, meta: dict, *, lang: str | None = None) -> None:
    sd.mkdir(parents=True, exist_ok=True)
    suffix = f".{lang}" if lang else ""
    (sd / f"{sid}.summary{suffix}.meta.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )


# ── _summary_fallback_provenance unit behavior ──


def test_provenance_returns_backend_model_on_fallback(tmp_path):
    sd = tmp_path / "01SID"
    _write_meta(sd, "01SID", {
        "backend": "openai", "model": "kimi-k3",
        "preset": "high-quality", "fallback_used": True,
    })
    assert worker._summary_fallback_provenance(sd) == "openai/kimi-k3"


def test_provenance_none_when_requested_preset_ran(tmp_path):
    sd = tmp_path / "01SID"
    _write_meta(sd, "01SID", {
        "backend": "claudemax", "model": "claude-sonnet-4-6",
        "preset": "high-quality", "fallback_used": False,
    })
    assert worker._summary_fallback_provenance(sd) is None


def test_provenance_none_when_meta_missing(tmp_path):
    sd = tmp_path / "01SID"
    sd.mkdir()
    assert worker._summary_fallback_provenance(sd) is None


def test_provenance_none_on_malformed_json(tmp_path):
    sd = tmp_path / "01SID"
    sd.mkdir()
    (sd / "01SID.summary.meta.json").write_text("{not json", encoding="utf-8")
    assert worker._summary_fallback_provenance(sd) is None


def test_provenance_none_on_old_millet_meta(tmp_path):
    # millet < 0.16.0 wrote no fallback_used field.
    sd = tmp_path / "01SID"
    _write_meta(sd, "01SID", {"backend": "openai", "model": "kimi-k3"})
    assert worker._summary_fallback_provenance(sd) is None


def test_provenance_missing_fields_fall_back_to_unknown(tmp_path):
    sd = tmp_path / "01SID"
    _write_meta(sd, "01SID", {"fallback_used": True})
    assert worker._summary_fallback_provenance(sd) == "unknown/unknown"


def test_provenance_lang_suffixed_meta(tmp_path):
    sd = tmp_path / "01SID"
    _write_meta(sd, "01SID", {
        "backend": "claudemax", "model": "claude-sonnet-4-6",
        "fallback_used": False,
    })
    _write_meta(sd, "01SID", {
        "backend": "openai", "model": "kimi-k3", "fallback_used": True,
    }, lang="de")
    # Primary summary: no fallback.  German additional summary: fallback.
    assert worker._summary_fallback_provenance(sd) is None
    assert worker._summary_fallback_provenance(sd, "de") == "openai/kimi-k3"


# ── queue.update_status sentinel semantics for summary_fallback ──


def test_update_status_summary_fallback_roundtrip(tmp_data):
    queue.create_team("blink", "Blink (test)")
    team_id = queue.get_team("blink")["id"]
    queue.enqueue("01SIDAAAA", github="alice", team_id=team_id)

    queue.update_status("01SIDAAAA", "done", summary_fallback="openai/kimi-k3")
    assert queue.get("01SIDAAAA")["summary_fallback"] == "openai/kimi-k3"

    # Sentinel default: a plain status update must NOT clobber the column.
    queue.update_status("01SIDAAAA", "needs_labeling")
    assert queue.get("01SIDAAAA")["summary_fallback"] == "openai/kimi-k3"

    # Explicit None clears it.
    queue.update_status("01SIDAAAA", "done", summary_fallback=None)
    assert queue.get("01SIDAAAA")["summary_fallback"] is None


# ── migration idempotency ──


def test_migrate_0_14_0_idempotent(tmp_data):
    first = migrations.migrate_0_14_0()
    assert first.get("version") == "0.14.0-summary-fallback"
    second = migrations.migrate_0_14_0()
    assert second == {"already_applied": True}
    # Column is usable after the migration.
    queue.create_team("blink", "Blink (test)")
    team_id = queue.get_team("blink")["id"]
    queue.enqueue("01SIDBBBB", github="alice", team_id=team_id)
    queue.update_status(
        "01SIDBBBB", "done", summary_fallback="openai/kimi-k3",
    )
    assert queue.get("01SIDBBBB")["summary_fallback"] == "openai/kimi-k3"


# ── full pipeline: fallback meta lands on the job row ──


def test_pipeline_records_summary_fallback(tmp_data, monkeypatch):
    sid = "01KWFALLBACK0000000000001"
    queue.create_team("blink", "Blink (test)")
    team_id = queue.get_team("blink")["id"]
    queue.enqueue(sid, github="alice", team_id=team_id, title="fallback demo")
    job = queue.get(sid)

    def fake_transcribe(session_dir, job_id, team_id, log_path, **kw):
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / f"{job_id}.json").write_text(json.dumps({
            "audio_file": f"{job_id}.ogg", "language": "en", "duration": 3.2,
            "segments": [
                {"start": 0.0, "end": 2.0, "speaker": "alice", "text": "hi"},
            ],
            "speakers": [{"id": "alice", "label": "alice"}],
        }), encoding="utf-8")
        (session_dir / f"{job_id}.txt").write_text("hi", encoding="utf-8")
        (session_dir / f"{job_id}.summary.md").write_text(
            "# Summary\n\ncontent", encoding="utf-8",
        )
        _write_meta(session_dir, job_id, {
            "backend": "openai", "model": "kimi-k3",
            "preset": "high-quality", "fallback_used": True,
        })
        return 0

    monkeypatch.setattr(worker.meet_runner, "transcribe", fake_transcribe)
    monkeypatch.setattr(worker.meet_runner, "label_auto", lambda *a, **k: 0)
    monkeypatch.setattr(worker.meet_runner, "team_has_sync_target", lambda t: False)
    monkeypatch.setattr(worker.meet_runner, "cleanup_home_shim", lambda j: None)

    worker.process_one(job)

    row = queue.get(sid)
    assert row["status"] == "done"
    assert row["summary_error"] is None
    assert row["summary_fallback"] == "openai/kimi-k3"


def test_pipeline_no_fallback_leaves_column_null(tmp_data, monkeypatch):
    sid = "01KWNOFALLBACK000000000001"
    queue.create_team("blink", "Blink (test)")
    team_id = queue.get_team("blink")["id"]
    queue.enqueue(sid, github="alice", team_id=team_id, title="normal run")
    job = queue.get(sid)

    def fake_transcribe(session_dir, job_id, team_id, log_path, **kw):
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / f"{job_id}.json").write_text(json.dumps({
            "audio_file": f"{job_id}.ogg", "language": "en", "duration": 3.2,
            "segments": [
                {"start": 0.0, "end": 2.0, "speaker": "alice", "text": "hi"},
            ],
            "speakers": [{"id": "alice", "label": "alice"}],
        }), encoding="utf-8")
        (session_dir / f"{job_id}.txt").write_text("hi", encoding="utf-8")
        (session_dir / f"{job_id}.summary.md").write_text(
            "# Summary\n\ncontent", encoding="utf-8",
        )
        _write_meta(session_dir, job_id, {
            "backend": "claudemax", "model": "claude-sonnet-4-6",
            "preset": "high-quality", "fallback_used": False,
        })
        return 0

    monkeypatch.setattr(worker.meet_runner, "transcribe", fake_transcribe)
    monkeypatch.setattr(worker.meet_runner, "label_auto", lambda *a, **k: 0)
    monkeypatch.setattr(worker.meet_runner, "team_has_sync_target", lambda t: False)
    monkeypatch.setattr(worker.meet_runner, "cleanup_home_shim", lambda j: None)

    worker.process_one(job)

    row = queue.get(sid)
    assert row["status"] == "done"
    assert row["summary_fallback"] is None
