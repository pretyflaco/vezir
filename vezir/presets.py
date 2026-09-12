"""Summary preset names — deprecated, kept as compatibility aliases.

Presets used to select between summarization backends with different
privacy and quality characteristics.  Since millet-pipeline 0.19.0 every
backend is private (a hardware-attested TEE, or fully local Ollama), so
there is nothing left to choose between and the axis is retired.

The three historical names are **still accepted** and all resolve to the
same default.  They cannot simply be deleted:

  * ~580 rows in the ``jobs`` table carry them in ``summary_preset``;
  * shipped vezir-android builds send one on every upload;
  * user scripts pass ``--preset``.

Removal is scheduled for 0.22.0 (two minor versions), matching millet's
own 0.21.0 removal of the preset mapping.

This module is the single source of truth, and lives at package level so
the client (TUI/CLI) and the server share it.  Before 0.20.0 the same three
literals were duplicated across ``cli.py`` (3 sites), ``sessions.py``,
``tui/detail_screen.py`` and ``tui/record_screen.py``, which is how the
Android label stayed two model-migrations out of date.
"""
from __future__ import annotations

# The canonical name.  Still called "confidential" because that is what
# existing clients and stored jobs send; it now describes every summary
# rather than selecting one.
DEFAULT_PRESET = "confidential"

# Accepted on the wire.  All resolve to DEFAULT_PRESET server-side; millet
# maps them to the same (backend, model) pair.
VALID_PRESETS: frozenset[str] = frozenset(
    {"high-quality", "confidential", "alternative"}
)

# Names that no longer select anything and will stop being accepted in
# 0.22.0.  Every currently-valid name is deprecated -- the whole axis is.
DEPRECATED_PRESETS: frozenset[str] = VALID_PRESETS


def is_valid(preset: str | None) -> bool:
    """True when ``preset`` is None (unset) or a currently accepted name."""
    return preset is None or preset in VALID_PRESETS


def normalize(preset: str | None) -> str | None:
    """Return the preset unchanged, or None when unset.

    Deliberately does NOT rewrite legacy names to ``DEFAULT_PRESET``: the
    value is stored on the job and shown in the UI, and silently rewriting
    it would make a user's history disagree with what they chose.  millet
    resolves all three to the same backend anyway, so the stored string is
    a historical record, not a routing instruction.
    """
    return preset or None
