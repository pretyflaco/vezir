#!/usr/bin/env python3
"""Repair a no-headphones meeting transcript polluted by speaker→mic crosstalk.

Problem
-------
When a meeting is recorded without headphones, the remote participants' audio
played through the room speakers bleeds into the local microphone (the left /
"mic" channel of the stereo recording).  millet's default ``dual-diarize``
transcription path labels the *entire* mic channel as the local speaker
("YOU" → e.g. "Kemal"), so the bled remote speech gets misattributed to the
local speaker.

Why segment energy alone is not enough
--------------------------------------
Without headphones the system channel is loud underneath the local speaker too,
so the per-segment mic/(mic+sys) energy ratio of genuine local speech (0.4-1.0)
overlaps the ratio of bled remote speech (0.3-0.55).  The reliable signals are:

* **Voiceprint identity** — compare each mic-channel segment's embedding against
  the team's speaker profiles (Kemal vs Kim vs Jonas …).  This directly answers
  "whose voice is this?" rather than guessing from energy.
* **Word-level channel energy** — a genuine local sentence with a remote
  interjection splits cleanly at word boundaries (local words mic-dominant,
  interjection words system-dominant); split it instead of dropping the whole.
* **Energy + remote-overlap fallback** — when the embedding is too weak/short to
  trust, drop-to-remote only when the segment is word-level system-dominant AND
  temporally overlaps an active remote segment.

Bled segments are *relabeled to the matched remote speaker* (not deleted).

Artifacts rewritten (friendly vezir names):
    transcript.json, transcript.srt, transcript.txt, transcript.pdf, summary.md
Originals are backed up to ``*.orig`` (unless they already exist).  The pristine
``transcript.json.orig`` is used as the source when present so re-runs don't
compound an earlier pass.

Usage
-----
    python scripts/repair_crosstalk.py SESSION_DIR \
        --profiles PATH/speaker_profiles.json [--you-label Kemal] \
        [--vp-margin 0.05] [--no-summary] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

# Friendly local artifact names (see vezir/client/artifacts.py).
TRANSCRIPT_JSON = "transcript.json"
TRANSCRIPT_SRT = "transcript.srt"
TRANSCRIPT_TXT = "transcript.txt"
TRANSCRIPT_PDF = "transcript.pdf"
SUMMARY_MD = "summary.md"

MIN_EMBED_SECONDS = 0.5     # below this a clip is too short to embed
MIN_EMBED_RMS = 1e-3        # below this a clip is too quiet to embed


def _find_audio(session_dir: Path) -> Path:
    """Locate the stereo source recording (.ogg preferred, then .wav)."""
    for ext in ("*.ogg", "*.wav"):
        hits = sorted(session_dir.glob(ext))
        if hits:
            return hits[0]
    raise FileNotFoundError(f"no .ogg/.wav audio found in {session_dir}")


def _decode_channels(audio: Path, sr: int = 16000):
    """Decode *audio* to (mic, system) float arrays at *sr* Hz (L=mic, R=sys).

    Returns (mic, system, sample_rate, n_samples).  Uses a real temp WAV file
    so the header carries the correct frame count.
    """
    import numpy as np

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        subprocess.run(
            [
                "ffmpeg", "-v", "error", "-y",
                "-i", str(audio),
                "-ar", str(sr), "-ac", "2",
                "-c:a", "pcm_s16le",
                str(tmp_path),
            ],
            check=True,
        )
        with wave.open(str(tmp_path)) as wf:
            n = wf.getnframes()
            ch = wf.getnchannels()
            raw = np.frombuffer(wf.readframes(n), dtype=np.int16)
        if ch != 2:
            raise ValueError(f"expected stereo audio, got {ch} channel(s)")
        raw = raw.reshape(-1, 2).astype(np.float32)
        # mic for embeddings normalised to [-1, 1]; raw kept for RMS ratios.
        mic = raw[:, 0]
        system = raw[:, 1]
        return mic, system, sr, len(mic)
    finally:
        tmp_path.unlink(missing_ok=True)


def _backup(path: Path) -> None:
    """Copy *path* to ``path.orig`` once (never clobber an existing backup)."""
    if not path.exists():
        return
    bak = path.with_suffix(path.suffix + ".orig")
    if not bak.exists():
        shutil.copy2(path, bak)


def _load_profiles(path: Path):
    """Load {name: np.ndarray(embedding)} from a speaker_profiles.json."""
    import numpy as np

    data = json.loads(path.read_text(encoding="utf-8"))
    out = {}
    for name, info in data.items():
        emb = info.get("embedding")
        if emb:
            v = np.asarray(emb, dtype=np.float32)
            out[name] = v / (np.linalg.norm(v) + 1e-9)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("session_dir", type=Path)
    ap.add_argument(
        "--profiles", type=Path, default=None,
        help="Path to the team speaker_profiles.json (enables voiceprint "
        "reassignment). Without it, only the energy+overlap fallback is used.",
    )
    ap.add_argument(
        "--you-label", default="Kemal",
        help="Local-speaker label on the mic channel (default: Kemal).",
    )
    ap.add_argument(
        "--vp-margin", type=float, default=0.05,
        help="Min cosine margin by which the best voiceprint match must beat "
        "the local-speaker score to reassign a segment (default 0.05).",
    )
    ap.add_argument(
        "--vp-floor", type=float, default=0.18,
        help="Min absolute cosine for a voiceprint match to be trusted. Below "
        "this the segment is routed to the energy+overlap fallback "
        "(default 0.18). Degraded bleed embeddings score near 0.",
    )
    ap.add_argument(
        "--word-sys-frac", type=float, default=0.60,
        help="Fallback: fraction of a segment's words that must be "
        "system-dominant to treat it as bleed (default 0.60).",
    )
    ap.add_argument(
        "--you-starts-at", type=float, default=None,
        help="Authoritative override: seconds before which the local speaker "
        "did NOT talk. Any local-labeled segment starting before this is "
        "reassigned to its overlapping remote (or generic REMOTE). Use when "
        "you know exactly when the local speaker first joined/spoke.",
    )
    ap.add_argument(
        "--no-summary", action="store_true",
        help="Skip regenerating summary.md (transcript artifacts only).",
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="Report what would change without writing any files.",
    )
    args = ap.parse_args()

    session_dir: Path = args.session_dir
    if not session_dir.is_dir():
        print(f"error: not a directory: {session_dir}", file=sys.stderr)
        return 2

    # Prefer the pristine pre-repair source so re-runs don't compound.
    src_json = session_dir / "transcript.json.orig"
    if not src_json.exists():
        src_json = session_dir / TRANSCRIPT_JSON
    if not src_json.exists():
        print(f"error: no transcript.json[.orig] in {session_dir}", file=sys.stderr)
        return 2
    print(f"Source transcript: {src_json.name}")

    import numpy as np
    from millet.transcribe import (
        Segment,
        Speaker,
        Transcript,
        _seg_channel_ratio,
    )

    data = json.loads(src_json.read_text(encoding="utf-8"))
    segments_raw = data.get("segments", [])
    you = args.you_label

    audio = _find_audio(session_dir)
    print(f"Audio:             {audio.name}  (decoding stereo...)")
    mic, system, sr, n = _decode_channels(audio)
    print(f"Decoded:           {n} samples @ {sr} Hz ({n / sr:.1f}s)")

    remote_segs_all = [s for s in segments_raw if s.get("speaker") != you]

    # Speakers actually present in THIS meeting (mic label + diarized remotes).
    present_speakers = {you}
    present_speakers.update(
        s.get("speaker") for s in remote_segs_all if s.get("speaker")
    )

    # ── Optional voiceprint setup ──
    # Restrict candidate profiles to speakers PRESENT in this meeting.  Matching
    # a degraded bleed embedding against all ~20 team profiles picks essentially
    # random absent names; restricting to present speakers keeps it meaningful.
    profiles = {}
    inference = None
    if args.profiles and args.profiles.exists():
        all_profiles = _load_profiles(args.profiles)
        profiles = {
            name: emb for name, emb in all_profiles.items()
            if name in present_speakers
        }
        if you not in profiles:
            print(f"  warning: no voiceprint for local speaker {you!r}; "
                  "voiceprint reassignment disabled", file=sys.stderr)
            profiles = {}
        if profiles:
            try:
                from millet.voiceprint import _get_inference

                inference = _get_inference()
                print(f"Voiceprints:       {len(profiles)} present-speaker "
                      f"profiles ({', '.join(sorted(profiles))}), model loaded")
            except Exception as exc:
                print(f"Voiceprints:       model load failed ({exc}); "
                      "using energy fallback only", file=sys.stderr)
                inference = None
    if inference is None:
        print("Voiceprints:       disabled (energy+overlap fallback only)")

    mic_norm = mic / 32768.0  # pyannote expects [-1, 1]

    def _embed(start: float, end: float):
        if inference is None:
            return None
        a = max(0, int(start * sr))
        b = min(int(end * sr), n)
        clip = mic_norm[a:b]
        if len(clip) < int(sr * MIN_EMBED_SECONDS):
            return None
        if float(np.sqrt(np.mean(clip ** 2))) < MIN_EMBED_RMS:
            return None
        try:
            import torch

            v = np.asarray(
                inference(
                    {"waveform": torch.tensor(clip).unsqueeze(0), "sample_rate": sr}
                )
            ).flatten()
            norm = np.linalg.norm(v)
            if norm < 1e-9:
                return None
            return v / norm
        except Exception:
            return None

    def _best_match(emb):
        """Return (best_name, best_cos, you_cos) over all profiles."""
        scores = {name: float(np.dot(emb, p)) for name, p in profiles.items()}
        you_cos = scores.get(you, -1.0)
        best_name = max(scores, key=scores.get)
        return best_name, scores[best_name], you_cos

    def _word_sys_fraction(seg):
        flags = []
        for w in seg.get("words") or []:
            r = _seg_channel_ratio(mic, system, w.get("start"), w.get("end"), sr, n)
            if r is not None:
                flags.append(r < 0.5)
        if not flags:
            return None
        return sum(flags) / len(flags)

    def _overlapping_remote(seg):
        """Return the remote speaker with the largest time overlap, or None."""
        best = None
        best_ov = 0.3  # require > 0.3s overlap
        for r in remote_segs_all:
            ov = min(seg["end"], r["end"]) - max(seg["start"], r["start"])
            if ov > best_ov:
                best_ov = ov
                best = r.get("speaker")
        return best

    # Generic remote target when we can't name the specific one.
    remote_names = [r.get("speaker") for r in remote_segs_all if r.get("speaker")]
    generic_remote = "REMOTE"
    for cand in ("REMOTE", *sorted(set(remote_names))):
        if cand:
            generic_remote = cand
            break

    # ── Classify / reassign every local ("Kemal") segment ──
    out_raw: list[dict] = []          # rebuilt segment dicts (all speakers)
    you_total = 0
    counts = {"kept": 0, "vp_remote": 0, "fallback_remote": 0,
              "split": 0, "empty": 0}

    def _norm(t):
        import re
        return re.sub(r"[^a-z0-9 ]", "", (t or "").lower()).strip()

    for seg in segments_raw:
        if seg.get("speaker") != you:
            out_raw.append(seg)
            continue
        you_total += 1

        if not _norm(seg.get("text", "")):
            counts["empty"] += 1
            continue  # drop empty/punctuation mic artifacts

        # Fallback reassignment target = the remote speaker actually overlapping
        # this segment in time (else generic REMOTE).
        overlap_remote = _overlapping_remote(seg)
        fallback_target = overlap_remote or generic_remote

        # 0) Authoritative boundary override: local speaker hadn't spoken yet.
        if args.you_starts_at is not None and seg["start"] < args.you_starts_at:
            s2 = dict(seg)
            s2["speaker"] = fallback_target
            out_raw.append(s2)
            counts["fallback_remote"] += 1
            continue

        emb = _embed(seg["start"], seg["end"])

        # ── Decide the segment's identity by voiceprint (restricted to present
        #    speakers, with an absolute-cosine floor so degraded bleed
        #    embeddings — cosine ~0 — are NOT trusted). ──
        vp_is_local = False     # confident this segment is the local speaker
        vp_remote_name = None   # confident this segment is a specific remote
        if emb is not None and profiles:
            best_name, best_cos, you_cos = _best_match(emb)
            if best_cos >= args.vp_floor:
                if best_name == you:
                    vp_is_local = True
                elif best_cos - you_cos >= args.vp_margin:
                    vp_remote_name = best_name

        # 1) Confident remote voiceprint -> reassign whole segment.
        if vp_remote_name is not None:
            s2 = dict(seg)
            s2["speaker"] = vp_remote_name
            out_raw.append(s2)
            counts["vp_remote"] += 1
            continue

        # 2) Confident LOCAL voiceprint -> genuine Kemal.  Keep the segment
        #    whole: the voiceprint already confirms it is the local speaker, and
        #    word-level splitting on noisy energy over-fragments real sentences
        #    (e.g. a tail the mic captured at lower gain).
        if vp_is_local:
            out_raw.append(seg)
            counts["kept"] += 1
            continue

        # 3) No confident local identity.  Treat as bleed when it is
        #    system-dominant AND overlaps an active remote; reassign the WHOLE
        #    segment to that remote (do NOT word-split — bleed word energy is
        #    noisy and would spawn spurious local fragments).
        wsf = _word_sys_fraction(seg)
        seg_ratio = _seg_channel_ratio(mic, system, seg["start"], seg["end"], sr, n)
        is_sys = (
            (wsf is not None and wsf >= args.word_sys_frac)
            or (seg_ratio is not None and seg_ratio < 0.45)
        )
        if overlap_remote is not None and is_sys:
            s2 = dict(seg)
            s2["speaker"] = fallback_target
            out_raw.append(s2)
            counts["fallback_remote"] += 1
        else:
            out_raw.append(seg)
            counts["kept"] += 1

    kept_you = sum(1 for s in out_raw if s.get("speaker") == you)
    print()
    print(f"Local ({you}) segments in:  {you_total}")
    print(f"  kept as {you}:           {counts['kept']}")
    print(f"  voiceprint -> remote:     {counts['vp_remote']}")
    print(f"  fallback  -> remote:      {counts['fallback_remote']}")
    print(f"  word-split (mixed):       {counts['split']}")
    print(f"  dropped (empty):          {counts['empty']}")
    print(f"Resulting {you} segments:   {kept_you}")
    print(f"Total segments:             {len(out_raw)}")

    if args.dry_run:
        print("\n[dry-run] no files written.")
        # Show the 0:00-8:40 region for review.
        print("\n=== 00:00-08:40 region (after) ===")
        for s in sorted(out_raw, key=lambda x: x["start"]):
            if s["start"] > 525:
                break
            mm = int(s["start"] // 60)
            ss = s["start"] % 60
            mark = "  <<<" if s.get("speaker") == you else ""
            print(f"  [{mm:02d}:{ss:05.2f}] {s.get('speaker'):8s} "
                  f"{(s.get('text') or '').strip()[:55]!r}{mark}")
        return 0

    # ── Rebuild Transcript ──
    new_segments = [
        Segment(
            start=s["start"], end=s["end"], text=s.get("text", ""),
            speaker=s.get("speaker"), words=s.get("words"),
        )
        for s in sorted(out_raw, key=lambda x: x["start"])
    ]
    present = {s.speaker for s in new_segments if s.speaker}
    # Preserve original speaker order, then any newly introduced labels.
    new_speakers = [
        Speaker(id=sp["id"], label=sp.get("label"))
        for sp in data.get("speakers", [])
        if sp["id"] in present
    ]
    known = {sp.id for sp in new_speakers}
    for lbl in sorted(present - known):
        new_speakers.append(Speaker(id=lbl, label=lbl))

    transcript = Transcript(
        segments=new_segments,
        speakers=new_speakers,
        language=data.get("language", "en"),
        audio_file=data.get("audio_file", audio.name),
        duration=data.get("duration"),
    )

    for name in (
        TRANSCRIPT_JSON, TRANSCRIPT_SRT, TRANSCRIPT_TXT,
        TRANSCRIPT_PDF, SUMMARY_MD, "frontmatter.json",
    ):
        _backup(session_dir / name)

    tmp_base = "._repair_tmp"
    files = transcript.save(session_dir, basename=tmp_base)
    for key, friendly in (
        ("json", TRANSCRIPT_JSON), ("srt", TRANSCRIPT_SRT), ("text", TRANSCRIPT_TXT),
    ):
        src = files.get(key)
        if src and Path(src).exists():
            shutil.move(str(src), str(session_dir / friendly))
    print(f"\nWrote {TRANSCRIPT_JSON}, {TRANSCRIPT_SRT}, {TRANSCRIPT_TXT}")

    summary_result = None
    if not args.no_summary:
        try:
            from millet.frontmatter import context_from_transcript
            from millet.summarize import SummaryConfig
            from millet.summarize import summarize as do_summarize

            print("Regenerating summary...")
            summary_result = do_summarize(
                transcript.to_text(), SummaryConfig(),
                language=transcript.language,
                progress_callback=lambda m: print(f"  {m}"),
            )
            fm_ctx = context_from_transcript(transcript, session_dir)
            sm_path = summary_result.save(
                session_dir, tmp_base, frontmatter_context=fm_ctx
            )
            if Path(sm_path).exists():
                shutil.move(str(sm_path), str(session_dir / SUMMARY_MD))
            fm_src = session_dir / f"{tmp_base}.frontmatter.json"
            if fm_src.exists():
                shutil.move(str(fm_src), str(session_dir / "frontmatter.json"))
            for stray in session_dir.glob(f"{tmp_base}*"):
                stray.unlink(missing_ok=True)
            print(f"Wrote {SUMMARY_MD}")
        except Exception as exc:
            print(f"  Summary regeneration failed: {exc}", file=sys.stderr)

    try:
        from millet.pdf import generate_pdf

        generate_pdf(
            transcript, session_dir / TRANSCRIPT_PDF,
            summary=summary_result, language=transcript.language,
        )
        print(f"Wrote {TRANSCRIPT_PDF}")
    except Exception as exc:
        print(f"  PDF generation failed: {exc}", file=sys.stderr)

    print("\nRepair complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
