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
