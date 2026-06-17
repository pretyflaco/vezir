"""Worker restart-recovery: re-queue jobs orphaned mid-pipeline.

Regression for the bug where restarting vezir.service (e.g. for a deploy)
while a transcription was running left the job stuck in ``transcribing``
forever: ``claim_next`` only picks up ``queued`` jobs, and nothing reset the
interrupted in-progress job.  The worker now re-queues orphaned
``transcribing`` / ``summarizing`` / ``syncing`` jobs at startup.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def tmp_data(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("VEZIR_DATA", d)
        yield Path(d)


def _enqueue(queue, job_id: str, status: str) -> None:
    queue.enqueue(job_id, github="alice", team_id="blink")
    if status != "queued":
        queue.update_status(job_id, status)


# ── queue.requeue_orphans ──

def test_requeue_orphans_resets_transcribing(tmp_data):
    from vezir.server import queue
    _enqueue(queue, "01HZ0000000000000000TRANS0", "transcribing")
    ids = queue.requeue_orphans()
    assert ids == ["01HZ0000000000000000TRANS0"]
    assert queue.get("01HZ0000000000000000TRANS0")["status"] == "queued"


def test_requeue_orphans_resets_all_inprogress_states(tmp_data):
    from vezir.server import queue
    _enqueue(queue, "01HZ000000000000000TRANS00", "transcribing")
    _enqueue(queue, "01HZ000000000000000SUMM000", "summarizing")
    _enqueue(queue, "01HZ000000000000000SYNC000", "syncing")
    ids = set(queue.requeue_orphans())
    assert ids == {
        "01HZ000000000000000TRANS00",
        "01HZ000000000000000SUMM000",
        "01HZ000000000000000SYNC000",
    }
    for jid in ids:
        assert queue.get(jid)["status"] == "queued"


def test_requeue_orphans_leaves_terminal_and_queued_untouched(tmp_data):
    from vezir.server import queue
    _enqueue(queue, "01HZ0000000000000000QUEUE0", "queued")
    _enqueue(queue, "01HZ0000000000000000NEEDS0", "needs_labeling")
    _enqueue(queue, "01HZ00000000000000000DONE0", "done")
    _enqueue(queue, "01HZ0000000000000000ERROR0", "error")
    _enqueue(queue, "01HZ0000000000000SYNCFAIL0", "sync_failed")

    ids = queue.requeue_orphans()
    assert ids == []  # nothing orphaned
    assert queue.get("01HZ0000000000000000QUEUE0")["status"] == "queued"
    assert queue.get("01HZ0000000000000000NEEDS0")["status"] == "needs_labeling"
    assert queue.get("01HZ00000000000000000DONE0")["status"] == "done"
    assert queue.get("01HZ0000000000000000ERROR0")["status"] == "error"
    assert queue.get("01HZ0000000000000SYNCFAIL0")["status"] == "sync_failed"


def test_requeue_orphans_empty_queue_is_noop(tmp_data):
    from vezir.server import queue
    queue.create_team("blink", "Blink")  # ensure DB exists
    assert queue.requeue_orphans() == []


def test_requeue_orphans_bumps_updated_at(tmp_data):
    from vezir.server import queue
    _enqueue(queue, "01HZ0000000000000000TSBUMP", "transcribing")
    before = queue.get("01HZ0000000000000000TSBUMP")["updated_at"]
    queue.requeue_orphans()
    after = queue.get("01HZ0000000000000000TSBUMP")["updated_at"]
    # updated_at is reset to now (>= the original); status moved to queued.
    assert after >= before


def test_requeued_orphan_is_claimable(tmp_data):
    """The whole point: a recovered orphan is picked up by claim_next again."""
    from vezir.server import queue
    _enqueue(queue, "01HZ000000000000000CLAIM00", "transcribing")
    # Before recovery, claim_next ignores it (not queued).
    assert queue.claim_next() is None
    queue.requeue_orphans()
    job = queue.claim_next()
    assert job is not None
    assert job["id"] == "01HZ000000000000000CLAIM00"
    assert job["status"] == "transcribing"


# ── worker._recover_orphaned_jobs ──

def test_worker_recovery_hook_requeues(tmp_data):
    from vezir.server import queue, worker
    _enqueue(queue, "01HZ00000000000000HOOK0000", "transcribing")
    worker._recover_orphaned_jobs()
    assert queue.get("01HZ00000000000000HOOK0000")["status"] == "queued"


def test_worker_recovery_hook_swallows_errors(tmp_data, monkeypatch):
    """A failure in requeue_orphans must not crash worker startup."""
    from vezir.server import queue, worker

    def _boom() -> list[str]:
        raise RuntimeError("db exploded")

    monkeypatch.setattr(queue, "requeue_orphans", _boom)
    # Should log and return, not raise.
    worker._recover_orphaned_jobs()
