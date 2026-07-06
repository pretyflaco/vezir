"""Queue worker: drains the job queue, runs millet pipeline.

Single-worker, single-job-at-a-time. Runs in a background thread inside
the FastAPI process. For larger deployments this would split into a
separate process; for v0 we keep it simple.

Pipeline per job:
  1. transcribe (millet transcribe <session-dir>) — produces .txt/.srt/.json/.summary.md/.pdf
  2. label --auto (millet label --auto --no-audio --no-summary <session-dir>)
        — applies confident voiceprint matches, leaves unknowns as REMOTE_N
  3. detect unknowns:
        if all speakers identified → status=syncing → millet sync → status=done
        else → status=needs_labeling → wait for human via web UI
  4. on completion (whether after auto or after human labeling), audio
     WAV is deleted to honor the storage policy.
"""
from __future__ import annotations

import logging
import os
import queue as _pyqueue
import re
import subprocess
import threading
import time
from pathlib import Path

from .. import config
from . import meet_runner, queue

log = logging.getLogger("vezir.worker")

POLL_INTERVAL_SEC = 2.0


# ── Follow-up task queue (v0.11.0) ──────────────────────────────────────────
#
# Retroactive sync, retry-summary, and post-labeling finalize used to be
# fired as ad-hoc daemon threads straight from request handlers.  Those
# threads raced the single background worker AND each other: two threads
# could process the same session concurrently, both mutating job status
# and the shared per-job HOME shim (which each `finally` rmtree'd out
# from under the other).  They are now serialized through the SAME
# worker thread as the transcription pipeline, restoring the
# single-writer model.  Tasks are in-process (not persisted): a restart
# drops pending tasks, which is the same behavior a killed thread had —
# the user just re-clicks.  Duplicate requests for a session with an
# identical task already pending/running are dropped (idempotent).

_TASKS: _pyqueue.Queue = _pyqueue.Queue()
_ACTIVE_TASKS: set[tuple[str, str]] = set()
_TASKS_LOCK = threading.Lock()

_TASK_KINDS = ("sync", "retry_summary", "finalize_labels")


def enqueue_task(kind: str, session_id: str, **kwargs) -> bool:
    """Queue a follow-up task for the single worker thread.

    Returns False (and does nothing) when an identical ``(kind,
    session_id)`` task is already pending or running — a double-clicked
    "Sync now" no longer spawns two concurrent syncs of one session.
    """
    if kind not in _TASK_KINDS:
        raise ValueError(f"unknown task kind: {kind!r}")
    key = (kind, session_id)
    with _TASKS_LOCK:
        if key in _ACTIVE_TASKS:
            log.info("task %s already pending for session %s; dropped", kind, session_id)
            return False
        _ACTIVE_TASKS.add(key)
    _TASKS.put((kind, session_id, kwargs))
    return True


def _run_task(kind: str, session_id: str, kwargs: dict) -> None:
    try:
        if kind == "sync":
            finalize_after_labeling(session_id, kwargs.get("meeting_type"))
        elif kind == "retry_summary":
            retry_summary_for_session(
                session_id,
                preset_override=kwargs.get("preset_override"),
                language_override=kwargs.get("language_override"),
            )
        elif kind == "finalize_labels":
            _finalize_labels_task(session_id, kwargs.get("label_map") or {})
    except Exception:
        log.exception("task %s failed for session %s", kind, session_id)
    finally:
        with _TASKS_LOCK:
            _ACTIVE_TASKS.discard((kind, session_id))


def _drain_tasks() -> None:
    """Run every pending follow-up task (worker thread only)."""
    while True:
        try:
            kind, session_id, kwargs = _TASKS.get_nowait()
        except _pyqueue.Empty:
            return
        log.info("running task %s for session %s", kind, session_id)
        _run_task(kind, session_id, kwargs)


def _skip_sync() -> bool:
    """If VEZIR_SKIP_SYNC is set to a truthy value, skip the millet sync step.

    Useful for early dogfood / pilot when no team-wide sync target has been
    decided yet. The artifacts remain in ~/vezir-data/sessions/<id>/.
    """
    return os.environ.get("VEZIR_SKIP_SYNC", "").lower() in ("1", "true", "yes")


def _delete_audio_enabled() -> bool:
    """Per pilot policy, audio deletion is OFF by default.

    Set VEZIR_DELETE_AUDIO=1 once the pilot is stable to enforce the
    'delete after artifacts produced' storage policy.
    """
    return os.environ.get("VEZIR_DELETE_AUDIO", "").lower() in ("1", "true", "yes")


def _tiny_speaker_thresholds() -> tuple[float, int]:
    """Thresholds below which an unresolved raw speaker is treated as noise.

    The dual-diarize path can split a backchannel one-liner or a distorted
    blip on the system channel into its own ``REMOTE``/``SPEAKER_n`` cluster
    that voiceprint never matches.  A single such cluster otherwise forces an
    otherwise-clean session into ``needs_labeling``.  We ignore any unresolved
    raw speaker with ``<=`` this much speech AND ``<=`` this many segments.

    Defaults (5.0s, 3 segments) match the millet-side ``absorb_tiny_speakers``
    constants.  Override with ``VEZIR_TINY_SPEAKER_MAX_SECONDS`` /
    ``VEZIR_TINY_SPEAKER_MAX_SEGMENTS``.
    """
    try:
        secs = float(os.environ.get("VEZIR_TINY_SPEAKER_MAX_SECONDS", "5.0"))
    except ValueError:
        secs = 5.0
    try:
        segs = int(os.environ.get("VEZIR_TINY_SPEAKER_MAX_SEGMENTS", "3"))
    except ValueError:
        segs = 3
    return secs, segs


