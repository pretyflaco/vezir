"""Tests for v0.6.6 utilities: audio levels, sync slug, local session lookup."""
from __future__ import annotations

import json
import struct
from pathlib import Path

# ── config.sync_slug ─────────────────────────────────────────────────────────


def test_sync_slug_basic():
    from vezir.config import sync_slug
    assert sync_slug("Dev Standup") == "dev-standup"
    assert sync_slug("Board Meeting Q2") == "board-meeting-q2"
    assert sync_slug("UX Weekly") == "ux-weekly"


def test_sync_slug_special_chars():
    from vezir.config import sync_slug
    assert sync_slug("weekly sync / @team") == "weekly-sync-team"
    assert sync_slug("  lots   of   spaces  ") == "lots-of-spaces"


def test_sync_slug_empty():
    from vezir.config import sync_slug
    assert sync_slug("") == ""
    assert sync_slug("   ") == ""


def test_sync_slug_truncates_at_64():
    # v0.11.1: cap raised 60 -> 64 to match millet's folder-slug limit
    # (^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$).
    from vezir.config import sync_slug
    long = "a" * 100
    assert len(sync_slug(long)) == 64


# ── config.sanitize_title ────────────────────────────────────────────────────


def test_sanitize_title_basic():
    from vezir.config import sanitize_title
    assert sanitize_title("AB Board") == "AB_BOARD"
    assert sanitize_title("weekly sync / @blink") == "WEEKLY_SYNC_BLINK"


def test_sanitize_title_empty():
    from vezir.config import sanitize_title
    assert sanitize_title("") == ""
    assert sanitize_title("   ") == ""


# ── audio._rms_to_bar_index ─────────────────────────────────────────────────


def test_rms_to_bar_silence():
    from vezir.client.audio import _rms_to_bar_index
    assert _rms_to_bar_index(0.0) == 0
    assert _rms_to_bar_index(1e-8) == 0


def test_rms_to_bar_full_scale():
    from vezir.client.audio import _rms_to_bar_index
    assert _rms_to_bar_index(1.0) == 8


def test_rms_to_bar_mid_range():
    from vezir.client.audio import _rms_to_bar_index
    # -30 dB (RMS ~0.032) should be around bar 4
    idx = _rms_to_bar_index(0.032)
    assert 3 <= idx <= 5


def test_rms_to_bar_monotonic():
    from vezir.client.audio import _rms_to_bar_index
    values = [0.001, 0.003, 0.01, 0.03, 0.1, 0.3, 1.0]
    indices = [_rms_to_bar_index(v) for v in values]
    # Each index should be >= the previous (monotonically increasing).
    for i in range(1, len(indices)):
        assert indices[i] >= indices[i - 1], f"{values[i]}: {indices[i]} < {indices[i-1]}"


# ── audio.render_level_bars ──────────────────────────────────────────────────


def test_render_level_bars_silence():
    from vezir.client.audio import render_level_bars
    result = render_level_bars([0.0] * 8)
    assert len(result) == 8
    # All should be spaces (bar index 0).
    assert result.strip() == ""


def test_render_level_bars_length():
    from vezir.client.audio import render_level_bars
    assert len(render_level_bars([0.1] * 12)) == 12
    assert len(render_level_bars([0.05] * 4)) == 4


# ── audio.read_chunk_levels ──────────────────────────────────────────────────


def _make_wav(path: Path, samples_per_channel: int, mic_val: int, sys_val: int):
    """Write a minimal stereo WAV with constant sample values."""
    n_channels = 2
    sample_rate = 16000
    bits_per_sample = 16
    n_samples = samples_per_channel * n_channels
    data_size = n_samples * 2  # 2 bytes per int16
    # WAV header (44 bytes).
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + data_size, b"WAVE",
        b"fmt ", 16, 1, n_channels, sample_rate,
        sample_rate * n_channels * 2, n_channels * 2, bits_per_sample,
        b"data", data_size,
    )
    # Interleaved stereo samples: [mic, sys, mic, sys, ...]
    pcm = b""
    for _ in range(samples_per_channel):
        pcm += struct.pack("<hh", mic_val, sys_val)
    path.write_bytes(header + pcm)


def test_read_chunk_levels_nonexistent():
    from vezir.client.audio import read_chunk_levels
    lvl = read_chunk_levels("/nonexistent/path.wav")
    assert lvl.mic_rms == 0.0
    assert lvl.sys_rms == 0.0


