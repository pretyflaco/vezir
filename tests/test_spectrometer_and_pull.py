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


def test_sync_slug_truncates_at_60():
    from vezir.config import sync_slug
    long = "a" * 100
    assert len(sync_slug(long)) == 60


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
