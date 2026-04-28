"""Vezir configuration: paths, env vars, defaults.

Runtime data lives at $VEZIR_DATA (default ~/vezir-data/), outside the repo.

Environment variables:
    VEZIR_DATA          Base dir for all runtime state (default ~/vezir-data)
    VEZIR_URL           Server URL for `vezir scribe` clients
    VEZIR_TOKEN         Bearer token for `vezir scribe` clients
    VEZIR_HOST          Bind address for `vezir serve` (default 0.0.0.0)
    VEZIR_PORT          Port for `vezir serve` (default 8000)
    VEZIR_MEET_BIN      Path to meetscribe `meet` binary (default: from PATH)
    VEZIR_LOG_LEVEL     Logging level (default INFO)
    VEZIR_MAX_UPLOAD_BYTES Maximum upload size (default 2 GiB)
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path


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
    found = shutil.which("meet")
    if not found:
        raise RuntimeError(
            "meetscribe `meet` binary not found in PATH. "
            "Install meetscribe-offline or set VEZIR_MEET_BIN."
        )
    return found


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
