"""Render branded clips with ffmpeg.

Each clip becomes up to three segments, encoded with identical parameters
and joined by stream copy: an optional title card, the clip itself, and an
end card.  The clip segment is one ffmpeg pass:

    brand background (+ pattern) -> source video fitted into the profile's
    video box -> logo -> one libass overlay: captions, the banner (band +
    scrolling text + separator icons, tilted where the profile allows it,
    from ``ticker.start_s``), header lines, lower third -> fades, with the
    audio loudness-normalized for social platforms (-14 LUFS).

Cards carry the kit's stings (``[audio]``) instead of silence.

Every input ffmpeg sees is staged into a private work directory under a
fixed ASCII name (``logo.png``, ``main.ass``, ``end-sting.wav`` ...) and
ffmpeg runs there, so no user path ever has to be escaped inside a
filtergraph.
"""
from __future__ import annotations

import json
import logging
import math
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ... import __version__, config
from . import ass, captions
from .kit import Brand, Campaign
from .profiles import Box, Profile, fit

log = logging.getLogger("vezir.client.clips.render")

LOUDNESS = "loudnorm=I=-14:TP=-1.5:LRA=11"
_MEASURED = ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")
AUDIO_ARGS = ["-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2"]
FADE_S = 0.25
STING_FADE_S = 0.3
MAX_CARD_S = {"title": 3.0, "end": 5.0}   # a sting may lengthen a card up to this
_LOGO_CARD = {"vertical": Box(190, 560, 700, 260), "landscape": Box(610, 170, 700, 220)}
_CARD_TEXT_TOP = {"vertical": 960, "landscape": 470}


class RenderError(RuntimeError):
    pass


@dataclass(frozen=True)
class Source:
    path: Path
    width: int
    height: int
    has_audio: bool


@dataclass(frozen=True)
class Credits:
    speaker: str | None = None
    talk: str | None = None
    recorded_at: str | None = None


def _run(cmd: list[str], cwd: Path) -> None:
    """Run ffmpeg; one retry if a signal killed it.

    A negative exit code is a native crash (seen once in ~100 renders: SIGSEGV
    inside Ubuntu's ffmpeg 6.1 / libass, not reproducible on replay), never an
    error ffmpeg reported about its input, so the same command is tried once
    more.  Real ffmpeg errors (positive exit) fail immediately.
    """
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if proc.returncode < 0:
        log.warning("ffmpeg was killed by signal %d; retrying once", -proc.returncode)
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-12:])
        reason = (f"killed by signal {-proc.returncode}" if proc.returncode < 0
                  else f"exit {proc.returncode}")
        raise RenderError(f"ffmpeg failed ({reason}):\n{tail}")


def require_ffmpeg() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            raise RenderError(f"{tool} not found on PATH")


def probe(path: Path) -> Source:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height",
         "-of", "json", str(path)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RenderError(f"ffprobe could not read {path}: {proc.stderr.strip()[-300:]}")
    streams = json.loads(proc.stdout or "{}").get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not video or not video.get("width") or not video.get("height"):
        raise RenderError(f"{path} has no video stream")
    return Source(
        path=path, width=int(video["width"]), height=int(video["height"]),
        has_audio=any(s.get("codec_type") == "audio" for s in streams),
    )


_ENCODER: dict[str, str] = {}


def detect_encoder(preference: str = "auto") -> str:
    """``libx264`` or ``h264_nvenc`` (auto: NVENC only if a test encode works)."""
    if preference == "x264":
        return "libx264"
    if preference == "nvenc":
        return "h264_nvenc"
    if preference != "auto":
        raise ValueError(f"unknown encoder {preference!r}; expected auto, nvenc or x264")
    if "auto" not in _ENCODER:
        ok = subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=s=256x256:d=0.1",
             "-c:v", "h264_nvenc", "-f", "null", "-"],
            capture_output=True,
        ).returncode == 0
        _ENCODER["auto"] = "h264_nvenc" if ok else "libx264"
    return _ENCODER["auto"]


