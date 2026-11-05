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
    """01JXCZHW case: big unmatched SPEAKER_00 + tiny REMOTE blip."""
    sd = _write_session(
        tmp_path,
        "01JXCZHWKFFZMR05A9D744DN5M",
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
    """765s YOU + 0.2s REMOTE (01JKF3F9 case) → resolved, routes to done."""
    sd = _write_session(
        tmp_path,
        "01JKF3F98GGQ4RXZ7KQB78ZTF5",
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
        "01JX61G71M3W3839BGGDX1YQZF",
        speakers=[{"id": "SPEAKER_00", "label": None}],
        segments=[_seg(0.0, 0.2, "SPEAKER_00", "noise")],
    )
    assert worker._has_unresolved_speakers(sd) is False


# ── substantial unknowns still need a human ──

def test_substantial_unmatched_speaker_still_needs_labeling(tmp_path):
    """A real unlabeled participant (37.7s) must still route to needs_labeling."""
    sd = _write_session(
        tmp_path,
        "01J5Q2SJ46ZK5XS85E5GNMP574",
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


# ── a real sentence is a person, not noise (millet >= 0.23.1) ──

def test_tiny_cluster_with_a_sentence_needs_labeling(tmp_path):
    """Incident 01JY3ZGY (58 s phone interview): the interviewee's only
    cluster was one 4.4 s, 21-word answer.  Tiny by size, but a person."""
    sd = _write_session(
        tmp_path,
        "01JY3ZGY0SSH9JHX41C83RF95E",
        speakers=[
            {"id": "SPEAKER_00", "label": None},
            {"id": "SPEAKER_01", "label": "Nora"},
        ],
        segments=[
            _seg(1.3, 42.9, "SPEAKER_01", "so now I'm just talking here " * 20),
            _seg(43.4, 47.8, "SPEAKER_00",
                 "I was riding with a 14-month-old, so it wasn't the easiest "
                 "flight, but could have been much worse."),
        ],
    )
    assert worker._has_unresolved_speakers(sd) is True
    _, unresolved = worker._speaker_resolution(sd)
    assert unresolved == ["SPEAKER_00"]


def test_tiny_cluster_of_fillers_is_still_noise(tmp_path):
    """Fillers, including one Whisper stretched over silence, stay ignorable."""
    sd = _write_session(
        tmp_path,
        "01KVFILLERS000000000000001",
        speakers=[
            {"id": "alice", "label": "alice"},
            {"id": "SPEAKER_01", "label": None},
        ],
        segments=[
            _seg(0.0, 300.0, "alice", "long"),
            _seg(310.0, 310.4, "SPEAKER_01", "Yeah."),
            # 6 words over 4.5 s: below 1.5 words/s.
            _seg(320.0, 324.5, "SPEAKER_01", "Okay, okay, sure, yes, right, fine."),
        ],
    )
    assert worker._has_unresolved_speakers(sd) is False


def test_missing_or_non_string_text_is_not_a_sentence(tmp_path):
    sd = _write_session(
        tmp_path,
        "01KVNOTEXT0000000000000001",
        speakers=[
            {"id": "alice", "label": "alice"},
            {"id": "REMOTE", "label": None},
        ],
        segments=[
            _seg(0.0, 300.0, "alice", "long"),
            {"start": 310.0, "end": 313.0, "speaker": "REMOTE"},
            {"start": 314.0, "end": 315.5, "speaker": "REMOTE", "text": 7},
        ],
    )
    assert worker._has_unresolved_speakers(sd) is False


@pytest.mark.parametrize(
    ("start", "end", "text", "dense"),
    [
        (0.0, 4.0, "we shipped the migration fix this morning", True),
        (0.0, 6.7, "Yeah.", False),                          # stretched filler
        (0.0, 1.0, "one two three four five six", False),    # < 1.5 s
        (0.0, 5.0, "one two three four five six", False),    # 1.2 words/s
        (0.0, 0.0, "", False),
        (0.0, 2.0, None, False),
    ],
)
def test_is_dense_utterance_mirrors_millet(start, end, text, dense):
    """Same cases as millet's tests/test_voiceprint_gate.py::test_is_dense_utterance."""
    from vezir.speakers import (
        DENSE_MIN_SECONDS,
        DENSE_MIN_WORDS,
        DENSE_MIN_WORDS_PER_SECOND,
        is_dense_utterance,
    )

    # millet.voiceprint: MIN_SEGMENT_DURATION, LEARN_MIN_WORDS,
    # LEARN_MIN_WORDS_PER_SECOND.
    assert (DENSE_MIN_SECONDS, DENSE_MIN_WORDS, DENSE_MIN_WORDS_PER_SECOND) == (1.5, 6, 1.5)
    assert is_dense_utterance(start, end, text) is dense


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


# ── CROSSTALK (millet >= 0.21.4) ──
# millet labels a ghost REMOTE bucket of unattributable fillers CROSSTALK.
# It is resolved (never blocks done) but it is not a person.

def test_crosstalk_does_not_block_done(tmp_path):
    """01JFRC3N09J6R3SS03CS3PBMNJ: every participant matched + a 10-segment
    filler bucket.  Pre-0.21.4 the raw REMOTE forced needs_labeling."""
    sd = _write_session(
        tmp_path,
        "01JFRC3N09J6R3SS03CS3PBMNJ",
        speakers=[
            {"id": "Nora", "label": "Nora"},
            {"id": "CROSSTALK", "label": "CROSSTALK"},
            {"id": "Pia", "label": "Pia"},
        ],
        segments=(
            [_seg(0.0, 400.0, "Nora", "long"), _seg(400.0, 900.0, "Pia", "long")]
            + [_seg(1000.0 + i * 60, 1000.3 + i * 60, "CROSSTALK", "Bye.")
               for i in range(10)]
        ),
    )
    assert worker._has_unresolved_speakers(sd) is False
    matched, unresolved = worker._speaker_resolution(sd)
    assert unresolved == []
    # Resolved, but not reported as a matched person.
    assert sorted(matched) == ["Nora", "Pia"]


def test_crosstalk_with_real_unknown_still_needs_labeling(tmp_path):
    sd = _write_session(
        tmp_path,
        "01KVCROSSTALKANDUNKNOWN000",
        speakers=[
            {"id": "Nora", "label": "Nora"},
            {"id": "CROSSTALK", "label": "CROSSTALK"},
            {"id": "SPEAKER_03", "label": None},
        ],
        segments=[
            _seg(0.0, 400.0, "Nora", "long"),
            _seg(410.0, 470.0, "SPEAKER_03", "a real guest talking"),
            _seg(500.0, 500.3, "CROSSTALK", "Yeah."),
        ],
    )
    assert worker._has_unresolved_speakers(sd) is True
    _, unresolved = worker._speaker_resolution(sd)
    assert unresolved == ["SPEAKER_03"]
