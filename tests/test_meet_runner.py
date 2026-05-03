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
    monkeypatch.setattr(
        meet_runner.config,
        "meet_torch_device",
        lambda device=None: None,
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
    monkeypatch.setattr(meet_runner.config, "meet_supports_option", lambda option: False)

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
    monkeypatch.setattr(meet_runner.config, "meet_supports_option", lambda option: False)

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


def test_transcribe_uses_apple_silicon_mps_defaults(monkeypatch, tmp_path):
    captured: dict[str, object] = {}

    monkeypatch.delenv("VEZIR_MEET_DEVICE", raising=False)
    monkeypatch.delenv("VEZIR_MEET_COMPUTE_TYPE", raising=False)
    monkeypatch.setattr(meet_runner.config.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(meet_runner.config.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(meet_runner.config, "_mps_available", lambda: True)
    monkeypatch.setattr(
        meet_runner.config,
        "_meet_supports_device",
        lambda device: device == "mps",
    )
    monkeypatch.setattr(
        meet_runner.config,
        "_ctranslate2_supports_device",
        lambda device: device == "mps",
    )
    monkeypatch.setattr(meet_runner.config, "meet_supports_option", lambda option: False)

    def fake_run_meet(args, job_id, log_path=None):
        captured["args"] = args
        captured["job_id"] = job_id
        captured["log_path"] = log_path
        return 0

    monkeypatch.setattr(meet_runner, "run_meet", fake_run_meet)

    session_dir = tmp_path / "session"
    session_dir.mkdir()
    log_path = tmp_path / "worker.log"

    rc = meet_runner.transcribe(session_dir, "job-macos-mps", log_path)

    assert rc == 0
    assert captured["args"] == [
        "transcribe",
        "--device",
        "mps",
        "--compute-type",
        "float32",
        str(session_dir),
    ]
    assert captured["job_id"] == "job-macos-mps"
    assert captured["log_path"] == log_path


def test_transcribe_uses_split_apple_silicon_torch_device(monkeypatch, tmp_path):
    captured: dict[str, object] = {}

    monkeypatch.delenv("VEZIR_MEET_DEVICE", raising=False)
    monkeypatch.delenv("VEZIR_MEET_COMPUTE_TYPE", raising=False)
    monkeypatch.delenv("VEZIR_MEET_TORCH_DEVICE", raising=False)
    monkeypatch.setattr(meet_runner.config.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(meet_runner.config.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(meet_runner.config, "_mps_available", lambda: True)
    monkeypatch.setattr(meet_runner.config, "_cuda_available", lambda: False)
    monkeypatch.setattr(meet_runner.config, "_meet_supports_device", lambda device: False)
    monkeypatch.setattr(meet_runner.config, "meet_supports_option", lambda option: option == "--torch-device")

    def fake_run_meet(args, job_id, log_path=None):
        captured["args"] = args
        captured["job_id"] = job_id
        captured["log_path"] = log_path
        return 0

    monkeypatch.setattr(meet_runner, "run_meet", fake_run_meet)

    session_dir = tmp_path / "session"
    session_dir.mkdir()
    log_path = tmp_path / "worker.log"

    rc = meet_runner.transcribe(session_dir, "job-macos-split", log_path)

    assert rc == 0
    assert captured["args"] == [
        "transcribe",
        "--device",
        "cpu",
        "--compute-type",
        "int8",
        "--torch-device",
        "mps",
        str(session_dir),
    ]
    assert captured["job_id"] == "job-macos-split"
    assert captured["log_path"] == log_path