def loudness_filter(source: Path, seek: float, duration: float) -> str:
    """Two-pass loudnorm: measure the clip's audio, then normalize linearly.

    Single-pass loudnorm adapts gain on the fly and lands speech 1-3 LU
    under target; the measured second pass hits -14 LUFS.  Falls back to
    single pass when the measurement can't be read.
    """
    proc = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-ss", f"{seek:.3f}", "-t", f"{duration:.3f}",
         "-i", str(source), "-vn", "-af", f"{LOUDNESS}:print_format=json", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    start = proc.stderr.rfind("{")
    try:
        stats = json.loads(proc.stderr[start:proc.stderr.rfind("}") + 1])
        values = {k: float(stats[k]) for k in _MEASURED}
    except (ValueError, KeyError, TypeError):
        return LOUDNESS
    if not all(math.isfinite(v) for v in values.values()):
        return LOUDNESS
    return (
        f"{LOUDNESS}:measured_I={values['input_i']}:measured_TP={values['input_tp']}"
        f":measured_LRA={values['input_lra']}:measured_thresh={values['input_thresh']}"
        f":offset={values['target_offset']}:linear=true"
    )


def _video_args(encoder: str, fps: int) -> list[str]:
    quality = (
        ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "21", "-b:v", "0"]
        if encoder == "h264_nvenc"
        else ["-c:v", "libx264", "-preset", "medium", "-crf", "20"]
    )
    return [*quality, "-profile:v", "high", "-pix_fmt", "yuv420p", "-r", str(fps),
            "-video_track_timescale", str(fps * 1000)]


def _hex(c: str) -> str:
    return "0x" + c[1:]


def _background(profile: Profile, brand: Brand, duration: float, pattern_input: int | None,
                label: str) -> list[str]:
    w, h = profile.width, profile.height
    parts = [f"color=c={_hex(brand.colors['background'])}:s={w}x{h}:r={profile.fps}"
             f":d={duration:.3f},format=yuv420p[bg0]"]
    if pattern_input is None:
        parts.append(f"[bg0]null[{label}]")
    else:
        parts.append(
            f"[{pattern_input}:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h},format=rgba,colorchannelmixer=aa={brand.pattern_opacity:.3f}[pat]"
        )
        parts.append(f"[bg0][pat]overlay=0:0[{label}]")
    return parts


def _logo(box: Box, logo_input: int, base: str, label: str) -> list[str]:
    return [
        f"[{logo_input}:v]scale=w={box.w}:h={box.h}:force_original_aspect_ratio=decrease,"
        "format=rgba[logo]",
        f"[{base}][logo]overlay=x='{box.x}+({box.w}-overlay_w)/2'"
        f":y='{box.y}+({box.h}-overlay_h)/2'[{label}]",
    ]


def card_duration(profile_s: float, sting_s: float | None, slot: str) -> float:
    """A card lasts its profile length, or longer to let a sting finish."""
    if sting_s is None:
        return profile_s
    return max(profile_s, min(sting_s + STING_FADE_S, MAX_CARD_S[slot]))


