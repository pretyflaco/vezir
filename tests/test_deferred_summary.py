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

import json
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


# ── retry-summary artifact naming — v0.22.3 ────────────────────────────────
#
# Incident 2026-09-17 (session 01M2P6FTRG4WAKKE5T7TV6HNFM): a language
# retry ('en') on an iteration-plan session succeeded in millet — which
# writes <base>.<template>.<lang>.md — but vezir's belt-and-suspenders
# check globbed only for .summary.en.md, false-failed the retry, and
# skipped the sync.  The summary existed; vezir's bookkeeping said it
# didn't.


class TestSummaryArtifactGlobs:
    def test_default_run(self):
        assert worker._summary_artifact_globs(None, None) == ["*.summary.md"]

    def test_language_only(self):
        assert worker._summary_artifact_globs(None, "en") == ["*.summary.en.md"]

    def test_template_only(self):
        assert worker._summary_artifact_globs("iteration-plan", None) == [
            "*.iteration-plan.md", "*.summary.md",
        ]

    def test_template_and_language(self):
        """The incident shape: template names the family, lang qualifies."""
        assert worker._summary_artifact_globs("iteration-plan", "en") == [
            "*.iteration-plan.en.md", "*.iteration-plan.md",
            "*.summary.en.md", "*.summary.md",
        ]


class TestFindArtifactsTemplateNaming:
    def test_template_with_language_is_listed(self, tmp_data):
        sd = tmp_data / "01SIDART"
        sd.mkdir()
        (sd / "01SIDART.iteration-plan.en.md").write_text("## Plan\n\nx")
        arts = worker._find_artifacts(sd)
        assert arts.get("iteration_plan_en") == "01SIDART.iteration-plan.en.md"

    def test_template_without_language_keeps_the_historic_key(self, tmp_data):
        sd = tmp_data / "01SIDART"
        sd.mkdir()
        (sd / "01SIDART.iteration-plan.md").write_text("## Plan\n\nx")
        arts = worker._find_artifacts(sd)
        assert arts.get("iteration_plan") == "01SIDART.iteration-plan.md"

    def test_default_and_translation_summaries_are_not_templates(self, tmp_data):
        sd = tmp_data / "01SIDART"
        sd.mkdir()
        (sd / "01SIDART.summary.md").write_text("s")
        (sd / "01SIDART.summary.en.md").write_text("s")
        (sd / "01SIDART.translation.de.md").write_text("t")
        arts = worker._find_artifacts(sd)
        assert arts.get("summary") == "01SIDART.summary.md"
        assert arts.get("summary_en") == "01SIDART.summary.en.md"
        assert not any(k.startswith("translation") for k in arts)


class TestFallbackProvenanceTemplateSidecar:
    def test_reads_template_named_meta(self, tmp_data):
        sd = tmp_data / "01SIDFB"
        sd.mkdir()
        (sd / "01SIDFB.iteration-plan.en.meta.json").write_text(json.dumps({
            "backend": "venice", "model": "glm-5.3-flash (TEE)",
            "fallback_used": True,
        }))
        assert (
            worker._summary_fallback_provenance(sd, "en")
            == "venice/glm-5.3-flash (TEE)"
        )

    def test_no_fallback_is_none(self, tmp_data):
        sd = tmp_data / "01SIDFB"
        sd.mkdir()
        (sd / "01SIDFB.iteration-plan.en.meta.json").write_text(json.dumps({
            "backend": "tinfoil", "model": "glm-5-3-flash (TEE)",
            "fallback_used": False,
        }))
        assert worker._summary_fallback_provenance(sd, "en") is None


