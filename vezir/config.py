"""Vezir configuration: paths, env vars, defaults.

Runtime data lives at $VEZIR_DATA (default ~/vezir-data/), outside the repo.

Environment variables:
    VEZIR_DATA          Base dir for all runtime state (default ~/vezir-data)
    VEZIR_URL           Server URL for `vezir scribe` clients
    VEZIR_TOKEN         Bearer token for `vezir scribe` clients
    VEZIR_HOST          Bind address for `vezir serve` (default 0.0.0.0)
    VEZIR_PORT          Port for `vezir serve` (default 8000)
    VEZIR_MEET_BIN      Path to meetscribe `meet` binary (default: from PATH)
    VEZIR_MEET_DEVICE   Device for `meet transcribe` (default: mps on Apple
                        Silicon when supported by the installed meetscribe
                        stack, cuda when available elsewhere, otherwise cpu)
    VEZIR_MEET_COMPUTE_TYPE Compute type for `meet transcribe` (default: int8
                        on cpu, float16 on cuda, float32 on mps)
    VEZIR_MEET_TORCH_DEVICE PyTorch device for meetscribe alignment/diarization
                        when the installed `meet transcribe` supports a
                        separate --torch-device option
    VEZIR_MEET_ASR_BACKEND ASR backend for `meet transcribe` when supported
                        (auto-selects mlx on Apple Silicon when available)
    VEZIR_MEET_MLX_MODEL MLX Whisper model path/repo when using mlx ASR
    VEZIR_LOG_LEVEL     Logging level (default INFO)
    VEZIR_MAX_UPLOAD_BYTES Maximum upload size (default 2 GiB)
"""
from __future__ import annotations

import importlib.util
import logging
import os
import platform
import re
import shutil
import subprocess
import sysconfig
import tempfile
from functools import lru_cache
from pathlib import Path

log = logging.getLogger("vezir.config")

_KNOWN_MEET_DEVICES = {"cpu", "cuda", "mps"}
_KNOWN_MEET_COMPUTE_TYPES = {"int8", "float16", "float32"}
_KNOWN_MEET_ASR_BACKENDS = {"whisperx", "mlx"}


def data_dir() -> Path:
    """Root dir for all vezir runtime state."""
    return Path(os.environ.get("VEZIR_DATA", str(Path.home() / "vezir-data")))


def sessions_dir() -> Path:
    return data_dir() / "sessions"


def jobs_dir() -> Path:
    """Per-job HOME-shim directories for shelling out to meetscribe."""
    return data_dir() / "jobs"


def logs_dir() -> Path:
    return data_dir() / "logs"


def speaker_profiles_path() -> Path:
    """Central voiceprint DB, seeded from ~/.config/meet/speaker_profiles.json."""
    return data_dir() / "speaker_profiles.json"


def team_json_path() -> Path:
    return data_dir() / "team.json"


def tokens_json_path() -> Path:
    return data_dir() / "tokens.json"


def queue_db_path() -> Path:
    return data_dir() / "vezir.sqlite"


def host() -> str:
    return os.environ.get("VEZIR_HOST", "0.0.0.0")


def port() -> int:
    return int(os.environ.get("VEZIR_PORT", "8000"))


def meet_binary() -> str:
    """Path to the meetscribe `meet` CLI."""
    explicit = os.environ.get("VEZIR_MEET_BIN")
    if explicit:
        return explicit
    scripts_dir = sysconfig.get_path("scripts")
    if scripts_dir:
        candidate = Path(scripts_dir) / "meet"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    found = shutil.which("meet")
    if not found:
        raise RuntimeError(
            "meetscribe `meet` binary not found in PATH. "
            "Install meetscribe-offline or set VEZIR_MEET_BIN."
        )
    return found


def _cuda_available() -> bool:
    try:
        import torch
    except Exception:
        return False
    try:
        return bool(torch.cuda.is_available())
    except Exception:
        return False


def _apple_silicon() -> bool:
    return platform.system() == "Darwin" and platform.machine().lower() in {
        "arm64",
        "aarch64",
    }


def _mps_available() -> bool:
    try:
        import torch
    except Exception:
        return False
    try:
        return bool(torch.backends.mps.is_available())
    except Exception:
        return False


def _mlx_whisper_available() -> bool:
    try:
        return importlib.util.find_spec("mlx_whisper") is not None
    except Exception:
        return False


