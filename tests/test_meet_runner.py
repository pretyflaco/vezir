from __future__ import annotations

from pathlib import Path

from vezir.server import meet_runner


def _session_dir(tmp_path: Path) -> Path:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    return session_dir


def _patch_transcribe_config(
    monkeypatch,
    *,
    device: str = "cpu",
    compute_type: str = "int8",
    torch_device: str | None = None,
    asr_backend: str | None = None,
    mlx_model: str | None = None,
) -> None:
    monkeypatch.setattr(meet_runner.config, "meet_device", lambda: device)
    monkeypatch.setattr(
        meet_runner.config,
        "meet_compute_type",
        lambda selected_device=None: compute_type,
    )
    monkeypatch.setattr(
        meet_runner.config,
        "meet_torch_device",
        lambda selected_device=None: torch_device,
    )
    monkeypatch.setattr(meet_runner.config, "meet_asr_backend", lambda: asr_backend)
    monkeypatch.setattr(
        meet_runner.config,
        "meet_mlx_model",
        lambda selected_backend=None: (
            mlx_model if selected_backend == asr_backend else None
        ),
    )


def test_build_transcribe_args_passes_device_and_compute_type(monkeypatch, tmp_path):
    _patch_transcribe_config(monkeypatch, device="cpu", compute_type="int8")

    assert meet_runner.build_transcribe_args(_session_dir(tmp_path)) == [
        "transcribe",
        "--device",
        "cpu",
        "--compute-type",
        "int8",
        str(tmp_path / "session"),
    ]


def test_transcribe_runs_built_args(monkeypatch, tmp_path):
    captured: dict[str, object] = {}
    _patch_transcribe_config(monkeypatch, device="cpu", compute_type="int8")

    def fake_run_meet(args, job_id, log_path=None):
        captured["args"] = args
        captured["job_id"] = job_id
        captured["log_path"] = log_path
        return 0

    monkeypatch.setattr(meet_runner, "run_meet", fake_run_meet)

    session_dir = _session_dir(tmp_path)
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


def test_build_transcribe_args_uses_linux_defaults(monkeypatch, tmp_path):
    monkeypatch.delenv("VEZIR_MEET_DEVICE", raising=False)
    monkeypatch.delenv("VEZIR_MEET_COMPUTE_TYPE", raising=False)
    monkeypatch.setattr(meet_runner.config.platform, "system", lambda: "Linux")
    monkeypatch.setattr(meet_runner.config, "_cuda_available", lambda: True)
    monkeypatch.setattr(
        meet_runner.config,
        "meet_supports_option",
        lambda option: False,
    )

    assert meet_runner.build_transcribe_args(_session_dir(tmp_path)) == [
        "transcribe",
        "--device",
        "cuda",
        "--compute-type",
        "float16",
        str(tmp_path / "session"),
    ]


def test_build_transcribe_args_uses_cpu_without_linux_cuda(monkeypatch, tmp_path):
    monkeypatch.delenv("VEZIR_MEET_DEVICE", raising=False)
    monkeypatch.delenv("VEZIR_MEET_COMPUTE_TYPE", raising=False)
    monkeypatch.setattr(meet_runner.config.platform, "system", lambda: "Linux")
    monkeypatch.setattr(meet_runner.config, "_cuda_available", lambda: False)
    monkeypatch.setattr(
        meet_runner.config,
        "meet_supports_option",
        lambda option: False,
    )

    assert meet_runner.build_transcribe_args(_session_dir(tmp_path)) == [
        "transcribe",
        "--device",
        "cpu",
        "--compute-type",
        "int8",
        str(tmp_path / "session"),
    ]


def test_build_transcribe_args_uses_apple_silicon_mps_defaults(
    monkeypatch,
    tmp_path,
):
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
    monkeypatch.setattr(
        meet_runner.config,
        "meet_supports_option",
        lambda option: False,
    )

    assert meet_runner.build_transcribe_args(_session_dir(tmp_path)) == [
        "transcribe",
        "--device",
        "mps",
        "--compute-type",
        "float32",
        str(tmp_path / "session"),
    ]


def test_build_transcribe_args_uses_split_apple_silicon_torch_device(
    monkeypatch,
    tmp_path,
):
    monkeypatch.delenv("VEZIR_MEET_DEVICE", raising=False)
    monkeypatch.delenv("VEZIR_MEET_COMPUTE_TYPE", raising=False)
    monkeypatch.delenv("VEZIR_MEET_TORCH_DEVICE", raising=False)
    monkeypatch.setattr(meet_runner.config.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(meet_runner.config.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(meet_runner.config, "_mps_available", lambda: True)
    monkeypatch.setattr(meet_runner.config, "_cuda_available", lambda: False)
    monkeypatch.setattr(meet_runner.config, "_meet_supports_device", lambda device: False)
    monkeypatch.setattr(
        meet_runner.config,
        "meet_supports_option",
        lambda option: option == "--torch-device",
    )

    assert meet_runner.build_transcribe_args(_session_dir(tmp_path)) == [
        "transcribe",
        "--device",
        "cpu",
        "--compute-type",
        "int8",
        "--torch-device",
        "mps",
        str(tmp_path / "session"),
    ]


def test_build_transcribe_args_uses_mlx_asr_backend(monkeypatch, tmp_path):
    _patch_transcribe_config(
        monkeypatch,
        device="cpu",
        compute_type="int8",
        torch_device="mps",
        asr_backend="mlx",
        mlx_model="mlx-community/whisper-tiny",
    )

    assert meet_runner.build_transcribe_args(_session_dir(tmp_path)) == [
        "transcribe",
        "--device",
        "cpu",
        "--compute-type",
        "int8",
        "--asr-backend",
        "mlx",
        "--mlx-model",
        "mlx-community/whisper-tiny",
        "--torch-device",
        "mps",
        str(tmp_path / "session"),
    ]
