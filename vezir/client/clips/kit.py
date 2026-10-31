"""Brand kits and campaigns: the data that makes a render brand-specific.

Both are TOML (comments are allowed, which matters for files a designer
edits).  Every path in a kit is relative to the kit directory unless it is
absolute.  Loading validates everything up front so a render never fails
halfway on a typo: unknown color names, a missing font or logo, a bad
glossary regex.
"""
from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import svgpath

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on 3.10 only
    import tomli as tomllib

KIT_FILE = "kit.toml"
COLOR_NAMES = ("primary", "background", "text", "on_primary", "outline")
CARD_SLOTS = ("title_landscape", "end_landscape", "end_vertical")
AUDIO_SLOTS = ("title", "end")       # stings mixed into the generated cards
MAX_TILT_DEG = 15.0
MAX_HEADER_LINES = 3

_HEX = re.compile(r"\A#[0-9a-fA-F]{6}\Z")


class KitError(ValueError):
    """A brand kit or campaign file is invalid."""


@dataclass(frozen=True)
class GlossaryRule:
    pattern: re.Pattern[str]
    replace: str

    def apply(self, text: str) -> str:
        return self.pattern.sub(self.replace, text)


@dataclass(frozen=True)
class Brand:
    root: Path
    name: str
    colors: dict[str, str]
    font_family: str
    font_file: Path
    logo_light: Path | None = None
    logo_dark: Path | None = None
    pattern: Path | None = None
    pattern_opacity: float = 0.12
    cards: dict[str, Path] = field(default_factory=dict)
    glossary: tuple[GlossaryRule, ...] = ()
    digest: str = ""
    ticker_tilt_deg: float = 0.0          # profiles that allow it tilt the banner
    ticker_icon: svgpath.Icon | None = None   # drawn between ticker items
    ticker_on_cards: bool = False          # the banner also runs across the cards
    audio: dict[str, Path] = field(default_factory=dict)   # AUDIO_SLOTS -> file

    @property
    def fonts_dir(self) -> Path:
        return self.font_file.parent


@dataclass(frozen=True)
class Campaign:
    name: str
    ticker_items: tuple[str, ...]
    ticker_separator: str = "   •   "
    ticker_start_s: float = 2.0
    ticker_speed: float = 140.0
    end_headline: str = ""
    end_lines: tuple[str, ...] = ()
    url: str | None = None
    hashtags: tuple[str, ...] = ()
    handles: dict[str, str] = field(default_factory=dict)
    glossary: tuple[GlossaryRule, ...] = ()
    digest: str = ""
    header_lines: tuple[str, ...] = ()     # beside the logo on vertical clips

    @property
    def ticker_text(self) -> str:
        return self.ticker_separator.join(self.ticker_items)


def _read_toml(path: Path) -> tuple[dict[str, Any], str]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise KitError(f"cannot read {path}: {exc}") from None
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise KitError(f"{path}: {exc}") from None
    return data, hashlib.sha256(raw).hexdigest()[:16]