def test_read_chunk_levels_tiny_file(tmp_path):
    from vezir.client.audio import read_chunk_levels
    tiny = tmp_path / "tiny.wav"
    tiny.write_bytes(b"RIFF" + b"\x00" * 10)
    lvl = read_chunk_levels(tiny)
    assert lvl.mic_rms == 0.0


def test_read_chunk_levels_known_signal(tmp_path):
    from vezir.client.audio import read_chunk_levels
    wav = tmp_path / "test.wav"
    # 1600 samples per channel = 100ms at 16kHz.
    mic_val = 3277  # ~0.1 of full scale (32768)
    sys_val = 6554  # ~0.2 of full scale
    _make_wav(wav, 1600, mic_val, sys_val)
    lvl = read_chunk_levels(wav)
    # RMS of a constant signal = abs(value) / 32768.
    expected_mic = mic_val / 32768.0
    expected_sys = sys_val / 32768.0
    assert abs(lvl.mic_rms - expected_mic) < 0.001
    assert abs(lvl.sys_rms - expected_sys) < 0.001
    assert abs(lvl.mic_peak - expected_mic) < 0.001
    assert abs(lvl.sys_peak - expected_sys) < 0.001


# ── pull.find_local_session_dir ──────────────────────────────────────────────


def test_find_local_via_manifest(tmp_path, monkeypatch):
    from vezir.client.pull import find_local_session_dir

    # Set up a fake recordings dir.
    team_dir = tmp_path / "myteam"
    session_dir = team_dir / "meeting-20260526-120000_TEST"
    session_dir.mkdir(parents=True)
    # Write manifest.
    manifest = {"01KSTEST": "meeting-20260526-120000_TEST"}
    (team_dir / ".pull-manifest.json").write_text(json.dumps(manifest))
    # Patch recordings_dir to return our tmp dir.
    from vezir import config
    monkeypatch.setattr(config, "recordings_dir", lambda team_id=None: team_dir)

    result = find_local_session_dir("01KSTEST", "myteam")
    assert result == session_dir


def test_find_local_via_session_json(tmp_path, monkeypatch):
    from vezir.client.pull import find_local_session_dir

    team_dir = tmp_path / "myteam"
    session_dir = team_dir / "meeting-20260526-130000_OTHER"
    session_dir.mkdir(parents=True)
    meta = {"session_id": "01KSOTHER"}
    (session_dir / "session.json").write_text(json.dumps(meta))
    from vezir import config
    monkeypatch.setattr(config, "recordings_dir", lambda team_id=None: team_dir)

    result = find_local_session_dir("01KSOTHER", "myteam")
    assert result == session_dir


def test_find_local_not_found(tmp_path, monkeypatch):
    from vezir.client.pull import find_local_session_dir

    team_dir = tmp_path / "myteam"
    team_dir.mkdir(parents=True)
    from vezir import config
    monkeypatch.setattr(config, "recordings_dir", lambda team_id=None: team_dir)

    result = find_local_session_dir("NONEXISTENT", "myteam")
    assert result is None


def test_find_local_global_fallback_uuid_finds_slug_dir(tmp_path, monkeypatch):
    """Regression: recordings live under the team SLUG, but the TUI passes
    the team UUID.  ``recordings_dir(uuid)`` points at a nonexistent dir;
    the global fallback must still find the session under its slug dir.
    """
    from vezir import config
    from vezir.client.pull import find_local_session_dir

    root = tmp_path / "vezir-meetings"
    # Recording written under the slug "blink".
    slug_dir = root / "blink"
    session_dir = slug_dir / "meeting-20260602-134206_DESTINY"
    session_dir.mkdir(parents=True)
    (session_dir / "session.json").write_text(
        json.dumps({"session_id": "01KT3YT9", "team_id": "5e0d-uuid"})
    )
    # A decoy second team dir without the session.
    (root / "twentyone").mkdir(parents=True)

    # recordings_dir honors team_id realistically: <root>/<team_id>.
    monkeypatch.setattr(
        config, "recordings_dir", lambda team_id=None: root / (team_id or "default")
    )

    # The UUID dir doesn't exist on disk; only the slug dir does.
    assert not (root / "5e0d-uuid").exists()
    result = find_local_session_dir("01KT3YT9", "5e0d-uuid")
    assert result == session_dir