def _speaker_segment_stats(data: dict) -> dict[str, tuple[int, float]]:
    """Per-speaker ``(segment_count, total_speech_seconds)`` from a transcript.

    Reads the flat ``segments`` array (each ``{start, end, speaker, ...}``).
    Used to distinguish a substantial unlabeled participant (needs a human)
    from a tiny noise cluster (safe to ignore).
    """
    stats: dict[str, tuple[int, float]] = {}
    for seg in data.get("segments", []) or []:
        sid = seg.get("speaker") or ""
        if not sid:
            continue
        try:
            dur = max(0.0, float(seg.get("end", 0.0)) - float(seg.get("start", 0.0)))
        except (TypeError, ValueError):
            dur = 0.0
        count, total = stats.get(sid, (0, 0.0))
        stats[sid] = (count + 1, total + dur)
    return stats


# `millet sync` exits 0 even on git clone/push failures (it catches the
# RuntimeError and just prints a warning). Vezir scans the log tail for
# these markers to detect silent failures.
_SYNC_FAILURE_MARKERS = (
    "fatal:",
    "Could not resolve host",
    "Authentication failed",
    "Sync failed",
    "Command failed: git",
    "Permission denied",
)


def _sync_log_indicates_failure(log_path: Path) -> str | None:
    """Scan the most recent `millet sync` block of the log for failure markers.

    Only inspects lines after the most recent '--- ... millet sync' marker,
    so prior stanzas (transcribe, label) don't bleed in.

    Returns the matched marker line if a failure was found, else None.
    """
    if not log_path.exists():
        return None
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    # Find last sync block
    idx = text.rfind("millet sync ")
    tail = text[idx:] if idx >= 0 else text
    for line in tail.splitlines():
        for marker in _SYNC_FAILURE_MARKERS:
            if marker in line:
                return line.strip()
    return None


# How many trailing log bytes to capture into the queue's `error` field
# when a `meet ...` subprocess fails. Helps debugging from the dashboard.
_ERROR_TAIL_BYTES = 2048


def _last_log_lines(log_path: Path, n_bytes: int = _ERROR_TAIL_BYTES) -> str:
    """Return the last ~n_bytes of a log file, line-aligned.

    Used to decorate the `error` field with the actual failure message
    (e.g. ValueError, traceback summary) instead of just an exit code.
    """
    if not log_path.exists():
        return ""
    try:
        size = log_path.stat().st_size
        with log_path.open("rb") as f:
            f.seek(max(0, size - n_bytes))
            tail = f.read().decode("utf-8", errors="replace")
    except Exception:
        return ""
    # Drop a possibly-truncated leading line.
    if "\n" in tail and len(tail) >= n_bytes:
        tail = tail.split("\n", 1)[1]
    # Trim trailing whitespace / blank lines.
    return tail.strip()


def _error_with_tail(prefix: str, log_path: Path) -> str:
    tail = _last_log_lines(log_path)
    if not tail:
        return prefix
    return f"{prefix}\n--- last lines of log ---\n{tail}"


_SUMMARY_ERROR_RE = re.compile(
    r"summary failed for preset '([^']+)':\s*(.*)",
    re.IGNORECASE,
)


def _extract_summary_error(log_path: Path) -> str | None:
    """Extract a human-readable summary error from the job log.

    Scans the last ~2 KiB for millet's preset-guard RuntimeError
    message (e.g. "summary failed for preset 'confidential': ...").
    Returns the full matched line, or None if not found.
    """
    tail = _last_log_lines(log_path)
    if not tail:
        return None
    for line in reversed(tail.splitlines()):
        m = _SUMMARY_ERROR_RE.search(line)
        if m:
            return line.strip()
        # Also catch the tinfoil-specific error directly.
        if "Failed to fetch router addresses" in line:
            return line.strip()
        if "Summary failed:" in line:
            return line.strip()
    return None


def _session_dir(session_id: str) -> Path:
    return config.sessions_dir() / session_id


def _job_log_path(session_id: str) -> Path:
    return config.logs_dir() / f"{session_id}.log"


def _find_artifacts(session_dir: Path) -> dict:
    """Map artifact type -> filename (relative to session_dir)."""
    out: dict = {}
    for p in sorted(session_dir.glob("*.txt")):
        out["txt"] = p.name
        break
    for p in sorted(session_dir.glob("*.srt")):
        out["srt"] = p.name
        break
    # Primary auto-detected summary: <base>.summary.md (a language-tagged
    # <base>.summary.<lang>.md does NOT match this glob — the ".md" must
    # directly follow ".summary").
    for p in sorted(session_dir.glob("*.summary.md")):
        out["summary"] = p.name
        break
    # Additional per-language summaries: <base>.summary.<lang>.md.  Exposed as
    # separate artifact keys (e.g. "summary_de") so the original is preserved.
    for p in sorted(session_dir.glob("*.summary.*.md")):
        if p.name.endswith(".meta.json"):  # defensive; .md glob can't match
            continue
        # Extract the language code between ".summary." and ".md".
        stem = p.name[: -len(".md")]           # <base>.summary.<lang>
        lang = stem.rsplit(".summary.", 1)[-1]  # <lang>
        if lang and "." not in lang:
            out[f"summary_{lang}"] = p.name
    for p in sorted(session_dir.glob("*.pdf")):
        out["pdf"] = p.name
        break
    for p in sorted(session_dir.glob("*.json")):
        if any(
            marker in p.name
            for marker in (
                ".session.", ".summary.", ".translation.",
                ".frontmatter.", ".autoid.",
            )
        ):
            continue
        out["json"] = p.name
        break
    return out