def _table(data: dict[str, Any], key: str, where: Path) -> dict[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise KitError(f"{where}: [{key}] must be a table")
    return value


def _str(value: Any, what: str, where: Path, *, required: bool = True) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise KitError(f"{where}: {what} must be a non-empty string")
    return value


def _str_list(value: Any, what: str, where: Path) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise KitError(f"{where}: {what} must be a list of strings")
    return tuple(value)


def _number(value: Any, what: str, where: Path, lo: float, hi: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not lo <= value <= hi:
        raise KitError(f"{where}: {what} must be a number between {lo} and {hi}")
    return float(value)


def _glossary(data: dict[str, Any], where: Path) -> tuple[GlossaryRule, ...]:
    rules = data.get("glossary", [])
    if not isinstance(rules, list):
        raise KitError(f"{where}: glossary must be an array of tables ([[glossary]])")
    out = []
    for i, rule in enumerate(rules):
        if not isinstance(rule, dict):
            raise KitError(f"{where}: glossary entry {i} must be a table")
        pattern = _str(rule.get("pattern"), f"glossary[{i}].pattern", where)
        replace = rule.get("replace")
        if not isinstance(replace, str):
            raise KitError(f"{where}: glossary[{i}].replace must be a string")
        try:
            out.append(GlossaryRule(re.compile(pattern), replace))
        except re.error as exc:
            raise KitError(f"{where}: glossary[{i}].pattern: {exc}") from None
    return tuple(out)


def _asset(root: Path, value: Any, what: str, where: Path) -> Path | None:
    if value is None:
        return None
    rel = _str(value, what, where)
    path = Path(rel).expanduser()
    if not path.is_absolute():
        path = root / path
    if not path.is_file():
        raise KitError(f"{where}: {what} not found: {path}")
    return path.resolve()


def find_font(family: str) -> Path | None:
    """Resolve a bold face of *family* through fontconfig, if available."""
    if not shutil.which("fc-match"):
        return None
    try:
        proc = subprocess.run(
            ["fc-match", "-f", "%{file}", f"{family}:bold"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    path = Path(proc.stdout.strip()) if proc.returncode == 0 else None
    return path if path and path.is_file() else None


def font_charset(font: Path) -> list[tuple[int, int]] | None:
    """Codepoint ranges covered by *font* (fontconfig), or None if unknown."""
    if not shutil.which("fc-query"):
        return None
    try:
        proc = subprocess.run(["fc-query", "-f", "%{charset}", str(font)],
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    ranges = []
    for item in proc.stdout.split() if proc.returncode == 0 else []:
        lo, _, hi = item.partition("-")
        try:
            ranges.append((int(lo, 16), int(hi or lo, 16)))
        except ValueError:
            return None
    return ranges or None


def missing_glyphs(font: Path, *texts: str) -> list[str]:
    """Characters in *texts* that *font* cannot draw (empty when unknown)."""
    ranges = font_charset(font)
    if ranges is None:
        return []
    chars = sorted({ch for t in texts for ch in t if not ch.isspace()})
    return [ch for ch in chars if not any(lo <= ord(ch) <= hi for lo, hi in ranges)]


def campaign_glyph_gaps(brand: Brand, campaign: Campaign) -> list[str]:
    """Campaign characters the brand's font lacks (ticker, header, end card).

    With a separator icon the text separator is never drawn, so it is not
    checked.
    """
    ticker = (" ".join(campaign.ticker_items) if brand.ticker_icon is not None
              else campaign.ticker_text)
    return missing_glyphs(
        brand.font_file, ticker, campaign.end_headline, *campaign.end_lines,
        *campaign.header_lines,
    )


def load_brand(kit_dir: str | Path) -> Brand:
    root = Path(kit_dir).expanduser().resolve()
    where = root / KIT_FILE
    if not where.is_file():
        raise KitError(f"no {KIT_FILE} in brand kit {root}")
    data, digest = _read_toml(where)

    colors_in = _table(data, "colors", where)
    unknown = sorted(set(colors_in) - set(COLOR_NAMES))
    if unknown:
        raise KitError(f"{where}: unknown color(s) {unknown}; expected {list(COLOR_NAMES)}")
    colors = {}
    for name in COLOR_NAMES:
        value = colors_in.get(name)
        if not isinstance(value, str) or not _HEX.match(value):
            raise KitError(f"{where}: colors.{name} must be a #rrggbb hex color")
        colors[name] = value.lower()

    fonts = _table(data, "fonts", where)
    family = _str(fonts.get("family"), "fonts.family", where)
    font_file = _asset(root, fonts.get("bold"), "fonts.bold", where)
    if font_file is None:
        font_file = find_font(family)
        if font_file is None:
            raise KitError(
                f"{where}: fonts.bold is not set and fontconfig has no bold "
                f"face for {family!r}"
            )

    assets = _table(data, "assets", where)
    opacity = assets.get("pattern_opacity", 0.12)
    cards_in = _table(data, "cards", where)
    unknown = sorted(set(cards_in) - set(CARD_SLOTS))
    if unknown:
        raise KitError(f"{where}: unknown card slot(s) {unknown}; expected {list(CARD_SLOTS)}")
    cards = {
        slot: path for slot in CARD_SLOTS
        if (path := _asset(root, cards_in.get(slot), f"cards.{slot}", where)) is not None
    }

    ticker = _table(data, "ticker", where)
    unknown = sorted(set(ticker) - {"tilt_deg", "separator_icon", "on_cards"})
    if unknown:
        raise KitError(f"{where}: unknown [ticker] key(s) {unknown}")
    icon_path = _asset(root, ticker.get("separator_icon"), "ticker.separator_icon", where)
    icon = None
    if icon_path is not None:
        try:
            icon = svgpath.load(icon_path)
        except svgpath.IconError as exc:
            raise KitError(f"{where}: ticker.separator_icon: {exc}") from None
    on_cards = ticker.get("on_cards", False)
    if not isinstance(on_cards, bool):
        raise KitError(f"{where}: ticker.on_cards must be true or false")

    audio_in = _table(data, "audio", where)
    unknown = sorted(set(audio_in) - set(AUDIO_SLOTS))
    if unknown:
        raise KitError(f"{where}: unknown audio slot(s) {unknown}; expected {list(AUDIO_SLOTS)}")
    audio = {
        slot: path for slot in AUDIO_SLOTS
        if (path := _asset(root, audio_in.get(slot), f"audio.{slot}", where)) is not None
    }
    return Brand(
        root=root,
        name=_str(data.get("name"), "name", where),
        colors=colors,
        font_family=family,
        font_file=font_file,
        logo_light=_asset(root, assets.get("logo_light"), "assets.logo_light", where),
        logo_dark=_asset(root, assets.get("logo_dark"), "assets.logo_dark", where),
        pattern=_asset(root, assets.get("pattern"), "assets.pattern", where),
        pattern_opacity=_number(opacity, "assets.pattern_opacity", where, 0.0, 1.0),
        cards=cards,
        glossary=_glossary(data, where),
        digest=digest,
        ticker_tilt_deg=_number(ticker.get("tilt_deg", 0.0), "ticker.tilt_deg", where,
                                -MAX_TILT_DEG, MAX_TILT_DEG),
        ticker_icon=icon,
        ticker_on_cards=on_cards,
        audio=audio,
    )


def load_campaign(path: str | Path) -> Campaign:
    where = Path(path).expanduser().resolve()
    data, digest = _read_toml(where)
    ticker = _table(data, "ticker", where)
    items = _str_list(ticker.get("items"), "ticker.items", where)
    if not items:
        raise KitError(f"{where}: ticker.items must list at least one item")
    end = _table(data, "end_card", where)
    handles = _table(data, "handles", where)
    if not all(isinstance(v, str) for v in handles.values()):
        raise KitError(f"{where}: handles values must be strings")
    url = data.get("url")
    header = _str_list(data.get("header_lines"), "header_lines", where)
    if len(header) > MAX_HEADER_LINES:
        raise KitError(f"{where}: header_lines takes at most {MAX_HEADER_LINES} lines")
    return Campaign(
        name=_str(data.get("name"), "name", where),
        ticker_items=items,
        ticker_separator=_str(ticker.get("separator", "   •   "), "ticker.separator", where),
        ticker_start_s=_number(ticker.get("start_s", 2.0), "ticker.start_s", where, 0.0, 60.0),
        ticker_speed=_number(ticker.get("speed", 140), "ticker.speed", where, 10.0, 2000.0),
        end_headline=_str(end.get("headline"), "end_card.headline", where, required=False),
        end_lines=_str_list(end.get("lines"), "end_card.lines", where),
        url=_str(url, "url", where) if url is not None else None,
        hashtags=_str_list(data.get("hashtags"), "hashtags", where),
        handles=dict(handles),
        glossary=_glossary(data, where),
        digest=digest,
        header_lines=header,
    )
