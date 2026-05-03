from __future__ import annotations

from pathlib import Path

from vezir import config


def _clear_config_caches() -> None:
    config._meet_transcribe_help.cache_clear()


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
    _clear_config_caches()
    monkeypatch.delenv("VEZIR_MEET_DEVICE", raising=False)
    monkeypatch.delenv("VEZIR_MEET_COMPUTE_TYPE", raising=False)
    monkeypatch.setattr(config.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(config.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(config, "_mps_available", lambda: False)

    assert config.meet_device() == "cpu"
    assert config.meet_compute_type() == "int8"


def test_meet_device_defaults_to_mps_on_apple_silicon_when_supported(monkeypatch):
    _clear_config_caches()
    monkeypatch.delenv("VEZIR_MEET_DEVICE", raising=False)
    monkeypatch.delenv("VEZIR_MEET_COMPUTE_TYPE", raising=False)
    monkeypatch.setattr(config.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(config.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(config, "_mps_available", lambda: True)
    monkeypatch.setattr(config, "_meet_supports_device", lambda device: device == "mps")
    monkeypatch.setattr(
        config,
        "_ctranslate2_supports_device",
        lambda device: device == "mps",
    )

    assert config.meet_device() == "mps"
    assert config.meet_compute_type() == "float32"


def test_meet_device_avoids_mps_when_meetscribe_does_not_support_it(monkeypatch):
    _clear_config_caches()
    monkeypatch.delenv("VEZIR_MEET_DEVICE", raising=False)
    monkeypatch.delenv("VEZIR_MEET_COMPUTE_TYPE", raising=False)
    monkeypatch.setattr(config.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(config.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(config, "_mps_available", lambda: True)
    monkeypatch.setattr(config, "_meet_supports_device", lambda device: False)
    monkeypatch.setattr(config, "_cuda_available", lambda: False)

    assert config.meet_device() == "cpu"
    assert config.meet_compute_type() == "int8"


def test_meet_device_avoids_mps_when_ctranslate2_does_not_support_it(monkeypatch):
    _clear_config_caches()
    monkeypatch.delenv("VEZIR_MEET_DEVICE", raising=False)
    monkeypatch.delenv("VEZIR_MEET_COMPUTE_TYPE", raising=False)
    monkeypatch.setattr(config.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(config.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(config, "_mps_available", lambda: True)
    monkeypatch.setattr(config, "_meet_supports_device", lambda device: device == "mps")
    monkeypatch.setattr(config, "_ctranslate2_supports_device", lambda device: False)
    monkeypatch.setattr(config, "_cuda_available", lambda: False)

    assert config.meet_device() == "cpu"
    assert config.meet_compute_type() == "int8"


def test_meet_torch_device_defaults_to_none_without_meetscribe_option(monkeypatch):
    _clear_config_caches()
    monkeypatch.delenv("VEZIR_MEET_TORCH_DEVICE", raising=False)
    monkeypatch.setattr(config, "meet_supports_option", lambda option: False)

    assert config.meet_torch_device("cpu") is None


def test_meet_torch_device_uses_mps_for_split_apple_silicon(monkeypatch):
    _clear_config_caches()
    monkeypatch.delenv("VEZIR_MEET_TORCH_DEVICE", raising=False)
    monkeypatch.setattr(config, "meet_supports_option", lambda option: option == "--torch-device")
    monkeypatch.setattr(config, "_cuda_available", lambda: False)
    monkeypatch.setattr(config.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(config.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(config, "_mps_available", lambda: True)

    assert config.meet_torch_device("cpu") == "mps"


def test_meet_torch_device_allows_env_override(monkeypatch):
    _clear_config_caches()
    monkeypatch.setenv("VEZIR_MEET_TORCH_DEVICE", "cpu")
    monkeypatch.setattr(config, "meet_supports_option", lambda option: False)

    assert config.meet_torch_device("cuda") == "cpu"


def test_meet_device_defaults_to_cuda_elsewhere(monkeypatch):
    _clear_config_caches()
    monkeypatch.delenv("VEZIR_MEET_DEVICE", raising=False)
    monkeypatch.delenv("VEZIR_MEET_COMPUTE_TYPE", raising=False)
    monkeypatch.setattr(config.platform, "system", lambda: "Linux")
    monkeypatch.setattr(config, "_cuda_available", lambda: True)

    assert config.meet_device() == "cuda"
    assert config.meet_compute_type() == "float16"


def test_meet_device_defaults_to_cpu_on_linux_without_cuda(monkeypatch):
    _clear_config_caches()
    monkeypatch.delenv("VEZIR_MEET_DEVICE", raising=False)
    monkeypatch.delenv("VEZIR_MEET_COMPUTE_TYPE", raising=False)
    monkeypatch.setattr(config.platform, "system", lambda: "Linux")
    monkeypatch.setattr(config, "_cuda_available", lambda: False)

    assert config.meet_device() == "cpu"
    assert config.meet_compute_type() == "int8"


def test_meet_device_and_compute_type_allow_env_override(monkeypatch):
    _clear_config_caches()
    monkeypatch.setenv("VEZIR_MEET_DEVICE", "cpu")
    monkeypatch.setenv("VEZIR_MEET_COMPUTE_TYPE", "float32")

    assert config.meet_device() == "cpu"
    assert config.meet_compute_type() == "float32"


def test_meet_supports_device_parses_transcribe_help(monkeypatch):
    _clear_config_caches()
    monkeypatch.setattr(
        config,
        "_meet_transcribe_help",
        lambda: "  --device [cuda|cpu|mps]  Device to run on",
    )

    assert config._meet_supports_device("mps") is True
    assert config._meet_supports_device("metal") is False


def test_meet_supports_option_parses_transcribe_help(monkeypatch):
    _clear_config_caches()
    monkeypatch.setattr(
        config,
        "_meet_transcribe_help",
        lambda: "  --device [cuda|cpu]\n  --torch-device [cuda|cpu|mps]",
    )

    assert config.meet_supports_option("--torch-device") is True
    assert config.meet_supports_option("--mlx") is False


def test_ctranslate2_supports_device(monkeypatch):
    class FakeCTranslate2:
        @staticmethod
        def get_supported_compute_types(device):
            if device == "mps":
                return {"float32"}
            raise ValueError("unsupported device")

    monkeypatch.setitem(__import__("sys").modules, "ctranslate2", FakeCTranslate2)

    assert config._ctranslate2_supports_device("mps") is True
    assert config._ctranslate2_supports_device("metal") is False
