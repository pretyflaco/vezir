from __future__ import annotations

import pytest

from vezir.server.transcript_import import validate_transcript


def _transcript() -> dict:
    return {
        "speakers": [{"id": "REMOTE", "label": None}],
        "segments": [{
            "start": 0.0,
            "end": 1.0,
            "text": "hello",
            "speaker": "REMOTE",
            "words": [
                {"start": 0.0, "end": 1.0, "word": "hello"},
                {"start": None, "end": None, "word": "untimestamped"},
            ],
        }],
    }


def test_accepts_millet_transcript_shape():
    validate_transcript(_transcript())


@pytest.mark.parametrize("segment", [
    None,
    [],
    {"start": "0", "end": 1, "text": "x"},
    {"start": 0, "end": float("nan"), "text": "x"},
    {"start": 0, "end": 10 ** 1000, "text": "x"},
    {"start": 2, "end": 1, "text": "x"},
    {"start": 0, "end": 1, "text": []},
    {"start": 0, "end": 1, "text": "x", "speaker": {}},
    {"start": 0, "end": 1, "text": "x", "words": {}},
    {"start": 0, "end": 1, "text": "x", "words": [[]]},
    {"start": 0, "end": 1, "text": "x", "unexpected": True},
])
def test_rejects_malformed_segments(segment):
    data = _transcript()
    data["segments"] = [segment]
    with pytest.raises(ValueError):
        validate_transcript(data)


@pytest.mark.parametrize("speakers", [{}, [None], [{"id": 1}], [{"id": "X", "extra": 1}]])
def test_rejects_malformed_speakers(speakers):
    data = _transcript()
    data["speakers"] = speakers
    with pytest.raises(ValueError):
        validate_transcript(data)