def luminance(hex_rgb: str) -> float:
    """Relative luminance (WCAG) of ``#rrggbb``, 0 = black, 1 = white."""
    def chan(c: str) -> float:
        v = int(c, 16) / 255
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (chan(hex_rgb[i:i + 2]) for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _logo_for(brand: Brand, *, on_video: bool = False) -> Path | None:
    """The logo that contrasts with what it sits on.

    Over footage (a landscape watermark) the light logo reads best; on the
    brand background (vertical canvas, cards) it depends on that color.
    """
    if not on_video and luminance(brand.colors["background"]) > 0.4:
        return brand.logo_dark or brand.logo_light
    return brand.logo_light or brand.logo_dark


class _Work:
    """A private scratch directory with every input under a fixed name."""

    def __init__(self, parent: Path) -> None:
        config.secure_mkdir(parent)
        self.dir = Path(tempfile.mkdtemp(prefix=".render-", dir=parent))

    def link(self, name: str, target: Path) -> str:
        path = self.dir / name
        if not path.is_symlink():
            path.symlink_to(target)
        return name

    def write(self, name: str, text: str) -> str:
        (self.dir / name).write_text(text, encoding="utf-8")
        return name

    def close(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)


def _main_segment(
    work: _Work, out: str, clip: dict, source: Source, profile: Profile, brand: Brand,
    campaign: Campaign, cues: list[captions.Cue], credits: Credits, encoder: str, *,
    fade_in: bool,
) -> None:
    """The clip itself; it always fades out into the end card."""
    duration = float(clip["duration_s"])
    seek = float(clip.get("seek_s", clip["start_s"]))
    inputs = ["-ss", f"{seek:.3f}", "-t", f"{duration:.3f}", "-i", str(source.path)]
    n = 1
    pattern_input = logo_input = None
    if brand.pattern is not None:
        inputs += ["-i", work.link("pattern.png", brand.pattern)]
        pattern_input, n = n, n + 1
    logo_box = (profile.header_logo if campaign.header_lines and profile.header_logo
                else profile.logo)
    logo = _logo_for(brand, on_video=profile.video.contains(logo_box))
    if logo is not None:
        inputs += ["-i", work.link("logo.png", logo)]
        logo_input = n

    vw, vh, vx, vy = fit(source.width, source.height, profile.video)
    graph = _background(profile, brand, duration, pattern_input, "bg")
    graph.append(f"[0:v]scale={vw}:{vh},setsar=1,fps={profile.fps}[vid]")
    graph.append(f"[bg][vid]overlay={vx}:{vy}:shortest=1[c0]")
    base = "c0"
    if logo_input is not None:
        graph += _logo(logo_box, logo_input, base, "c1")
        base = "c1"

    work.link("fonts", brand.fonts_dir)
    work.write("main.ass", ass.main_script(
        profile, brand, cues, campaign=campaign, duration=duration,
        tilt=profile.ticker_tilt_for(brand.ticker_tilt_deg),
        speaker=credits.speaker, talk=credits.talk,
    ))
    video_chain = []
    lt = profile.lower_third
    if lt is not None and (credits.speaker or credits.talk):
        # Translucent panel behind the lower third: talk footage is busy.
        t0, t1 = profile.lower_third_s
        pad = 24
        width = min(lt.w, ass.lower_third_width(profile, credits.speaker, credits.talk))
        video_chain.append(
            f"drawbox=x={lt.x - pad}:y={lt.y - pad // 2}:w={width + 2 * pad}:h={lt.h}"
            f":color={_hex(brand.colors['background'])}@0.72:t=fill"
            f":enable='between(t,{t0:.2f},{t1:.2f})'"
        )
    video_chain.append("ass=main.ass:fontsdir=fonts")
    audio_chain = [
        loudness_filter(source.path, seek, duration) if source.has_audio else LOUDNESS,
        "aresample=48000",
    ]
    if fade_in:
        video_chain.append(f"fade=t=in:st=0:d={FADE_S}")
        audio_chain.append(f"afade=t=in:st=0:d={FADE_S}")
    video_chain.append(f"fade=t=out:st={duration - FADE_S:.3f}:d={FADE_S}")
    audio_chain.append(f"afade=t=out:st={duration - FADE_S:.3f}:d={FADE_S}")
    graph.append(f"[{base}]" + ",".join(video_chain) + "[v]")
    if source.has_audio:
        graph.append("[0:a]" + ",".join(audio_chain) + "[a]")
    else:
        graph.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{duration:.3f}[a]")
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-y", *inputs,
           "-filter_complex", ";".join(graph), "-map", "[v]", "-map", "[a]",
           *_video_args(encoder, profile.fps), *AUDIO_ARGS, "-t", f"{duration:.3f}", out]
    _run(cmd, work.dir)


