"""Speaker-label vocabulary shared by the server's routing and the TUI.

vezir never imports millet (it's a subprocess), so the labels millet writes
into a transcript are mirrored here — one definition for every consumer.
"""
from __future__ import annotations

import re

# Raw placeholders from the transcription engine: not yet a person.  A
# speaker whose label still matches this needs a human (or is tiny noise).
UNRESOLVED_RE = re.compile(r"^(YOU|REMOTE(?:_\d+)?|SPEAKER_\d+)$")

# millet >= 0.21.4 labels a ghost REMOTE bucket (sub-second fillers from
# several people that no voiceprint can match) CROSSTALK: heard, but cannot be
# assigned to a speaker.  Resolved (never blocks ``done``), yet not a person.
CROSSTALK = "CROSSTALK"


def is_crosstalk(name: str | None) -> bool:
    """True if ``name`` is the reserved CROSSTALK label (case-insensitive)."""
    return name is not None and name.strip().upper() == CROSSTALK


# millet's ``voiceprint.is_dense_utterance``: a real sentence spoken at a
# speaking rate, not a filler ("Okay.") or a filler Whisper stretched over
# silence.  millet (>= 0.23.1) no longer folds a tiny cluster holding one into
# the dominant speaker, so vezir must not wave it through as noise either.
DENSE_MIN_SECONDS = 1.5
DENSE_MIN_WORDS = 6
DENSE_MIN_WORDS_PER_SECOND = 1.5


def is_dense_utterance(start: float, end: float, text: str | None) -> bool:
    """True if a segment is a real sentence (mirrors millet's definition)."""
    dur = end - start
    words = len((text or "").split())
    return (
        dur >= DENSE_MIN_SECONDS
        and words >= DENSE_MIN_WORDS
        and words / dur >= DENSE_MIN_WORDS_PER_SECOND
    )