_UNRESOLVED_RE = re.compile(r"^(YOU|REMOTE(?:_\d+)?|SPEAKER_\d+)$")


def _is_tiny_speaker(sid: str, stats: dict[str, tuple[int, float]]) -> bool:
    """True if speaker ``sid`` is a spurious tiny noise cluster.

    A speaker counts as tiny when its total speech and segment count are both
    at/below the configured thresholds (see :func:`_tiny_speaker_thresholds`).
    Such clusters are backchannel one-liners or distorted blips, not a real
    participant that warrants a human labeling round.
    """
    max_secs, max_segs = _tiny_speaker_thresholds()
    count, total = stats.get(sid, (0, 0.0))
    return total <= max_secs and count <= max_segs


def _has_unresolved_speakers(session_dir: Path) -> bool:
    """True if a *substantial* speaker label still looks auto-generated.

    Uses the JSON transcript to inspect the actual speaker IDs after the
    --auto labeling pass.  An unresolved raw placeholder
    (``YOU``/``REMOTE``/``REMOTE_N``/``SPEAKER_N``) that is *tiny* — a noise
    blip or one-line backchannel below the
    :func:`_tiny_speaker_thresholds` limits — is ignored, so a single
    unmatchable noise cluster no longer forces the whole session into
    ``needs_labeling``.  A substantial unlabeled participant still does.

    Fix for #6: previously glob'd for ``*.json`` and skipped known
    non-transcript suffixes, but ``.frontmatter.json`` (and any future
    sidecar) slipped through and was treated as the transcript.
    Now positively selects ``<session_dir.name>.json`` — the canonical
    transcript filename that millet produces.
    """
    import json as _json

    tj = session_dir / f"{session_dir.name}.json"
    if not tj.exists():
        return False  # no transcript yet; treat as resolved (caller will surface error)

    try:
        data = _json.loads(tj.read_text(encoding="utf-8"))
    except Exception:
        return False

    stats = _speaker_segment_stats(data)
    speakers = data.get("speakers", []) or []
    for sp in speakers:
        sid = sp.get("id") or ""
        label = sp.get("label") or ""
        # If no label set, fall back to id which will likely be a placeholder.
        effective = label if label else sid
        if not _UNRESOLVED_RE.match(effective):
            continue
        # Ignore spurious tiny noise clusters; only a substantial unlabeled
        # speaker forces needs_labeling.
        if _is_tiny_speaker(sid, stats):
            continue
        return True
    return False


def _speaker_resolution(session_dir: Path) -> tuple[list[str], list[str]]:
    """Return (matched_names, unresolved_ids) from the transcript.

    ``matched_names`` are confirmed human labels; ``unresolved_ids`` are the
    raw placeholders (``YOU``/``REMOTE``/``SPEAKER_N``) still needing a name.
    Tiny noise clusters (see :func:`_is_tiny_speaker`) are not reported as
    unresolved — they're ignored exactly as in :func:`_has_unresolved_speakers`
    — so ``vezir relabel`` reporting stays consistent with the routing
    decision.  Used by :func:`reauto_label_session` for per-session CLI
    reporting.
    """
    import json as _json

    tj = session_dir / f"{session_dir.name}.json"
    if not tj.exists():
        return [], []
    try:
        data = _json.loads(tj.read_text(encoding="utf-8"))
    except Exception:
        return [], []

    stats = _speaker_segment_stats(data)
    matched: list[str] = []
    unresolved: list[str] = []
    for sp in data.get("speakers", []) or []:
        sid = sp.get("id") or ""
        label = sp.get("label") or ""
        effective = label if label else sid
        if _UNRESOLVED_RE.match(effective):
            if _is_tiny_speaker(sid, stats):
                continue
            unresolved.append(effective)
        elif effective:
            matched.append(effective)
    return matched, unresolved


def _delete_audio(session_dir: Path) -> None:
    """Optionally delete audio (.wav, .ogg, .mp3) after artifacts are produced.

    Disabled by default during the pilot (see _delete_audio_enabled()).
    """
    if not _delete_audio_enabled():
        log.debug("audio deletion disabled (VEZIR_DELETE_AUDIO not set)")
        return
    for pattern in ("*.wav", "*.ogg", "*.mp3", "*.part-*"):
        for f in session_dir.glob(pattern):
            try:
                f.unlink()
                log.info("deleted audio: %s", f)
            except Exception as exc:
                log.warning("could not delete %s: %s", f, exc)


