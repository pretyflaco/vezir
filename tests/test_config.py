from __future__ import annotations

from pathlib import Path

from vezir import config


def test_meet_binary_prefers_active_python_scripts_dir(monkeypatch, tmp_path):
    scripts_dir = tmp_path / "bin"
    scripts_dir.mkdir()
    meet = scripts_dir / "meet"
    meet.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    meet.chmod(0o755)

    monkeypatch.delenv("VEZIR_MEET_BIN", raising=False)
    monkeypatch.setattr(config.sysconfig, "get_path", lambda name: str(scripts_dir) if name == "scripts" else None)
    monkeypatch.setattr(config.shutil, "which", lambda name: None)

    assert config.meet_binary() == str(meet)


def test_meet_binary_falls_back_to_path(monkeypatch):
    monkeypatch.delenv("VEZIR_MEET_BIN", raising=False)
    monkeypatch.setattr(config.sysconfig, "get_path", lambda name: None)
    monkeypatch.setattr(config.shutil, "which", lambda name: "/usr/local/bin/meet")

    assert config.meet_binary() == "/usr/local/bin/meet"


def test_meet_device_defaults_to_cpu_on_macos(monkeypatch):
    monkeypatch.delenv("VEZIR_MEET_DEVICE", raising=False)
    monkeypatch.delenv("VEZIR_MEET_COMPUTE_TYPE", raising=False)
    monkeypatch.setattr(config.platform, "system", lambda: "Darwin")

    assert config.meet_device() == "cpu"
    assert config.meet_compute_type() == "int8"


def test_meet_device_defaults_to_cuda_elsewhere(monkeypatch):
    monkeypatch.delenv("VEZIR_MEET_DEVICE", raising=False)
    monkeypatch.delenv("VEZIR_MEET_COMPUTE_TYPE", raising=False)
    monkeypatch.setattr(config.platform, "system", lambda: "Linux")
    monkeypatch.setattr(config, "_cuda_available", lambda: True)

    assert config.meet_device() == "cuda"
    assert config.meet_compute_type() == "float16"


def test_meet_device_defaults_to_cpu_on_linux_without_cuda(monkeypatch):
    monkeypatch.delenv("VEZIR_MEET_DEVICE", raising=False)
    monkeypatch.delenv("VEZIR_MEET_COMPUTE_TYPE", raising=False)
    monkeypatch.setattr(config.platform, "system", lambda: "Linux")
    monkeypatch.setattr(config, "_cuda_available", lambda: False)

    assert config.meet_device() == "cpu"
    assert config.meet_compute_type() == "int8"


def test_meet_device_and_compute_type_allow_env_override(monkeypatch):
    monkeypatch.setenv("VEZIR_MEET_DEVICE", "cpu")
    monkeypatch.setenv("VEZIR_MEET_COMPUTE_TYPE", "float32")

    assert config.meet_device() == "cpu"
    assert config.meet_compute_type() == "float32"