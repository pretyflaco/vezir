"""Tests for Batch 3 server throughput optimizations:
- SQLite per-thread WAL connection reuse & inode-change reconnection
- Instant Event-based worker wakeup on enqueue / enqueue_task
- Parallel cue-frame extraction for larger cue sets
- Single-pass `millet sync --fallback-meeting-type`
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

from vezir import config
from vezir.server import meet_runner, queue, worker


def test_queue_reuses_sqlite_connection_and_reconnects_on_db_replace(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("VEZIR_DATA", str(tmp_path / "data"))
    queue.close_thread_connection()

    connect_calls = 0
    real_connect = sqlite3.connect

    def counting_connect(*args, **kwargs):
        nonlocal connect_calls
        connect_calls += 1
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", counting_connect)

    # First call opens connection & initializes schema.
    queue.create_team("team-a", "Team A")
    assert connect_calls == 1

    # Subsequent reads and writes on the same DB reuse the cached connection.
    for i in range(5):
        queue.enqueue(f"01J000000000000000000000{i:02d}", "alice", team_id="team-a")
        assert queue.get(f"01J000000000000000000000{i:02d}") is not None
    assert queue.list_teams()
    assert connect_calls == 1

    # If the DB file is deleted and replaced (new inode), reconnect & recreate schema.
    db_path = config.queue_db_path()
    db_path.unlink()
    queue.create_team("team-b", "Team B")
    assert connect_calls == 2
    assert queue.get_team("team-b") is not None


def test_worker_wake_event_fires_on_enqueue_and_tasks(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("VEZIR_DATA", str(tmp_path / "data"))
    queue.create_team("team-a", "Team A")

    worker._WAKE_EVENT.clear()
    queue.enqueue("01J000000000000000000WAKE1", "alice", team_id="team-a")
    assert worker._WAKE_EVENT.is_set()

    worker._WAKE_EVENT.clear()
    try:
        worker.enqueue_task("sync", "01J000000000000000000WAKE1")
        assert worker._WAKE_EVENT.is_set()
    finally:
        # Drain the queued item so we don't leak state to other tests.
        while not worker._TASKS.empty():
            worker._TASKS.get_nowait()
        with worker._TASKS_LOCK:
            worker._ACTIVE_TASKS.clear()


def test_extract_frames_parallel_batch(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("VEZIR_DATA", str(tmp_path / "data"))
    sid = "01J000000000000000000FRMS1"
    sdir = config.sessions_dir() / sid
    sdir.mkdir(parents=True)
    (sdir / f"{sid}.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
    cues = [float(i * 10) for i in range(10)]  # >= _FRAME_PARALLEL_THRESHOLD (6)
    (sdir / f"{sid}.json").write_text(
        json.dumps({"segments": [{"start": s, "end": s + 2.0, "text": "x"} for s in cues]})
    )

    monkeypatch.setattr(worker, "_video_start_time", lambda _v: 0.0)

    def fake_run(cmd, stdout=None, stderr=None):
        Path(cmd[-1]).write_bytes(b"\x89PNG\r\n\x1a\nfake")

        class _R:
            returncode = 0
            stdout = b""

        return _R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    log_path = config.logs_dir() / f"{sid}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    written = worker._extract_frames(sdir, sid, log_path)
    assert written == 10
    pngs = sorted(p.name for p in (sdir / "attachments").glob("cue_*.png"))
    assert len(pngs) == 10
    assert pngs[0] == "cue_00-00-00.png"
    assert pngs[-1] == "cue_00-01-30.png"


def test_meet_runner_sync_single_pass_with_fallback_option(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("VEZIR_DATA", str(tmp_path / "data"))
    queue.create_team("team-a", "Team A", sync_remote="https://git.example/repo.git")
    team_uuid = queue.resolve_team_uuid("team-a")
    assert team_uuid is not None

    sid = "01J000000000000000000SYNC1"
    queue.enqueue(sid, "alice", title="Design Review", team_id=team_uuid)
    sdir = config.sessions_dir() / sid
    sdir.mkdir(parents=True)
    log_path = config.logs_dir() / f"{sid}.log"

    calls: list[list[str]] = []
    monkeypatch.setattr(
        meet_runner,
        "run_meet",
        lambda args, job_id, team_id, log_path=None: calls.append(list(args)) or 0,
    )
    monkeypatch.setattr(
        config,
        "meet_sync_supports_option",
        lambda opt: opt == "--fallback-meeting-type",
    )

    rc = meet_runner.sync(sdir, sid, team_uuid, log_path)
    assert rc == 0
    assert len(calls) == 1
    assert "--fallback-meeting-type" in calls[0]
    fb_idx = calls[0].index("--fallback-meeting-type")
    assert calls[0][fb_idx + 1].startswith("design-review-")
    assert "--force" not in calls[0]