def test_find_local_team_specific_takes_precedence(tmp_path, monkeypatch):
    """When the team-specific dir resolves, it wins over the global scan."""
    from vezir import config
    from vezir.client.pull import find_local_session_dir

    root = tmp_path / "vezir-meetings"
    blink = root / "blink"
    right = blink / "meeting-A_RIGHT"
    right.mkdir(parents=True)
    (right / "session.json").write_text(json.dumps({"session_id": "01DUP"}))
    # A same-session_id dir under a different team (should NOT be returned).
    other = root / "twentyone" / "meeting-B_WRONG"
    other.mkdir(parents=True)
    (other / "session.json").write_text(json.dumps({"session_id": "01DUP"}))

    monkeypatch.setattr(
        config, "recordings_dir", lambda team_id=None: root / (team_id or "default")
    )
    result = find_local_session_dir("01DUP", "blink")
    assert result == right


# ── pull.record_uploaded_session (upload-time bridge) ────────────────────────


def test_record_uploaded_session_writes_stub_not_manifest(tmp_path, monkeypatch):
    from vezir.client.pull import find_local_session_dir, record_uploaded_session

    team_dir = tmp_path / "blink"
    rec = team_dir / "meeting-20260604-153156_STABLESATS_BRAINSTORM"
    rec.mkdir(parents=True)
    # Simulate a fresh recording dir: audio only, no session.json.
    (rec / "meeting-20260604-153156.ogg").write_bytes(b"\x00")

    record_uploaded_session(rec, "01UPLOAD", title="Stablesats Brainstorm",
                            team_id="blink")

    meta = json.loads((rec / "session.json").read_text())
    assert meta["session_id"] == "01UPLOAD"
    assert meta["created_by"] == "vezir-upload"
    # The manifest must NOT be written at upload time — it means "artifacts
    # downloaded", which isn't true yet (only the stub exists).
    assert not (team_dir / ".pull-manifest.json").exists()

    # The dir is still discoverable via session.json -> no duplicate pull.
    from vezir import config
    monkeypatch.setattr(config, "recordings_dir", lambda team_id=None: team_dir)
    assert find_local_session_dir("01UPLOAD", "blink") == rec


def test_record_uploaded_session_idempotent_and_nonclobbering(tmp_path):
    from vezir.client.pull import record_uploaded_session

    rec = tmp_path / "blink" / "meeting-20260604-153156_X"
    rec.mkdir(parents=True)
    # A pre-existing (richer) session.json must NOT be clobbered.
    (rec / "session.json").write_text(json.dumps({
        "session_id": "01UPLOAD", "title": "X", "created_by": "vezir",
        "status": "done",
    }))
    record_uploaded_session(rec, "01UPLOAD", title="X", team_id="blink")
    meta = json.loads((rec / "session.json").read_text())
    # Existing file preserved (still has the richer 'status' field).
    assert meta["status"] == "done"
    assert meta["created_by"] == "vezir"


def test_record_uploaded_session_noops_on_missing_dir(tmp_path):
    from vezir.client.pull import record_uploaded_session
    # Should not raise when the dir doesn't exist or id is empty.
    record_uploaded_session(tmp_path / "nope", "01X")
    record_uploaded_session(tmp_path, "")


# ── artifact-completeness helpers (0.7.19) ───────────────────────────────────


def test_dir_has_artifacts(tmp_path):
    from vezir.client.pull import _dir_has_artifacts

    # Stub-only recording dir (audio + session.json, no artifacts).
    rec = tmp_path / "rec"
    rec.mkdir()
    (rec / "meeting-x.ogg").write_bytes(b"\x00")
    (rec / "session.json").write_text("{}")
    assert _dir_has_artifacts(rec) is False

    (rec / "summary.md").write_text("# summary")
    assert _dir_has_artifacts(rec) is True

    # transcript.* alone also counts.
    rec2 = tmp_path / "rec2"
    rec2.mkdir()
    (rec2 / "transcript.txt").write_text("hi")
    assert _dir_has_artifacts(rec2) is True


def test_missing_server_artifacts(tmp_path):
    from vezir.client.pull import missing_server_artifacts

    class _S:
        artifacts = {
            "summary": "01X.summary.md",
            "txt": "01X.txt",
            "pdf": "01X.pdf",
        }

    rec = tmp_path / "rec"
    rec.mkdir()
    (rec / "summary.md").write_text("x")  # only summary present locally
    missing = missing_server_artifacts(_S(), rec)
    assert "summary.md" not in missing
    assert "transcript.txt" in missing
    assert "transcript.pdf" in missing


