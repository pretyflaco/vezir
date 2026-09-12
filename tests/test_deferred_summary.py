"""Deferred summary for narrated screen recordings — v0.21.0.

Cue frames are sampled from transcript timestamps, so they cannot exist
while `millet transcribe` is running — which is exactly when the summary
used to be produced.  A screen recording therefore got a text-only
iteration plan describing a screen nobody had looked at.

For those sessions vezir now suppresses the summary during transcription,
extracts frames, and summarizes once afterwards (millet >= 0.20.0 sends
the frames to the model).  Every other session keeps the single-pass flow.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from vezir.server import meet_runner, worker


@pytest.fixture
def tmp_data(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("VEZIR_DATA", d)
        yield Path(d)


# ── which sessions defer ───────────────────────────────────────────────────


class TestShouldDeferSummary:
    def test_video_iteration_plan_defers(self):
        assert worker._should_defer_summary(
            {"video": 1, "summary_template": "iteration-plan"}
        )

    def test_plain_audio_meeting_does_not(self):
        """The common path must pay nothing for this."""
        assert not worker._should_defer_summary(
            {"video": 0, "summary_template": None}
        )

    def test_video_without_the_template_does_not(self):
        """Frames exist, but the default prompt doesn't know about them."""
        assert not worker._should_defer_summary(
            {"video": 1, "summary_template": None}
        )

    def test_template_without_video_does_not(self):
        """No video means no frames to wait for."""
        assert not worker._should_defer_summary(
            {"video": 0, "summary_template": "iteration-plan"}
        )


# ── transcribe args ────────────────────────────────────────────────────────


class TestTranscribeArgs:
    def test_deferred_run_suppresses_the_summary(self, tmp_data):
        args = meet_runner.build_transcribe_args(
            tmp_data, summary_preset="confidential",
            summary_template="iteration-plan", defer_summary=True,
        )
        assert "--no-summarize" in args
        # Passing these would do the work twice.
        assert "--summary-preset" not in args
        assert "--summary-template" not in args

    def test_normal_run_is_unchanged(self, tmp_data):
        args = meet_runner.build_transcribe_args(
            tmp_data, summary_preset="confidential", defer_summary=False,
        )
        assert "--no-summarize" not in args
        assert "--summary-preset" in args

    def test_defaults_to_not_deferring(self, tmp_data):
        assert "--no-summarize" not in meet_runner.build_transcribe_args(tmp_data)


# ── the deferred summary step ──────────────────────────────────────────────


class TestRunDeferredSummary:
    def _job(self):
        return {"summary_preset": "confidential", "summary_template": "iteration-plan"}

    def test_reuses_the_summary_only_primitive(self, tmp_data, monkeypatch):
        """millet documents `label --apply-json` with an empty map as
        're-run just the summary+PDF step' — the retry-summary path."""
        captured = {}

        def fake_apply(session_dir, job_id, team_id, log_path, label_map, **kw):
            captured["label_map"] = label_map
            captured.update(kw)
            return 0

        monkeypatch.setattr(meet_runner, "apply_labels_json", fake_apply)
        sd = tmp_data / "01SID"
        (sd / "attachments").mkdir(parents=True)
        err = worker._run_deferred_summary(
            sd, "01SID", "team", tmp_data / "log.txt", self._job(),
        )
        assert err is None
        assert captured["label_map"] == {}
        assert captured["regenerate_summary"] is True
        assert captured["summary_preset"] == "confidential"
        assert captured["summary_template"] == "iteration-plan"

    def test_nonzero_exit_is_a_summary_error_not_a_job_error(self, tmp_data, monkeypatch):
        monkeypatch.setattr(
            meet_runner, "apply_labels_json", lambda *a, **k: 1,
        )
        sd = tmp_data / "01SID"
        sd.mkdir(parents=True)
        err = worker._run_deferred_summary(
            sd, "01SID", "team", tmp_data / "log.txt", self._job(),
        )
        assert err and "summary failed" in err

    def test_exception_is_caught_and_reported(self, tmp_data, monkeypatch):
        """A raising subprocess wrapper must not kill the job: the
        transcript is already on disk and the user can retry."""
        def boom(*a, **k):
            raise RuntimeError("millet exploded")

        monkeypatch.setattr(meet_runner, "apply_labels_json", boom)
        sd = tmp_data / "01SID"
        sd.mkdir(parents=True)
        err = worker._run_deferred_summary(
            sd, "01SID", "team", tmp_data / "log.txt", self._job(),
        )
        assert err and "millet exploded" in err


# ── end-to-end ordering through process_one ────────────────────────────────


class TestPipelineOrdering:
    def test_frames_exist_before_the_summary_runs(self, tmp_data, monkeypatch):
        """The whole point: the summary must see the frames."""
        from vezir.server import queue

        queue.create_team("blink", "Blink (test)")
        team_id = queue.get_team("blink")["id"]
        job_id = "01SIDDEFER"
        queue.enqueue(
            job_id, github="alice", team_id=team_id,
            summary_preset="confidential", summary_template="iteration-plan",
            video=True,
        )

        sd = tmp_data / "sessions" / job_id
        sd.mkdir(parents=True, exist_ok=True)
        (sd / f"{job_id}.txt").write_text("[00:00:01] Kemal: hi")
        (sd / f"{job_id}.json").write_text('{"segments": [], "language": "en"}')
        order: list[str] = []

        def fake_transcribe(session_dir, jid, tid, log_path, **kw):
            order.append(f"transcribe(defer={kw.get('defer_summary')})")
            return 0

        def fake_frames(session_dir, jid, log_path):
            order.append("extract_frames")
            (session_dir / "attachments").mkdir(exist_ok=True)
            (session_dir / "attachments" / "cue_00-00-01.png").write_bytes(b"\x89PNG")
            return 1

        def fake_apply(session_dir, jid, tid, log_path, label_map, **kw):
            n = len(list((session_dir / "attachments").glob("cue_*.png")))
            order.append(f"summary(frames={n})")
            (session_dir / f"{jid}.iteration-plan.md").write_text("## Overview\n\nx")
            return 0

        monkeypatch.setattr(meet_runner, "transcribe", fake_transcribe)
        monkeypatch.setattr(worker, "_extract_frames", fake_frames)
        monkeypatch.setattr(meet_runner, "apply_labels_json", fake_apply)
        monkeypatch.setattr(meet_runner, "label_auto", lambda *a, **k: 0)
        monkeypatch.setattr(worker, "_extract_video_audio", lambda *a, **k: None)
        monkeypatch.setattr(worker, "_has_unresolved_speakers", lambda sd: False)
        monkeypatch.setattr(worker, "_is_empty_transcript", lambda sd: False)
        monkeypatch.setattr(meet_runner, "team_has_sync_target", lambda *a, **k: False)
        monkeypatch.setattr(meet_runner, "cleanup_home_shim", lambda *a, **k: None)

        worker.process_one(queue.get(job_id))

        assert order == [
            "transcribe(defer=True)",
            "extract_frames",
            "summary(frames=1)",
        ], order