def _merge_multi_audio(session_dir: Path, job_id: str, log_path: Path) -> None:
    """Concatenate ``<id>.part-NNN<ext>`` files into the canonical ``<id><ext>``.

    A multi-audio meeting (v0.9.0) lands as several ordered part files (one
    upload per Telegram voicenote, etc.).  Before transcribe we stitch them,
    in filename order, into the single audio file millet expects.

    Uses ffmpeg's concat demuxer with ``-c copy`` (all parts share a codec, so
    this is a fast remux); falls back to re-encoding to Opus if the stream copy
    fails (e.g. mismatched parameters).  Idempotent: a single part is renamed,
    and a no-part dir is left untouched.
    """
    parts = sorted(session_dir.glob(f"{job_id}.part-*"))
    if not parts:
        return  # not a multi-audio session (or already merged)

    ext = parts[0].suffix
    out = session_dir / f"{job_id}{ext}"

    log.info("merging %d audio part(s) for session %s -> %s",
             len(parts), job_id, out.name)

    if len(parts) == 1:
        parts[0].replace(out)
        config.secure_chmod_file(out)
        return

    # Write a concat list file (ffmpeg concat demuxer, safe mode off so we can
    # use absolute paths).  Single-quote-escape per ffmpeg's syntax.
    concat_list = session_dir / f"{job_id}.concat.txt"
    lines = []
    for p in parts:
        esc = str(p).replace("'", r"'\''")
        lines.append(f"file '{esc}'")
    config.secure_write_text(concat_list, "\n".join(lines) + "\n")

    copy_cmd = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", str(concat_list), "-c", "copy", str(out),
    ]
    reencode_cmd = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", str(concat_list), "-c:a", "libopus", "-b:a", "48k", str(out),
    ]

    def _run(cmd: list[str]) -> int:
        with log_path.open("a", encoding="utf-8") as lf:
            lf.write(f"\n[merge] {' '.join(cmd)}\n")
            lf.flush()
            proc = subprocess.run(cmd, stdout=lf, stderr=subprocess.STDOUT)
        return proc.returncode

    rc = _run(copy_cmd)
    if rc != 0 or not out.exists() or out.stat().st_size == 0:
        log.warning(
            "concat -c copy failed for %s (rc=%s); re-encoding to opus",
            job_id, rc,
        )
        out.unlink(missing_ok=True)
        rc = _run(reencode_cmd)
        if rc != 0 or not out.exists() or out.stat().st_size == 0:
            raise RuntimeError(
                f"ffmpeg failed to merge {len(parts)} audio parts "
                f"for session {job_id} (rc={rc}); see {log_path}"
            )

    config.secure_chmod_file(out)
    concat_list.unlink(missing_ok=True)
    for p in parts:
        p.unlink(missing_ok=True)
    log.info("merged session %s into %s (%d bytes)",
             job_id, out.name, out.stat().st_size)


def process_one(job: dict) -> None:
    """Run the full pipeline for one claimed job."""
    job_id = job["id"]
    sd = _session_dir(job_id)
    log_path = _job_log_path(job_id)
    # v0.6.2+: every job row carries team_id (enforced at enqueue +
    # backfilled by the v0.6.0 migration).  The meet_runner needs it
    # for the per-team HOME-shim wiring.
    team_id = job.get("team_id") or ""
    if not team_id:
        # Defensive: a job row with empty team_id is a migration bug.
        # Fail the job rather than silently letting it pick up the
        # wrong team's voiceprint DB.
        queue.update_status(
            job_id, "error",
            error=(
                f"job {job_id} has empty team_id; refusing to run "
                f"(v0.6.0 migration may have been skipped)"
            ),
        )
        return

    try:
        # 0. multi-audio: stitch ordered part files into one canonical audio
        # file before transcribe.  Gated on the job flag, but the helper also
        # no-ops when no part files are present, so a re-run is safe.
        if job.get("multi_audio"):
            try:
                _merge_multi_audio(sd, job_id, log_path)
            except Exception as exc:
                queue.update_status(
                    job_id, "error",
                    error=f"audio merge failed: {exc}",
                )
                return

        # 1. transcribe
        requested_preset = job.get("summary_preset")
        rc = meet_runner.transcribe(
            sd, job_id, team_id, log_path,
            summary_preset=requested_preset,
        )

        # Distinguish between transcription failures and summary-only
        # failures.  When `millet transcribe` exits non-zero *and* a preset
        # was requested, the failure may be summary-only (the preset guard
        # in millet raises RuntimeError after the transcript is already
        # on disk).  If we have transcript artifacts (.txt, .json), the
        # transcription itself succeeded and we should treat this as a
        # partial success: mark the job `done` with a `summary_error` so
        # the user gets their transcript and can retry the summary later.
        summary_err_msg: str | None = None
        sync_err_msg: str | None = None

        if rc != 0:
            has_transcript = bool(list(sd.glob("*.txt"))) and bool(list(sd.glob("*.json")))
            if requested_preset and has_transcript:
                # Transcription OK, summary failed.  Treat as partial
                # success: extract the summary error from the log tail,
                # stash it in summary_error, and continue the pipeline.
                summary_err_msg = _extract_summary_error(log_path) or (
                    f"summary failed for preset '{requested_preset}' (exit {rc})"
                )
                log.warning(
                    "job %s: transcription OK but summary failed: %s",
                    job_id, summary_err_msg,
                )
            else:
                # Genuine transcription failure.
                queue.update_status(
                    job_id, "error",
                    error=_error_with_tail(f"millet transcribe exited {rc}", log_path),
                )
                return

        # Belt-and-suspenders: if a preset was explicitly requested but no
        # summary file ended up on disk AND we haven't already captured a
        # summary error, record it as a summary_error (not a hard error).
        if requested_preset and not list(sd.glob("*.summary.md")) and not summary_err_msg:
            summary_err_msg = _extract_summary_error(log_path) or (
                f"preset '{requested_preset}' requested but no summary was generated"
            )

        # 2. label --auto against central voiceprint DB (per-job opt-out)
        # The job row's auto_label_enabled flag is set at upload time from
        # the client's checkbox / switch.  When False, skip auto-labeling
        # entirely so every session lands in needs_labeling for human
        # review.  This is a privacy / control choice some users prefer.
        auto_label_enabled = bool(job.get("auto_label_enabled", 1))
        if auto_label_enabled:
            rc = meet_runner.label_auto(sd, job_id, team_id, log_path)
            if rc != 0:
                log.warning("label --auto returned %s; continuing", rc)
        else:
            log.info(
                "job %s: auto_label_enabled=0; skipping label --auto",
                job_id,
            )

        artifacts = _find_artifacts(sd)

        # 3. unresolved speakers?
        if _has_unresolved_speakers(sd):
            queue.update_status(
                job_id, "needs_labeling", artifacts=artifacts,
                summary_error=summary_err_msg,
            )
            log.info("job %s needs labeling", job_id)
            return

        # 4. sync to git.  Two independent gates both must allow sync:
        #    - VEZIR_SKIP_SYNC env var (operator-side kill switch)
        #    - per-job sync_enabled flag (user-side opt-out at upload)
        sync_enabled = bool(job.get("sync_enabled", 1))
        if _skip_sync():
            log.info("job %s: VEZIR_SKIP_SYNC set, skipping millet sync", job_id)
        elif not sync_enabled:
            log.info(
                "job %s: sync_enabled=0; keeping session local-only",
                job_id,
            )
        elif not meet_runner.team_has_sync_target(team_id):
            # No team-scoped git remote configured: keep the session local-only
            # instead of invoking millet, which would fall back to its
            # placeholder remote and fail (0.8.10).  Not an error.
            log.info(
                "job %s: team %s has no sync remote; keeping session local-only",
                job_id, team_id,
            )
        else:
            queue.update_status(job_id, "syncing", artifacts=artifacts)
            rc = meet_runner.sync(sd, job_id, team_id, log_path)
            if rc != 0:
                sync_err_msg = _error_with_tail(
                    f"millet sync exited {rc}", log_path,
                )
                log.warning("job %s: sync failed: %s", job_id, sync_err_msg)
            else:
                # `millet sync` may exit 0 even when git clone/push failed.
                # Inspect the log for failure markers.
                failure = _sync_log_indicates_failure(log_path)
                if failure:
                    sync_err_msg = _error_with_tail(
                        f"millet sync failed silently: {failure}", log_path,
                    )
                    log.warning(
                        "job %s: sync silent failure: %s", job_id, failure,
                    )

        # 5. cleanup (no-op unless VEZIR_DELETE_AUDIO=1)
        _delete_audio(sd)
        queue.update_status(
            job_id, "done", artifacts=artifacts,
            summary_error=summary_err_msg,
            sync_error=sync_err_msg,
        )
        parts = []
        if summary_err_msg:
            parts.append(f"summary failed: {summary_err_msg}")
        if sync_err_msg:
            parts.append(f"sync failed: {sync_err_msg}")
        if parts:
            log.info("job %s done (%s)", job_id, "; ".join(parts))
        else:
            log.info("job %s done", job_id)
    except Exception as exc:
        log.exception("job %s failed", job_id)
        queue.update_status(job_id, "error", error=str(exc))
    finally:
        meet_runner.cleanup_home_shim(job_id)


