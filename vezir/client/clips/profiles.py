"""Output profiles: where a clip is posted, as layout geometry.

All coordinates are canvas pixels, origin top-left.  ``safe`` is the area
platform UI leaves visible: on TikTok / Reels / Shorts the top ~11 % carries
the app chrome, the bottom ~22 % the description and music line, and a
column on the right the like/comment/share buttons.  Captions, the ticker
text's resting area and the logo must sit inside it; the ticker *strip*
may run full width (text scrolls through the icon column).
"""
from __future__ import annotations

import math
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
    ticker: Box              # the banner strip, before any tilt
    ticker_size: int
    safe: Box
    title_card_s: float      # 0 = no title card (shorts open on the hook)
    end_card_s: float
    ticker_tilt: bool = False         # may the kit tilt the banner over footage?
    card_band_y: int = 0              # center of the banner on cards (kit on_cards)
    header_logo: Box | None = None    # logo box when the campaign has header lines
    header_text: tuple[int, int] | None = None   # (right edge x, center y) of those lines
    lower_third: Box | None = None
    lower_third_s: tuple[float, float] = (0.4, 4.5)
    fps: int = 30

    def max_ticker_tilt(self) -> float:
        """Largest tilt (degrees) that keeps the banner between the captions
        and the bottom of the safe area; 0 where tilting isn't allowed."""
        if not self.ticker_tilt:
            return 0.0
        center = self.ticker.y + self.ticker.h / 2
        room = min(center - self.captions.bottom, self.safe.bottom - center) - self.ticker.h / 2
        return max(0.0, math.degrees(math.atan(room / (self.width / 2))))

    def ticker_tilt_for(self, requested: float) -> float:
        """The kit's tilt, clamped to what this profile's layout allows."""
        limit = self.max_ticker_tilt()
        return max(-limit, min(limit, requested))


VERTICAL = Profile(
    name="vertical",
    width=1080, height=1920,
    logo=Box(240, 215, 600, 230),
    header_logo=Box(80, 215, 420, 230),
    header_text=(960, 330),
    video=Box(0, 470, 1080, 608),
    captions=Box(80, 1090, 840, 230),
    caption_size=84,
    caption_max_chars=22,
    caption_highlight=True,
    caption_box=False,
    ticker=Box(0, 1376, 1080, 72),
    ticker_size=42,
    safe=Box(60, 210, 900, 1290),
    title_card_s=0.0,
    end_card_s=3.0,
    ticker_tilt=True,
    card_band_y=400,
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
    card_band_y=960,
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
