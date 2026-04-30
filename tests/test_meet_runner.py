from __future__ import annotations

from vezir.server import meet_runner


def test_transcribe_passes_device_and_compute_type(monkeypatch, tmp_path):
    captured: dict[str, object] = {}

    monkeypatch.setattr(meet_runner.config, "meet_device", lambda: "cpu")
    monkeypatch.setattr(
        meet_runner.config,
        "meet_compute_type",
        lambda device=None: "int8",
    )

    def fake_run_meet(args, job_id, log_path=None):
        captured["args"] = args
        captured["job_id"] = job_id
        captured["log_path"] = log_path
        return 0

    monkeypatch.setattr(meet_runner, "run_meet", fake_run_meet)

    session_dir = tmp_path / "session"
    session_dir.mkdir()
    log_path = tmp_path / "worker.log"

    rc = meet_runner.transcribe(session_dir, "job-1", log_path)

    assert rc == 0
    assert captured["args"] == [
        "transcribe",
        "--device",
        "cpu",
        "--compute-type",
        "int8",
        str(session_dir),
    ]
    assert captured["job_id"] == "job-1"
    assert captured["log_path"] == log_path


def test_transcribe_uses_linux_defaults(monkeypatch, tmp_path):
    captured: dict[str, object] = {}

    monkeypatch.delenv("VEZIR_MEET_DEVICE", raising=False)
    monkeypatch.delenv("VEZIR_MEET_COMPUTE_TYPE", raising=False)
    monkeypatch.setattr(meet_runner.config.platform, "system", lambda: "Linux")
    monkeypatch.setattr(meet_runner.config, "_cuda_available", lambda: True)

    def fake_run_meet(args, job_id, log_path=None):
        captured["args"] = args
        captured["job_id"] = job_id
        captured["log_path"] = log_path
        return 0

    monkeypatch.setattr(meet_runner, "run_meet", fake_run_meet)

    session_dir = tmp_path / "session"
    session_dir.mkdir()
    log_path = tmp_path / "worker.log"

    rc = meet_runner.transcribe(session_dir, "job-linux", log_path)

    assert rc == 0
    assert captured["args"] == [
        "transcribe",
        "--device",
        "cuda",
        "--compute-type",
        "float16",
        str(session_dir),
    ]
    assert captured["job_id"] == "job-linux"
    assert captured["log_path"] == log_path


def test_transcribe_uses_cpu_without_linux_cuda(monkeypatch, tmp_path):
    captured: dict[str, object] = {}

    monkeypatch.delenv("VEZIR_MEET_DEVICE", raising=False)
    monkeypatch.delenv("VEZIR_MEET_COMPUTE_TYPE", raising=False)
    monkeypatch.setattr(meet_runner.config.platform, "system", lambda: "Linux")
    monkeypatch.setattr(meet_runner.config, "_cuda_available", lambda: False)

    def fake_run_meet(args, job_id, log_path=None):
        captured["args"] = args
        captured["job_id"] = job_id
        captured["log_path"] = log_path
        return 0

    monkeypatch.setattr(meet_runner, "run_meet", fake_run_meet)

    session_dir = tmp_path / "session"
    session_dir.mkdir()
    log_path = tmp_path / "worker.log"

    rc = meet_runner.transcribe(session_dir, "job-linux-cpu", log_path)

    assert rc == 0
    assert captured["args"] == [
        "transcribe",
        "--device",
        "cpu",
        "--compute-type",
        "int8",
        str(session_dir),
    ]
    assert captured["job_id"] == "job-linux-cpu"
    assert captured["log_path"] == log_path