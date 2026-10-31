"""Captions from the cut list's word timings.

``clips.json`` (``get_clips(include_words=True)``) carries every word with
start/end relative to the clip's first frame.  Words are grouped into cues
of at most two lines, broken at sentence ends, pauses and a maximum on-screen
time, then run through the brand + campaign glossary.

Burned-in text can't be fixed after export, so each clip gets an editable
``<id>.captions.json``.  A render prefers it when present: fix a word in
``text`` and the next render uses it.  When the edited text has the same
number of words as before, every word keeps its own timing (so the word
highlight still lands); otherwise the cue's time is shared out by length.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .kit import GlossaryRule

CAPTIONS_VERSION = 1
PAUSE_S = 0.5
MAX_CUE_S = 3.0
MIN_CUE_S = 0.7
HOLD_S = 0.25
_SENTENCE_END = re.compile("[.!?\u2026]['\"\u201d\u2019)]*$")
_SPEAKER_PREFIX = re.compile(r"^[^:\n]{1,40}:\s+")


@dataclass
class Word:
    text: str
    start: float
    end: float


@dataclass
class Cue:
    start: float
    end: float
    words: list[Word] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)


def words_from_clip(clip: dict) -> list[Word]:
    """Word timings for a clip; evenly timed text when the clip has none."""
    out: list[Word] = []
    for w in clip.get("words") or []:
        if not isinstance(w, dict):
            continue
        text, start, end = w.get("word"), w.get("start"), w.get("end")
        if (
            isinstance(text, str) and text.strip()
            and isinstance(start, (int, float)) and isinstance(end, (int, float))
            and end >= start
        ):
            out.append(Word(text.strip(), float(start), float(end)))
    if out:
        return sorted(out, key=lambda w: w.start)
    lines = [_SPEAKER_PREFIX.sub("", ln) for ln in str(clip.get("text") or "").splitlines()]
    tokens = " ".join(lines).split()
    duration = float(clip.get("duration_s") or 0.0)
    if not tokens or duration <= 0:
        return []
    return _spread(tokens, 0.0, duration)


def _spread(tokens: Sequence[str], start: float, end: float) -> list[Word]:
    """Share ``[start, end]`` among tokens in proportion to their length."""
    total = sum(len(t) + 1 for t in tokens)
    span = max(end - start, 0.0)
    out, t = [], start
    for tok in tokens:
        d = span * (len(tok) + 1) / total
        out.append(Word(tok, round(t, 3), round(t + d, 3)))
        t += d
    return out


def retime(cue_start: float, cue_end: float, old: Sequence[Word], text: str) -> list[Word]:
    """Words for ``text``: keep per-word timing when the word count matches."""
    tokens = text.split()
    if len(tokens) == len(old):
        return [Word(tok, w.start, w.end) for tok, w in zip(tokens, old, strict=True)]
    return _spread(tokens, cue_start, cue_end)


def apply_glossary(text: str, rules: Iterable[GlossaryRule]) -> str:
    for rule in rules:
        text = rule.apply(text)
    return re.sub(r"\s+", " ", text).strip()


def wrap(tokens: Sequence[str], max_chars: int) -> list[str]:
    """One line if it fits, else the most balanced two-line split."""
    one = " ".join(tokens)
    if len(one) <= max_chars or len(tokens) < 2:
        return [one]
    best = None
    for i in range(1, len(tokens)):
        a, b = " ".join(tokens[:i]), " ".join(tokens[i:])
        score = (max(len(a), len(b)), abs(len(a) - len(b)))
        if best is None or score < best[0]:
            best = (score, [a, b])
    assert best is not None
    return best[1]


def build_cues(
    words: Sequence[Word],
    max_chars: int,
    glossary: Iterable[GlossaryRule] = (),
    *,
    clip_end: float | None = None,
) -> list[Cue]:
    """Group words into cues of at most two lines of ``max_chars``."""
    rules = tuple(glossary)
    groups: list[list[Word]] = []
    cur: list[Word] = []
    for w in words:
        if cur:
            chars = len(" ".join(x.text for x in cur)) + 1 + len(w.text)
            if (
                chars > 2 * max_chars
                or w.start - cur[-1].end > PAUSE_S
                or w.end - cur[0].start > MAX_CUE_S
                or _SENTENCE_END.search(cur[-1].text)
            ):
                groups.append(cur)
                cur = []
        cur.append(w)
    if cur:
        groups.append(cur)

    cues: list[Cue] = []
    for g in groups:
        start, end = g[0].start, g[-1].end
        text = apply_glossary(" ".join(w.text for w in g), rules)
        cue_words = retime(start, end, g, text)
        cues.append(Cue(start, end, cue_words, wrap([w.text for w in cue_words], max_chars)))
    _settle(cues, clip_end)
    return cues


def _settle(cues: list[Cue], clip_end: float | None) -> None:
    """Minimum on-screen time and a short hold, never overlapping the next cue."""
    for i, cue in enumerate(cues):
        limit = cues[i + 1].start if i + 1 < len(cues) else clip_end
        target = max(cue.end + HOLD_S, cue.start + MIN_CUE_S)
        cue.end = round(min(target, limit) if limit is not None else target, 3)
        cue.end = max(cue.end, cue.start)


# ── sidecar formats ─────────────────────────────────────────────────────────


def _ts(seconds: float, sep: str) -> str:
    ms = max(0, round(seconds * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def to_srt(cues: Sequence[Cue], offset: float = 0.0) -> str:
    blocks = [
        f"{i}\n{_ts(c.start + offset, ',')} --> {_ts(c.end + offset, ',')}\n" + "\n".join(c.lines)
        for i, c in enumerate(cues, 1)
    ]
    return "\n\n".join(blocks) + "\n" if blocks else ""


def to_vtt(cues: Sequence[Cue], offset: float = 0.0) -> str:
    blocks = [
        f"{_ts(c.start + offset, '.')} --> {_ts(c.end + offset, '.')}\n" + "\n".join(c.lines)
        for c in cues
    ]
    return "WEBVTT\n\n" + "\n\n".join(blocks) + ("\n" if blocks else "")


# ── editable captions file ──────────────────────────────────────────────────


def dumps(cues: Sequence[Cue], clip_id: str) -> str:
    doc = {
        "version": CAPTIONS_VERSION,
        "clip": clip_id,
        "note": "Edit 'text' to fix a caption; keep the word count to keep the "
                "word highlight in sync.  Delete this file to rebuild it.",
        "cues": [
            {
                "start": c.start, "end": c.end, "text": c.text,
                "words": [{"text": w.text, "start": w.start, "end": w.end} for w in c.words],
            }
            for c in cues
        ],
    }
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def load(path: Path, max_chars: int) -> list[Cue]:
    """Read an (optionally hand-edited) captions file."""
    doc = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or doc.get("version") != CAPTIONS_VERSION:
        raise ValueError(f"{path}: not a version-{CAPTIONS_VERSION} captions file")
    cues = []
    for i, raw in enumerate(doc.get("cues") or []):
        try:
            start, end = float(raw["start"]), float(raw["end"])
            text = str(raw["text"])
            old = [Word(str(w["text"]), float(w["start"]), float(w["end"]))
                   for w in raw.get("words") or []]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{path}: cue {i} is malformed ({exc})") from None
        if end < start:
            raise ValueError(f"{path}: cue {i} ends before it starts")
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue  # an emptied cue is a deliberate deletion
        words = retime(start, end, old, text)
        cues.append(Cue(start, end, words, wrap([w.text for w in words], max_chars)))
    return cues