def test_pull_repulls_when_manifest_folder_lacks_artifacts(tmp_path, monkeypatch):
    """A manifest entry pointing at an artifact-less folder must NOT block the
    download — the regression that left upload-bridged folders empty."""
    from vezir import config
    from vezir.client import pull as pull_mod

    team_dir = tmp_path / "blink"
    rec = team_dir / "meeting-20260605-123757_STARTUPS_ORG"
    rec.mkdir(parents=True)
    (rec / "meeting-x.ogg").write_bytes(b"\x00")
    (rec / "session.json").write_text(json.dumps({"session_id": "01INCOMPLETE"}))
    # Stale manifest entry says "pulled" though no artifacts are on disk.
    (team_dir / ".pull-manifest.json").write_text(
        json.dumps({"01INCOMPLETE": rec.name})
    )

    class _S:
        id = "01INCOMPLETE"
        status = "done"
        title = "Startups Org"
        github = "alice"
        created_at = "2026-06-05T10:22:18Z"
        team_id = "blink"
        artifacts = {"summary": "01INCOMPLETE.summary.md"}

    class _Result:
        def is_ok(self):
            return True
        ok = _S()

    class _Api:
        def get_session(self, sid):
            return _Result()

    monkeypatch.setattr(config, "recordings_dir", lambda team_id=None: team_dir)

    downloaded = {}

    def fake_download(api, session, dest_dir, *, overwrite=False):
        (dest_dir / "summary.md").write_text("# summary")
        downloaded["dest"] = dest_dir
        return [dest_dir / "summary.md"]

    monkeypatch.setattr(pull_mod, "download_session_artifacts", fake_download)

    n = pull_mod.pull_team_sessions(_Api(), session_id="01INCOMPLETE")
    assert n == 1
    # Downloaded INTO the existing recording folder (no duplicate).
    assert downloaded["dest"] == rec
    assert (rec / "summary.md").exists()


# ── artifacts.download_session_artifacts upgrades upload stub ─────────────────


def test_download_artifacts_upgrades_upload_stub(tmp_path, monkeypatch):
    """An upload-time stub session.json is upgraded to the full record on
    auto-download; a non-stub user file is left alone."""
    from vezir.client.artifacts import download_session_artifacts

    class _FakeResult:
        def is_ok(self):
            return True

    class _FakeApi:
        def save_artifact(self, sid, name, dest):
            Path(dest).write_text("x")
            return _FakeResult()

    class _S:
        id = "01UPLOAD"
        title = "Stablesats"
        status = "done"
        github = "alice"
        created_at = "2026-06-04T15:31:00Z"
        team_id = "blink"
        artifacts = {"summary": "x.summary.md"}

    dest = tmp_path / "rec"
    dest.mkdir()
    # Pre-existing upload stub.
    (dest / "session.json").write_text(json.dumps({
        "session_id": "01UPLOAD", "created_by": "vezir-upload",
    }))
    download_session_artifacts(_FakeApi(), _S(), dest)
    meta = json.loads((dest / "session.json").read_text())
    # Upgraded: the stub is replaced by the full record (server-side fields
    # present, and "created_by: vezir-upload" is gone — now "pulled_by").
    assert meta["status"] == "done"
    assert meta["github"] == "alice"
    assert meta.get("pulled_by") == "vezir"
    assert "created_by" not in meta


# ── meet_runner._sync_log_shows_push ─────────────────────────────────────────


def test_sync_log_push_detected(tmp_path):
    from vezir.server.meet_runner import _sync_log_shows_push
    log = tmp_path / "test.log"
    log.write_text(
        "--- millet sync /some/path\n"
        "Syncing: 01KSTEST\n"
        "  Staged: meetings/2026-05-26_dev-standup-daily/summary.md\n"
        "  Pushed 3 file(s).\n"
        "  Done: 3 file(s) pushed as dev-standup-daily/\n"
    )
    assert _sync_log_shows_push(log) is True


def test_sync_log_skipped(tmp_path):
    from vezir.server.meet_runner import _sync_log_shows_push
    log = tmp_path / "test.log"
    log.write_text(
        "--- millet sync /some/path\n"
        "Syncing: 01KSTEST\n"
        "  Skipped: not a scheduled meeting\n"
    )
    assert _sync_log_shows_push(log) is False


def test_sync_log_empty(tmp_path):
    from vezir.server.meet_runner import _sync_log_shows_push
    log = tmp_path / "test.log"
    log.write_text("")
    assert _sync_log_shows_push(log) is False


def test_sync_log_nonexistent():
    from vezir.server.meet_runner import _sync_log_shows_push
    assert _sync_log_shows_push(Path("/nonexistent")) is False
