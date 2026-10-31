"""`vezir clips` (v0.29.0): brand kits, captions, layouts, ffmpeg rendering.

Unit tests cover every module without ffmpeg.  The end-to-end tests at the
bottom render real clips from a synthetic source and skip when ffmpeg or a
DejaVu font is unavailable (CI installs both).
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from vezir.client.clips import ass, captions, kit, profiles, render, session
from vezir.client.clips.captions import Cue, Word
from vezir.client.clips.profiles import LANDSCAPE, VERTICAL, Box

NEUTRAL = Path(kit.__file__).parent / "kits" / "neutral"


# ── helpers ─────────────────────────────────────────────────────────────────


def _kit(tmp_path: Path, body: str, files: dict[str, bytes] | None = None) -> Path:
    d = tmp_path / "kit"
    d.mkdir(exist_ok=True)
    for name, data in (files or {}).items():
        p = d / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    (d / "kit.toml").write_text(body)
    return d


FONT = b"not-really-a-font"
COLORS = """
[colors]
primary = "#fab44b"
background = "#484139"
text = "#ffffff"
on_primary = "#000000"
outline = "#111111"
"""
GOOD_KIT = 'name = "T"\n' + COLORS + '[fonts]\nfamily = "X"\nbold = "f/X-Bold.ttf"\n'


def _brand(tmp_path: Path, extra: str = "", files: dict | None = None) -> kit.Brand:
    return kit.load_brand(_kit(tmp_path, GOOD_KIT + extra, {"f/X-Bold.ttf": FONT, **(files or {})}))


def _campaign(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "c.toml"
    p.write_text(body)
    return p


GOOD_CAMPAIGN = """
name = "C"
url = "https://example.org"
hashtags = ["#a", "#b"]
[ticker]
items = ["ONE", "TWO"]
[end_card]
headline = "HEAD"
lines = ["line 1", "line 2"]
[handles]
x = "@x"
"""


def _words(text: str, start: float = 0.2, step: float = 0.35) -> list[Word]:
    out, t = [], start
    for w in text.split():
        out.append(Word(w, round(t, 3), round(t + step - 0.05, 3)))
        t += step
    return out


# ── kit ─────────────────────────────────────────────────────────────────────


def test_neutral_kit_and_example_campaign_load():
    if kit.find_font("DejaVu Sans") is None:
        pytest.skip("no DejaVu Sans via fontconfig")
    brand = kit.load_brand(NEUTRAL)
    camp = kit.load_campaign(NEUTRAL / "campaign.example.toml")
    assert brand.name == "Neutral" and brand.font_file.is_file()
    assert brand.fonts_dir == brand.font_file.parent
    assert camp.ticker_items and camp.end_lines and camp.ticker_start_s == 2.0
    assert len(brand.digest) == 16 and len(camp.digest) == 16


def test_brand_loads_assets_cards_glossary(tmp_path):
    brand = _brand(tmp_path, """