_worker_thread: threading.Thread | None = None
_stop_flag = threading.Event()


_DNS_WARMUP_HOSTS = ("huggingface.co", "github.com")
_DNS_WARMUP_TIMEOUT = 60  # seconds total
_DNS_WARMUP_INTERVAL = 3  # seconds between retries


def _dns_warmup() -> None:
    """Block until key external hosts resolve, or timeout.

    After a server restart, systemd-resolved may take several seconds to
    become fully operational.  Jobs that run immediately will fail with
    ``[Errno -2] Name or service not known`` on HuggingFace (pyannote
    model check) or Tinfoil (summary router).  This warmup loop ensures
    DNS is working before the worker starts claiming jobs.
    """
    import socket

    deadline = time.monotonic() + _DNS_WARMUP_TIMEOUT
    for host in _DNS_WARMUP_HOSTS:
        while time.monotonic() < deadline:
            try:
                socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
                log.debug("dns warmup: %s OK", host)
                break
            except socket.gaierror:
                remaining = max(0, int(deadline - time.monotonic()))
                log.info(
                    "dns warmup: %s not resolvable yet, retrying (%ds left)",
                    host, remaining,
                )
                time.sleep(_DNS_WARMUP_INTERVAL)
        else:
            log.warning(
                "dns warmup: %s still unresolvable after %ds; proceeding anyway",
                host, _DNS_WARMUP_TIMEOUT,
            )


_UPLOAD_SWEEP_INTERVAL_SEC = 60 * 60  # hourly


def _recover_orphaned_jobs() -> None:
    """Re-queue jobs left mid-pipeline by a previous restart/crash.

    Single-worker invariant: at startup nothing is being processed yet, so any
    job still in an in-progress state (``transcribing`` / ``summarizing`` /
    ``syncing``) was interrupted (e.g. the service was restarted for a deploy
    while a transcription was running).  ``claim_next`` only picks up ``queued``
    jobs, so such a job would otherwise stay stuck forever.  Reset it to
    ``queued`` so the poll loop re-claims and replays it.
    """
    try:
        ids = queue.requeue_orphans()
    except Exception:
        log.exception("orphan-recovery failed (non-fatal)")
        return
    if ids:
        log.warning(
            "recovered %d orphaned job(s) interrupted by a previous "
            "restart/crash; re-queued: %s",
            len(ids), ", ".join(ids),
        )


