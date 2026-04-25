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
"""
from __future__ import annotations

import os
import shutil
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


def ensure_dirs() -> None:
    """Create runtime directories if they don't exist."""
    for d in (data_dir(), sessions_dir(), jobs_dir(), logs_dir()):
        d.mkdir(parents=True, exist_ok=True)