@lru_cache(maxsize=1)
def _meet_transcribe_help() -> str:
    """Return cached `meet transcribe --help` output.

    The cache assumes the `meet` binary and its supported options do not
    change while the vezir process is running. Restart vezir after upgrading
    meetscribe so option auto-detection sees the new CLI surface.
    """
    try:
        meet = meet_binary()
    except Exception:
        return ""
    try:
        proc = subprocess.run(
            [meet, "transcribe", "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return ""
    return "\n".join(part for part in (proc.stdout, proc.stderr) if part)


def _meet_supports_device(device: str) -> bool:
    """Return True if the installed meetscribe CLI accepts a device value."""
    help_text = _meet_transcribe_help()
    if not help_text:
        return False
    for line in help_text.splitlines():
        if "--device" not in line:
            continue
        if re.search(rf"(?<![\w-]){re.escape(device)}(?![\w-])", line):
            return True
    return False


def meet_supports_option(option: str) -> bool:
    """Return True if `meet transcribe --help` advertises an option."""
    help_text = _meet_transcribe_help()
    if not help_text:
        return False
    return any(
        line.lstrip().startswith(option)
        for line in help_text.splitlines()
    )


def _warn_unknown_env_choice(name: str, value: str, known: set[str]) -> None:
    if value in known:
        return
    log.warning(
        "%s=%r is not one of the known values: %s. Passing it through to "
        "`meet transcribe`; check for typos if transcription fails.",
        name,
        value,
        ", ".join(sorted(known)),
    )


def _ctranslate2_supports_device(device: str) -> bool:
    try:
        import ctranslate2
    except Exception:
        return False
    try:
        ctranslate2.get_supported_compute_types(device)
    except Exception:
        return False
    return True


def _best_torch_device() -> str:
    if _cuda_available():
        return "cuda"
    if _apple_silicon() and _mps_available():
        return "mps"
    return "cpu"


def meet_device() -> str:
    """Primary ASR device to use for `meet transcribe`."""
    explicit = os.environ.get("VEZIR_MEET_DEVICE")
    if explicit:
        _warn_unknown_env_choice("VEZIR_MEET_DEVICE", explicit, _KNOWN_MEET_DEVICES)
        return explicit
    if (
        _apple_silicon()
        and _mps_available()
        and _meet_supports_device("mps")
        and _ctranslate2_supports_device("mps")
    ):
        return "mps"
    if _cuda_available():
        return "cuda"
    return "cpu"


def meet_torch_device(primary_device: str | None = None) -> str | None:
    """Optional PyTorch device for alignment/diarization in newer meetscribe.

    The current meetscribe 0.5 CLI has one --device flag that feeds both
    CTranslate2 ASR and PyTorch stages. That cannot use Apple MPS because
    CTranslate2 does not support it. A newer meetscribe can expose a
    separate --torch-device flag; when present, Vezir will keep ASR on the
    primary device and move PyTorch work to the best available accelerator.
    """
    explicit = os.environ.get("VEZIR_MEET_TORCH_DEVICE")
    if explicit:
        _warn_unknown_env_choice(
            "VEZIR_MEET_TORCH_DEVICE",
            explicit,
            _KNOWN_MEET_DEVICES,
        )
        return explicit
    if not meet_supports_option("--torch-device"):
        return None
    resolved_primary = primary_device or meet_device()
    torch_device = _best_torch_device()
    if torch_device == resolved_primary:
        return None
    return torch_device


def meet_compute_type(device: str | None = None) -> str:
    """Compute type to use for `meet transcribe`."""
    explicit = os.environ.get("VEZIR_MEET_COMPUTE_TYPE")
    if explicit:
        _warn_unknown_env_choice(
            "VEZIR_MEET_COMPUTE_TYPE",
            explicit,
            _KNOWN_MEET_COMPUTE_TYPES,
        )
        return explicit
    resolved_device = device or meet_device()
    if resolved_device == "cpu":
        return "int8"
    if resolved_device == "mps":
        return "float32"
    return "float16"


def meet_asr_backend() -> str | None:
    """Optional ASR backend for newer meetscribe."""
    explicit = os.environ.get("VEZIR_MEET_ASR_BACKEND")
    if explicit:
        _warn_unknown_env_choice(
            "VEZIR_MEET_ASR_BACKEND",
            explicit,
            _KNOWN_MEET_ASR_BACKENDS,
        )
        return explicit
    if not meet_supports_option("--asr-backend"):
        return None
    if _apple_silicon() and _mlx_whisper_available():
        return "mlx"
    return None


def meet_mlx_model(asr_backend: str | None = None) -> str | None:
    """Optional MLX Whisper model path/repo for newer meetscribe."""
    explicit = os.environ.get("VEZIR_MEET_MLX_MODEL")
    if not explicit:
        return None
    resolved_backend = asr_backend or meet_asr_backend()
    if resolved_backend != "mlx":
        return None
    if not meet_supports_option("--mlx-model"):
        return None
    return explicit


def log_level() -> str:
    return os.environ.get("VEZIR_LOG_LEVEL", "INFO").upper()


def server_url() -> str:
    return os.environ.get("VEZIR_URL", "http://localhost:8000")


def client_token() -> str | None:
    return os.environ.get("VEZIR_TOKEN")


def max_upload_bytes() -> int:
    """Maximum accepted upload size in bytes (default: 2 GiB)."""
    raw = os.environ.get("VEZIR_MAX_UPLOAD_BYTES")
    if raw is None:
        return 2 * 1024 * 1024 * 1024
    return int(raw)


def secure_mkdir(path: Path) -> Path:
    """Create a private runtime directory and enforce mode 0700."""
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except PermissionError:
        # Best effort for unusual filesystems; systemd UMask still helps.
        pass
    return path


def secure_chmod_file(path: Path) -> Path:
    """Enforce mode 0600 on a sensitive runtime file if it exists."""
    if path.exists():
        try:
            path.chmod(0o600)
        except PermissionError:
            pass
    return path


def secure_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """Write a sensitive text file with mode 0600 via same-dir replace."""
    secure_mkdir(path.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(text)
        tmp.chmod(0o600)
        tmp.replace(path)
        secure_chmod_file(path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def harden_umask() -> None:
    """Ensure newly created runtime files default to private permissions."""
    os.umask(0o077)


def ensure_dirs() -> None:
    """Create runtime directories if they don't exist."""
    for d in (data_dir(), sessions_dir(), jobs_dir(), logs_dir()):
        secure_mkdir(d)
