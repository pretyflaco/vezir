"""Tiny-noise speaker gate: don't force needs_labeling on spurious REMOTE.

The dual-diarize path can split a backchannel one-liner or a heavily
distorted blip on the system channel into its own ``REMOTE``/``SPEAKER_n``
cluster that voiceprint never matches.  A single such cluster used to force
an otherwise-clean session into ``needs_labeling``.  ``_has_unresolved_speakers``
now ignores any unresolved raw speaker that is *tiny* (<= 5.0s of speech AND
<= 3 segments by default; tunable via ``VEZIR_TINY_SPEAKER_MAX_SECONDS`` /
``VEZIR_TINY_SPEAKER_MAX_SEGMENTS``), while a substantial unlabeled
participant still routes to needs_labeling.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from vezir.server import worker


def _write_session(tmp_path: Path, sid: str, speakers: list, segments: list) -> Path:
    sd = tmp_path / sid
    sd.mkdir()
    data = {
        "audio_file": f"{sid}.ogg",
        "language": "en",
        "duration": 1000.0,
        "speakers": speakers,
        "segments": segments,
    }
    (sd / f"{sid}.json").write_text(json.dumps(data), encoding="utf-8")
    return sd


def _seg(start, end, speaker, text="x"):
    return {"start": start, "end": end, "text": text, "speaker": speaker}


# ── tiny noise is ignored ──

def test_tiny_remote_with_large_unmatched_speaker_is_resolved(tmp_path):
    """01KV8G41 case: big unmatched SPEAKER_00 + tiny REMOTE blip."""
    sd = _write_session(
        tmp_path,
        "01KV8G41525G2VTZJB2JCBS2DK",
        speakers=[
            {"id": "YOU", "label": "YOU"},
            {"id": "REMOTE", "label": None},
            {"id": "SPEAKER_00", "label": None},
        ],
        segments=(
            [_seg(0.0, 481.0, "SPEAKER_00", "long real turn")]
            + [_seg(312.6, 313.7, "REMOTE", "Thank you."),
               _seg(864.0, 866.1, "REMOTE", "Fine.")]
            + [_seg(0.0, 20.0, "YOU", "host")]
        ),
    )
    # SPEAKER_00 is substantial+unmatched → still needs labeling; but the
    # REMOTE blip alone must not be the reason.  Here SPEAKER_00 keeps it
    # unresolved (correct).
    assert worker._has_unresolved_speakers(sd) is True
    matched, unresolved = worker._speaker_resolution(sd)
    # REMOTE (tiny) is dropped from the unresolved report; SPEAKER_00 + YOU stay.
    assert "REMOTE" not in unresolved
    assert "SPEAKER_00" in unresolved


def test_tiny_remote_with_named_speaker_is_resolved(tmp_path):
    """765s YOU + 0.2s REMOTE (01KRZQYS case) → resolved, routes to done."""
    sd = _write_session(
        tmp_path,
        "01KRZQYS774V1A2403F6NBHD0R",
        speakers=[
            {"id": "REMOTE", "label": None},
            {"id": "alice", "label": "alice"},
        ],
        segments=[
            _seg(0.0, 765.0, "alice", "long real turn"),
            _seg(400.0, 400.2, "REMOTE", "blip"),
        ],
    )
    assert worker._has_unresolved_speakers(sd) is False
    matched, unresolved = worker._speaker_resolution(sd)
    assert unresolved == []
    assert "alice" in matched


def test_all_noise_session_is_resolved(tmp_path):
    """A session that is only a tiny SPEAKER_00 blip → done, not stuck."""
    sd = _write_session(
        tmp_path,
        "01KVQSH0RPZ1BEMFK188KW4SPK",
        speakers=[{"id": "SPEAKER_00", "label": None}],
        segments=[_seg(0.0, 0.2, "SPEAKER_00", "noise")],
    )
    assert worker._has_unresolved_speakers(sd) is False


# ── substantial unknowns still need a human ──

def test_substantial_unmatched_speaker_still_needs_labeling(tmp_path):
    """A real unlabeled participant (37.7s) must still route to needs_labeling."""
    sd = _write_session(
        tmp_path,
        "01KV81WT6B086BQ69YS3H27R7G",
        speakers=[
            {"id": "YOU", "label": "YOU"},
            {"id": "SPEAKER_00", "label": None},
        ],
        segments=[
            _seg(0.0, 20.0, "YOU", "host"),
            _seg(20.0, 40.0, "SPEAKER_00", "real other person"),
            _seg(50.0, 67.7, "SPEAKER_00", "still talking"),
        ],
    )
    assert worker._has_unresolved_speakers(sd) is True
    _, unresolved = worker._speaker_resolution(sd)
    assert "SPEAKER_00" in unresolved


# ── env-var override ──

def test_threshold_override_via_env(tmp_path, monkeypatch):
    """A 10s REMOTE is normally substantial, but a raised threshold ignores it."""
    sd = _write_session(
        tmp_path,
        "01KVOVERRIDE0000000000000A",
        speakers=[
            {"id": "alice", "label": "alice"},
            {"id": "REMOTE", "label": None},
        ],
        segments=[
            _seg(0.0, 300.0, "alice", "long"),
            _seg(310.0, 320.0, "REMOTE", "ten seconds"),
        ],
    )
    # Default: 10s/1seg → over the 5.0s ceiling → substantial → needs labeling.
    assert worker._has_unresolved_speakers(sd) is True
    # Raise the seconds ceiling above 10s → now treated as tiny noise.
    monkeypatch.setenv("VEZIR_TINY_SPEAKER_MAX_SECONDS", "15")
    assert worker._has_unresolved_speakers(sd) is False


# ── boundaries ──

def test_boundary_exactly_at_thresholds_is_tiny(tmp_path):
    """Exactly 5.0s across exactly 3 segments → tiny (<=)."""
    sd = _write_session(
        tmp_path,
        "01KVBOUNDARY00000000000001",
        speakers=[
            {"id": "alice", "label": "alice"},
            {"id": "REMOTE", "label": None},
        ],
        segments=[
            _seg(0.0, 300.0, "alice", "long"),
            _seg(400.0, 402.0, "REMOTE"),
            _seg(410.0, 412.0, "REMOTE"),
            _seg(420.0, 421.0, "REMOTE"),
        ],
    )
    assert worker._has_unresolved_speakers(sd) is False


def test_just_over_segment_count_is_substantial(tmp_path):
    """4 short segments (> 3) → not tiny by the segment-count gate."""
    sd = _write_session(
        tmp_path,
        "01KVBOUNDARY00000000000002",
        speakers=[
            {"id": "alice", "label": "alice"},
            {"id": "REMOTE", "label": None},
        ],
        segments=[
            _seg(0.0, 300.0, "alice", "long"),
            _seg(400.0, 400.2, "REMOTE"),
            _seg(401.0, 401.2, "REMOTE"),
            _seg(402.0, 402.2, "REMOTE"),
            _seg(403.0, 403.2, "REMOTE"),
        ],
    )
    assert worker._has_unresolved_speakers(sd) is True


def test_thresholds_default(monkeypatch):
    monkeypatch.delenv("VEZIR_TINY_SPEAKER_MAX_SECONDS", raising=False)
    monkeypatch.delenv("VEZIR_TINY_SPEAKER_MAX_SEGMENTS", raising=False)
    assert worker._tiny_speaker_thresholds() == (5.0, 3)


def test_thresholds_bad_env_falls_back(monkeypatch):
    monkeypatch.setenv("VEZIR_TINY_SPEAKER_MAX_SECONDS", "not-a-number")
    monkeypatch.setenv("VEZIR_TINY_SPEAKER_MAX_SEGMENTS", "xyz")
    assert worker._tiny_speaker_thresholds() == (5.0, 3)


@pytest.mark.parametrize("missing", ["nofile", "badjson"])
def test_missing_or_bad_transcript_treated_resolved(tmp_path, missing):
    sd = tmp_path / "01KVEMPTY000000000000000XX"
    sd.mkdir()
    if missing == "badjson":
        (sd / f"{sd.name}.json").write_text("{not json", encoding="utf-8")
    assert worker._has_unresolved_speakers(sd) is False
