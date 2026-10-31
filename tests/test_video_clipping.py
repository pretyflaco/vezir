"""video-clipping template (v0.28.0, millet >= 0.23.0).

millet writes ``<id>.clips.json`` (cut points on the transcript timeline);
the worker registers it as the ``clips`` artifact, adds the source media and
the video's timeline offset, skips cue frames, and never holds the session
in ``needs_labeling``.  Clients offer the template, name the file, and the
MCP server hands the cut list to an agent.  ffmpeg/ffprobe are mocked.
"""
from __future__ import annotations

import json
import os
import stat
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


def _ffprobe(start_time: str):
    """subprocess.run stand-in: ffprobe answers *start_time*, ffmpeg 'succeeds'."""
    calls: list[list[str]] = []

    def _run(cmd, stdout=None, stderr=None, **kw):
        calls.append(list(cmd))

        class _R:
            returncode = 0
            stdout = start_time + "\n"

        if cmd[0] == "ffmpeg":
            Path(cmd[-1]).write_bytes(b"OUT")
        return _R()

    _run.calls = calls  # type: ignore[attr-defined]
    return _run


def _clips_doc(start_s=12.15, duration_s=30.6):
    return {
        "schema_version": 1, "template": "video-clipping", "timeline": "transcript",
        "source": {"transcript": "x.json", "audio": "x.ogg"},
        "clips": [{
            "id": "short-01", "kind": "short", "title": "Hook", "score": 9,
            "start_s": start_s, "end_s": start_s + duration_s, "duration_s": duration_s,
            "start_ts": "00:00:12.150", "end_ts": "00:00:42.750",
            "ffmpeg_args": ["-ss", f"{start_s:.3f}", "-i", "{input}"],
        }],
        "dropped": [], "error": None,
    }


# ── presets ─────────────────────────────────────────────────────────────────


def test_templates_constant():
    from vezir import presets

    assert presets.TEMPLATES == ("iteration-plan", "video-clipping")
    assert all(presets.TEMPLATE_RE.match(t) for t in presets.TEMPLATES)
    assert presets.is_clipping("video-clipping")
    assert not presets.is_clipping("iteration-plan") and not presets.is_clipping(None)


# ── _find_artifacts ─────────────────────────────────────────────────────────


def test_find_artifacts_registers_clips_and_never_as_transcript(tmp_data):
    from vezir.server import worker

    sid = "01HZCLIPS0000000000000AA"
    sdir = _session(tmp_data, sid)
    (sdir / f"{sid}.json").write_text("{}")
    (sdir / f"{sid}.clips.json").write_text("{}")      # sorts before <id>.json
    (sdir / f"{sid}.clips.de.json").write_text("{}")
    (sdir / f"{sid}.video-clipping.md").write_text("clips")
    (sdir / f"{sid}.video-clipping.meta.json").write_text("{}")

    arts = worker._find_artifacts(sdir)
    assert arts["json"] == f"{sid}.json"
    assert arts["clips"] == f"{sid}.clips.json"
    assert arts["clips_de"] == f"{sid}.clips.de.json"
    assert arts["video_clipping"] == f"{sid}.video-clipping.md"


# ── friendly names ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(("stored", "friendly"), [
    ("01X.video-clipping.md", "20261031_launch_talk.video-clipping.md"),
    ("01X.clips.json", "20261031_launch_talk.clips.json"),
    ("01X.clips.de.json", "20261031_launch_talk.clips.de.json"),
    ("01X.json", "20261031_launch_talk.json"),
])
def test_artifact_friendly_names(stored, friendly):
    from vezir import config

    assert config.artifact_friendly_name(stored, "20261031", "Launch Talk") == friendly


# ── _enrich_clips ───────────────────────────────────────────────────────────


