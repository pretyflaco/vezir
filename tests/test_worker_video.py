"""Worker video pipeline (v0.18.0): audio extraction + cue frames.

A video upload (.mp4/.mov) lands at the session root; the worker extracts
the audio track into ``<id>.ogg`` before transcribe and later pulls one PNG
frame per narrated cue (transcript segment start) into ``attachments/`` —
flat, so the existing attachments list/download/pull/sync channels carry
them with no new plumbing.  ffmpeg is mocked throughout.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def tmp_data(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("VEZIR_DATA", d)
        yield Path(d)


def _session(tmp_data: Path, sid: str) -> Path:
    sdir = tmp_data / "sessions" / sid
    sdir.mkdir(parents=True, exist_ok=True)
    return sdir


def _log_path(tmp_data: Path, sid: str) -> Path:
    lp = tmp_data / "logs" / f"{sid}.log"
    lp.parent.mkdir(parents=True, exist_ok=True)
    return lp


def _ok_ffmpeg(cmd, stdout=None, stderr=None):
    """Fake ffmpeg that 'succeeds' by writing the output file (last argv)."""
    Path(cmd[-1]).write_bytes(b"OUT")

    class _R:
        returncode = 0

    return _R()


def _ffprobe_aware(start_time: str):
    """Fake subprocess.run that answers ffprobe with *start_time* and
    otherwise behaves like _ok_ffmpeg."""
    def _run(cmd, stdout=None, stderr=None, **kw):
        if cmd and cmd[0] == "ffprobe":
            class _R:
                returncode = 0
                stdout = start_time + "\n"

            return _R()
        return _ok_ffmpeg(cmd, stdout=stdout, stderr=stderr)

    return _run


def _write_transcript(sdir: Path, sid: str, starts: list[float]) -> None:
    segments = [{"start": s, "end": s + 2.0, "text": "x", "speaker": "YOU"} for s in starts]
    (sdir / f"{sid}.json").write_text(json.dumps({"segments": segments}))


# ── _extract_video_audio ─────────────────────────────────────────────────────


def test_extract_video_audio_writes_ogg(tmp_data, monkeypatch):
    from vezir.server import worker

    sid = "01HZVIDEO00000000000000A"
    sdir = _session(tmp_data, sid)
    video = sdir / f"{sid}.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42")

    calls: list = []

    def _run(cmd, stdout=None, stderr=None):
        calls.append(cmd)
        return _ok_ffmpeg(cmd)

    monkeypatch.setattr(subprocess, "run", _run)
    worker._extract_video_audio(sdir, sid, _log_path(tmp_data, sid))

    out = sdir / f"{sid}.ogg"
    assert out.exists()
    # 16 kHz mono Opus, matching millet's decode expectations.
    cmd = calls[0]
    assert "-vn" in cmd and "16000" in cmd and "libopus" in cmd
    assert cmd[cmd.index("-i") + 1] == str(video)
    # Source video retained at the session root.
    assert video.exists()


def test_extract_video_audio_idempotent(tmp_data, monkeypatch):
    from vezir.server import worker

    sid = "01HZVIDEO00000000000000B"
    sdir = _session(tmp_data, sid)
    (sdir / f"{sid}.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
    (sdir / f"{sid}.ogg").write_bytes(b"OggSDONE")

    def _boom(*a, **k):
        raise AssertionError("ffmpeg must not run when <id>.ogg already exists")

    monkeypatch.setattr(subprocess, "run", _boom)
    worker._extract_video_audio(sdir, sid, _log_path(tmp_data, sid))
    assert (sdir / f"{sid}.ogg").read_bytes() == b"OggSDONE"


def test_extract_video_audio_mov(tmp_data, monkeypatch):
    from vezir.server import worker

    sid = "01HZVIDEO00000000000000C"
    sdir = _session(tmp_data, sid)
    (sdir / f"{sid}.mov").write_bytes(b"\x00\x00\x00\x14ftypqt  ")

    monkeypatch.setattr(subprocess, "run", _ok_ffmpeg)
    worker._extract_video_audio(sdir, sid, _log_path(tmp_data, sid))
    assert (sdir / f"{sid}.ogg").exists()


def test_extract_video_audio_missing_video_raises(tmp_data):
    from vezir.server import worker

    sid = "01HZVIDEO00000000000000D"
    sdir = _session(tmp_data, sid)
    with pytest.raises(RuntimeError, match="flagged video but no .mp4/.mov"):
        worker._extract_video_audio(sdir, sid, _log_path(tmp_data, sid))


def test_extract_video_audio_ffmpeg_failure_raises(tmp_data, monkeypatch):
    from vezir.server import worker

    sid = "01HZVIDEO00000000000000E"
    sdir = _session(tmp_data, sid)
    (sdir / f"{sid}.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")

    def _run(cmd, stdout=None, stderr=None):
        class _R:
            returncode = 1

        return _R()

    monkeypatch.setattr(subprocess, "run", _run)
    with pytest.raises(RuntimeError, match="failed to extract audio"):
        worker._extract_video_audio(sdir, sid, _log_path(tmp_data, sid))
    # Partial output cleaned up.
    assert not (sdir / f"{sid}.ogg").exists()


# ── _extract_frames ──────────────────────────────────────────────────────────


def test_extract_frames_writes_cue_pngs_flat(tmp_data, monkeypatch):
    from vezir.server import worker

    sid = "01HZFRAMES0000000000000A"
    sdir = _session(tmp_data, sid)
    (sdir / f"{sid}.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
    _write_transcript(sdir, sid, [0.0, 65.0, 3723.4])

    calls: list = []

    def _run(cmd, stdout=None, stderr=None):
        calls.append(cmd)
        return _ok_ffmpeg(cmd)

    monkeypatch.setattr(subprocess, "run", _run)
    written = worker._extract_frames(sdir, sid, _log_path(tmp_data, sid))

    assert written == 3
    adir = sdir / "attachments"
    names = sorted(p.name for p in adir.iterdir())
    assert names == ["cue_00-00-00.png", "cue_00-01-05.png", "cue_01-02-03.png"]
    # ffmpeg seeked to the cue timestamps.
    seeks = [cmd[cmd.index("-ss") + 1] for cmd in calls]
    assert seeks == ["00:00:00", "00:01:05", "01:02:03"]


def test_extract_frames_no_video_is_noop(tmp_data, monkeypatch):
    from vezir.server import worker

    sid = "01HZFRAMES0000000000000B"
    sdir = _session(tmp_data, sid)
    _write_transcript(sdir, sid, [0.0])

    def _boom(*a, **k):
        raise AssertionError("ffmpeg must not run for audio-only sessions")

    monkeypatch.setattr(subprocess, "run", _boom)
    assert worker._extract_frames(sdir, sid, _log_path(tmp_data, sid)) == 0
    assert not (sdir / "attachments").exists()


def test_extract_frames_no_cues_is_noop(tmp_data, monkeypatch):
    from vezir.server import worker

    sid = "01HZFRAMES0000000000000C"
    sdir = _session(tmp_data, sid)
    (sdir / f"{sid}.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
    # No transcript json at all.
    assert worker._extract_frames(sdir, sid, _log_path(tmp_data, sid)) == 0


def test_extract_frames_idempotent_keeps_existing(tmp_data, monkeypatch):
    from vezir.server import worker

    sid = "01HZFRAMES0000000000000D"
    sdir = _session(tmp_data, sid)
    (sdir / f"{sid}.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
    _write_transcript(sdir, sid, [0.0, 5.0])
    adir = sdir / "attachments"
    adir.mkdir()
    (adir / "cue_00-00-00.png").write_bytes(b"OLD")

    calls: list = []

    def _run(cmd, stdout=None, stderr=None):
        calls.append(cmd)
        return _ok_ffmpeg(cmd)

    monkeypatch.setattr(subprocess, "run", _run)
    written = worker._extract_frames(sdir, sid, _log_path(tmp_data, sid))

    assert written == 2
    assert len(calls) == 1  # only the missing frame ran ffmpeg
    assert (adir / "cue_00-00-00.png").read_bytes() == b"OLD"


def test_extract_frames_caps_and_samples_evenly(tmp_data, monkeypatch):
    from vezir.server import worker

    sid = "01HZFRAMES0000000000000E"
    sdir = _session(tmp_data, sid)
    (sdir / f"{sid}.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
    starts = [float(i * 10) for i in range(120)]  # 120 cues over 20 min
    _write_transcript(sdir, sid, starts)

    monkeypatch.setattr(subprocess, "run", _ok_ffmpeg)
    written = worker._extract_frames(sdir, sid, _log_path(tmp_data, sid))

    assert written == worker._MAX_FRAMES
    frames = sorted((sdir / "attachments").glob("cue_*.png"))
    assert len(frames) == worker._MAX_FRAMES
    # Even sampling: first and last cues are both covered.
    assert frames[0].name == "cue_00-00-00.png"
    assert frames[-1].name == "cue_00-19-50.png"


def test_extract_frames_failed_seek_is_skipped(tmp_data, monkeypatch):
    from vezir.server import worker

    sid = "01HZFRAMES0000000000000F"
    sdir = _session(tmp_data, sid)
    (sdir / f"{sid}.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
    _write_transcript(sdir, sid, [0.0, 5.0])

    def _run(cmd, stdout=None, stderr=None):
        if cmd[cmd.index("-ss") + 1] == "00:00:05":
            class _R:
                returncode = 1

            return _R()
        return _ok_ffmpeg(cmd)

    monkeypatch.setattr(subprocess, "run", _run)
    written = worker._extract_frames(sdir, sid, _log_path(tmp_data, sid))
    assert written == 1
    assert not (sdir / "attachments" / "cue_00-00-05.png").exists()


# ── offset-timeline videos (v0.19.1) ─────────────────────────────────────────


def test_extract_frames_seeks_with_start_time_offset(tmp_data, monkeypatch):
    """Pre-0.12.2 android screen recordings carry boot-clock PTS
    (start_time ≈ uptime): seeks must be start_time + cue, not cue."""
    from vezir.server import worker

    sid = "01HZOFFSET000000000000A"
    sdir = _session(tmp_data, sid)
    (sdir / f"{sid}.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
    _write_transcript(sdir, sid, [0.0, 65.0])

    calls: list = []

    def _run(cmd, stdout=None, stderr=None, **kw):
        if cmd[0] == "ffprobe":
            class _R:
                returncode = 0
                stdout = "270487.521800\n"

            return _R()
        calls.append(cmd)
        return _ok_ffmpeg(cmd)

    monkeypatch.setattr(subprocess, "run", _run)
    written = worker._extract_frames(sdir, sid, _log_path(tmp_data, sid))

    assert written == 2
    seeks = [cmd[cmd.index("-ss") + 1] for cmd in calls]
    assert seeks == [
        worker._hhmmss(270487.521800 + 0.0),
        worker._hhmmss(270487.521800 + 65.0),
    ]
    # Frame files keep the CUE names (offset only affects the seek).
    names = sorted(p.name for p in (sdir / "attachments").iterdir())
    assert names == ["cue_00-00-00.png", "cue_00-01-05.png"]


def test_extract_frames_ffprobe_failure_falls_back_to_zero(tmp_data, monkeypatch):
    from vezir.server import worker

    sid = "01HZOFFSET000000000000B"
    sdir = _session(tmp_data, sid)
    (sdir / f"{sid}.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
    _write_transcript(sdir, sid, [0.0])

    def _run(cmd, stdout=None, stderr=None, **kw):
        if cmd[0] == "ffprobe":
            raise FileNotFoundError("ffprobe not installed")
        return _ok_ffmpeg(cmd)

    monkeypatch.setattr(subprocess, "run", _run)
    assert worker._video_start_time(sdir / f"{sid}.mp4") == 0.0
    written = worker._extract_frames(sdir, sid, _log_path(tmp_data, sid))
    assert written == 1


def test_video_start_time_parses_and_clamps(tmp_data, monkeypatch):
    from vezir.server import worker

    vid = tmp_data / "v.mp4"
    vid.write_bytes(b"x")

    monkeypatch.setattr(subprocess, "run", _ffprobe_aware("12.500000"))
    assert worker._video_start_time(vid) == 12.5

    monkeypatch.setattr(subprocess, "run", _ffprobe_aware("-3.0"))
    assert worker._video_start_time(vid) == 0.0

    monkeypatch.setattr(subprocess, "run", _ffprobe_aware("not-a-number"))
    assert worker._video_start_time(vid) == 0.0


# ── helpers ──────────────────────────────────────────────────────────────────


def test_even_sample_boundaries():
    from vezir.server import worker

    assert worker._even_sample([1.0, 2.0], 5) == [1.0, 2.0]
    assert worker._even_sample([1.0], 5) == [1.0]
    sampled = worker._even_sample([float(i) for i in range(101)], 3)
    assert sampled == [0.0, 50.0, 100.0]


def test_cue_timestamps_parsing(tmp_data):
    from vezir.server import worker

    sid = "01HZCUES00000000000000AA"
    sdir = _session(tmp_data, sid)
    _write_transcript(sdir, sid, [9.0, 1.5])
    assert worker._cue_timestamps(sdir, sid) == [1.5, 9.0]
    assert worker._cue_timestamps(sdir, "01HZNONE000000000000000") == []


# ── _find_artifacts: template summary + meta sidecar ordering ────────────────


def test_find_artifacts_iteration_plan(tmp_data):
    from vezir.server import worker

    sid = "01HZARTS00000000000000AA"
    sdir = _session(tmp_data, sid)
    (sdir / f"{sid}.txt").write_text("t")
    (sdir / f"{sid}.json").write_text("{}")
    (sdir / f"{sid}.iteration-plan.md").write_text("plan")
    (sdir / f"{sid}.iteration-plan.meta.json").write_text("{}")
    (sdir / f"{sid}.summary.md").write_text("sum")

    artifacts = worker._find_artifacts(sdir)
    assert artifacts["iteration_plan"] == f"{sid}.iteration-plan.md"
    assert artifacts["summary"] == f"{sid}.summary.md"
    # The template meta sidecar must never be picked as the transcript json
    # (it sorts before <id>.json alphabetically).
    assert artifacts["json"] == f"{sid}.json"


# ── process_one: video flag drives extraction + frames ───────────────────────


def test_process_one_video_pipeline(tmp_data, monkeypatch):
    from vezir.server import meet_runner, queue, worker

    sid = "01HZPROC000000000000000A"
    sdir = _session(tmp_data, sid)
    (sdir / f"{sid}.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")

    queue.enqueue(sid, github="alice", team_id="blink", video=True,
                  summary_template="iteration-plan")

    calls: list[str] = []

    def _run(cmd, stdout=None, stderr=None):
        calls.append(cmd[1] if cmd[0] == "ffmpeg" else "?")
        return _ok_ffmpeg(cmd)

    monkeypatch.setattr(subprocess, "run", _run)

    transcribe_kwargs: dict = {}

    def fake_transcribe(sd, job_id, team_id, log_path, **kwargs):
        transcribe_kwargs.update(kwargs)
        # millet would produce these; stub them in.  Speaker is already
        # labeled so the session doesn't route to needs_labeling.
        segments = [
            {"start": 0.0, "end": 2.0, "text": "hi", "speaker": "Alice"},
            {"start": 10.0, "end": 12.0, "text": "there", "speaker": "Alice"},
        ]
        (sd / f"{sid}.json").write_text(json.dumps({
            "segments": segments,
            "speakers": [{"id": "Alice", "label": "Alice"}],
        }))
        (sd / f"{sid}.txt").write_text("[00:00:00] Alice: hi")
        (sd / f"{sid}.iteration-plan.md").write_text("plan")
        return 0

    monkeypatch.setattr(meet_runner, "transcribe", fake_transcribe)
    monkeypatch.setattr(meet_runner, "label_auto", lambda *a, **k: 0)
    monkeypatch.setattr(meet_runner, "team_has_sync_target", lambda team_id: False)
    monkeypatch.setattr(meet_runner, "cleanup_home_shim", lambda job_id: None)

    job = queue.get(sid)
    worker.process_one(job)

    row = queue.get(sid)
    assert row["status"] == "done"
    # Audio extraction ran before transcribe, template forwarded.
    assert (sdir / f"{sid}.ogg").exists()
    assert transcribe_kwargs["summary_template"] == "iteration-plan"
    # Frames extracted inline (before the sync gate).
    assert (sdir / "attachments" / "cue_00-00-00.png").exists()
    artifacts = json.loads(row["artifacts"])
    assert artifacts["iteration_plan"] == f"{sid}.iteration-plan.md"


def test_process_one_video_extraction_failure_errors_job(tmp_data, monkeypatch):
    from vezir.server import meet_runner, queue, worker

    sid = "01HZPROC000000000000000B"
    sdir = _session(tmp_data, sid)
    (sdir / f"{sid}.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")

    queue.enqueue(sid, github="alice", team_id="blink", video=True)

    def _run(cmd, stdout=None, stderr=None):
        class _R:
            returncode = 1

        return _R()

    monkeypatch.setattr(subprocess, "run", _run)

    def _boom(*a, **k):
        raise AssertionError("transcribe must not run after extraction failure")

    monkeypatch.setattr(meet_runner, "transcribe", _boom)
    monkeypatch.setattr(meet_runner, "cleanup_home_shim", lambda job_id: None)

    worker.process_one(queue.get(sid))

    row = queue.get(sid)
    assert row["status"] == "error"
    assert "video audio extraction failed" in (row.get("error") or "")