[assets]
logo_light = "l.png"
pattern = "p.png"
pattern_opacity = 0.3
[cards]
end_vertical = "end.mp4"
[[glossary]]
pattern = "\\\\bTCP ?IP\\\\b"
replace = "TCP/IP"
""", {"l.png": b"x", "p.png": b"x", "end.mp4": b"x"})
    assert brand.logo_light and brand.logo_dark is None and brand.pattern_opacity == 0.3
    assert set(brand.cards) == {"end_vertical"}
    assert brand.glossary[0].apply("TCP IP and TCPIP") == "TCP/IP and TCP/IP"
    assert brand.colors["primary"] == "#fab44b"


def test_absolute_asset_paths(tmp_path):
    logo = tmp_path / "abs.png"
    logo.write_bytes(b"x")
    brand = _brand(tmp_path, f'[assets]\nlogo_dark = "{logo}"\n')
    assert brand.logo_dark == logo.resolve()


@pytest.mark.parametrize(("body", "message"), [
    (GOOD_KIT.replace('primary = "#fab44b"', 'primary = "orange"'), "colors.primary"),
    (GOOD_KIT.replace("[colors]", "[colors]\nmagenta = \"#ff00ff\""), "unknown color"),
    (GOOD_KIT.replace('name = "T"', 'name = ""'), "name must be"),
    (GOOD_KIT.replace('family = "X"', "family = 3"), "fonts.family"),
    (GOOD_KIT.replace('bold = "f/X-Bold.ttf"', 'bold = "missing.ttf"'), "fonts.bold not found"),
    (GOOD_KIT + "[assets]\npattern_opacity = 2\n", "pattern_opacity"),
    (GOOD_KIT + "[assets]\npattern_opacity = true\n", "pattern_opacity"),
    (GOOD_KIT + '[cards]\nintro_vertical = "x.mp4"\n', "unknown card slot"),
    (GOOD_KIT + '[cards]\nend_vertical = "nope.mp4"\n', "cards.end_vertical not found"),
    ('glossary = "x"\n' + GOOD_KIT, "glossary must be an array"),
    ("glossary = [1]\n" + GOOD_KIT, "glossary entry 0"),
    (GOOD_KIT + '[[glossary]]\npattern = "("\nreplace = "x"\n', "glossary[0].pattern"),
    (GOOD_KIT + '[[glossary]]\npattern = "a"\nreplace = 1\n', "glossary[0].replace"),
    (GOOD_KIT + '[fonts]\nfamily = "Y"\n', None),  # duplicate table: TOML error
    ('name = "T"\ncolors = 3\n', "[colors] must be a table"),
])
def test_brand_validation_errors(tmp_path, body, message):
    d = _kit(tmp_path, body, {"f/X-Bold.ttf": FONT})
    with pytest.raises(kit.KitError) as err:
        kit.load_brand(d)
    if message:
        assert message in str(err.value)


def test_brand_without_kit_file(tmp_path):
    with pytest.raises(kit.KitError, match="no kit.toml"):
        kit.load_brand(tmp_path)


def test_brand_unreadable_and_non_utf8(tmp_path, monkeypatch):
    d = _kit(tmp_path, GOOD_KIT, {"f/X-Bold.ttf": FONT})
    (d / "kit.toml").write_bytes(b"\xff\xfe")
    with pytest.raises(kit.KitError):
        kit.load_brand(d)
    with pytest.raises(kit.KitError, match="cannot read"):
        kit.load_campaign(tmp_path / "nope.toml")


def test_brand_font_from_fontconfig(tmp_path, monkeypatch):
    font = tmp_path / "Found-Bold.ttf"
    font.write_bytes(b"x")
    monkeypatch.setattr(kit, "find_font", lambda family: font)
    brand = kit.load_brand(_kit(tmp_path, 'name = "T"\n' + COLORS + '[fonts]\nfamily = "X"\n'))
    assert brand.font_file == font
    monkeypatch.setattr(kit, "find_font", lambda family: None)
    with pytest.raises(kit.KitError, match="fontconfig has no bold face"):
        kit.load_brand(_kit(tmp_path, 'name = "T"\n' + COLORS + '[fonts]\nfamily = "X"\n'))


def test_find_font_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(kit.shutil, "which", lambda name: None)
    assert kit.find_font("X") is None
    monkeypatch.setattr(kit.shutil, "which", lambda name: "/usr/bin/" + name)

    def boom(*a, **k):
        raise OSError("no")

    monkeypatch.setattr(kit.subprocess, "run", boom)
    assert kit.find_font("X") is None
    font = tmp_path / "F.ttf"
    font.write_bytes(b"x")

    class R:
        def __init__(self, rc, out):
            self.returncode, self.stdout = rc, out

    monkeypatch.setattr(kit.subprocess, "run", lambda *a, **k: R(0, str(font)))
    assert kit.find_font("X") == font
    monkeypatch.setattr(kit.subprocess, "run", lambda *a, **k: R(1, ""))
    assert kit.find_font("X") is None


def test_campaign_loads(tmp_path):
    camp = kit.load_campaign(_campaign(tmp_path, GOOD_CAMPAIGN))
    assert camp.ticker_text == "ONE   •   TWO"
    assert camp.end_headline == "HEAD" and camp.handles == {"x": "@x"}
    assert camp.url == "https://example.org" and camp.hashtags == ("#a", "#b")
    bare = kit.load_campaign(_campaign(tmp_path, 'name = "N"\n[ticker]\nitems = ["A"]\n'))
    assert bare.url is None and bare.end_headline == "" and bare.end_lines == ()


@pytest.mark.parametrize(("body", "message"), [
    ('name = "N"\n', "ticker.items must list"),
    ('name = "N"\n[ticker]\nitems = []\n', "ticker.items must list"),
    ('name = "N"\n[ticker]\nitems = [1]\n', "ticker.items must be a list"),
    ('name = "N"\n[ticker]\nitems = ["A"]\nstart_s = -1\n', "ticker.start_s"),
    ('name = "N"\n[ticker]\nitems = ["A"]\nseparator = ""\n', "ticker.separator"),
    ('name = "N"\n[ticker]\nitems = ["A"]\n[handles]\nx = 1\n', "handles values"),
    ('name = "N"\nurl = 3\n[ticker]\nitems = ["A"]\n', "url must be"),
    ('name = "N"\nticker = 3\n', "[ticker] must be a table"),
    ('name = "N"\n[ticker]\nitems = ["A"]\n[end_card]\nheadline = 3\n', "end_card.headline"),
])
def test_campaign_validation_errors(tmp_path, body, message):
    with pytest.raises(kit.KitError, match=message.replace("[", r"\[").replace("]", r"\]")):
        kit.load_campaign(_campaign(tmp_path, body))


def test_missing_glyphs(monkeypatch, tmp_path):
    class R:
        returncode, stdout = 0, "20-7e a0-ff 2022"

    monkeypatch.setattr(kit.shutil, "which", lambda name: "/usr/bin/fc-query")
    monkeypatch.setattr(kit.subprocess, "run", lambda *a, **k: R())
    assert kit.missing_glyphs(tmp_path / "f.ttf", "A • B ⚡ →", "é") == ["→", "⚡"]
    brand = _brand(tmp_path)
    camp = kit.load_campaign(_campaign(tmp_path, GOOD_CAMPAIGN.replace("TWO", "TWO ⚡")))
    assert kit.campaign_glyph_gaps(brand, camp) == ["⚡"]

    class Bad:
        returncode, stdout = 0, "zz-qq"

    monkeypatch.setattr(kit.subprocess, "run", lambda *a, **k: Bad())
    assert kit.missing_glyphs(tmp_path / "f.ttf", "⚡") == []

    def boom(*a, **k):
        raise subprocess.TimeoutExpired("fc-query", 10)

    monkeypatch.setattr(kit.subprocess, "run", boom)
    assert kit.font_charset(tmp_path / "f.ttf") is None
    monkeypatch.setattr(kit.shutil, "which", lambda name: None)
    assert kit.missing_glyphs(tmp_path / "f.ttf", "⚡") == []


# ── profiles ────────────────────────────────────────────────────────────────


def test_for_clip():
    assert profiles.for_clip("short") is VERTICAL
    assert profiles.for_clip("long") is LANDSCAPE
    assert profiles.for_clip(None) is LANDSCAPE
    assert profiles.for_clip("short", "landscape") is LANDSCAPE
    with pytest.raises(ValueError, match="unknown profile"):
        profiles.for_clip("short", "square")


def test_fit():
    assert profiles.fit(1280, 720, VERTICAL.video) == (1080, 606, 0, 471)
    w, h, x, y = profiles.fit(720, 1280, LANDSCAPE.video)
    assert (w, h) == (606, 1080) and x == (1920 - 606) // 2 and y == 0
    assert all(v % 2 == 0 for v in profiles.fit(1001, 333, Box(0, 0, 777, 555))[:2])
    with pytest.raises(ValueError):
        profiles.fit(0, 10, VERTICAL.video)


@pytest.mark.parametrize("profile", [VERTICAL, LANDSCAPE])
def test_layout_respects_safe_zones(profile):
    safe = profile.safe
    assert safe.contains(profile.captions)
    assert safe.contains(profile.logo)
    if profile.lower_third is not None:
        assert safe.contains(profile.lower_third)
    # Captions never overlap the ticker strip; the strip stays on canvas.
    assert profile.captions.bottom <= profile.ticker.y
    assert profile.ticker.bottom <= profile.height
    # Vertical: the strip sits above the bottom UI zone of TikTok/Reels.
    if profile is VERTICAL:
        assert profile.ticker.bottom <= safe.bottom
        assert profile.video.bottom <= profile.captions.y
        assert not profile.video.contains(profile.logo)
    else:
        assert profile.video.contains(profile.logo)


def test_box_helpers():
    b = Box(10, 20, 30, 40)
    assert (b.right, b.bottom) == (40, 60)
    assert b.contains(Box(10, 20, 30, 40)) and not b.contains(Box(9, 20, 30, 40))


# ── captions ────────────────────────────────────────────────────────────────


def test_words_from_clip_and_fallback():
    clip = {"words": [{"word": " hi ", "start": 1.0, "end": 1.2}, "junk",
                      {"word": "", "start": 0, "end": 1}, {"word": "x", "start": "a"},
                      {"word": "back", "start": 2.0, "end": 1.0},
                      {"word": "first", "start": 0.5, "end": 0.7}]}
    assert [w.text for w in captions.words_from_clip(clip)] == ["first", "hi"]
    spread = captions.words_from_clip(
        {"text": "SPEAKER_00: hello there\nAlice: bye", "duration_s": 6.0})
    assert [w.text for w in spread] == ["hello", "there", "bye"]
    assert spread[0].start == 0.0 and spread[-1].end == pytest.approx(6.0, abs=0.01)
    assert captions.words_from_clip({"text": "", "duration_s": 5}) == []
    assert captions.words_from_clip({"text": "x", "duration_s": 0}) == []


def test_build_cues_breaks():
    words = _words("This is one. Here comes another sentence")
    cues = captions.build_cues(words, 22)
    assert cues[0].text == "This is one."   # sentence end
    pause = _words("a b") + _words("c d", start=3.0)
    assert [c.text for c in captions.build_cues(pause, 22)] == ["a b", "c d"]
    long = _words(" ".join(["word"] * 20), step=0.1)
    cues = captions.build_cues(long, 10)
    assert all(len(c.text) <= 20 for c in cues)
    assert all(len(line) <= 10 for c in cues for line in c.lines)
    slow = _words("one two three four five six", step=0.9)
    assert all(c.words[-1].end - c.words[0].start <= captions.MAX_CUE_S + 0.9
               for c in captions.build_cues(slow, 40))


def test_build_cues_timing_rules():
    cues = captions.build_cues([Word("Hi.", 1.0, 1.1), Word("Bye.", 1.3, 1.4)], 20)
    assert cues[0].end == pytest.approx(1.3)            # never overlaps the next cue
    assert cues[1].end == pytest.approx(1.3 + captions.MIN_CUE_S)  # minimum on screen
    assert captions.build_cues([], 20) == []
    clamped = captions.build_cues([Word("End.", 4.8, 4.9)], 20, clip_end=5.0)
    assert clamped[0].end == pytest.approx(5.0)


def test_glossary_keeps_or_spreads_timing():
    rule = kit.GlossaryRule(__import__("re").compile(r"\bsets\b(?= worth)"), "sats")
    words = [Word("single", 0.0, 0.3), Word("sets", 0.4, 0.6), Word("worth", 0.7, 0.9)]
    (cue,) = captions.build_cues(words, 30, [rule])
    assert cue.text == "single sats worth"
    assert [w.start for w in cue.words] == [0.0, 0.4, 0.7]   # same count: kept
    merge = kit.GlossaryRule(__import__("re").compile(r"\bTCP IP\b"), "TCP/IP")
    (cue,) = captions.build_cues([Word("TCP", 0.0, 0.2), Word("IP", 0.3, 0.5),
                                  Word("rocks", 0.6, 0.9)], 30, [merge])
    assert cue.text == "TCP/IP rocks" and len(cue.words) == 2   # count changed: spread
    assert cue.words[0].start == 0.0 and cue.words[-1].end == pytest.approx(0.9, abs=0.01)


def test_wrap():
    assert captions.wrap(["short"], 10) == ["short"]
    assert captions.wrap(["averyveryverylongword"], 5) == ["averyveryverylongword"]
    assert captions.wrap("aa bb cc dd ee ff".split(), 9) == ["aa bb cc", "dd ee ff"]


def test_srt_vtt():
    cues = [Cue(0.25, 1.5, _words("hello world"), ["hello world"]),
            Cue(61.0, 3725.5, _words("two lines"), ["two", "lines"])]
    srt = captions.to_srt(cues, offset=1.5)
    assert srt.startswith("1\n00:00:01,750 --> 00:00:03,000\nhello world\n\n2\n")
    assert "01:02:07,000 --> 01:02:07,000" not in srt
    assert "00:01:02,500 --> 01:02:07,000\ntwo\nlines" in srt
    vtt = captions.to_vtt(cues)
    assert vtt.startswith("WEBVTT\n\n00:00:00.250 --> 00:00:01.500\nhello world")
    assert captions.to_srt([]) == "" and captions.to_vtt([]) == "WEBVTT\n\n"


def test_captions_file_roundtrip_and_edits(tmp_path):
    cues = captions.build_cues(_words("we stream single sets worth of money"), 18)
    path = tmp_path / "c.json"
    path.write_text(captions.dumps(cues, "short-01"))
    doc = json.loads(path.read_text())
    assert doc["clip"] == "short-01" and doc["version"] == 1
    again = captions.load(path, 18)
    assert [c.text for c in again] == [c.text for c in cues]
    doc["cues"][0]["text"] = doc["cues"][0]["text"].replace("sets", "sats")
    doc["cues"].append({"start": 9, "end": 10, "text": "  ", "words": []})
    doc["cues"].append({"start": 11, "end": 12, "text": "brand new words here", "words": []})
    path.write_text(json.dumps(doc))
    edited = captions.load(path, 18)
    assert "sats" in edited[0].text
    assert [w.start for w in edited[0].words] == [w.start for w in cues[0].words]
    assert len(edited) == len(cues) + 1             # emptied cue removed
    assert edited[-1].words[0].start == 11 and edited[-1].words[-1].end == pytest.approx(12)


@pytest.mark.parametrize(("doc", "message"), [
    ({"version": 2, "cues": []}, "version-1"),
    ([], "version-1"),
    ({"version": 1, "cues": [{"start": 1}]}, "cue 0 is malformed"),
    ({"version": 1, "cues": [{"start": 2, "end": 1, "text": "x"}]}, "ends before"),
])
def test_captions_file_errors(tmp_path, doc, message):
    path = tmp_path / "c.json"
    path.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match=message):
        captions.load(path, 20)


# ── ass ─────────────────────────────────────────────────────────────────────


def test_color_and_escape():
    assert ass.color("#fab44b") == "&H004BB4FA"
    assert ass.color("#000000", 0x80) == "&H80000000"
    assert ass.escape("{\\fs200}x\ny") == "(⧵fs200)x y"


def test_main_script_highlight_and_plain(tmp_path):
    brand = _brand(tmp_path)
    cue = Cue(0.2, 1.4, _words("one two three"), ["one two", "three"])
    vert = ass.main_script(VERTICAL, brand, [cue])
    events = [ln for ln in vert.splitlines() if ln.startswith("Dialogue:")]
    assert len(events) == 3                        # one per word
    assert "{\\c&H004BB4FA}two{\\r}" in events[1] and "\\N" in events[1]
    assert "PlayResX: 1080" in vert and "LowerName" not in vert
    land = ass.main_script(LANDSCAPE, brand, [cue], speaker="Ann", talk="Talk")
    assert len([ln for ln in land.splitlines() if ",Caption," in ln]) == 1
    assert "Style: Caption" in land and ",3," in land.split("Style: Caption", 1)[1]
    assert "{\\pos(80,610)}Ann" in land and "Talk" in land
    talk_only = ass.main_script(LANDSCAPE, brand, [], talk="Only")
    assert "{\\pos(80,610)}Only" in talk_only and "LowerName" in talk_only
    # A zero-length word event is skipped, not emitted.
    flat = Cue(1.0, 1.0, [Word("a", 1.0, 1.0), Word("b", 1.0, 1.0)], ["a b"])
    assert "Dialogue" not in ass.main_script(VERTICAL, brand, [flat])
    nowords = Cue(0.0, 1.0, [], ["x"])
    assert ass.main_script(VERTICAL, brand, [nowords]).count("Dialogue:") == 1


def test_card_script(tmp_path):
    brand = _brand(tmp_path)
    full = ass.card_script(VERTICAL, brand, 3.0, "HEAD", ["a", " ", "b"], text_top=900)
    (event,) = [ln for ln in full.splitlines() if ln.startswith("Dialogue:")]
    assert ",CardHead," in event and "HEAD\\N" in event and "{\\rCardLine}a\\Nb" in event
    lines_only = ass.card_script(VERTICAL, brand, 3.0, "", ["only"], text_top=900)
    assert ",CardLine," in lines_only and "\\fs" not in lines_only.split("[Events]")[1]
    assert "Dialogue" not in ass.card_script(VERTICAL, brand, 3.0, " ", [], text_top=0)


def test_lower_third_width_grows_with_text():
    assert ass.lower_third_width(LANDSCAPE, "A" * 30, None) > ass.lower_third_width(
        LANDSCAPE, "A", None)
    assert ass.lower_third_width(LANDSCAPE, None, None) == 0


# ── render (no ffmpeg) ──────────────────────────────────────────────────────


def test_luminance_and_logo_choice(tmp_path):
    assert render.luminance("#000000") == 0 and render.luminance("#ffffff") == pytest.approx(1)
    files = {"l.png": b"x", "d.png": b"x"}
    both = '[assets]\nlogo_light = "l.png"\nlogo_dark = "d.png"\n'
    dark_bg = _brand(tmp_path, both, files)
    assert render._logo_for(dark_bg).name == "l.png"
    light = kit.load_brand(_kit(tmp_path, GOOD_KIT.replace("#484139", "#fab44b") + both,
                                {"f/X-Bold.ttf": FONT, **files}))
    assert render._logo_for(light).name == "d.png"
    assert render._logo_for(light, on_video=True).name == "l.png"
    only_light = kit.load_brand(_kit(tmp_path, GOOD_KIT.replace("#484139", "#fab44b")
                                     + '[assets]\nlogo_light = "l.png"\n', files))
    assert render._logo_for(only_light).name == "l.png"


def test_ticker_line_repeats_enough(tmp_path):
    camp = kit.load_campaign(_campaign(tmp_path, GOOD_CAMPAIGN))
    brand = _brand(tmp_path)
    short = ass.ticker_line(VERTICAL, brand, camp, 5 * camp.ticker_speed)
    long = ass.ticker_line(VERTICAL, brand, camp, 120 * camp.ticker_speed)
    assert long.count("ONE") > short.count("ONE") >= 2
    unit = (len("ONETWO") * 0.6 + 2 * len(camp.ticker_separator) * 0.6) * VERTICAL.ticker_size
    assert long.count("ONE") * unit >= 120 * camp.ticker_speed
    assert "•" in short and "\\p3" not in short       # text separator, no icon


def test_encoder_selection(monkeypatch):
    assert render.detect_encoder("x264") == "libx264"
    assert render.detect_encoder("nvenc") == "h264_nvenc"
    with pytest.raises(ValueError):
        render.detect_encoder("vp9")

    class R:
        def __init__(self, rc):
            self.returncode = rc

    render._ENCODER.clear()
    monkeypatch.setattr(render.subprocess, "run", lambda *a, **k: R(1))
    assert render.detect_encoder() == "libx264"
    render._ENCODER.clear()
    monkeypatch.setattr(render.subprocess, "run", lambda *a, **k: R(0))
    assert render.detect_encoder() == "h264_nvenc"
    monkeypatch.setattr(render.subprocess, "run", lambda *a, **k: R(1))
    assert render.detect_encoder() == "h264_nvenc"   # cached
    render._ENCODER.clear()
    assert "-cq" in render._video_args("h264_nvenc", 30)
    assert "-crf" in render._video_args("libx264", 25) and "25" in render._video_args(
        "libx264", 25)


def test_loudness_filter_two_pass_and_fallbacks(monkeypatch, tmp_path):
    stats = {"input_i": "-32.30", "input_tp": "-9.07", "input_lra": "8.10",
             "input_thresh": "-43.32", "target_offset": "2.83"}

    class R:
        def __init__(self, stderr):
            self.returncode, self.stderr = 0, stderr

    def run_with(stderr):
        monkeypatch.setattr(render.subprocess, "run", lambda *a, **k: R(stderr))
        return render.loudness_filter(tmp_path / "s.mp4", 1.0, 5.0)

    two_pass = run_with("[Parsed_loudnorm_0] noise\n" + json.dumps(stats) + "\n")
    assert two_pass.startswith(render.LOUDNESS) and ":linear=true" in two_pass
    assert "measured_I=-32.3:" in two_pass and "offset=2.83" in two_pass
    assert run_with("no stats here") == render.LOUDNESS
    assert run_with(json.dumps({**stats, "input_i": "-inf"})) == render.LOUDNESS
    assert run_with(json.dumps({"input_i": "-20"})) == render.LOUDNESS


def test_post_text(tmp_path):
    camp = kit.load_campaign(_campaign(tmp_path, GOOD_CAMPAIGN))
    text = render.post_text({"id": "s1", "title": "T", "hook": "H"}, camp,
                            render.Credits("Ann", "Talk", "Conf 2024"))
    assert text.startswith("# T\n\n\u201cH\u201d")
    assert "Ann \u2014 Talk at Conf 2024" in text
    assert "HEAD\nline 1\nline 2\nhttps://example.org" in text
    assert "x: @x" in text and text.rstrip().endswith("#a #b")
    bare = kit.load_campaign(_campaign(tmp_path, 'name = "N"\n[ticker]\nitems = ["A"]\n'))
    assert render.post_text({"id": "s1"}, bare, render.Credits()) == "# s1\n"


def _clip(**kw):
    clip = {"id": "short-01", "kind": "short", "title": "Hook", "hook": "Hi there",
            "start_s": 3.0, "seek_s": 3.0, "duration_s": 6.0,
            "words": [{"word": w, "start": 0.3 + i * 0.4, "end": 0.6 + i * 0.4}
                      for i, w in enumerate("TCP IP is the universal language.".split())]}
    clip.update(kw)
    return clip


def test_render_clip_captions_only(tmp_path):
    brand = _brand(tmp_path, '[[glossary]]\npattern = "TCP IP"\nreplace = "TCP/IP"\n')
    camp = kit.load_campaign(_campaign(tmp_path, GOOD_CAMPAIGN))
    out = tmp_path / "out"
    m = render.render_clip(_clip(), None, VERTICAL, brand, camp, out, captions_only=True)
    cap = out / "short-01" / "short-01.captions.json"
    assert m["outputs"] == [] and m["captions"] == cap.name
    assert stat.S_IMODE(cap.stat().st_mode) == 0o600
    assert json.loads(cap.read_text())["cues"][0]["text"].startswith("TCP/IP")
    doc = json.loads(cap.read_text())
    doc["cues"][0]["text"] = "Edited by hand."
    cap.write_text(json.dumps(doc))
    render.render_clip(_clip(), None, VERTICAL, brand, camp, out, captions_only=True)
    assert json.loads(cap.read_text())["cues"][0]["text"] == "Edited by hand."   # kept
    render.render_clip(_clip(), None, VERTICAL, brand, camp, out, captions_only=True,
                       recaption=True)
    assert json.loads(cap.read_text())["cues"][0]["text"].startswith("TCP/IP")  # rebuilt
    with pytest.raises(render.RenderError, match="no source video"):
        render.render_clip(_clip(), None, VERTICAL, brand, camp, out)


@pytest.mark.parametrize("bad", ["../x", ".hidden", "a/b"])
def test_render_clip_refuses_unsafe_ids(tmp_path, bad):
    brand = _brand(tmp_path)
    camp = kit.load_campaign(_campaign(tmp_path, GOOD_CAMPAIGN))
    with pytest.raises(render.RenderError, match="unsafe clip id"):
        render.render_clip(_clip(id=bad), None, VERTICAL, brand, camp, tmp_path / "o",
                           captions_only=True)


def test_probe_and_duration_errors(monkeypatch, tmp_path):
    class R:
        def __init__(self, rc, out="", err=""):
            self.returncode, self.stdout, self.stderr = rc, out, err

    monkeypatch.setattr(render.subprocess, "run", lambda *a, **k: R(1, err="bad file"))
    with pytest.raises(render.RenderError, match="could not read"):
        render.probe(tmp_path / "x.mp4")
    monkeypatch.setattr(render.subprocess, "run",
                        lambda *a, **k: R(0, json.dumps({"streams": [{"codec_type": "audio"}]})))
    with pytest.raises(render.RenderError, match="no video stream"):
        render.probe(tmp_path / "x.mp4")
    monkeypatch.setattr(render.subprocess, "run", lambda *a, **k: R(0, "N/A"))
    with pytest.raises(render.RenderError, match="duration"):
        render._duration(tmp_path / "x.mp4")
    monkeypatch.setattr(render.subprocess, "run", lambda *a, **k: R(1, err="x\n" * 30))
    with pytest.raises(render.RenderError, match="ffmpeg failed"):
        render._run(["ffmpeg"], tmp_path)
    monkeypatch.setattr(render.shutil, "which", lambda name: None)
    with pytest.raises(render.RenderError, match="ffmpeg not found"):
        render.require_ffmpeg()


# ── session ─────────────────────────────────────────────────────────────────


def _doc():
    return {"source": {"media": "01S.mp4"}, "clips": [_clip(), _clip(id="long-01", kind="long"),
                                                     "junk"],
            "dropped": [{"title": "weak", "reason": "overlaps"}, "junk"], "error": "boom"}


def test_load_doc_variants(tmp_path, monkeypatch):
    f = tmp_path / "clips.json"
    f.write_text(json.dumps(_doc()))
    assert len(session.load_doc(None, f)["clips"]) == 3
    with pytest.raises(session.ClipsError, match="cannot read"):
        session.load_doc(None, tmp_path / "missing.json")
    f.write_text("[1]")
    with pytest.raises(session.ClipsError, match="not a clips document"):
        session.load_doc(None, f)
    with pytest.raises(session.ClipsError, match="SESSION_ID or --clips-file"):
        session.load_doc(None, None)

    seen = {}

    def fake_get_clips(sid, include_words=False):
        seen.update(sid=sid, words=include_words, team=os.environ.get("VEZIR_TEAM_ID"))
        return _doc()

    from vezir.client import mcp_server
    monkeypatch.setattr(mcp_server, "get_clips", fake_get_clips)
    monkeypatch.setenv("VEZIR_TEAM_ID", "before")
    session.load_doc("01S", None, team="abc")
    assert seen == {"sid": "01S", "words": True, "team": "abc"}
    assert os.environ["VEZIR_TEAM_ID"] == "before"           # restored
    monkeypatch.delenv("VEZIR_TEAM_ID")
    session.load_doc("01S", None, team="abc")
    assert "VEZIR_TEAM_ID" not in os.environ                 # removed again
    session.load_doc("01S", None)
    assert seen["team"] is None


def test_select():
    doc = _doc()
    assert [c["id"] for c in session.select(doc, None)] == ["short-01", "long-01"]
    assert [c["id"] for c in session.select(doc, "long-01, short-01")] == ["long-01", "short-01"]
    with pytest.raises(session.ClipsError, match="unknown clip id"):
        session.select(doc, "short-09")


def test_ensure_source(tmp_path, monkeypatch):
    local = tmp_path / "talk.mp4"
    local.write_bytes(b"v")
    assert session.ensure_source(_doc(), None, local, tmp_path) == local
    with pytest.raises(session.ClipsError, match="source not found"):
        session.ensure_source(_doc(), None, tmp_path / "nope.mp4", tmp_path)
    with pytest.raises(session.ClipsError, match="--source is required"):
        session.ensure_source(_doc(), None, None, tmp_path)
    for media in ("../evil.mp4", ".hidden.mp4", None):
        with pytest.raises(session.ClipsError, match="unusable source media"):
            session.ensure_source({"source": {"media": media}}, "01S", None, tmp_path)
    calls = []

    def fake_get_artifact(sid, name, save_path=None):
        calls.append((sid, name, os.environ.get("VEZIR_TEAM_ID")))
        Path(save_path).parent.mkdir(parents=True, exist_ok=True)
        Path(save_path).write_bytes(b"video")
        return save_path

    from vezir.client import mcp_server
    monkeypatch.setattr(mcp_server, "get_artifact", fake_get_artifact)
    dest = session.ensure_source(_doc(), "01S", None, tmp_path / "out", team="abc")
    assert dest == tmp_path / "out" / "_source" / "01S.mp4" and dest.read_bytes() == b"video"
    session.ensure_source(_doc(), "01S", None, tmp_path / "out", team="abc")
    assert calls == [("01S", "01S.mp4", "abc")]                # downloaded once


def test_summary_table():
    rows = session.summary_table(_doc())
    assert rows[0].startswith("short-01") and "Hook" in rows[0]
    assert "dropped: weak: overlaps" in rows and rows[-1] == "error: boom"
    plain = session.summary_table({"clips": [{"id": "x"}]})
    assert "  - " in plain[0]


# ── CLI ─────────────────────────────────────────────────────────────────────


def _cli(*args):
    from click.testing import CliRunner

    from vezir import cli
    return CliRunner().invoke(cli.main, ["clips", *args])


def test_cli_list_and_kit_path(tmp_path):
    f = tmp_path / "clips.json"
    f.write_text(json.dumps(_doc()))
    res = _cli("list", "--clips-file", str(f))
    assert res.exit_code == 0 and "short-01" in res.output and "dropped: weak" in res.output
    assert _cli("list").exit_code != 0
    res = _cli("kit-path")
    assert res.exit_code == 0 and (Path(res.output.strip()) / "kit.toml").is_file()


def test_cli_render_captions_only_and_failures(tmp_path, monkeypatch):
    brand_dir = _kit(tmp_path, GOOD_KIT, {"f/X-Bold.ttf": FONT})
    camp = _campaign(tmp_path, GOOD_CAMPAIGN.replace("TWO", "TWO ⚡"))
    f = tmp_path / "clips.json"
    f.write_text(json.dumps(_doc()))
    monkeypatch.setattr(kit, "campaign_glyph_gaps", lambda b, c: ["⚡"])
    out = tmp_path / "out"
    base = ["render", "--clips-file", str(f), "--brand", str(brand_dir),
            "--campaign", str(camp), "-o", str(out), "--captions-only"]
    res = _cli(*base)
    assert res.exit_code == 0, res.output
    assert "no glyph for ⚡" in res.stderr
    assert (out / "short-01" / "short-01.captions.json").is_file()
    assert (out / "long-01" / "long-01.captions.json").is_file()
    res = _cli(*base, "--clips", "short-09")
    assert res.exit_code != 0 and "unknown clip id" in res.output

    def boom(*a, **k):
        raise render.RenderError("encoder exploded")

    monkeypatch.setattr(render, "render_clip", boom)
    res = _cli(*base)
    assert res.exit_code != 0 and "encoder exploded" in res.stderr
    assert "2 clip(s) failed" in res.output

    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"clips": []}))
    res = _cli("render", "--clips-file", str(empty), "--brand", str(brand_dir),
               "--campaign", str(camp), "--captions-only")
    assert res.exit_code == 0 and "no clips to render" in res.output


# ── end to end (ffmpeg) ─────────────────────────────────────────────────────


def _ffmpeg_ready() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe")
                and kit.find_font("DejaVu Sans"))


needs_ffmpeg = pytest.mark.skipif(not _ffmpeg_ready(), reason="ffmpeg/ffprobe/DejaVu missing")


def _ff(*args, cwd=None):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True, cwd=cwd)


def _probe_streams(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=codec_type,width,height,r_frame_rate:format=duration", "-of", "json",
         str(path)], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    if not _ffmpeg_ready():
        pytest.skip("ffmpeg/ffprobe/DejaVu missing")
    d = tmp_path_factory.mktemp("media")
    _ff("-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=12", "-f", "lavfi",
        "-i", "sine=f=330:d=12", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
        "-shortest", str(d / "src.mp4"))
    _ff("-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=4", "-c:v", "libx264",
        "-preset", "ultrafast", str(d / "silent.mp4"))
    _ff("-f", "lavfi", "-i", "color=c=white:s=300x120", "-frames:v", "1", str(d / "logo.png"))
    _ff("-f", "lavfi", "-i", "testsrc=s=200x200", "-frames:v", "1", str(d / "pattern.png"))
    _ff("-f", "lavfi", "-i", "color=c=red:s=1080x1920:r=30:d=1.2", "-f", "lavfi",
        "-i", "sine=f=500:d=1.2", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
        "-shortest", str(d / "end.mp4"))
    _ff("-f", "lavfi", "-i", "color=c=blue:s=1280x720:r=30:d=1.0", "-c:v", "libx264",
        "-preset", "ultrafast", str(d / "title.mp4"))
    return d


@needs_ffmpeg
def test_render_end_to_end_neutral(tmp_path, media):
    brand = kit.load_brand(NEUTRAL)
    camp = kit.load_campaign(NEUTRAL / "campaign.example.toml")
    src = render.probe(media / "src.mp4")
    assert (src.width, src.height, src.has_audio) == (640, 360, True)
    out = tmp_path / "out"
    credits = render.Credits("Ann Speaker", "A Talk", "Conf 2024")

    m = render.render_clip(_clip(duration_s=5.0), src, VERTICAL, brand, camp, out,
                           credits=credits, encoder="libx264")
    final = out / "short-01" / "short-01.vertical.mp4"
    info = _probe_streams(final)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert (v["width"], v["height"], v["r_frame_rate"]) == (1080, 1920, "30/1")
    assert any(s["codec_type"] == "audio" for s in info["streams"])
    assert float(info["format"]["duration"]) == pytest.approx(5.0 + VERTICAL.end_card_s, abs=0.15)
    assert m["intro_s"] == 0 and m["outputs"][0] == final.name
    assert stat.S_IMODE(final.stat().st_mode) == 0o600
    assert (out / "short-01" / "short-01.srt").read_text().startswith("1\n00:00:00,300")
    assert json.loads((out / "short-01" / "render.json").read_text())["encoder"] == "libx264"
    assert not [p for p in (out / "short-01").iterdir() if p.name.startswith(".render-")]

    long_clip = _clip(id="long-01", kind="long", duration_s=6.0)
    m = render.render_clip(long_clip, src, LANDSCAPE, brand, camp, out, credits=credits,
                           encoder="libx264")
    info = _probe_streams(out / "long-01" / "long-01.landscape.mp4")
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert (v["width"], v["height"]) == (1920, 1080)
    total = LANDSCAPE.title_card_s + 6.0 + LANDSCAPE.end_card_s
    assert float(info["format"]["duration"]) == pytest.approx(total, abs=0.2)
    assert m["intro_s"] == LANDSCAPE.title_card_s
    assert (out / "long-01" / "long-01.srt").read_text().startswith("1\n00:00:01,800")


@needs_ffmpeg
def test_render_with_assets_designer_cards_and_silent_source(tmp_path, media):
    font = kit.find_font("DejaVu Sans")
    assets = {n: (media / n).read_bytes() for n in ("logo.png", "pattern.png", "end.mp4",
                                                    "title.mp4")}
    body = (GOOD_KIT.replace('family = "X"', 'family = "DejaVu Sans"')
            .replace('bold = "f/X-Bold.ttf"', f'bold = "{font}"')
            + '[assets]\nlogo_light = "logo.png"\npattern = "pattern.png"\n'
            + '[cards]\nend_vertical = "end.mp4"\ntitle_landscape = "title.mp4"\n')
    brand = kit.load_brand(_kit(tmp_path, body, assets))
    camp = kit.load_campaign(_campaign(tmp_path, GOOD_CAMPAIGN))
    out = tmp_path / "out"

    src = render.probe(media / "src.mp4")
    render.render_clip(_clip(duration_s=4.0), src, VERTICAL, brand, camp, out,
                       encoder="libx264")
    info = _probe_streams(out / "short-01" / "short-01.vertical.mp4")
    assert float(info["format"]["duration"]) == pytest.approx(4.0 + 1.2, abs=0.15)

    silent = render.probe(media / "silent.mp4")
    assert silent.has_audio is False
    m = render.render_clip(_clip(id="long-01", kind="long", duration_s=3.0), silent,
                           LANDSCAPE, brand, camp, out,
                           credits=render.Credits(speaker="Ann"), encoder="libx264")
    assert m["intro_s"] == pytest.approx(1.0, abs=0.05)          # designer title card
    info = _probe_streams(out / "long-01" / "long-01.landscape.mp4")
    assert any(s["codec_type"] == "audio" for s in info["streams"])
    assert float(info["format"]["duration"]) == pytest.approx(1.0 + 3.0 + 3.0, abs=0.2)


@needs_ffmpeg
def test_cli_render_end_to_end(tmp_path, media):
    f = tmp_path / "clips.json"
    f.write_text(json.dumps({"source": {"media": "x.mp4"},
                             "clips": [_clip(duration_s=3.0)]}))
    res = _cli("render", "--clips-file", str(f), "--brand", str(NEUTRAL),
               "--campaign", str(NEUTRAL / "campaign.example.toml"),
               "--source", str(media / "src.mp4"), "-o", str(tmp_path / "out"),
               "--encoder", "x264", "--speaker", "Ann")
    assert res.exit_code == 0, res.output
    assert "short-01  vertical" in res.output
    assert (tmp_path / "out" / "short-01" / "short-01.vertical.mp4").is_file()


# ── 0.30.0: svg icons, tilted banner, header lines, stings ──────────────────

from vezir.client.clips import svgpath  # noqa: E402

BOLT = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 20">'
        '<path d="M5 0L10 0L4 20Z"/></svg>')


def test_svgpath_commands_absolute_and_relative():
    segs = svgpath.parse_path("M0 0 10 0 h5 v5 H20 V10 l-5 5 z")
    assert segs[0] == ("m", (0.0, 0.0))
    assert ("l", (10.0, 0.0)) in segs                       # implicit lineto after M
    assert ("l", (15.0, 0.0)) in segs and ("l", (15.0, 5.0)) in segs
    assert ("l", (20.0, 5.0)) in segs and ("l", (20.0, 10.0)) in segs
    assert ("l", (15.0, 15.0)) in segs and segs[-1] == ("l", (0.0, 0.0))   # z closes
    rel = svgpath.parse_path("m1 1 l1 0 m2 2 2 0")
    assert rel == [("m", (1.0, 1.0)), ("l", (2.0, 1.0)), ("m", (4.0, 3.0)),
                   ("l", (6.0, 3.0))]
    assert svgpath.parse_path("M0 0 v1 h1") == [("m", (0.0, 0.0)), ("l", (0.0, 1.0)),
                                                ("l", (1.0, 1.0))]


def test_svgpath_curves():
    c = svgpath.parse_path("M0 0 C1 1 2 1 3 0 S5 -1 6 0")
    assert c[1] == ("b", (1.0, 1.0, 2.0, 1.0, 3.0, 0.0))
    assert c[2] == ("b", (4.0, -1.0, 5.0, -1.0, 6.0, 0.0))    # reflected control point
    rel = svgpath.parse_path("M1 1 c1 1 2 1 3 0 s2 -1 3 0")
    assert rel[1] == ("b", (2.0, 2.0, 3.0, 2.0, 4.0, 1.0))
    assert rel[2] == ("b", (5.0, 0.0, 6.0, 0.0, 7.0, 1.0))
    s_first = svgpath.parse_path("M0 0 S2 2 4 0")             # no previous C: c1 = current
    assert s_first[1][1][:2] == (0.0, 0.0)
    q = svgpath.parse_path("M0 0 Q3 3 6 0 T12 0")
    assert q[1] == ("b", (2.0, 2.0, 4.0, 2.0, 6.0, 0.0))
    assert q[2][1][-2:] == (12.0, 0.0) and q[2][1][1] == pytest.approx(-2.0)
    rq = svgpath.parse_path("m0 0 q3 3 6 0 t6 0")
    assert rq[2][1][-2:] == (12.0, 0.0)
    t_first = svgpath.parse_path("M0 0 T3 0")                 # no previous Q: control = current
    assert t_first[1] == ("b", (0.0, 0.0, 1.0, 0.0, 3.0, 0.0))
    assert svgpath.parse_path("M0 0 Z Z") == [("m", (0.0, 0.0)), ("l", (0.0, 0.0)),
                                               ("l", (0.0, 0.0))]
    assert svgpath.parse_path("Z M0 0") == [("m", (0.0, 0.0))]


@pytest.mark.parametrize(("d", "message"), [
    ("M0 0 A1 1 0 0 1 2 2", "unsupported path command"),
    ("10 10", "must start with a command"),
    ("M0", "missing numbers"),
    ("M0 0 L1 L2 2", "missing numbers"),
    ("M0 0 L1 #2", "characters this parser"),
])
def test_svgpath_rejects(d, message):
    with pytest.raises(svgpath.IconError, match=message):
        svgpath.parse_path(d)


def test_svgpath_load_and_drawing(tmp_path):
    f = tmp_path / "bolt.svg"
    f.write_text(BOLT)
    icon = svgpath.load(f)
    assert icon.aspect == pytest.approx(6 / 20)               # bbox x 4..10, y 0..20
    assert icon.segments[0] == ("m", (pytest.approx(0.05), 0.0))
    d = icon.drawing(40)                                      # 40 px tall, \p3 = x4
    assert d.startswith("m 8 0 l 48 0 l 0 160")
    multi = tmp_path / "multi.svg"
    multi.write_text('<svg xmlns="http://www.w3.org/2000/svg"><g><path d="M0 0L1 1"/>'
                     '<path d="M2 0L3 2"/><rect/></g></svg>')
    assert svgpath.load(multi).aspect == pytest.approx(1.5)


@pytest.mark.parametrize(("body", "message"), [
    ("<svg", "cannot read SVG"),
    ('<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>', "no <path"),
    ('<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0L5 0"/></svg>', "no area"),
    ('<svg xmlns="http://www.w3.org/2000/svg"><g transform="scale(2)">'
     '<path d="M0 0L1 1"/></g></svg>', "transform"),
])
def test_svgpath_load_errors(tmp_path, body, message):
    f = tmp_path / "x.svg"
    f.write_text(body)
    with pytest.raises(svgpath.IconError, match=message):
        svgpath.load(f)
    with pytest.raises(svgpath.IconError, match="cannot read"):
        svgpath.load(tmp_path / "missing.svg")


TICKER_KIT = """
[ticker]
tilt_deg = 5
separator_icon = "bolt.svg"
on_cards = true
[audio]
end = "end.wav"
title = "title.wav"
"""


def test_brand_ticker_and_audio(tmp_path):
    brand = _brand(tmp_path, TICKER_KIT, {"bolt.svg": BOLT.encode(), "end.wav": b"x",
                                          "title.wav": b"x"})
    assert brand.ticker_tilt_deg == 5 and brand.ticker_on_cards is True
    assert brand.ticker_icon is not None and set(brand.audio) == {"end", "title"}
    plain = _brand(tmp_path)
    assert plain.ticker_tilt_deg == 0 and plain.ticker_icon is None and plain.audio == {}


@pytest.mark.parametrize(("extra", "message"), [
    ("[ticker]\nspeed = 3\n", "unknown \\[ticker\\] key"),
    ("[ticker]\ntilt_deg = 40\n", "ticker.tilt_deg"),
    ("[ticker]\non_cards = 1\n", "ticker.on_cards"),
    ('[ticker]\nseparator_icon = "nope.svg"\n', "separator_icon not found"),
    ('[ticker]\nseparator_icon = "bad.svg"\n', "ticker.separator_icon: .*no <path"),
    ('[audio]\nintro = "end.wav"\n', "unknown audio slot"),
    ('[audio]\nend = "missing.wav"\n', "audio.end not found"),
])
def test_brand_ticker_audio_errors(tmp_path, extra, message):
    files = {"f/X-Bold.ttf": FONT, "end.wav": b"x",
             "bad.svg": b'<svg xmlns="http://www.w3.org/2000/svg"/>'}
    with pytest.raises(kit.KitError, match=message):
        kit.load_brand(_kit(tmp_path, GOOD_KIT + extra, files))


def test_campaign_header_lines(tmp_path):
    camp = kit.load_campaign(_campaign(tmp_path, 'header_lines = ["A", "B"]\n' + GOOD_CAMPAIGN))
    assert camp.header_lines == ("A", "B")
    with pytest.raises(kit.KitError, match="at most 3"):
        kit.load_campaign(_campaign(tmp_path, 'header_lines = ["1","2","3","4"]\n'
                                    + GOOD_CAMPAIGN))
    with pytest.raises(kit.KitError, match="header_lines must be a list"):
        kit.load_campaign(_campaign(tmp_path, "header_lines = 3\n" + GOOD_CAMPAIGN))


def test_glyph_gaps_skip_icon_separator_and_check_header(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(kit, "missing_glyphs", lambda font, *texts: seen.extend(texts) or [])
    camp = kit.load_campaign(_campaign(tmp_path, 'header_lines = ["HEAD ✓"]\n'
                                       + GOOD_CAMPAIGN.replace('items = ["ONE", "TWO"]',
                                                               'items = ["ONE", "TWO"]\n'
                                                               'separator = " ⚡ "')))
    kit.campaign_glyph_gaps(_brand(tmp_path), camp)
    assert any("⚡" in t for t in seen) and "HEAD ✓" in seen
    seen.clear()
    iconed = _brand(tmp_path, '[ticker]\nseparator_icon = "bolt.svg"\n',
                    {"bolt.svg": BOLT.encode()})
    kit.campaign_glyph_gaps(iconed, camp)
    assert not any("⚡" in t for t in seen) and "ONE TWO" in seen


def test_tilt_budget():
    assert 5.0 < VERTICAL.max_ticker_tilt() < 6.0
    assert LANDSCAPE.max_ticker_tilt() == 0 and LANDSCAPE.ticker_tilt_for(5) == 0
    assert VERTICAL.ticker_tilt_for(5) == 5
    assert VERTICAL.ticker_tilt_for(12) == pytest.approx(VERTICAL.max_ticker_tilt())
    assert VERTICAL.ticker_tilt_for(-12) == pytest.approx(-VERTICAL.max_ticker_tilt())
    # At the maximum tilt the band stays between the captions and the safe bottom.
    import math
    t = math.radians(VERTICAL.max_ticker_tilt())
    center = VERTICAL.ticker.y + VERTICAL.ticker.h / 2
    reach = VERTICAL.ticker.h / 2 + VERTICAL.width / 2 * math.tan(t)
    assert center - reach >= VERTICAL.captions.bottom - 1e-6
    assert center + reach <= VERTICAL.safe.bottom + 1e-6
    squeezed = profiles.Profile(**{**VERTICAL.__dict__, "captions": Box(0, 0, 10, 1440)})
    assert squeezed.max_ticker_tilt() == 0


def test_header_layout_inside_safe_area():
    assert VERTICAL.header_logo is not None and VERTICAL.safe.contains(VERTICAL.header_logo)
    x, y = VERTICAL.header_text
    assert VERTICAL.header_logo.right < x <= VERTICAL.safe.right
    assert VERTICAL.header_logo.y <= y <= VERTICAL.header_logo.bottom
    assert LANDSCAPE.header_text is None


def test_ticker_events_tilt_icon_and_speed(tmp_path):
    brand = _brand(tmp_path, TICKER_KIT, {"bolt.svg": BOLT.encode(), "end.wav": b"x",
                                          "title.wav": b"x"})
    camp = kit.load_campaign(_campaign(tmp_path, GOOD_CAMPAIGN))
    styles, (band, text) = ass.ticker_events(VERTICAL, brand, camp, 2.0, 12.0,
                                             center_y=1412, tilt=5)
    assert any(s.startswith("Style: Band,") for s in styles)
    assert "\\frz5" in band and "\\org(540,1412)" in band and "\\p1}m 0 0 l 1512 0" in band
    assert "\\q2" in text and "\\frz5" in text and "{\\p3}m " in text
    travel = camp.ticker_speed * 10
    assert f"\\move(1080,1412,{round(1080 - travel)},1412,0,10000)" in text
    _, (_, full) = ass.ticker_events(VERTICAL, brand, camp, 0, 3, center_y=400, tilt=5,
                                     prefilled=True)
    assert "\\move(0,400," in full
    _, (flat, _) = ass.ticker_events(LANDSCAPE, brand, camp, 0, 3, center_y=900, tilt=0)
    assert "\\frz" not in flat


def test_main_script_ticker_and_header(tmp_path):
    brand = _brand(tmp_path)
    camp = kit.load_campaign(_campaign(tmp_path, 'header_lines = ["26 FEB", "CITY"]\n'
                                       + GOOD_CAMPAIGN))
    vert = ass.main_script(VERTICAL, brand, [], campaign=camp, duration=10.0, tilt=5)
    assert ",Band," in vert and ",Ticker," in vert and "\\frz5" in vert
    assert "Dialogue: 1,0:00:00.00,0:00:10.00,Header,,0,0,0,,{\\pos(960,330)}26 FEB\\NCITY" in vert
    short = ass.main_script(VERTICAL, brand, [], campaign=camp, duration=1.5)
    assert ",Band," not in short                                  # ends before start_s
    land = ass.main_script(LANDSCAPE, brand, [], campaign=camp, duration=10.0)
    assert ",Header," not in land and "\\frz" not in land


def test_card_script_band_only_when_enabled(tmp_path):
    camp = kit.load_campaign(_campaign(tmp_path, GOOD_CAMPAIGN))
    plain = ass.card_script(VERTICAL, _brand(tmp_path), 3.0, "H", ["l"], text_top=900,
                            campaign=camp)
    assert ",Band," not in plain
    on = _brand(tmp_path, "[ticker]\ntilt_deg = 5\non_cards = true\n")
    card = ass.card_script(LANDSCAPE, on, 3.0, "H", ["l"], text_top=470, campaign=camp)
    assert ",Band," in card and "\\frz5" in card and "\\move(0,960," in card


def test_card_duration():
    assert render.card_duration(3.0, None, "end") == 3.0
    assert render.card_duration(3.0, 1.5, "end") == 3.0
    assert render.card_duration(3.0, 4.0, "end") == pytest.approx(4.3)
    assert render.card_duration(3.0, 9.0, "end") == 5.0
    assert render.card_duration(1.5, 2.0, "title") == pytest.approx(2.3)
    assert render.card_duration(1.5, 9.0, "title") == 3.0


def _mean_volume(path: Path, start: float, duration: float) -> float:
    out = subprocess.run(["ffmpeg", "-nostats", "-ss", str(start), "-t", str(duration),
                          "-i", str(path), "-vn", "-af", "volumedetect", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    return float(next(ln for ln in out.splitlines() if "mean_volume" in ln).split()[-2])


@needs_ffmpeg
def test_render_end_to_end_tilt_icon_header_stings(tmp_path, media):
    font = kit.find_font("DejaVu Sans")
    _ff("-f", "lavfi", "-i", "sine=f=880:d=4.0", "-ac", "2", str(tmp_path / "end.wav"))
    _ff("-f", "lavfi", "-i", "sine=f=660:d=0.8", str(tmp_path / "title.wav"))
    body = (GOOD_KIT.replace('family = "X"', 'family = "DejaVu Sans"')
            .replace('bold = "f/X-Bold.ttf"', f'bold = "{font}"')
            + '[assets]\nlogo_light = "logo.png"\n'
            + TICKER_KIT.replace('"end.wav"', f'"{tmp_path / "end.wav"}"')
            .replace('"title.wav"', f'"{tmp_path / "title.wav"}"'))
    brand = kit.load_brand(_kit(tmp_path, body, {"bolt.svg": BOLT.encode(),
                                                 "logo.png": (media / "logo.png").read_bytes()}))
    camp = kit.load_campaign(_campaign(tmp_path, 'header_lines = ["26 FEB", "CITY"]\n'
                                       + GOOD_CAMPAIGN))
    src = render.probe(media / "src.mp4")
    out = tmp_path / "out"

    m = render.render_clip(_clip(duration_s=4.0), src, VERTICAL, brand, camp, out,
                           encoder="libx264")
    final = out / "short-01" / "short-01.vertical.mp4"
    total = float(_probe_streams(final)["format"]["duration"])
    assert total == pytest.approx(4.0 + 4.3, abs=0.15)            # end card follows the sting
    assert m["ticker_tilt_deg"] == 5 and m["stings"] == ["end", "title"]
    assert _mean_volume(final, 4.2, 3.0) > -40                    # the sting plays on the card

    m = render.render_clip(_clip(id="long-01", kind="long", duration_s=3.0), src, LANDSCAPE,
                           brand, camp, out, credits=render.Credits("Ann", "Talk"),
                           encoder="libx264")
    assert m["ticker_tilt_deg"] == 0 and m["intro_s"] == LANDSCAPE.title_card_s
    total = float(_probe_streams(out / "long-01" / "long-01.landscape.mp4")["format"]["duration"])
    assert total == pytest.approx(1.5 + 3.0 + 4.3, abs=0.2)


def test_run_retries_once_after_a_crash(monkeypatch, tmp_path):
    codes = iter([-11, 0])
    calls = []

    class R:
        def __init__(self, rc):
            self.returncode, self.stderr = rc, "boom"

    def fake(*a, **k):
        calls.append(1)
        return R(next(codes))

    monkeypatch.setattr(render.subprocess, "run", fake)
    render._run(["ffmpeg"], tmp_path)
    assert len(calls) == 2
    monkeypatch.setattr(render.subprocess, "run", lambda *a, **k: R(-11))
    with pytest.raises(render.RenderError, match="killed by signal 11"):
        render._run(["ffmpeg"], tmp_path)
    calls.clear()
    monkeypatch.setattr(render.subprocess, "run", lambda *a, **k: calls.append(1) or R(1))
    with pytest.raises(render.RenderError, match="exit 1"):
        render._run(["ffmpeg"], tmp_path)
    assert len(calls) == 1                     # a reported error is not retried
