"""Output profiles: where a clip is posted, as layout geometry.

All coordinates are canvas pixels, origin top-left.  ``safe`` is the area
platform UI leaves visible: on TikTok / Reels / Shorts the top ~11 % carries
the app chrome, the bottom ~22 % the description and music line, and a
column on the right the like/comment/share buttons.  Captions, the ticker
text's resting area and the logo must sit inside it; the ticker *strip*
may run full width (text scrolls through the icon column).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Box:
    x: int
    y: int
    w: int
    h: int

    @property
    def bottom(self) -> int:
        return self.y + self.h

    @property
    def right(self) -> int:
        return self.x + self.w

    def contains(self, other: Box) -> bool:
        return (
            self.x <= other.x and self.y <= other.y
            and other.right <= self.right and other.bottom <= self.bottom
        )


@dataclass(frozen=True)
class Profile:
    name: str
    width: int
    height: int
    video: Box               # the source video is fitted (letterboxed) into this
    logo: Box                # logo is fitted by height, centered in the box
    captions: Box            # caption block; text is bottom-aligned inside it
    caption_size: int
    caption_max_chars: int   # per line; two lines max
    caption_highlight: bool  # current word in the brand's primary color
    caption_box: bool        # translucent box behind captions (they sit on footage)
    ticker: Box              # the banner strip
    ticker_size: int
    safe: Box
    title_card_s: float      # 0 = no title card (shorts open on the hook)
    end_card_s: float
    lower_third: Box | None = None
    lower_third_s: tuple[float, float] = (0.4, 4.5)
    fps: int = 30


VERTICAL = Profile(
    name="vertical",
    width=1080, height=1920,
    logo=Box(240, 215, 600, 230),
    video=Box(0, 470, 1080, 608),
    captions=Box(80, 1110, 840, 250),
    caption_size=84,
    caption_max_chars=22,
    caption_highlight=True,
    caption_box=False,
    ticker=Box(0, 1395, 1080, 84),
    ticker_size=44,
    safe=Box(60, 210, 900, 1290),
    title_card_s=0.0,
    end_card_s=3.0,
)

LANDSCAPE = Profile(
    name="landscape",
    width=1920, height=1080,
    logo=Box(1620, 30, 250, 130),
    video=Box(0, 0, 1920, 1080),
    captions=Box(160, 820, 1600, 170),
    caption_size=64,
    caption_max_chars=52,
    caption_highlight=False,
    caption_box=True,
    ticker=Box(0, 1014, 1920, 54),
    ticker_size=32,
    safe=Box(48, 27, 1824, 1026),
    title_card_s=1.5,
    end_card_s=3.0,
    lower_third=Box(80, 610, 1100, 170),
)

PROFILES: dict[str, Profile] = {p.name: p for p in (VERTICAL, LANDSCAPE)}


def for_clip(kind: str | None, choice: str = "auto") -> Profile:
    """``auto``: short clips go vertical, long clips landscape."""
    if choice != "auto":
        try:
            return PROFILES[choice]
        except KeyError:
            raise ValueError(
                f"unknown profile {choice!r}; expected auto or one of {sorted(PROFILES)}"
            ) from None
    return VERTICAL if kind == "short" else LANDSCAPE


def fit(src_w: int, src_h: int, box: Box) -> tuple[int, int, int, int]:
    """Fit ``src`` inside ``box`` keeping aspect ratio; even dims, centered.

    Returns ``(w, h, x, y)``.
    """
    if src_w <= 0 or src_h <= 0:
        raise ValueError("source dimensions must be positive")
    scale = min(box.w / src_w, box.h / src_h)
    w = max(2, int(src_w * scale) // 2 * 2)
    h = max(2, int(src_h * scale) // 2 * 2)
    return w, h, box.x + (box.w - w) // 2, box.y + (box.h - h) // 2