def _card_segment(
    work: _Work, out: str, name: str, profile: Profile, brand: Brand, duration: float,
    headline: str, lines: list[str], encoder: str, *, campaign: Campaign | None = None,
    sting: Path | None = None, band: bool = False,
) -> float:
    """Generated card; returns its duration.

    With a *sting*, it plays from the card's first frame and fades out at its
    end; otherwise the card is silent.  ``band``: end cards carry the
    campaign banner when the kit asks for it.
    """
    inputs, n = [], 0
    pattern_input = logo_input = None
    if brand.pattern is not None:
        inputs += ["-i", work.link(f"{name}-pattern.png", brand.pattern)]
        pattern_input, n = n, n + 1
    logo = _logo_for(brand)
    if logo is not None:
        inputs += ["-i", work.link(f"{name}-logo.png", logo)]
        logo_input = n
    graph = _background(profile, brand, duration, pattern_input, "bg")
    base = "bg"
    if logo_input is not None:
        graph += _logo(_LOGO_CARD[profile.name], logo_input, base, "c1")
        base = "c1"
    work.link("fonts", brand.fonts_dir)
    script = work.write(f"{name}.ass", ass.card_script(
        profile, brand, duration, headline, lines, text_top=_CARD_TEXT_TOP[profile.name],
        campaign=campaign, band=band,
    ))
    graph.append(
        f"[{base}]ass={script}:fontsdir=fonts,fade=t=in:st=0:d={FADE_S},"
        f"fade=t=out:st={duration - FADE_S:.3f}:d={FADE_S}[v]"
    )
    if sting is not None:
        n = len([x for x in inputs if x == "-i"])
        inputs += ["-i", work.link(f"{name}-sting{sting.suffix.lower()}", sting)]
        graph.append(
            f"[{n}:a]aresample=48000,aformat=channel_layouts=stereo,apad,"
            f"atrim=0:{duration:.3f},afade=t=out:st={duration - STING_FADE_S:.3f}"
            f":d={STING_FADE_S}[a]"
        )
    else:
        graph.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{duration:.3f}[a]")
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-y", *inputs,
           "-filter_complex", ";".join(graph), "-map", "[v]", "-map", "[a]",
           *_video_args(encoder, profile.fps), *AUDIO_ARGS, "-t", f"{duration:.3f}", out]
    _run(cmd, work.dir)
    return duration


def _designer_card(work: _Work, out: str, name: str, path: Path, profile: Profile,
                   encoder: str, *, brand: Brand | None = None,
                   campaign: Campaign | None = None, band: bool = False) -> float:
    """Conform a designer-supplied motion file to the profile; returns its duration.

    The motion file keeps its own audio.  ``band`` (end cards) burns the same
    campaign banner over it that a generated end card gets.
    """
    src = probe(path)
    duration = _duration(path)
    w, h = profile.width, profile.height
    inputs = ["-i", work.link(f"{name}-motion{path.suffix.lower()}", path)]
    overlay = ""
    script = (ass.card_band_script(profile, brand, campaign, duration)
              if band and brand is not None else None)
    if script is not None and brand is not None:
        work.link("fonts", brand.fonts_dir)
        overlay = f",ass={work.write(f'{name}-band.ass', script)}:fontsdir=fonts"
    graph = [f"[0:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
             f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={profile.fps}{overlay}[v]"]
    if src.has_audio:
        graph.append("[0:a]aresample=48000[a]")
    else:
        graph.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{duration:.3f}[a]")
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-y", *inputs,
           "-filter_complex", ";".join(graph), "-map", "[v]", "-map", "[a]",
           *_video_args(encoder, profile.fps), *AUDIO_ARGS, "-t", f"{duration:.3f}", out]
    _run(cmd, work.dir)
    return duration


def _duration(path: Path) -> float:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
         str(path)], capture_output=True, text=True,
    )
    try:
        return float(proc.stdout.strip())
    except ValueError:
        raise RenderError(f"ffprobe could not read the duration of {path}") from None


def post_text(clip: dict, campaign: Campaign, credits: Credits) -> str:
    """Suggested social post copy (deterministic template, no LLM)."""
    lines = [f"# {clip.get('title') or clip.get('id')}", ""]
    if clip.get("hook"):
        lines += [f"\u201c{clip['hook']}\u201d", ""]
    who = " \u2014 ".join(x for x in (credits.speaker, credits.talk) if x)
    if who or credits.recorded_at:
        lines.append(" at ".join(x for x in (who, credits.recorded_at) if x))
        lines.append("")
    if campaign.end_headline:
        lines.append(campaign.end_headline)
    lines += list(campaign.end_lines)
    if campaign.url:
        lines.append(campaign.url)
    if campaign.handles:
        lines += ["", " ".join(f"{k}: {v}" for k, v in campaign.handles.items())]
    if campaign.hashtags:
        lines += ["", " ".join(campaign.hashtags)]
    return "\n".join(lines).rstrip() + "\n"


