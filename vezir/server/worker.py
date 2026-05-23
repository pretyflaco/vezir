"""Queue worker: drains the job queue, runs meetscribe pipeline.

Single-worker, single-job-at-a-time. Runs in a background thread inside
the FastAPI process. For larger deployments this would split into a
separate process; for v0 we keep it simple.

Pipeline per job:
  1. transcribe (meet transcribe <session-dir>) — produces .txt/.srt/.json/.summary.md/.pdf
  2. label --auto (meet label --auto --no-audio --no-summary <session-dir>)
        — applies confident voiceprint matches, leaves unknowns as REMOTE_N
  3. detect unknowns:
        if all speakers identified → status=syncing → meet sync → status=done
        else → status=needs_labeling → wait for human via web UI
  4. on completion (whether after auto or after human labeling), audio
     WAV is deleted to honor the storage policy.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from pathlib import Path

from .. import config
from . import meet_runner, queue

log = logging.getLogger("vezir.worker")

POLL_INTERVAL_SEC = 2.0


def _skip_sync() -> bool:
    """If VEZIR_SKIP_SYNC is set to a truthy value, skip the meet sync step.

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


# `meet sync` exits 0 even on git clone/push failures (it catches the
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
    """Scan the most recent `meet sync` block of the log for failure markers.

    Only inspects lines after the most recent '--- ... meet sync' marker,
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
    idx = text.rfind("meet sync ")
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

    Scans the last ~2 KiB for meetscribe's preset-guard RuntimeError
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
    for p in sorted(session_dir.glob("*.summary.md")):
        out["summary"] = p.name
        break
    for p in sorted(session_dir.glob("*.pdf")):
        out["pdf"] = p.name
        break
    for p in sorted(session_dir.glob("*.json")):
        if ".session." in p.name or ".summary." in p.name or ".translation." in p.name:
            continue
        out["json"] = p.name
        break
    return out


_UNRESOLVED_RE = re.compile(r"^(YOU|REMOTE(?:_\d+)?|SPEAKER_\d+)$")


def _has_unresolved_speakers(session_dir: Path) -> bool:
    """True if any speaker label still looks auto-generated.

    Uses the JSON transcript to inspect the actual speaker IDs after the
    --auto labeling pass.

    Fix for #6: previously glob'd for ``*.json`` and skipped known
    non-transcript suffixes, but ``.frontmatter.json`` (and any future
    sidecar) slipped through and was treated as the transcript.
    Now positively selects ``<session_dir.name>.json`` — the canonical
    transcript filename that meetscribe produces.
    """
    import json as _json

    tj = session_dir / f"{session_dir.name}.json"
    if not tj.exists():
        return False  # no transcript yet; treat as resolved (caller will surface error)

    try:
        data = _json.loads(tj.read_text(encoding="utf-8"))
    except Exception:
        return False

    speakers = data.get("speakers", []) or []
    for sp in speakers:
        sid = sp.get("id") or ""
        label = sp.get("label") or ""
        # If no label set, fall back to id which will likely be a placeholder.
        effective = label if label else sid
        if _UNRESOLVED_RE.match(effective):
            return True
    return False


def _delete_audio(session_dir: Path) -> None:
    """Optionally delete audio (.wav, .ogg) after artifacts are produced.

    Disabled by default during the pilot (see _delete_audio_enabled()).
    """
    if not _delete_audio_enabled():
        log.debug("audio deletion disabled (VEZIR_DELETE_AUDIO not set)")
        return
    for pattern in ("*.wav", "*.ogg"):
        for f in session_dir.glob(pattern):
            try:
                f.unlink()
                log.info("deleted audio: %s", f)
            except Exception as exc:
                log.warning("could not delete %s: %s", f, exc)


def process_one(job: dict) -> None:
    """Run the full pipeline for one claimed job."""
    job_id = job["id"]
    sd = _session_dir(job_id)
    log_path = _job_log_path(job_id)

    try:
        # 1. transcribe
        requested_preset = job.get("summary_preset")
        rc = meet_runner.transcribe(sd, job_id, log_path, summary_preset=requested_preset)

        # Distinguish between transcription failures and summary-only
        # failures.  When `meet transcribe` exits non-zero *and* a preset
        # was requested, the failure may be summary-only (the preset guard
        # in meetscribe raises RuntimeError after the transcript is already
        # on disk).  If we have transcript artifacts (.txt, .json), the
        # transcription itself succeeded and we should treat this as a
        # partial success: mark the job `done` with a `summary_error` so
        # the user gets their transcript and can retry the summary later.
        summary_err_msg: str | None = None

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
                    error=_error_with_tail(f"meet transcribe exited {rc}", log_path),
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
            rc = meet_runner.label_auto(sd, job_id, log_path)
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
            log.info("job %s: VEZIR_SKIP_SYNC set, skipping meet sync", job_id)
        elif not sync_enabled:
            log.info(
                "job %s: sync_enabled=0; keeping session local-only",
                job_id,
            )
        else:
            queue.update_status(job_id, "syncing", artifacts=artifacts)
            rc = meet_runner.sync(sd, job_id, log_path)
            if rc != 0:
                queue.update_status(
                    job_id, "error",
                    error=_error_with_tail(f"meet sync exited {rc}", log_path),
                    artifacts=artifacts,
                )
                return
            # `meet sync` may exit 0 even when git clone/push failed.
            # Inspect the log for failure markers; if found, mark error
            # so the operator can retry.
            failure = _sync_log_indicates_failure(log_path)
            if failure:
                log.warning("job %s: sync silent failure: %s", job_id, failure)
                queue.update_status(
                    job_id, "error",
                    error=_error_with_tail(
                        f"meet sync failed silently: {failure}", log_path,
                    ),
                    artifacts=artifacts,
                )
                return

        # 5. cleanup (no-op unless VEZIR_DELETE_AUDIO=1)
        _delete_audio(sd)
        queue.update_status(
            job_id, "done", artifacts=artifacts,
            summary_error=summary_err_msg,
        )
        if summary_err_msg:
            log.info("job %s done (summary failed: %s)", job_id, summary_err_msg)
        else:
            log.info("job %s done", job_id)
    except Exception as exc:
        log.exception("job %s failed", job_id)
        queue.update_status(job_id, "error", error=str(exc))
    finally:
        meet_runner.cleanup_home_shim(job_id)


_worker_thread: threading.Thread | None = None
_stop_flag = threading.Event()


def _loop() -> None:
    log.info("vezir worker started")
    while not _stop_flag.is_set():
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


def retry_summary_for_session(session_id: str) -> None:
    """Re-run summary generation for a completed session.

    Called when the user requests a summary retry (e.g. after a transient
    Tinfoil/network failure). The transcription artifacts must already
    exist on disk.
    """
    sd = _session_dir(session_id)
    log_path = _job_log_path(session_id)

    try:
        job = queue.get(session_id)
        if not job:
            log.error("retry-summary: session %s not found", session_id)
            return

        requested_preset = job.get("summary_preset")
        queue.update_status(
            session_id, "transcribing",
            summary_error=None,  # clear previous summary error
        )

        rc = meet_runner.retry_summary(
            sd, session_id, log_path,
            summary_preset=requested_preset,
        )
        if rc != 0:
            # Summary retry failed again.
            summary_err = _extract_summary_error(log_path) or (
                f"summary retry failed (exit {rc})"
            )
            log.warning(
                "retry-summary %s failed: %s", session_id, summary_err,
            )
            artifacts = _find_artifacts(sd)
            queue.update_status(
                session_id, "done",
                artifacts=artifacts,
                summary_error=summary_err,
            )
            return

        # Summary succeeded — update artifacts and clear summary_error.
        artifacts = _find_artifacts(sd)

        # If the session was previously done and now has a summary, re-run
        # sync if enabled (the summary artifact is new content).
        sync_enabled = bool(job.get("sync_enabled", 1))
        if _skip_sync():
            log.info("retry-summary %s: VEZIR_SKIP_SYNC set", session_id)
        elif not sync_enabled:
            log.info("retry-summary %s: sync_enabled=0", session_id)
        else:
            queue.update_status(session_id, "syncing", artifacts=artifacts)
            src = meet_runner.sync(sd, session_id, log_path)
            if src != 0:
                queue.update_status(
                    session_id, "error",
                    error=_error_with_tail(f"meet sync exited {src}", log_path),
                    artifacts=artifacts,
                )
                return
            failure = _sync_log_indicates_failure(log_path)
            if failure:
                queue.update_status(
                    session_id, "error",
                    error=_error_with_tail(
                        f"meet sync failed silently: {failure}", log_path,
                    ),
                    artifacts=artifacts,
                )
                return

        queue.update_status(
            session_id, "done",
            artifacts=artifacts,
            summary_error=None,
        )
        log.info("retry-summary %s succeeded", session_id)
    except Exception as exc:
        log.exception("retry-summary %s failed", session_id)
        queue.update_status(session_id, "error", error=str(exc))
    finally:
        meet_runner.cleanup_home_shim(session_id)


def finalize_after_labeling(session_id: str) -> None:
    """Called when the web UI saves human labels.

    Re-runs `meet label` (with summary regeneration) via subprocess so the
    artifacts reflect the new names, then syncs, deletes audio, marks done.
    """
    sd = _session_dir(session_id)
    log_path = _job_log_path(session_id)

    try:
        # meet label without --auto and without --no-summary will regenerate
        # everything based on already-applied labels in labels.json. But since
        # vezir's web UI applies labels via meetscribe's apply_labels()
        # directly (see labels.py), the artifacts are already regenerated.
        # All that remains is sync (or not — both VEZIR_SKIP_SYNC and the
        # per-job sync_enabled flag can independently veto).
        job = queue.get(session_id) or {}
        sync_enabled = bool(job.get("sync_enabled", 1))
        if _skip_sync():
            log.info(
                "post-labeling: VEZIR_SKIP_SYNC set, skipping meet sync for %s",
                session_id,
            )
        elif not sync_enabled:
            log.info(
                "post-labeling: sync_enabled=0; keeping session %s local-only",
                session_id,
            )
        else:
            queue.update_status(session_id, "syncing")
            rc = meet_runner.sync(sd, session_id, log_path)
            if rc != 0:
                queue.update_status(
                    session_id, "error",
                    error=_error_with_tail(
                        f"meet sync exited {rc}", log_path,
                    ),
                )
                return
            failure = _sync_log_indicates_failure(log_path)
            if failure:
                queue.update_status(
                    session_id, "error",
                    error=_error_with_tail(
                        f"meet sync failed silently: {failure}", log_path,
                    ),
                )
                return
        artifacts = _find_artifacts(sd)
        _delete_audio(sd)
        queue.update_status(session_id, "done", artifacts=artifacts)
    except Exception as exc:
        log.exception("post-labeling sync failed for %s", session_id)
        queue.update_status(session_id, "error", error=str(exc))
    finally:
        meet_runner.cleanup_home_shim(session_id)
