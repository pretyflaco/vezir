"""Tests for run_meet's no-progress watchdog (VEZIR_MILLET_WATCHDOG_SECONDS).

Incident 2026-09-15: a stalled TEE summary pinned a vezir job in
``transcribing`` for 30+ minutes with the GPU idle; the 4 h hard budget
(VEZIR_MILLET_TIMEOUT) would eventually have reaped it, but 4 h of a blocked
single-worker queue is its own outage.  The watchdog kills a millet step
that shows NO observable progress — no log-file growth, no artifact change,
negligible process-group CPU — within minutes, while leaving legitimately
quiet-but-working steps (busy CPU, streaming logs) untouched.
"""
from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

import pytest

from vezir import config
from vezir.server import meet_runner


@pytest.fixture
def tmp_data(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("VEZIR_DATA", d)
        yield Path(d)


def _install_fake_millet(monkeypatch, tmp_path, *, binary="sleep"):
    monkeypatch.setattr(config, "meet_binary", lambda: binary)
    monkeypatch.setattr(meet_runner, "build_home_shim", lambda j, t: tmp_path)
    monkeypatch.setattr(
        meet_runner, "_env_for_meet", lambda home, team: os.environ.copy()
    )


def _budgets(monkeypatch, *, budget=None, watchdog=None):
    monkeypatch.setattr(config, "millet_timeout_seconds", lambda: budget)
    monkeypatch.setattr(config, "millet_watchdog_seconds", lambda: watchdog)


# ── stalled step: no log, no artifacts, no CPU → killed fast ────────────────


@pytest.mark.timeout(30)
def test_watchdog_kills_stalled_step(tmp_data, monkeypatch, tmp_path):
    _install_fake_millet(monkeypatch, tmp_path, binary="sleep")
    _budgets(monkeypatch, budget=None, watchdog=1)

    log_path = tmp_path / "job.log"
    t0 = time.monotonic()
    rc = meet_runner.run_meet(["30"], "01JOB", "team-uuid", log_path)
    elapsed = time.monotonic() - t0

    assert rc == meet_runner.TIMEOUT_EXIT_CODE
    assert elapsed < 15, "stalled step must die at the watchdog, not sleep 30"
    banner = log_path.read_text()
    assert "WATCHDOG" in banner
    assert "no progress for 1s" in banner


@pytest.mark.timeout(30)
def test_watchdog_banner_names_what_it_saw(tmp_data, monkeypatch, tmp_path):
    """The banner lands in the session log so the stored job error (built
    from the log tail) states the cause."""
    _install_fake_millet(monkeypatch, tmp_path, binary="sleep")
    _budgets(monkeypatch, budget=None, watchdog=1)

    log_path = tmp_path / "job.log"
    meet_runner.run_meet(["5"], "01JOB", "team-uuid", log_path)
    banner = log_path.read_text()
    assert "log" in banner
    assert "bytes unchanged" in banner
    assert "process group killed" in banner


@pytest.mark.timeout(30)
def test_watchdog_sigterm_ignored_escalates_to_sigkill(
    tmp_data, monkeypatch, tmp_path
):
    """A step that traps SIGTERM must still die — SIGKILL follows the grace
    period (monkeypatched short so the test stays fast)."""
    monkeypatch.setattr(meet_runner, "_KILL_GRACE_SECONDS", 1.0)
    monkeypatch.setattr(
        config,
        "meet_binary",
        lambda: "python3",
    )
    monkeypatch.setattr(meet_runner, "build_home_shim", lambda j, t: tmp_path)
    monkeypatch.setattr(
        meet_runner, "_env_for_meet", lambda home, team: os.environ.copy()
    )
    _budgets(monkeypatch, budget=None, watchdog=1)

    t0 = time.monotonic()
    rc = meet_runner.run_meet(
        ["-c", "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN);"
               " [time.sleep(60) for _ in range(60)]"],
        "01JOB", "team-uuid", tmp_path / "job.log",
    )
    elapsed = time.monotonic() - t0

    assert rc == meet_runner.TIMEOUT_EXIT_CODE
    assert elapsed < 20, "SIGKILL escalation must not wait out sleep 60"


# ── working steps: busy CPU or growing log → watchdog holds fire ────────────


@pytest.mark.timeout(30)
def test_watchdog_spares_cpu_busy_step(tmp_data, monkeypatch, tmp_path):
    """A step burning CPU with a silent log is WORKING (GPU inference is
    kernel-launch bound, not log bound) — must not be killed."""
    monkeypatch.setattr(config, "meet_binary", lambda: "python3")
    monkeypatch.setattr(meet_runner, "build_home_shim", lambda j, t: tmp_path)
    monkeypatch.setattr(
        meet_runner, "_env_for_meet", lambda home, team: os.environ.copy()
    )
    # Watchdog shorter than the step's total runtime: only CPU progress can
    # save it.  (Step: spin ~1.2s per pass, 3 passes = ~3.6s; watchdog 2s.)
    _budgets(monkeypatch, budget=None, watchdog=2)

    log_path = tmp_path / "job.log"
    rc = meet_runner.run_meet(
        ["-c", "import time\n"
               "x=0\n"
               "for _ in range(3):\n"
               "    t=time.monotonic()\n"
               "    while time.monotonic()-t < 1.2: x+=1\n"],
        "01JOB", "team-uuid", log_path,
    )

    assert rc == 0, "CPU-busy step must complete on its own"
    assert "WATCHDOG" not in log_path.read_text()


@pytest.mark.timeout(30)
def test_watchdog_spares_log_growing_step(tmp_data, monkeypatch, tmp_path):
    """A step streaming output to the log is making progress even at 0 CPU
    (network wait with periodic output — the retry-with-backoff shape)."""
    monkeypatch.setattr(config, "meet_binary", lambda: "bash")
    monkeypatch.setattr(meet_runner, "build_home_shim", lambda j, t: tmp_path)
    monkeypatch.setattr(
        meet_runner, "_env_for_meet", lambda home, team: os.environ.copy()
    )
    _budgets(monkeypatch, budget=None, watchdog=1)

    log_path = tmp_path / "job.log"
    rc = meet_runner.run_meet(
        ["-c", "for i in 1 2 3 4 5 6; do echo tick $i; sleep 0.5; done"],
        "01JOB", "team-uuid", log_path,
    )

    assert rc == 0, "log-growing step must complete on its own"


@pytest.mark.timeout(30)
def test_watchdog_spares_artifact_growing_step(
    tmp_data, monkeypatch, tmp_path
):
    """A silent step writing into its session dir is making progress."""
    monkeypatch.setattr(config, "meet_binary", lambda: "bash")
    monkeypatch.setattr(meet_runner, "build_home_shim", lambda j, t: tmp_path)
    monkeypatch.setattr(
        meet_runner, "_env_for_meet", lambda home, team: os.environ.copy()
    )
    _budgets(monkeypatch, budget=None, watchdog=1)

    session_dir = tmp_path / "sd"
    session_dir.mkdir()
    log_path = tmp_path / "job.log"
    rc = meet_runner.run_meet(
        ["-c",
         "for i in 1 2 3 4 5 6; do echo $i >> "
         + str(session_dir / "transcript.json")
         + "; sleep 0.5; done",
         str(session_dir)],
        "01JOB", "team-uuid", log_path,
    )

    assert rc == 0, "artifact-growing step must complete on its own"


# ── config + disabled paths ─────────────────────────────────────────────────


def test_watchdog_disabled_by_zero(tmp_data, monkeypatch, tmp_path):
    """0 disables the watchdog; the step runs to natural completion even
    with zero progress (the 4 h budget alone governs — disabled here)."""
    _install_fake_millet(monkeypatch, tmp_path, binary="sleep")
    _budgets(monkeypatch, budget=None, watchdog=0)

    rc = meet_runner.run_meet(["0.3"], "01JOB", "team-uuid", tmp_path / "job.log")
    assert rc == 0


def test_watchdog_config_default_and_env(monkeypatch):
    monkeypatch.delenv("VEZIR_MILLET_WATCHDOG_SECONDS", raising=False)
    assert config.millet_watchdog_seconds() == 900
    monkeypatch.setenv("VEZIR_MILLET_WATCHDOG_SECONDS", "0")
    assert config.millet_watchdog_seconds() is None
    monkeypatch.setenv("VEZIR_MILLET_WATCHDOG_SECONDS", "42")
    assert config.millet_watchdog_seconds() == 42
    monkeypatch.setenv("VEZIR_MILLET_WATCHDOG_SECONDS", "not-a-number")
    assert config.millet_watchdog_seconds() == 900


def test_budget_timeout_still_works_alongside_watchdog(
    tmp_data, monkeypatch, tmp_path
):
    """The pre-existing hard-budget behavior is preserved: budget fires even
    when the (longer) watchdog never would, with its original banner."""
    _install_fake_millet(monkeypatch, tmp_path, binary="sleep")
    _budgets(monkeypatch, budget=1, watchdog=600)

    log_path = tmp_path / "job.log"
    rc = meet_runner.run_meet(["30"], "01JOB", "team-uuid", log_path)

    assert rc == meet_runner.TIMEOUT_EXIT_CODE
    text = log_path.read_text()
    assert "TIMED OUT after 1s" in text
    assert "WATCHDOG" not in text


# ── process-group CPU probe ─────────────────────────────────────────────────


def test_process_group_cpu_covers_children():
    """A child in the same process group counts toward progress — a parent
    waiting on a busy ffmpeg is still a working step."""
    import subprocess
    import sys

    parent = subprocess.Popen(
        [sys.executable, "-c",
         "import subprocess, time; "
         "subprocess.run(['bash', '-c', 'while :; do :; done']); "
         "time.sleep(60)"],
        start_new_session=True,
    )
    try:
        t0 = meet_runner._process_group_cpu_seconds(parent.pid)
        assert t0 is not None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            t1 = meet_runner._process_group_cpu_seconds(parent.pid)
            if t1 is not None and t1 - t0 >= 1.0:
                break
            time.sleep(0.2)
        else:
            pytest.fail("child CPU never registered in the group total")
    finally:
        import os as _os
        import signal as _signal
        _os.killpg(parent.pid, _signal.SIGKILL)
        parent.wait()