def render_clip(
    clip: dict, source: Source | None, profile: Profile, brand: Brand, campaign: Campaign,
    out_dir: Path, *, credits: Credits | None = None, encoder: str = "libx264",
    captions_only: bool = False, recaption: bool = False,
) -> dict[str, Any]:
    """Render one clip; returns the manifest (also written as ``render.json``)."""
    credits = credits or Credits()
    clip_id = str(clip["id"])
    if config.safe_path_component(clip_id) is None or clip_id.startswith("."):
        raise RenderError(f"refusing unsafe clip id {clip_id!r}")
    clip_dir = out_dir / clip_id
    config.secure_mkdir(clip_dir)
    duration = float(clip["duration_s"])

    cap_path = clip_dir / f"{clip_id}.captions.json"
    if cap_path.exists() and not recaption:
        cues = captions.load(cap_path, profile.caption_max_chars)
    else:
        rules = (*brand.glossary, *campaign.glossary)
        cues = captions.build_cues(captions.words_from_clip(clip), profile.caption_max_chars,
                                   rules, clip_end=duration)
        config.secure_write_text(cap_path, captions.dumps(cues, clip_id))

    title_slot = f"title_{profile.name}"
    end_slot = f"end_{profile.name}"
    has_title = profile.title_card_s > 0 or title_slot in brand.cards
    manifest: dict[str, Any] = {
        "clip": clip_id, "profile": profile.name, "kind": clip.get("kind"),
        "vezir": __version__, "kit": {"name": brand.name, "digest": brand.digest},
        "campaign": {"name": campaign.name, "digest": campaign.digest},
        "source": {"seek_s": clip.get("seek_s", clip.get("start_s")), "duration_s": duration},
        "captions": cap_path.name, "outputs": [],
    }
    if captions_only:
        return manifest
    if source is None:
        raise RenderError("no source video to render from")

    work = _Work(clip_dir)
    try:
        segments: list[str] = []
        stings_used: list[str] = []
        intro = 0.0
        if title_slot in brand.cards:
            intro = _designer_card(work, "title.mp4", "title", brand.cards[title_slot],
                                   profile, encoder)
            segments.append("title.mp4")
        elif has_title:
            title_lines = [x for x in (credits.speaker, credits.recorded_at) if x]
            sting = brand.audio.get("title")
            intro = _card_segment(
                work, "title.mp4", "title", profile, brand,
                card_duration(profile.title_card_s, _duration(sting) if sting else None,
                              "title"),
                credits.talk or str(clip.get("title") or ""), title_lines, encoder,
                campaign=campaign, sting=sting,
            )
            stings_used += ["title"] if sting else []
            segments.append("title.mp4")
        _main_segment(work, "main.mp4", clip, source, profile, brand, campaign, cues, credits,
                      encoder, fade_in=bool(segments))
        segments.append("main.mp4")
        if end_slot in brand.cards:
            _designer_card(work, "end.mp4", "end", brand.cards[end_slot], profile, encoder,
                           brand=brand, campaign=campaign, band=True)
        else:
            sting = brand.audio.get("end")
            _card_segment(
                work, "end.mp4", "end", profile, brand,
                card_duration(profile.end_card_s, _duration(sting) if sting else None, "end"),
                campaign.end_headline, list(campaign.end_lines), encoder,
                campaign=campaign, sting=sting, band=True,
            )
            stings_used += ["end"] if sting else []
        segments.append("end.mp4")

        work.write("segments.txt", "".join(f"file '{s}'\n" for s in segments))
        final = clip_dir / f"{clip_id}.{profile.name}.mp4"
        _run(["ffmpeg", "-nostdin", "-hide_banner", "-y", "-f", "concat", "-safe", "0",
              "-i", "segments.txt", "-c", "copy", "-movflags", "+faststart", "final.mp4"],
             work.dir)
        (work.dir / "final.mp4").replace(final)
        config.secure_chmod_file(final)
    finally:
        work.close()

    srt = clip_dir / f"{clip_id}.srt"
    vtt = clip_dir / f"{clip_id}.vtt"
    config.secure_write_text(srt, captions.to_srt(cues, offset=intro))
    config.secure_write_text(vtt, captions.to_vtt(cues, offset=intro))
    post = clip_dir / "post.md"
    config.secure_write_text(post, post_text(clip, campaign, credits))
    manifest.update({
        "encoder": encoder, "intro_s": intro,
        "ticker_tilt_deg": profile.ticker_tilt_for(brand.ticker_tilt_deg),
        # The stings that actually played: shorts have no title card, and a
        # designer motion card keeps its own audio.
        "stings": sorted(stings_used),
        "outputs": [final.name, srt.name, vtt.name, post.name],
    })
    config.secure_write_text(clip_dir / "render.json",
                             json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    return manifest
