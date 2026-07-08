"""Meeting-type / folder-slug length safety (v0.11.1 regression).

A long session title used to produce a ``--meeting-type`` string longer
than millet's 64-char folder limit
(``^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$``), so ``millet sync --force
--meeting-type <slug>`` exited 1 and the session got stuck in
``sync_failed``.

The reproducing case (from a real 2026-07-08 session):

    title = "strategy: migration marketing / API / self-custody apps +
             interfaces as growth hacking / team of nyms / public
             appearances / push notifications"

which slugified to a 60-char base and, after ``_meeting_type_for``
appended ``-121734Z-RSB2CA`` (15 chars), produced a 75-char folder name.

``config.sync_slug`` and ``meet_runner._meeting_type_for`` now guarantee
the result validates against millet's rule.
"""
from __future__ import annotations

import re

from vezir import config
from vezir.server import meet_runner

# The authoritative rule lives in millet.sync._FOLDER_SLUG_RE; import it so
# these tests track any future tightening.  Fall back to the current literal
# if millet isn't importable in a stripped CI environment.
try:
    from millet.sync import _FOLDER_SLUG_RE  # type: ignore
except Exception:  # pragma: no cover - CI without millet-pipeline
    _FOLDER_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


_LONG_TITLE = (
    "strategy: migration marketing / API / self-custody apps + interfaces "
    "as growth hacking / team of nyms / public appearances / push notifications"
)

# A valid-length ULID whose HHMMSS + random suffix mirror the reported case.
_SESSION_ID = "01KX0TJZH0RMBM9EMWF0RSB2CA"


def test_sync_slug_caps_at_64_and_is_valid():
    slug = config.sync_slug(_LONG_TITLE)
    assert len(slug) <= 64
    assert _FOLDER_SLUG_RE.match(slug), slug
    # Never ends on a separator after truncation.
    assert not slug.endswith(("-", "_", "."))


def test_sync_slug_short_title_unchanged():
    assert config.sync_slug("Dev Standup") == "dev-standup"


def test_meeting_type_for_long_base_is_valid():
    """The reported regression: 60-char base + 15-char suffix = 75 -> invalid."""
    base = config.sync_slug(_LONG_TITLE)
    mtype = meet_runner._meeting_type_for(_SESSION_ID, base=base)
    assert len(mtype) <= 64, f"{mtype} is {len(mtype)} chars"
    assert _FOLDER_SLUG_RE.match(mtype), mtype
    # The disambiguating suffix must survive the truncation intact.
    assert mtype.endswith("Z-RSB2CA")


def test_meeting_type_for_truncation_strips_trailing_separator():
    """A base that would truncate onto a hyphen must not leave one."""
    base = "a" * 40 + "-" + "b" * 20  # 61 chars; cut lands mid-run
    mtype = meet_runner._meeting_type_for(_SESSION_ID, base=base)
    assert _FOLDER_SLUG_RE.match(mtype), mtype
    # Segment before the suffix does not end on '-'.
    before_suffix = mtype[: mtype.rfind("-", 0, mtype.rfind("Z-"))]
    assert not before_suffix.endswith(("-", "_", "."))


def test_meeting_type_for_empty_base_falls_back():
    """A base that empties out after truncation falls back to 'meeting'."""
    mtype = meet_runner._meeting_type_for(_SESSION_ID, base="")
    assert _FOLDER_SLUG_RE.match(mtype), mtype
    assert mtype.startswith("meeting-")


def test_meeting_type_for_normal_base_preserved():
    mtype = meet_runner._meeting_type_for(_SESSION_ID, base="dev-sync")
    assert mtype.startswith("dev-sync-")
    assert mtype.endswith("Z-RSB2CA")
    assert _FOLDER_SLUG_RE.match(mtype), mtype