class TestRetrySummaryOnTemplateSession:
    """End-to-end through retry_summary_for_session with mocked millet."""

    def _setup(self, tmp_data):
        from vezir.server import queue

        queue.create_team("blink", "Blink (test)")
        team_id = queue.get_team("blink")["id"]
        job_id = "01SIDRETRY"
        queue.enqueue(
            job_id, github="alice", team_id=team_id,
            summary_preset="confidential", summary_template="iteration-plan",
            video=True,
        )
        queue.update_status(
            job_id, "done", summary_error="summary retry failed (old bug)",
        )
        sd = tmp_data / "sessions" / job_id
        sd.mkdir(parents=True, exist_ok=True)
        return queue, job_id, sd

    def _no_sync(self, monkeypatch):
        monkeypatch.setattr(meet_runner, "team_has_sync_target", lambda *a: False)
        monkeypatch.setattr(meet_runner, "cleanup_home_shim", lambda *a, **k: None)

    def test_language_retry_on_template_session_succeeds(
        self, tmp_data, monkeypatch,
    ):
        """The exact incident: millet writes <base>.<template>.<lang>.md —
        the retry must validate it, list it as an artifact, clear
        summary_error, and record provenance from the template sidecar."""
        queue, job_id, sd = self._setup(tmp_data)
        self._no_sync(monkeypatch)

        def fake_apply(session_dir, jid, tid, log_path, label_map, **kw):
            (session_dir / f"{jid}.iteration-plan.en.md").write_text("## Plan\n\nx")
            (session_dir / f"{jid}.iteration-plan.en.meta.json").write_text(
                json.dumps({
                    "backend": "tinfoil", "model": "glm-5-3-flash (TEE)",
                    "fallback_used": False, "frames_used": 10,
                })
            )
            return 0

        monkeypatch.setattr(meet_runner, "apply_labels_json", fake_apply)

        worker.retry_summary_for_session(job_id, language_override="en")

        row = queue.get(job_id)
        assert row["status"] == "done"
        assert row["summary_error"] is None
        arts = json.loads(row["artifacts"])
        assert arts.get("iteration_plan_en") == (
            f"{job_id}.iteration-plan.en.md"
        )
        assert row["summary_provenance"] == "tinfoil/glm-5-3-flash (TEE)"

    def test_template_only_retry_succeeds(self, tmp_data, monkeypatch):
        queue, job_id, sd = self._setup(tmp_data)
        self._no_sync(monkeypatch)

        def fake_apply(session_dir, jid, tid, log_path, label_map, **kw):
            (session_dir / f"{jid}.iteration-plan.md").write_text("## Plan\n\nx")
            return 0

        monkeypatch.setattr(meet_runner, "apply_labels_json", fake_apply)

        worker.retry_summary_for_session(job_id)

        row = queue.get(job_id)
        assert row["summary_error"] is None
        assert json.loads(row["artifacts"]).get("iteration_plan") == (
            f"{job_id}.iteration-plan.md"
        )

    def test_degraded_summary_still_satisfies_a_template_retry(
        self, tmp_data, monkeypatch,
    ):
        """Old millet degrades a template request to the default summary;
        the check tolerates that (mirrors the finalize path)."""
        queue, job_id, sd = self._setup(tmp_data)
        self._no_sync(monkeypatch)

        def fake_apply(session_dir, jid, tid, log_path, label_map, **kw):
            (session_dir / f"{jid}.summary.md").write_text("## Overview\n\nx")
            return 0

        monkeypatch.setattr(meet_runner, "apply_labels_json", fake_apply)

        worker.retry_summary_for_session(job_id, language_override="en")

        assert queue.get(job_id)["summary_error"] is None

    def test_missing_artifact_still_reports_failure(self, tmp_data, monkeypatch):
        """The check must not become a no-op: a millet that exits 0 without
        writing anything is still a summary error — naming the shape it
        expected."""
        queue, job_id, sd = self._setup(tmp_data)
        self._no_sync(monkeypatch)
        monkeypatch.setattr(
            meet_runner, "apply_labels_json", lambda *a, **k: 0,
        )

        worker.retry_summary_for_session(job_id, language_override="en")

        row = queue.get(job_id)
        assert row["summary_error"] and "iteration-plan.en.md" in row["summary_error"]