def _loop() -> None:
    log.info("vezir worker started")
    _dns_warmup()
    _recover_orphaned_jobs()
    next_sweep = 0.0
    while not _stop_flag.is_set():
        # Periodically sweep abandoned resumable-upload staging files.
        now = time.monotonic()
        if now >= next_sweep:
            try:
                from . import uploads
                uploads.sweep_abandoned_uploads()
            except Exception:
                log.exception("resumable-upload sweep failed (non-fatal)")
            next_sweep = now + _UPLOAD_SWEEP_INTERVAL_SEC

        # Follow-up tasks (sync now / retry summary / finalize labels)
        # run on this same thread, serialized with the pipeline.
        try:
            _drain_tasks()
        except Exception:
            log.exception("task drain failed (non-fatal)")

        try:
            job = queue.claim_next()
        except Exception:
            log.exception("error claiming job")
            time.sleep(POLL_INTERVAL_SEC)
            continue

        if job is None:
            time.sleep(POLL_INTERVAL_SEC)
            continue

        log.info("processing job %s (uploaded by %s)", job["id"], job["github"])
        process_one(job)
    log.info("vezir worker stopped")


def start_background_worker() -> None:
    """Launch the worker thread once."""
    global _worker_thread
    if _worker_thread is not None and _worker_thread.is_alive():
        return
    _stop_flag.clear()
    _worker_thread = threading.Thread(
        target=_loop, name="vezir-worker", daemon=True
    )
    _worker_thread.start()


def stop_background_worker() -> None:
    _stop_flag.set()


def retry_summary_for_session(
    session_id: str,
    *,
    preset_override: str | None = None,
    language_override: str | None = None,
) -> None:
    """Re-run summary generation for a completed session.

    Called when the user requests a summary retry (e.g. after a transient
    Tinfoil/network failure).  v0.11.0: runs ``millet label --apply-json``
    with an empty label map + summary regeneration as a SUBPROCESS
    through the per-job HOME shim.  (Pre-0.11.0 this imported
    ``millet.label.apply_labels`` in-process and mutated
    ``os.environ["HOME"]`` around the call — racing every other thread in
    the server.  The non-interactive apply mode in millet-pipeline 0.13.0
    also fixes the interactive-prompt bug that had originally forced the
    in-process approach.)

    ``language_override`` (e.g. "de") regenerates the summary in that language
    and saves it as an ADDITIONAL ``*.summary.<lang>.md`` artifact, preserving
    the primary auto-detected summary.
    """
    sd = _session_dir(session_id)
    log_path = _job_log_path(session_id)

    try:
        job = queue.get(session_id)
        if not job:
            log.error("retry-summary: session %s not found", session_id)
            return

        team_id = job.get("team_id") or ""
        if not team_id:
            log.error(
                "retry-summary: session %s has empty team_id; aborting",
                session_id,
            )
            queue.update_status(
                session_id, "error",
                error=f"session {session_id} has empty team_id",
            )
            return

        requested_preset = preset_override or job.get("summary_preset")
        if preset_override:
            log.info(
                "retry-summary %s: using override preset '%s' (original: '%s')",
                session_id, preset_override, job.get("summary_preset"),
            )
        if language_override:
            log.info(
                "retry-summary %s: additional-language summary '%s'",
                session_id, language_override,
            )
        queue.update_status(
            session_id, "summarizing",
            summary_error=None,  # clear previous summary error
        )

        # Run `millet label --apply-json` (empty map, summary regen) as a
        # subprocess through the per-job HOME shim so millet picks up the
        # correct per-team voiceprint DB and config paths — no process-wide
        # env mutation.
        summary_err: str | None = None
        try:
            rc = meet_runner.apply_labels_json(
                sd, session_id, team_id, log_path,
                label_map={},
                regenerate_summary=True,
                summary_preset=requested_preset,
                summary_language=language_override,
            )
            if rc != 0:
                summary_err = _error_with_tail(
                    f"summary retry failed (millet label exited {rc})",
                    log_path,
                )
                log.warning("retry-summary %s failed: %s", session_id, summary_err)
            # Belt-and-suspenders: verify the expected summary file appeared.
            elif language_override:
                if not list(sd.glob(f"*.summary.{language_override}.md")):
                    summary_err = (
                        f"summary retry produced no "
                        f".summary.{language_override}.md"
                    )
                    log.warning("retry-summary %s: %s", session_id, summary_err)
            elif requested_preset and not list(sd.glob("*.summary.md")):
                summary_err = (
                    f"summary retry produced no .summary.md for preset "
                    f"'{requested_preset}'"
                )
                log.warning("retry-summary %s: %s", session_id, summary_err)
        except Exception as exc:
            summary_err = f"summary retry failed: {exc}"
            log.warning("retry-summary %s failed: %s", session_id, summary_err)

        artifacts = _find_artifacts(sd)

        if summary_err:
            queue.update_status(
                session_id, "done",
                artifacts=artifacts,
                summary_error=summary_err,
            )
            return

        # Summary succeeded.  Re-sync if enabled (new summary artifact).
        sync_err_msg: str | None = None
        sync_enabled = bool(job.get("sync_enabled", 1))
        if _skip_sync():
            log.info("retry-summary %s: VEZIR_SKIP_SYNC set", session_id)
        elif not sync_enabled:
            log.info("retry-summary %s: sync_enabled=0", session_id)
        elif not meet_runner.team_has_sync_target(team_id):
            log.info(
                "retry-summary %s: team %s has no sync remote; local-only",
                session_id, team_id,
            )
        else:
            queue.update_status(session_id, "syncing", artifacts=artifacts)
            src = meet_runner.sync(sd, session_id, team_id, log_path)
            if src != 0:
                sync_err_msg = _error_with_tail(
                    f"millet sync exited {src}", log_path,
                )
                log.warning(
                    "retry-summary %s: sync failed: %s",
                    session_id, sync_err_msg,
                )
            else:
                failure = _sync_log_indicates_failure(log_path)
                if failure:
                    sync_err_msg = _error_with_tail(
                        f"millet sync failed silently: {failure}", log_path,
                    )
                    log.warning(
                        "retry-summary %s: sync silent failure: %s",
                        session_id, failure,
                    )

        # Summary-retry is a SUMMARY path (not an explicit sync).  Per the
        # status rule, a properly-completed session whose later re-sync failed
        # stays `done` with a sync-err badge (only the explicit Sync now /
        # post-label path uses `sync_failed`).
        queue.update_status(
            session_id, "done",
            artifacts=artifacts,
            summary_error=None,
            sync_error=sync_err_msg,
        )
        log.info("retry-summary %s succeeded", session_id)
    except Exception as exc:
        log.exception("retry-summary %s failed", session_id)
        queue.update_status(session_id, "error", error=str(exc))
    finally:
        meet_runner.cleanup_home_shim(session_id)


