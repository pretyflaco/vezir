"""Validation for user-supplied millet transcript JSON."""
from __future__ import annotations

import math
from typing import cast

_SEGMENT_KEYS = {"start", "end", "text", "speaker", "words"}
_SPEAKER_KEYS = {"id", "label"}


def _is_seconds(value: object) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def validate_transcript(data: object) -> None:
    """Raise ValueError unless *data* has the shapes millet reads."""
    if not isinstance(data, dict):
        raise ValueError("transcript must be a JSON object")
    speakers = data.get("speakers", [])
    if not isinstance(speakers, list):
        raise ValueError("transcript 'speakers' must be a list")
    for i, speaker in enumerate(speakers):
        if not isinstance(speaker, dict) or not isinstance(speaker.get("id"), str):
            raise ValueError(f"invalid speaker at index {i}")
        if set(speaker) - _SPEAKER_KEYS:
            raise ValueError(f"unexpected speaker fields at index {i}")
        if speaker.get("label") is not None and not isinstance(speaker.get("label"), str):
            raise ValueError(f"invalid speaker label at index {i}")

    segments = data.get("segments")
    if not isinstance(segments, list):
        raise ValueError("transcript .json has no 'segments' list")
    if len(segments) > 200_000:
        raise ValueError("transcript has too many segments")
    for i, segment in enumerate(segments):
        if not isinstance(segment, dict):
            raise ValueError(f"invalid segment at index {i}")
        if set(segment) - _SEGMENT_KEYS:
            raise ValueError(f"unexpected segment fields at index {i}")
        start = cast(float, segment.get("start"))
        end = cast(float, segment.get("end"))
        if not _is_seconds(start) or not _is_seconds(end):
            raise ValueError(f"invalid segment timestamps at index {i}")
        if start < 0 or end < start:
            raise ValueError(f"invalid segment range at index {i}")
        if not isinstance(segment.get("text"), str):
            raise ValueError(f"invalid segment text at index {i}")
        if segment.get("speaker") is not None and not isinstance(segment.get("speaker"), str):
            raise ValueError(f"invalid segment speaker at index {i}")
        words = segment.get("words")
        if words is not None:
            if not isinstance(words, list) or any(not isinstance(w, dict) for w in words):
                raise ValueError(f"invalid segment words at index {i}")
            for word in words:
                for key in ("start", "end"):
                    if key in word and not _is_seconds(word[key]):
                        raise ValueError(f"invalid word timestamp at segment {i}")