def test_enrich_clips_rebases_onto_video_offset(tmp_data, monkeypatch):
    from vezir.server import worker

    sid = "01HZCLIPS0000000000000AB"
    sdir = _session(tmp_data, sid)
    (sdir / f"{sid}.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
    (sdir / f"{sid}.ogg").write_bytes(b"OggS")
    (sdir / f"{sid}.clips.json").write_text(json.dumps(_clips_doc()))
    monkeypatch.setattr(subprocess, "run", _ffprobe("270487.5"))

    old = os.umask(0o022)
    try:
        assert worker._enrich_clips(sdir, sid) == 1
    finally:
        os.umask(old)
    path = sdir / f"{sid}.clips.json"
    doc = json.loads(path.read_text())
    assert doc["source"]["media"] == f"{sid}.mp4"
    assert doc["source"]["media_kind"] == "video"
    assert doc["source"]["video_start_time"] == 270487.5
    assert doc["source"]["transcript"] == "x.json"  # millet's fields kept
    clip = doc["clips"][0]
    assert clip["seek_s"] == pytest.approx(270487.5 + 12.15)
    args = clip["ffmpeg_args"]
    assert args[args.index("-ss") + 1] == f"{270487.5 + 12.15:.3f}"
    assert args[args.index("-t") + 1] == "30.600"
    assert "libx264" in args and args[-1] == "{output}"
    assert clip["start_s"] == 12.15  # transcript timeline untouched
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_enrich_clips_is_idempotent(tmp_data, monkeypatch):
    from vezir.server import worker

    sid = "01HZCLIPS0000000000000AC"
    sdir = _session(tmp_data, sid)
    (sdir / f"{sid}.mp4").write_bytes(b"\x00")
    (sdir / f"{sid}.clips.json").write_text(json.dumps(_clips_doc()))
    monkeypatch.setattr(subprocess, "run", _ffprobe("2.0"))
    worker._enrich_clips(sdir, sid)
    first = (sdir / f"{sid}.clips.json").read_text()
    worker._enrich_clips(sdir, sid)
    assert (sdir / f"{sid}.clips.json").read_text() == first
    assert json.loads(first)["clips"][0]["seek_s"] == pytest.approx(14.15)


def test_enrich_clips_audio_only_session(tmp_data, monkeypatch):
    from vezir.server import worker

    sid = "01HZCLIPS0000000000000AD"
    sdir = _session(tmp_data, sid)
    (sdir / f"{sid}.ogg").write_bytes(b"OggS")
    (sdir / f"{sid}.clips.json").write_text(json.dumps(_clips_doc()))

    def _no_probe(*a, **k):
        raise AssertionError("no video: ffprobe must not run")

    monkeypatch.setattr(subprocess, "run", _no_probe)
    worker._enrich_clips(sdir, sid)
    doc = json.loads((sdir / f"{sid}.clips.json").read_text())
    assert doc["source"]["media"] == f"{sid}.ogg"
    assert doc["source"]["media_kind"] == "audio"
    args = doc["clips"][0]["ffmpeg_args"]
    assert "-vn" in args and "libx264" not in args
    assert doc["clips"][0]["seek_s"] == 12.15


def test_enrich_clips_survives_garbage(tmp_data, monkeypatch):
    from vezir.server import worker

    sid = "01HZCLIPS0000000000000AE"
    sdir = _session(tmp_data, sid)
    (sdir / f"{sid}.clips.json").write_text("not json")
    (sdir / f"{sid}.clips.de.json").write_text(json.dumps({"clips": "nope"}))
    assert worker._enrich_clips(sdir, sid) == 0
    assert (sdir / f"{sid}.clips.json").read_text() == "not json"
    assert worker._enrich_clips(_session(tmp_data, "01HZEMPTY"), "01HZEMPTY") == 0


# ── process_one ─────────────────────────────────────────────────────────────


def _fake_millet(sid: str, speaker: str):
    def fake_transcribe(sd, job_id, team_id, log_path, **kwargs):
        fake_transcribe.kwargs = kwargs
        (sd / f"{sid}.json").write_text(json.dumps({
            "segments": [{"start": 0.0, "end": 120.0, "text": "talk", "speaker": speaker}],
            "speakers": [{"id": speaker, "label": speaker}],
        }))
        (sd / f"{sid}.txt").write_text(f"[00:00:00] {speaker}: talk")
        (sd / f"{sid}.video-clipping.md").write_text("## Overview\nx")
        (sd / f"{sid}.clips.json").write_text(json.dumps(_clips_doc()))
        return 0

    return fake_transcribe


def _patch_pipeline(monkeypatch, meet_runner, sid, speaker, start_time="0"):
    fake = _fake_millet(sid, speaker)
    monkeypatch.setattr(meet_runner, "transcribe", fake)
    monkeypatch.setattr(meet_runner, "label_auto", lambda *a, **k: 0)
    monkeypatch.setattr(meet_runner, "team_has_sync_target", lambda team_id: False)
    monkeypatch.setattr(meet_runner, "cleanup_home_shim", lambda job_id: None)
    run = _ffprobe(start_time)
    monkeypatch.setattr(subprocess, "run", run)
    return fake, run


def test_process_one_clipping_session(tmp_data, monkeypatch):
    from vezir.server import meet_runner, queue, worker

    sid = "01HZCLIPPROC00000000000A"
    sdir = _session(tmp_data, sid)
    (sdir / f"{sid}.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
    queue.enqueue(sid, github="alice", team_id="blink", video=True,
                  summary_template="video-clipping")
    # Unnamed speaker: a podcast guest with no voiceprint.
    fake, run = _patch_pipeline(monkeypatch, meet_runner, sid, "SPEAKER_00", "3.5")

    worker.process_one(queue.get(sid))

    row = queue.get(sid)
    assert row["status"] == "done"  # not needs_labeling
    assert fake.kwargs["summary_template"] == "video-clipping"
    assert fake.kwargs["defer_summary"] is False
    # No cue frames for a clipping session.
    assert not (sdir / "attachments").exists() or not list(
        (sdir / "attachments").glob("cue_*.png"))
    assert not any(c[0] == "ffmpeg" and "-frames:v" in c for c in run.calls)
    arts = json.loads(row["artifacts"])
    assert arts["clips"] == f"{sid}.clips.json"
    assert arts["json"] == f"{sid}.json"
    doc = json.loads((sdir / f"{sid}.clips.json").read_text())
    assert doc["source"]["video_start_time"] == 3.5
    assert doc["clips"][0]["seek_s"] == pytest.approx(15.65)


def test_unresolved_speakers_still_gate_other_templates(tmp_data, monkeypatch):
    from vezir.server import meet_runner, queue, worker

    sid = "01HZCLIPPROC00000000000B"
    sdir = _session(tmp_data, sid)
    (sdir / f"{sid}.mp4").write_bytes(b"\x00")
    queue.enqueue(sid, github="alice", team_id="blink", video=True)
    _patch_pipeline(monkeypatch, meet_runner, sid, "SPEAKER_00")
    worker.process_one(queue.get(sid))
    assert queue.get(sid)["status"] == "needs_labeling"


def test_reauto_label_does_not_gate_clipping(tmp_data, monkeypatch):
    from vezir.server import meet_runner, queue, worker

    sid = "01HZCLIPPROC00000000000C"
    sdir = _session(tmp_data, sid)
    queue.enqueue(sid, github="alice", team_id="blink", video=True,
                  summary_template="video-clipping")
    _fake_millet(sid, "SPEAKER_00")(sdir, sid, "blink", None)
    monkeypatch.setattr(meet_runner, "label_auto", lambda *a, **k: 0)
    monkeypatch.setattr(meet_runner, "cleanup_home_shim", lambda job_id: None)
    assert worker.reauto_label_session(sid, force=True)["status"] == "done"


def test_retry_summary_enriches_rewritten_clips(tmp_data, monkeypatch):
    from vezir.server import meet_runner, queue, worker

    sid = "01HZCLIPPROC00000000000D"
    sdir = _session(tmp_data, sid)
    (sdir / f"{sid}.mp4").write_bytes(b"\x00")
    queue.enqueue(sid, github="alice", team_id="blink", video=True)

    def fake_apply(sd, job_id, team_id, log_path, **kwargs):
        assert kwargs["summary_template"] == "video-clipping"
        (sd / f"{sid}.video-clipping.md").write_text("## Overview\nx")
        (sd / f"{sid}.clips.json").write_text(json.dumps(_clips_doc()))
        return 0

    monkeypatch.setattr(meet_runner, "apply_labels_json", fake_apply)
    monkeypatch.setattr(meet_runner, "team_has_sync_target", lambda team_id: False)
    monkeypatch.setattr(meet_runner, "cleanup_home_shim", lambda job_id: None)
    monkeypatch.setattr(subprocess, "run", _ffprobe("1.0"))

    worker.retry_summary_for_session(sid, template_override="video-clipping")
    row = queue.get(sid)
    assert row["status"] == "done" and not row.get("summary_error")
    assert json.loads(row["artifacts"])["clips"] == f"{sid}.clips.json"
    doc = json.loads((sdir / f"{sid}.clips.json").read_text())
    assert doc["clips"][0]["seek_s"] == pytest.approx(13.15)


# ── TUI: video template choice ──────────────────────────────────────────────


def test_template_for_video_choices(tmp_path):
    from vezir.client.tui.record_screen import _template_for

    vid, ogg = tmp_path / "talk.mp4", tmp_path / "talk.ogg"
    assert _template_for(vid, "video-clipping") == "video-clipping"
    assert _template_for(vid, "iteration-plan") == "iteration-plan"
    assert _template_for(vid, "") is None
    assert _template_for(ogg, "video-clipping") is None
    assert _template_for(vid, True) == "iteration-plan"  # pre-0.28 boolean


def test_video_template_pref_migration_and_cycle():
    from vezir.client.tui.record_screen import (
        _next_video_template,
        _video_template_label,
        _video_template_pref,
    )

    assert _video_template_pref({}) == "iteration-plan"
    assert _video_template_pref({"iteration_plan": True}) == "iteration-plan"
    assert _video_template_pref({"iteration_plan": False}) == ""
    assert _video_template_pref({"video_template": "video-clipping",
                                 "iteration_plan": False}) == "video-clipping"
    assert _video_template_pref({"video_template": ""}) == ""
    assert _video_template_pref({"video_template": "retired"}) == "iteration-plan"
    assert _next_video_template("iteration-plan") == "video-clipping"
    assert _next_video_template("video-clipping") == ""
    assert _next_video_template("") == "iteration-plan"
    assert _video_template_label("video-clipping") == "Clips"
    assert _video_template_label("") == "No template"


def test_render_clips_view():
    from vezir.client.tui.artifact_screen import _is_clips, render_clips

    doc = _clips_doc()
    doc["clips"][0].update(hook="Every founder undercharges.", reason="Contrarian.",
                           tags=["pricing"])
    doc["dropped"] = [{"title": "weak", "reason": "overlaps short clip"}]
    doc["source"]["media"] = "01X.mp4"
    out = render_clips(json.dumps(doc))
    assert "1 clip(s) from 01X.mp4" in out
    assert "short-01" in out and "9/10" in out and "Every founder" in out
    assert "ffmpeg -y -ss 12.150" in out
    assert "weak: overlaps short clip" in out
    assert render_clips("not json") == "not json"
    assert _is_clips("01X.clips.json") and _is_clips("01X.clips.de.json")
    assert not _is_clips("01X.json")


# ── MCP ─────────────────────────────────────────────────────────────────────


class _Result:
    def __init__(self, ok=None, success=True):
        self.ok, self._s = ok, success

    def is_ok(self):
        return self._s

    def error_message(self):
        return "boom"


class _Session:
    id = "01CLP"
    artifacts = {"clips": "01CLP.clips.json", "clips_de": "01CLP.clips.de.json"}


class _Api:
    def __init__(self):
        self.streamed: list = []

    def get_session(self, sid):
        return _Result(_Session()) if sid == "01CLP" else _Result(None, False)

    def download_artifact(self, sid, name):
        doc = _clips_doc()
        doc["clips"][0]["words"] = [{"word": "hi", "start": 0.0, "end": 0.4}]
        if ".de." in name:
            doc["clips"][0]["title"] = "Haken"
        return _Result(json.dumps(doc).encode())

    def list_attachments(self, sid):
        return _Result([])

    def stream_artifact(self, sid, name, dest):
        from vezir import config

        self.streamed.append(name)
        config.secure_write_bytes(Path(dest), b"VIDEO")
        return _Result(Path(dest))


def test_mcp_get_clips(monkeypatch):
    from vezir.client import mcp_server

    monkeypatch.setattr(mcp_server, "_client", _Api)
    doc = mcp_server.get_clips("01CLP")
    assert doc["session_id"] == "01CLP"
    assert doc["clips"][0]["id"] == "short-01"
    assert "words" not in doc["clips"][0]
    assert mcp_server.get_clips("01CLP", include_words=True)["clips"][0]["words"]
    assert mcp_server.get_clips("01CLP", lang="de")["clips"][0]["title"] == "Haken"
    with pytest.raises(RuntimeError, match="session 01NOPE"):
        mcp_server.get_clips("01NOPE")
    assert mcp_server.get_clips in mcp_server.TOOLS


def test_mcp_get_artifact_streams_video_to_disk(monkeypatch, tmp_path):
    from vezir.client import mcp_server

    api = _Api()
    monkeypatch.setattr(mcp_server, "_client", lambda: api)
    dest = tmp_path / "talk.mp4"
    assert mcp_server.get_artifact("01CLP", "01CLP.mp4", save_path=str(dest)) == str(dest)
    assert api.streamed == ["01CLP.mp4"] and dest.read_bytes() == b"VIDEO"


# ── api.stream_artifact (real HTTP, loopback) ───────────────────────────────


def test_stream_artifact_writes_privately_and_atomically(tmp_path, monkeypatch):
    import httpx

    from vezir.client.api import VezirClient

    body = os.urandom(3 * (1 << 20) + 17)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/missing.mp4"):
            return httpx.Response(404, text="artifact not found")
        return httpx.Response(200, content=body)

    real_client = httpx.Client

    def _client(*a, **k):
        k.pop("verify", None)
        return real_client(*a, transport=httpx.MockTransport(handler), **k)

    monkeypatch.setattr(httpx, "Client", _client)
    api = VezirClient("https://vezir.example", "vzr_" + "x" * 43, team_id="blink")
    target = tmp_path / "victim"
    target.write_bytes(b"keep")
    dest = tmp_path / "out" / "talk.mp4"
    dest.parent.mkdir()
    dest.symlink_to(target)

    res = api.stream_artifact("01CLP", "01CLP.mp4", dest)
    assert res.is_ok() and res.ok == dest
    assert not dest.is_symlink() and dest.read_bytes() == body
    assert target.read_bytes() == b"keep"
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600

    miss = api.stream_artifact("01CLP", "missing.mp4", tmp_path / "out" / "m.mp4")
    assert not miss.is_ok()
    assert not (tmp_path / "out" / "m.mp4").exists()
    assert not [p for p in (tmp_path / "out").iterdir() if p.name.startswith(".")]