def _finalize_labels_task(session_id: str, label_map: dict[str, str]) -> None:
    """Post-labeling follow-up: voiceprint update + sync/cleanup.

    Runs on the worker thread after the labeling endpoint has already
    applied the labels (fast, synchronous `millet label --apply-json
    --no-summary`).  The voiceprint update loads a neural network and
    runs speaker-embedding inference (30-60 s on CPU) — far beyond the
    client's read timeout, which is why it's deferred here.

    Re-running the apply with the same map is an idempotent no-op on the
    already-relabeled artifacts; ``--update-profiles`` is the part that
    matters, and millet treats its failure as non-fatal (matching the
    pre-0.11.0 behavior where the in-process voiceprint update swallowed
    exceptions).  ``finalize_after_labeling`` then syncs and finalizes
    status regardless.
    """
    sd = _session_dir(session_id)
    log_path = _job_log_path(session_id)
    try:
        job = queue.get(session_id) or {}
        team_id = job.get("team_id") or ""
        if team_id and label_map:
            rc = meet_runner.apply_labels_json(
                sd, session_id, team_id, log_path,
                label_map=label_map,
                update_profiles=True,
            )
            if rc != 0:
                log.warning(
                    "finalize-labels %s: voiceprint update pass exited %s "
                    "(non-fatal; labels already applied)",
                    session_id, rc,
                )
    except Exception:
        log.exception(
            "finalize-labels %s: voiceprint update failed (non-fatal)",
            session_id,
        )
    finalize_after_labeling(session_id)


def reauto_label_session(session_id: str, *, sync: bool = False) -> dict:
    """Re-run ``millet label --auto`` for an already-transcribed session.

    Use case: a session was processed while its team's voiceprint DB was
    empty (or sparse), so every speaker landed as a raw placeholder and the
    session is stuck in ``needs_labeling``.  After the DB is (re)seeded, this
    re-runs auto-labeling against the now-populated per-team DB and re-routes
    the session's status, applying any confident matches to the artifacts.

    This mirrors the label->status stage of the main pipeline
    (:func:`process_session`) but starts from an existing transcript — it does
    NOT re-transcribe.  Partial matches are expected: speakers the DB can't
    recognize stay raw and the session remains ``needs_labeling`` (with the
    known speakers now pre-filled).

    ``sync`` (default False): when True and the session fully resolves, sync to
    the team repo exactly like the main pipeline.  For controlled recovery runs
    leave it False — labels/artifacts/status are updated but nothing is pushed.

    Returns a small result dict for CLI reporting:
    ``{"session_id", "team_id", "status", "matched", "unresolved", "synced",
    "error"}``.
    """
    sd = _session_dir(session_id)
    log_path = _job_log_path(session_id)
    result: dict = {
        "session_id": session_id,
        "team_id": None,
        "status": None,
        "matched": [],
        "unresolved": [],
        "synced": False,
        "error": None,
    }

    try:
        job = queue.get(session_id)
        if not job:
            result["error"] = "session not found"
            log.error("relabel: session %s not found", session_id)
            return result

        team_id = job.get("team_id") or ""
        result["team_id"] = team_id
        if not team_id:
            result["error"] = "empty team_id"
            log.error("relabel: session %s has empty team_id; aborting", session_id)
            return result

        if not bool(job.get("auto_label_enabled", 1)):
            # Respect the per-job opt-out: don't auto-label a session the
            # uploader explicitly wanted human-only.
            result["error"] = "auto_label_enabled=0 (skipped)"
            result["status"] = job.get("status")
            log.info("relabel: job %s auto_label_enabled=0; skipping", session_id)
            return result

        tj = sd / f"{session_id}.json"
        if not tj.exists():
            result["error"] = "no transcript on disk"
            result["status"] = job.get("status")
            log.warning("relabel: job %s has no transcript at %s", session_id, tj)
            return result

        # Re-run auto-labeling against the team's (now-seeded) voiceprint DB.
        rc = meet_runner.label_auto(sd, session_id, team_id, log_path)
        if rc != 0:
            log.warning("relabel: label --auto returned %s for %s; continuing", rc, session_id)

        # Report which speakers got resolved vs. remain raw.
        matched, unresolved = _speaker_resolution(sd)
        result["matched"] = matched
        result["unresolved"] = unresolved

        artifacts = _find_artifacts(sd)

        if _has_unresolved_speakers(sd):
            queue.update_status(session_id, "needs_labeling", artifacts=artifacts)
            result["status"] = "needs_labeling"
            log.info(
                "relabel: job %s still needs labeling (%d matched, %d unresolved)",
                session_id, len(matched), len(unresolved),
            )
            return result

        # Fully resolved.
        if not sync:
            queue.update_status(session_id, "done", artifacts=artifacts)
            result["status"] = "done"
            log.info("relabel: job %s fully resolved (no sync requested)", session_id)
            return result

        # sync=True: mirror the main pipeline's sync gates.
        sync_err_msg: str | None = None
        sync_enabled = bool(job.get("sync_enabled", 1))
        if _skip_sync():
            log.info("relabel: job %s VEZIR_SKIP_SYNC set; not syncing", session_id)
        elif not sync_enabled:
            log.info("relabel: job %s sync_enabled=0; not syncing", session_id)
        else:
            queue.update_status(session_id, "syncing", artifacts=artifacts)
            rc = meet_runner.sync(sd, session_id, team_id, log_path)
            if rc != 0:
                sync_err_msg = _error_with_tail(f"millet sync exited {rc}", log_path)
            else:
                failure = _sync_log_indicates_failure(log_path)
                if failure:
                    sync_err_msg = _error_with_tail(
                        f"millet sync failed silently: {failure}", log_path,
                    )
            result["synced"] = sync_err_msg is None

        queue.update_status(
            session_id, "done", artifacts=artifacts, sync_error=sync_err_msg,
        )
        result["status"] = "done"
        result["error"] = sync_err_msg
        log.info("relabel: job %s done (synced=%s)", session_id, result["synced"])
        return result
    except Exception as exc:
        log.exception("relabel: %s failed", session_id)
        result["error"] = str(exc)
        return result
    finally:
        meet_runner.cleanup_home_shim(session_id)


