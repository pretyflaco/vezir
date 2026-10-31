"""ASS (libass) overlay scripts: captions, lower third and card text.

One script per rendered segment, sized to the profile's canvas so every
coordinate is a canvas pixel.  Text never carries ASS override codes from
the input: braces and backslashes are neutralised, so a caption reading
``{\\fs200}`` stays literal.
"""
from __future__ import annotations

from collections.abc import Sequence

from .captions import Cue
from .kit import Brand
from .profiles import Profile

LOWER_NAME = 1.0   # lower-third sizes relative to the caption size
LOWER_TALK = 0.8


def color(hex_rgb: str, alpha: int = 0) -> str:
    """``#rrggbb`` -> ASS ``&HAABBGGRR``."""
    r, g, b = hex_rgb[1:3], hex_rgb[3:5], hex_rgb[5:7]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def escape(text: str) -> str:
    return (
        text.replace("\\", "\u29f5").replace("{", "(").replace("}", ")")
        .replace("\n", " ").strip()
    )


def _t(seconds: float) -> str:
    cs = max(0, round(seconds * 100))
    h, rem = divmod(cs, 360_000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _style(
    name: str, brand: Brand, size: int, fg: str, *, align: int,
    margins: tuple[int, int, int] = (0, 0, 0), outline: float = 0.0, bold: bool = True,
    box: bool = False,
) -> str:
    """One style line.  ``box``: an opaque-box border (BorderStyle 3) in the
    brand background at ~70 % opacity, ``outline`` then being its padding."""
    ml, mr, mv = margins
    border, edge = (3, color(brand.colors["background"], 0x4C)) if box else (
        1, color(brand.colors["outline"]))
    return (
        f"Style: {name},{brand.font_family},{size},{color(fg)},{color(fg)},"
        f"{edge},{color('#000000', 0x80)},"
        f"{-1 if bold else 0},0,0,0,100,100,0,0,{border},{outline:g},0,{align},{ml},{mr},{mv},1"
    )


def _header(profile: Profile, styles: Sequence[str]) -> str:
    return "\n".join([
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {profile.width}",
        f"PlayResY: {profile.height}",
        # Smart wrapping inside the style margins: a safety net with the real
        # font metrics (our own line breaks come first, as explicit \\N).
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        *styles,
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ])


def _event(start: float, end: float, style: str, text: str, layer: int = 0) -> str:
    return f"Dialogue: {layer},{_t(start)},{_t(end)},{style},,0,0,0,,{text}"


def _caption_lines(cue: Cue, highlight: int | None, hl_color: str) -> str:
    """Cue text with ``\\N`` line breaks; word *highlight* recolored."""
    out, i = [], 0
    for line in cue.lines:
        parts = []
        for _ in line.split(" "):
            word = escape(cue.words[i].text) if i < len(cue.words) else ""
            parts.append(f"{{\\c{hl_color}}}{word}{{\\r}}" if i == highlight else word)
            i += 1
        out.append(" ".join(parts))
    return "\\N".join(out)


def main_script(
    profile: Profile, brand: Brand, cues: Sequence[Cue], *,
    speaker: str | None = None, talk: str | None = None,
) -> str:
    """Overlay for the clip itself: captions (+ lower third on landscape)."""
    cap = profile.captions
    styles = [
        _style("Caption", brand, profile.caption_size, brand.colors["text"], align=2,
               margins=(cap.x, profile.width - cap.right, profile.height - cap.bottom),
               outline=(profile.caption_size / 5) if profile.caption_box
               else max(2.0, profile.caption_size / 14),
               box=profile.caption_box),
    ]
    events: list[str] = []
    hl = color(brand.colors["primary"])
    for cue in cues:
        if profile.caption_highlight and cue.words:
            # One event per word: the whole cue, with the current word recolored.
            for i, w in enumerate(cue.words):
                start = cue.start if i == 0 else w.start
                end = cue.words[i + 1].start if i + 1 < len(cue.words) else cue.end
                if end > start:
                    events.append(_event(start, end, "Caption", _caption_lines(cue, i, hl)))
        else:
            events.append(_event(cue.start, cue.end, "Caption", _caption_lines(cue, None, hl)))

    lt = profile.lower_third
    if lt is not None and (speaker or talk):
        name_size = round(profile.caption_size * LOWER_NAME)
        styles += [
            _style("LowerName", brand, name_size, brand.colors["text"], align=7, outline=2),
            _style("LowerTalk", brand, round(profile.caption_size * LOWER_TALK),
                   brand.colors["primary"], align=7, outline=2, bold=False),
        ]
        t0, t1 = profile.lower_third_s
        fade = "{\\fad(250,300)}"
        if speaker:
            events.append(_event(t0, t1, "LowerName",
                                 f"{fade}{{\\pos({lt.x},{lt.y})}}{escape(speaker)}", layer=1))
        if talk:
            y = lt.y + round(name_size * 1.2) if speaker else lt.y
            events.append(_event(t0, t1, "LowerTalk",
                                 f"{fade}{{\\pos({lt.x},{y})}}{escape(talk)}", layer=1))
    return _header(profile, styles) + "\n" + "\n".join(events) + "\n"


def card_script(
    profile: Profile, brand: Brand, duration: float, headline: str,
    lines: Sequence[str], *, text_top: int,
) -> str:
    """Card text as one block: headline (primary color), then lines.

    A single top-centered event lets libass wrap every line with the real
    font metrics inside the profile's safe area.
    """
    size = profile.caption_size
    margins = (profile.safe.x, profile.width - profile.safe.right, text_top)
    styles = [
        _style("CardHead", brand, round(size * 1.1), brand.colors["primary"], align=8,
               margins=margins),
        _style("CardLine", brand, round(size * 0.75), brand.colors["text"], align=8,
               margins=margins, bold=False),
    ]
    parts = []
    if headline.strip():
        parts.append(escape(headline))
    body = [escape(x) for x in lines if x.strip()]
    if body:
        gap = f"{{\\fs{round(size * 0.4)}}} \\N" if parts else ""
        parts.append(gap + "{\\rCardLine}" + "\\N".join(body))
    events = []
    if parts:
        events.append(_event(0, duration, "CardHead" if headline.strip() else "CardLine",
                             "{\\fad(200,200)}" + "\\N".join(parts)))
    return _header(profile, styles) + "\n" + "\n".join(events) + "\n"


def lower_third_width(profile: Profile, speaker: str | None, talk: str | None) -> int:
    """Estimated width of the lower-third text block, for its backing panel.

    A generous sans-serif estimate: the panel may be a little wider than the
    text, never narrower for common fonts.
    """
    name = len(speaker or "") * 0.6 * profile.caption_size * LOWER_NAME
    sub = len(talk or "") * 0.55 * profile.caption_size * LOWER_TALK
    return int(max(name, sub))