def finalize_after_labeling(
    session_id: str, meeting_type_override: str | None = None
) -> None:
    """Called when the web UI saves human labels.

    Re-runs `millet label` (with summary regeneration) via subprocess so the
    artifacts reflect the new names, then syncs, deletes audio, marks done.

    ``meeting_type_override`` (from the "sync as" dialog / sync endpoint) is
    threaded into :func:`meet_runner.sync` so the operator can force the
    target folder instead of relying on schedule/title auto-detection.
    """
    sd = _session_dir(session_id)
    log_path = _job_log_path(session_id)

    try:
        # millet label without --auto and without --no-summary will regenerate
        # everything based on already-applied labels in labels.json. But since
        # vezir's web UI applies labels via millet's apply_labels()
        # directly (see labels.py), the artifacts are already regenerated.
        # All that remains is sync (or not — both VEZIR_SKIP_SYNC and the
        # per-job sync_enabled flag can independently veto).
        job = queue.get(session_id) or {}
        team_id = job.get("team_id") or ""
        if not team_id:
            log.error(
                "post-labeling: session %s has empty team_id; aborting",
                session_id,
            )
            queue.update_status(
                session_id, "error",
                error=f"session {session_id} has empty team_id",
            )
            return
        sync_enabled = bool(job.get("sync_enabled", 1))
        sync_err_msg: str | None = None
        if _skip_sync():
            log.info(
                "post-labeling: VEZIR_SKIP_SYNC set, skipping millet sync for %s",
                session_id,
            )
        elif not sync_enabled:
            log.info(
                "post-labeling: sync_enabled=0; keeping session %s local-only",
                session_id,
            )
        elif not meet_runner.team_has_sync_target(team_id):
            log.info(
                "post-labeling: team %s has no sync remote; keeping session "
                "%s local-only",
                team_id, session_id,
            )
        else:
            queue.update_status(session_id, "syncing")
            rc = meet_runner.sync(
                sd, session_id, team_id, log_path,
                meeting_type=meeting_type_override,
            )
            if rc != 0:
                sync_err_msg = _error_with_tail(
                    f"millet sync exited {rc}", log_path,
                )
                log.warning(
                    "post-labeling sync %s failed: %s",
                    session_id, sync_err_msg,
                )
            else:
                failure = _sync_log_indicates_failure(log_path)
                if failure:
                    sync_err_msg = _error_with_tail(
                        f"millet sync failed silently: {failure}", log_path,
                    )
                    log.warning(
                        "post-labeling sync %s silent failure: %s",
                        session_id, failure,
                    )
        artifacts = _find_artifacts(sd)
        _delete_audio(sd)
        # Explicit sync intent (post-label finalize / Sync now): a failed push
        # is the headline outcome -> `sync_failed`.  (The main transcribe
        # pipeline keeps `done` + a sync-err badge instead.)
        final_status = "sync_failed" if sync_err_msg else "done"
        queue.update_status(
            session_id, final_status, artifacts=artifacts,
            sync_error=sync_err_msg,
        )
    except Exception as exc:
        log.exception("post-labeling sync failed for %s", session_id)
        queue.update_status(session_id, "error", error=str(exc))
    finally:
        meet_runner.cleanup_home_shim(session_id)
