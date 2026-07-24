"""Vezir configuration: paths, env vars, defaults.

Runtime data lives at $VEZIR_DATA (default ~/vezir-data/), outside the repo.

Environment variables:
    VEZIR_DATA          Base dir for all runtime state (default ~/vezir-data)
    VEZIR_URL           Server URL for `vezir scribe` / `vezir tui` clients
    VEZIR_TOKEN         Bearer token for `vezir scribe` / `vezir tui` clients
    VEZIR_HOST          Bind address for `vezir serve` (default 127.0.0.1
                        from 0.1.12 onward, when no Caddy reverse proxy is
                        configured; previously 0.0.0.0). Set explicitly to
                        0.0.0.0 to opt back into the old behavior.
    VEZIR_PORT          Port for `vezir serve` (default 8000)
    VEZIR_COOKIE_SECURE Set to 1 to add ``Secure`` to the session cookie
                        (recommended when serving over HTTPS via Caddy).
    VEZIR_DISABLE_RATELIMIT  Set to 1 to disable the in-process rate
                             limiter. Tests use this; do not set in prod.
    VEZIR_MILLET_BIN    Path to millet (formerly `meet`) binary (default:
                        from PATH).  Legacy alias: VEZIR_MEET_BIN — read
                        with a DeprecationWarning, removed in vezir 0.6.0.
    VEZIR_MILLET_DEVICE Device for `millet transcribe` (default: mps on
                        Apple Silicon when supported by the installed
                        millet stack, cuda when available elsewhere,
                        otherwise cpu).  millet-pipeline auto-detects, so
                        this env var is optional (still respected as an
                        explicit override).  Legacy alias: VEZIR_MEET_DEVICE.
    VEZIR_MILLET_COMPUTE_TYPE Compute type for `millet transcribe`
                        (default: int8 on cpu, float16 on cuda, float32
                        on mps).  Legacy alias: VEZIR_MEET_COMPUTE_TYPE.
    VEZIR_MILLET_TORCH_DEVICE PyTorch device for alignment/diarization
                        when the installed `millet transcribe` supports a
                        separate --torch-device option.  Legacy alias:
                        VEZIR_MEET_TORCH_DEVICE.
    VEZIR_MILLET_ASR_BACKEND ASR backend for `millet transcribe` when
                        supported (auto-selects mlx on Apple Silicon
                        when available).  Legacy alias:
                        VEZIR_MEET_ASR_BACKEND.
    VEZIR_MILLET_MLX_MODEL MLX Whisper model path/repo when using mlx
                        ASR.  Legacy alias: VEZIR_MEET_MLX_MODEL.
    VEZIR_SUMMARY_PRESET    Summary quality preset (high-quality|confidential|alternative)
    VEZIR_LOG_LEVEL     Logging level (default INFO)
    VEZIR_MAX_UPLOAD_BYTES Maximum upload size (default 2 GiB)
    VEZIR_TINY_SPEAKER_MAX_SECONDS  Max total speech (seconds) for an
                        unresolved raw speaker to be treated as spurious
                        noise and ignored when routing to needs_labeling
                        (default 5.0)
    VEZIR_TINY_SPEAKER_MAX_SEGMENTS Max segment count for an unresolved raw
                        speaker to count as tiny noise (default 3)
    VEZIR_ACCESS_TTL    Lifetime (seconds) of a session **access** JWT minted
                        via the refresh flow (default 3600 = 60 min).  Short
                        by design: a leaked access token dies within this
                        window.  The refresh token carries the long session.
    VEZIR_REFRESH_IDLE_TTL  Idle lifetime (seconds) of a refresh token
                        (default 604800 = 7 days).  Reset on every rotation,
                        so an actively-refreshing session never hits it; a
                        session left unused this long requires a fresh login.
    VEZIR_SESSION_MAX_TTL  Absolute lifetime (seconds) of a session from its
                        creation (default 2592000 = 30 days), regardless of
                        refresh activity.  Bounds a compromised refresh
                        family; a full re-login (signer prompt / Google
                        grant) is forced once exceeded.
    VEZIR_REFRESH_GRACE  Lost-response grace window (seconds) after a refresh
                        rotation (default 60).  The one-generation-old
                        refresh token presented within this window replays
                        the SAME pair the rotation minted (idempotent
                        lost-response retry) instead of revoking the family;
                        it never mints a new generation and never slides the
                        window (v0.12.1 hijack hardening).  0 = strict (any
                        reuse revokes).
    VEZIR_MILLET_TIMEOUT  Hard timeout (seconds) for each millet subprocess
                        step (default 14400 = 4 h).  A wedged transcription
                        no longer blocks the single worker forever; the job
                        is marked error on expiry.

Legacy ``VEZIR_MEET_*`` aliases are still honored (with a one-time
``DeprecationWarning`` on read) but are deprecated and slated for removal
— prefer the ``VEZIR_MILLET_*`` names.  (The earlier docstring claimed
removal in 0.6.0; the fallback is in fact still live as of 0.12.x — see
the alias-reading code below and AGENTS.md's tech-debt list.)

Not every env var is listed above; notable others read elsewhere in this
module include ``VEZIR_PUBLIC_URL``, ``VEZIR_GOOGLE_CLIENT_ID`` /
``…_SECRET[_FILE]`` / ``…_ALLOWED_DOMAIN``, ``VEZIR_RECORD_DIR``,
``VEZIR_SKIP_SYNC``, ``VEZIR_DELETE_AUDIO``, ``VEZIR_CADDY_ROOT_CERT_PATH``,
and ``VEZIR_TUI_DISABLE_UPDATE_CHECK``.
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

# ── env-var deprecation aliasing (vezir 0.4.0 — millet rename) ──────────────
#
# Every VEZIR_MEET_X env var has a VEZIR_MILLET_X equivalent.  The new name
# wins if both are set; the old name still works for two minor versions
# (through vezir 0.5.x) and logs a one-time ``DeprecationWarning``.  Removed
# in vezir 0.6.0.

_DEPRECATED_ENV_ALIASES = {
    "VEZIR_MEET_BIN": "VEZIR_MILLET_BIN",
    "VEZIR_MEET_DEVICE": "VEZIR_MILLET_DEVICE",
    "VEZIR_MEET_COMPUTE_TYPE": "VEZIR_MILLET_COMPUTE_TYPE",
    "VEZIR_MEET_TORCH_DEVICE": "VEZIR_MILLET_TORCH_DEVICE",
    "VEZIR_MEET_ASR_BACKEND": "VEZIR_MILLET_ASR_BACKEND",
    "VEZIR_MEET_MLX_MODEL": "VEZIR_MILLET_MLX_MODEL",
}

_warned_env_aliases: set[str] = set()


def _read_millet_env(new_name: str, default: str | None = None) -> str | None:
    """Read a ``VEZIR_MILLET_*`` env var with legacy ``VEZIR_MEET_*`` fallback.

    Order of precedence:
      1. New-name env var if set and non-empty.
      2. Legacy old-name env var if set and non-empty — emits a one-time
         DeprecationWarning per old name.
      3. ``default``.

    Empty strings are treated as "unset" to match ``os.environ.get`` behavior
    for the existing callsites (most of which short-circuit on falsy).
    """
    # Find the legacy alias for this new-name var.
    legacy_name = None
    for legacy, new in _DEPRECATED_ENV_ALIASES.items():
        if new == new_name:
            legacy_name = legacy
            break

    value = os.environ.get(new_name)
    if value:
        return value
    if legacy_name:
        legacy_value = os.environ.get(legacy_name)
        if legacy_value:
            if legacy_name not in _warned_env_aliases:
                _warned_env_aliases.add(legacy_name)
                log.warning(
                    "%s is deprecated; use %s instead.  Will be removed "
                    "in vezir 0.6.0.",
                    legacy_name, new_name,
                )
            return legacy_value
    return default


def data_dir() -> Path:
    """Root dir for all vezir runtime state."""
    return Path(os.environ.get("VEZIR_DATA", str(Path.home() / "vezir-data")))


def sessions_dir() -> Path:
    return data_dir() / "sessions"


def jobs_dir() -> Path:
    """Per-job HOME-shim directories for shelling out to millet."""
    return data_dir() / "jobs"


def uploads_tmp_dir() -> Path:
    """Staging dir for in-progress resumable uploads (v0.7.3+).

    Each resumable upload session gets a ``<upload_id>.part`` file plus a
    ``<upload_id>.meta.json`` sidecar here until it completes (then it's
    assembled into ``sessions/<session_id>/``) or is swept after the TTL.
    """
    return data_dir() / "uploads-tmp"


def logs_dir() -> Path:
    return data_dir() / "logs"


def speaker_profiles_path() -> Path:
    """Legacy central voiceprint DB path (pre-v0.6.2).

    Kept for the v0.6.2 migration step (which moves this file under
    ``teams/blink/speaker_profiles.json``) and for back-compat with
    callers that haven't yet been team-scoped.  Application code should
    use :func:`team_speaker_profiles_path` instead.
    """
    return data_dir() / "speaker_profiles.json"


def team_json_path() -> Path:
    """Legacy global roster path (pre-v0.6.0).

    Kept for the migration step that reads this file and writes
    ``teams/<team_id>/roster.json`` per team.  Application code should
    use :func:`team_roster_path` instead.
    """
    return data_dir() / "team.json"


def teams_dir() -> Path:
    """Per-team data dir root (v0.6.0+).

    Layout:
        ~/vezir-data/teams/<team_id>/roster.json
        ~/vezir-data/teams/<team_id>/speaker_profiles.json  # v0.6.2+
        ~/vezir-data/teams/<team_id>/sync_config.json       # v0.6.2+ (optional)
    """
    return data_dir() / "teams"


def team_roster_path(team_id: str) -> Path:
    """Per-team roster (label dropdown candidates).

    Replaces the legacy global ``team.json`` in v0.6.0+.  Created by
    the migration; subsequent edits via admin tooling (CLI to be
    added in 0.6.1).
    """
    return teams_dir() / team_id / "roster.json"


def team_speaker_profiles_path(team_id: str) -> Path:
    """Per-team voiceprint DB (v0.6.2+).

    Each team holds its own ``speaker_profiles.json`` so that voiceprint
    training stays isolated per team.  The v0.6.2 migration moves the
    legacy central DB under ``teams/blink/`` and seeds an empty DB at
    ``teams/twentyone/``.

    The worker exposes this DB to unmodified millet via the per-job
    HOME shim (see :func:`vezir.server.meet_runner.build_home_shim`).
    """
    return teams_dir() / team_id / "speaker_profiles.json"


def team_sync_config_path(team_id: str) -> Path:
    """Per-team millet sync_config.json — operator-provided override (v0.6.2+).

    Optional escape hatch (the "B2" path from the v0.6.2 design): if
    this file exists, the worker symlinks it into the per-job HOME
    shim as ``~/.config/meet/sync_config.json`` instead of
    materializing one from the team row's ``sync_remote`` column.
    This lets ops hand-tune millet's full sync config per team (branch,
    ssh key, etc.) when ``sync_remote`` alone isn't enough.

    The vezir-managed alternative — auto-materialized from
    ``team.sync_remote`` — lives at
    :func:`team_materialized_sync_config_path` so it can be regenerated
    on remote-URL changes without trampling an operator's hand-tuned
    override.
    """
    return teams_dir() / team_id / "sync_config.json"


def team_materialized_sync_config_path(team_id: str) -> Path:
    """Vezir-managed per-team sync_config.json materialized from team.sync_remote.

    This file is owned by vezir and regenerated whenever
    ``team.sync_remote`` changes.  Operators should NOT edit it by
    hand — to customize, drop a full sync_config.json at the path
    returned by :func:`team_sync_config_path` instead (it wins).
    """
    return teams_dir() / team_id / "sync_config.materialized.json"


def tokens_json_path() -> Path:
    return data_dir() / "tokens.json"


def queue_db_path() -> Path:
    return data_dir() / "vezir.sqlite"


def server_json_path() -> Path:
    return data_dir() / "server.json"


def session_secret_path() -> Path:
    """Path to the HMAC secret used to sign nostr session JWTs.

    A random 0600 file created on first use by ``server.nostr_auth``.
    Deleting it invalidates every outstanding session (forces re-login),
    which is the intended rotation mechanism.
    """
    return data_dir() / ".session-secret"


def server_config() -> dict:
    """Read optional ``server.json`` from the data dir.

    Returns an empty dict if the file is missing or unparseable.
    The file is re-read on every call (no caching) so changes
    take effect without a server restart.
    """
    p = server_json_path()
    if not p.is_file():
        return {}
    try:
        import json as _json
        data = _json.loads(p.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def alternate_urls() -> list[str]:
    """Alternate server URLs for client failover.

    Read from ``server.json`` → ``alternate_urls`` key.  Each entry
    must be a full URL (``https://…``).  Clients use these as fallback
    when the primary enrollment URL is unreachable.
    """
    raw = server_config().get("alternate_urls", [])
    if not isinstance(raw, list):
        return []
    return [u for u in raw if isinstance(u, str) and u.startswith("http")]


def public_url() -> str | None:
    """The server's canonical public base URL (no trailing slash), if set.

    From ``$VEZIR_PUBLIC_URL`` (preferred) or ``server.json`` →
    ``public_url``.  When set, NIP-98 login URL verification uses this
    instead of reconstructing the URL from request headers — so a caller
    that reaches uvicorn directly cannot spoof ``X-Forwarded-Proto`` /
    ``Host`` to make an event signed for an arbitrary URL validate.
    Returns None when unset (the server then falls back to header-based
    reconstruction, the pre-0.8.2 behavior).
    """
    env = os.environ.get("VEZIR_PUBLIC_URL", "").strip()
    if env:
        return env.rstrip("/")
    raw = server_config().get("public_url")
    if isinstance(raw, str) and raw.startswith("http"):
        return raw.rstrip("/")
    return None


# ── Google sign-in (server-side OAuth, v0.8.x) ───────────────────────────────
# vezir verifies a Google ID token (obtained by the client via the OAuth 2.0
# Device Authorization Grant) and maps the verified `@<domain>` email to a
# member.  The client_id is public (it's the token's `aud`); the client_secret
# stays server-side (the server runs the device/token exchange so clients never
# handle it — see server.google_auth).


def google_client_id() -> str | None:
    """OAuth client_id for Google sign-in (public; the ID token's ``aud``).

    From ``$VEZIR_GOOGLE_CLIENT_ID``.  Returns None if unset (Google
    sign-in then reports "not configured").
    """
    cid = os.environ.get("VEZIR_GOOGLE_CLIENT_ID", "").strip()
    return cid or None


def google_client_secret() -> str | None:
    """OAuth client_secret for the device/token exchange (server-only).

    Resolution order:
      1. ``$VEZIR_GOOGLE_CLIENT_SECRET`` (the raw secret), else
      2. ``$VEZIR_GOOGLE_CLIENT_SECRET_FILE`` (a path to read it from).
    Returns None if neither yields a value.  Never logged or returned to
    clients.
    """
    raw = os.environ.get("VEZIR_GOOGLE_CLIENT_SECRET", "").strip()
    if raw:
        return raw
    path = os.environ.get("VEZIR_GOOGLE_CLIENT_SECRET_FILE", "").strip()
    if path:
        try:
            val = Path(path).read_text().strip()
            return val or None
        except Exception:
            return None
    return None


def google_allowed_domain() -> str:
    """Workspace domain that may sign in via Google (default ``blinkbtc.com``).

    The ID token must carry this as its ``hd`` (hosted-domain) claim and/or
    an ``email`` ending in ``@<domain>``.  Override with
    ``$VEZIR_GOOGLE_ALLOWED_DOMAIN``.
    """
    return os.environ.get("VEZIR_GOOGLE_ALLOWED_DOMAIN", "blinkbtc.com").strip() or "blinkbtc.com"


def host() -> str:
    """Bind address for `vezir serve`.

    Default changed in 0.1.12 from ``0.0.0.0`` (listen on all interfaces)
    to ``127.0.0.1`` (loopback only). The intended deployment is now to
    front vezir with Caddy (which listens on the VPN-reachable address
    and forwards to localhost). Operators who deliberately want vezir
    directly on the VPN can set ``VEZIR_HOST=0.0.0.0`` to opt back in.

    Rationale: pre-0.1.12 anyone on the mesh could speak HTTP directly
    to FastAPI. With Caddy in front we gain TLS, edge rate limits, and
    the ability to scrub Authorization headers from access logs — but
    only if FastAPI is not also reachable on the same port.
    """
    return os.environ.get("VEZIR_HOST", "127.0.0.1")


def port() -> int:
    return int(os.environ.get("VEZIR_PORT", "8000"))


def meet_binary() -> str:
    """Path to the millet (formerly `meet`) CLI.

    Resolution order:
      1. ``VEZIR_MILLET_BIN`` env var (or its legacy alias ``VEZIR_MEET_BIN``
         with deprecation warning) if set.
      2. ``<scripts_dir>/millet`` — primary, when millet-record is installed.
      3. ``<scripts_dir>/meet`` — legacy, when only the pre-rename
         millet-record is installed.
      4. ``shutil.which("millet")`` — primary.
      5. ``shutil.which("meet")`` — legacy.

    The function name is preserved (``meet_binary``) for now since several
    callers reference it; renaming to ``millet_binary`` is a follow-up.
    """
    explicit = _read_millet_env("VEZIR_MILLET_BIN")
    if explicit:
        return explicit
    scripts_dir = sysconfig.get_path("scripts")
    if scripts_dir:
        for bin_name in ("millet", "meet"):
            candidate = Path(scripts_dir) / bin_name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    for bin_name in ("millet", "meet"):
        found = shutil.which(bin_name)
        if found:
            return found
    raise RuntimeError(
        "millet binary not found in PATH.  "
        "Install millet-pipeline (or set VEZIR_MILLET_BIN to the "
        "executable path)."
    )


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
    """Return cached `millet transcribe --help` output.

    The cache assumes the `meet` binary and its supported options do not
    change while the vezir process is running. Restart vezir after upgrading
    millet so option auto-detection sees the new CLI surface.
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
    """Return True if the installed millet CLI accepts a device value."""
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
    """Return True if `millet transcribe --help` advertises an option."""
    help_text = _meet_transcribe_help()
    if not help_text:
        return False
    return any(
        line.lstrip().startswith(option)
        for line in help_text.splitlines()
    )


@lru_cache(maxsize=1)
def _meet_label_help() -> str:
    """Return cached `millet label --help` output (same caveats as
    :func:`_meet_transcribe_help`: restart vezir after upgrading millet)."""
    try:
        meet = meet_binary()
    except Exception:
        return ""
    try:
        proc = subprocess.run(
            [meet, "label", "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return ""
    return "\n".join(part for part in (proc.stdout, proc.stderr) if part)


def meet_label_supports_apply_json() -> bool:
    """True if the installed millet's `label` command has `--apply-json`.

    vezir >= 0.11.0 applies speaker labels through the
    ``millet label --apply-json`` subprocess boundary (millet-pipeline
    >= 0.13.0) instead of importing millet in-process.  Callers use this
    probe to fail loudly with an actionable upgrade message rather than
    a confusing argv error.
    """
    help_text = _meet_label_help()
    if not help_text:
        return False
    return any(
        line.lstrip().startswith("--apply-json")
        for line in help_text.splitlines()
    )


def millet_timeout_seconds() -> int | None:
    """Hard timeout for each millet subprocess step, or None to disable.

    From ``$VEZIR_MILLET_TIMEOUT`` (seconds; default 14400 = 4 h; 0 or
    a negative value disables).  A wedged transcription (GPU hang,
    stalled network inside millet) previously blocked the single worker
    forever; with the timeout the process group is killed and the job is
    marked ``error``.
    """
    raw = os.environ.get("VEZIR_MILLET_TIMEOUT")
    default = 4 * 60 * 60
    if raw is None or not raw.strip():
        return default
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return default
    return val if val > 0 else None


def _warn_unknown_env_choice(name: str, value: str, known: set[str]) -> None:
    if value in known:
        return
    log.warning(
        "%s=%r is not one of the known values: %s. Passing it through to "
        "`millet transcribe`; check for typos if transcription fails.",
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
    """Primary ASR device to use for `millet transcribe`."""
    explicit = _read_millet_env("VEZIR_MILLET_DEVICE")
    if explicit:
        _warn_unknown_env_choice("VEZIR_MILLET_DEVICE", explicit, _KNOWN_MEET_DEVICES)
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
    """Optional PyTorch device for alignment/diarization in newer millet.

    The current millet 0.5 CLI has one --device flag that feeds both
    CTranslate2 ASR and PyTorch stages. That cannot use Apple MPS because
    CTranslate2 does not support it. A newer millet can expose a
    separate --torch-device flag; when present, Vezir will keep ASR on the
    primary device and move PyTorch work to the best available accelerator.
    """
    explicit = _read_millet_env("VEZIR_MILLET_TORCH_DEVICE")
    if explicit:
        _warn_unknown_env_choice(
            "VEZIR_MILLET_TORCH_DEVICE",
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
    """Compute type to use for `millet transcribe`."""
    explicit = _read_millet_env("VEZIR_MILLET_COMPUTE_TYPE")
    if explicit:
        _warn_unknown_env_choice(
            "VEZIR_MILLET_COMPUTE_TYPE",
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
    """Optional ASR backend for newer millet."""
    explicit = _read_millet_env("VEZIR_MILLET_ASR_BACKEND")
    if explicit:
        _warn_unknown_env_choice(
            "VEZIR_MILLET_ASR_BACKEND",
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
    """Optional MLX Whisper model path/repo for newer millet."""
    explicit = _read_millet_env("VEZIR_MILLET_MLX_MODEL")
    if not explicit:
        return None
    resolved_backend = asr_backend or meet_asr_backend()
    if resolved_backend != "mlx":
        return None
    if not meet_supports_option("--mlx-model"):
        return None
    return explicit


def meet_default_language() -> str | None:
    """Global soft default-language bias passed to ``millet transcribe``.

    When set, millet keeps this language for auto-detected meetings unless a
    channel confidently detects another language — preventing drift to a
    low-confidence minority detection (e.g. an opening "Gracias" mislabeling
    an English meeting as Spanish).  A per-team override in the team's
    ``sync_config.json`` (``default_language``) takes precedence; see
    ``meet_runner.build_transcribe_args``.
    """
    return _read_millet_env("VEZIR_MILLET_DEFAULT_LANGUAGE") or None


def summary_preset() -> str | None:
    """Return the configured summarization preset, or None for the default."""
    return os.environ.get("VEZIR_SUMMARY_PRESET")


def log_level() -> str:
    return os.environ.get("VEZIR_LOG_LEVEL", "INFO").upper()


def log_format() -> str:
    """Log output format: ``"text"`` (default) or ``"json"``.

    Read from ``server.json`` → ``log_format`` key.
    """
    fmt = server_config().get("log_format", "text")
    return fmt if fmt in ("text", "json") else "text"


def log_file() -> Path | None:
    """Log file path, or None to disable file logging.

    Default: ``<data_dir>/logs/vezir.log``.  Set ``"log_file": false``
    in ``server.json`` to disable.
    """
    val = server_config().get("log_file")
    if val is False:
        return None
    if isinstance(val, str):
        return Path(val)
    return logs_dir() / "vezir.log"


class _JsonFormatter(logging.Formatter):
    """Minimal single-line JSON log formatter (no external deps)."""

    def format(self, record: logging.LogRecord) -> str:
        import json as _json
        entry = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info and record.exc_info[1]:
            entry["exc"] = self.formatException(record.exc_info)
        return _json.dumps(entry, ensure_ascii=False)


def configure_logging() -> None:
    """Set up Python logging for the server process.

    * Console handler: always present (text or JSON per ``log_format``).
    * File handler: ``RotatingFileHandler`` to ``<data_dir>/logs/vezir.log``
      (10 MB, 5 backups) unless disabled in ``server.json``.

    Call once at process startup (``create_app()``).
    """
    import logging.handlers

    level = getattr(logging, log_level(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)

    # Remove any existing handlers (e.g., from basicConfig in tests).
    root.handlers.clear()

    # Console handler.
    console = logging.StreamHandler()
    console.setLevel(level)
    if log_format() == "json":
        console.setFormatter(_JsonFormatter())
    else:
        console.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s",
        ))
    root.addHandler(console)

    # File handler (RotatingFileHandler).
    fpath = log_file()
    if fpath:
        fpath.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            str(fpath), maxBytes=10 * 1024 * 1024, backupCount=5,
        )
        fh.setLevel(level)
        # File always uses JSON for machine parseability.
        fh.setFormatter(_JsonFormatter())
        root.addHandler(fh)


def server_url() -> str:
    """Resolve the server URL using v0.6.1+ multi-team precedence.

    Precedence:
      1. ``VEZIR_URL`` env var
      2. teams.json active team's url (if any)
      3. client.json ``url`` (if any)
      4. ``http://localhost:8000`` default

    The fallback chain matches :func:`vezir.client.config.resolve_credentials`
    but lives here in the shared config module so server-side code
    (which can't import client.config without a circular dep) still
    has a single source of truth for the default URL.
    """
    env = os.environ.get("VEZIR_URL")
    if env:
        return env
    try:
        from .client.config import resolve_credentials as _rc
        url, _tok, _team, _src = _rc()
        if url:
            return str(url)
    except Exception:
        pass
    return "http://localhost:8000"


def client_token() -> str | None:
    """Resolve the bearer token using v0.6.1+ multi-team precedence.

    See :func:`server_url` for the precedence chain.  Returns ``None``
    when no token is configured anywhere; callers must surface a
    helpful error in that case.
    """
    env = os.environ.get("VEZIR_TOKEN")
    if env:
        return env
    try:
        from .client.config import resolve_credentials as _rc
        _url, tok, _team, _src = _rc()
        if tok:
            return str(tok)
    except Exception:
        pass
    return None


def client_team_id() -> str | None:
    """Resolve the active team_id using v0.7.0 multi-team precedence.

    Returns ``None`` when no team has been configured locally.  Callers
    that need to make a team-scoped HTTP request must surface this as
    a setup error or fall back to /api/me to discover memberships.
    """
    env = os.environ.get("VEZIR_TEAM_ID")
    if env:
        return env
    try:
        from .client.config import resolve_credentials as _rc
        _url, _tok, team, _src = _rc()
        if team:
            return str(team)
    except Exception:
        pass
    return None


_TOKEN_PREFIX = "vzr_"
_TOKEN_EXPECTED_LEN = 47  # "vzr_" (4) + token_urlsafe(32) (43)
_TOKEN_BODY_CHARS = set(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
)


def validate_token_format(token: str) -> None:
    """Warn on likely copy-paste errors in the bearer token.

    Prints to stderr; never raises. Designed to catch the most common
    mistakes (trailing backslash, whitespace, swapped secrets) before
    the server returns a cryptic 401.
    """
    import sys

    # Session JWTs (nostr/Google `vezir login`) are valid bearers but are
    # not `vzr_` opaque tokens; recognise them and skip the vzr_-oriented
    # heuristics.  Mirrors the server's JWT fast-path (nostr_auth.py), with
    # an extra `eyJ` (base64 `{"`) guard so a stray dotted `vzr_` value is
    # still validated rather than mistaken for a JWT.
    if token.count(".") == 2 and token.startswith("eyJ"):
        return

    if not token.startswith(_TOKEN_PREFIX):
        if token.startswith("nvpn://"):
            print(
                "vezir: WARNING: VEZIR_TOKEN looks like an nvpn invite, "
                "not a vezir bearer token. The invite goes to "
                "`nvpn import-invite`; VEZIR_TOKEN should start with 'vzr_'.",
                file=sys.stderr,
                flush=True,
            )
        else:
            print(
                f"vezir: WARNING: token does not start with '{_TOKEN_PREFIX}' "
                "-- is this the right value for VEZIR_TOKEN?",
                file=sys.stderr,
                flush=True,
            )
        return  # further checks assume vzr_ prefix

    if len(token) != _TOKEN_EXPECTED_LEN:
        print(
            f"vezir: WARNING: token length is {len(token)}, expected "
            f"{_TOKEN_EXPECTED_LEN} -- check for trailing whitespace, "
            "backslash, or truncation",
            file=sys.stderr,
            flush=True,
        )

    body = token[len(_TOKEN_PREFIX):]
    for i, ch in enumerate(body):
        if ch not in _TOKEN_BODY_CHARS:
            pos = len(_TOKEN_PREFIX) + i
            if ch == "\\":
                hint = "trailing backslash (common copy-paste artifact)"
            elif ch in (" ", "\t", "\n", "\r"):
                hint = "whitespace"
            else:
                hint = f"character {ch!r}"
            print(
                f"vezir: WARNING: token contains unexpected {hint} "
                f"at position {pos} -- check for copy-paste artifacts",
                file=sys.stderr,
                flush=True,
            )
            break  # one warning is enough


def max_upload_bytes() -> int:
    """Maximum accepted upload size in bytes (default: 2 GiB)."""
    raw = os.environ.get("VEZIR_MAX_UPLOAD_BYTES")
    if raw is None:
        return 2 * 1024 * 1024 * 1024
    return int(raw)


def _positive_env_int(name: str, default: int) -> int:
    """Read a positive integer from ``name``; fall back to ``default``.

    A missing, empty, non-integer, or non-positive value yields the
    default (never raises), so a fat-fingered env var can't brick the
    server's auth path.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return default
    return val if val > 0 else default


def access_ttl_seconds() -> int:
    """Session **access** JWT lifetime in seconds (default 3600 = 60 min).

    From ``$VEZIR_ACCESS_TTL``.  Deliberately short: the refresh flow
    mints a fresh access token before this elapses, so a leaked access
    JWT is only useful for at most this window.
    """
    return _positive_env_int("VEZIR_ACCESS_TTL", 60 * 60)


def refresh_idle_ttl_seconds() -> int:
    """Refresh-token idle lifetime in seconds (default 604800 = 7 days).

    From ``$VEZIR_REFRESH_IDLE_TTL``.  Reset on every rotation; a session
    unused for longer than this forces a full re-login.
    """
    return _positive_env_int("VEZIR_REFRESH_IDLE_TTL", 7 * 24 * 60 * 60)


def session_max_ttl_seconds() -> int:
    """Absolute session lifetime in seconds (default 2592000 = 30 days).

    From ``$VEZIR_SESSION_MAX_TTL``.  Measured from session creation and
    never extended by refresh; bounds a compromised refresh family.
    """
    return _positive_env_int("VEZIR_SESSION_MAX_TTL", 30 * 24 * 60 * 60)


def refresh_grace_seconds() -> int:
    """Lost-response grace window after a refresh rotation (default 60 s).

    From ``$VEZIR_REFRESH_GRACE``.  Presenting the one-generation-old
    refresh token within this many seconds of its rotation re-issues the
    pair (a legitimate client whose rotation response was lost) instead
    of revoking the session family.  Set to 0 for strict behavior
    (any reuse revokes; ``_positive_env_int`` treats 0 as unset, so the
    explicit "0" is handled here).
    """
    raw = os.environ.get("VEZIR_REFRESH_GRACE", "")
    if raw.strip() == "0":
        return 0
    return _positive_env_int("VEZIR_REFRESH_GRACE", 60)


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


def recordings_dir(team_id: str | None = None) -> Path:
    """Client-side recording output directory.

    Standardised layout (v0.7.0+):

        ~/vezir-meetings/<team_id>/meeting-YYYYMMDD-HHMMSS[_TITLE]/

    When *team_id* is ``None`` the function resolves the active team
    from ``teams.json``; if no team is configured it falls back to
    ``"default"``.

    The ``VEZIR_RECORD_DIR`` env var overrides everything (the team
    subdirectory is still appended beneath the override root).
    """
    if team_id is None:
        try:
            from .client.config import active_team_credentials
            tid, _url, _tok = active_team_credentials()
            team_id = tid or "default"
        except Exception:
            team_id = "default"

    root = Path(
        os.environ.get("VEZIR_RECORD_DIR", str(Path.home() / "vezir-meetings"))
    )
    return root / team_id


def sync_slug(title: str) -> str:
    """Convert a meeting title to a sync-repo-friendly folder slug.

    Matches the established convention in sync target repos:
    lowercase, hyphens, no special characters.

    >>> sync_slug("Dev Standup")
    'dev-standup'
    >>> sync_slug("Board Meeting Q2")
    'board-meeting-q2'
    >>> sync_slug("UX Weekly")
    'ux-weekly'
    >>> sync_slug("  weekly sync / @team  ")
    'weekly-sync-team'

    The result is a valid single path segment for millet's folder
    validator (``^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$``): capped at 64 chars
    and re-stripped of a trailing hyphen so truncation can't leave a
    dangling ``-``.
    """
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", title.strip())
    slug = slug.strip("-").lower()
    # Cap at millet's 64-char folder limit; re-strip in case the cut landed
    # on a hyphen (e.g. "...apps-" -> "...apps").
    return slug[:64].rstrip("-") if slug else ""


def sanitize_title(title: str) -> str:
    """Convert a free-form meeting title into a filesystem-safe slug.

    Rules:
      * Strip leading/trailing whitespace.
      * Replace any run of non-alphanumeric characters with a single
        underscore (except hyphens which are kept).
      * Uppercase the result so it stands out as a human label in
        ``ls`` output.
      * Cap at 60 characters to avoid path-length issues.

    >>> sanitize_title("AB Board")
    'AB_BOARD'
    >>> sanitize_title("  weekly sync / @blink  ")
    'WEEKLY_SYNC_BLINK'
    """
    slug = re.sub(r"[^a-zA-Z0-9-]+", "_", title.strip())
    slug = slug.strip("_").upper()
    return slug[:60] if slug else ""


def rename_session_dir_with_title(session_dir: Path, title: str | None) -> Path:
    """Append ``_TITLE`` to a ``meeting-YYYYMMDD-HHMMSS`` directory name.

    If the directory already carries a title suffix, or *title* is empty
    after sanitisation, the directory is returned unchanged.  The rename
    is a same-parent ``Path.rename()`` so it is atomic on POSIX.
    """
    if not title:
        return session_dir
    slug = sanitize_title(title)
    if not slug:
        return session_dir
    name = session_dir.name
    # Already has a label suffix?  (e.g. meeting-20260525-140054_BITIKA)
    if "_" in name.split("-", 2)[-1]:
        return session_dir
    new = session_dir.parent / f"{name}_{slug}"
    try:
        return session_dir.rename(new)
    except OSError as exc:
        log.warning("could not rename %s -> %s: %s", session_dir, new, exc)
        return session_dir


def ensure_dirs() -> None:
    """Create runtime directories if they don't exist."""
    for d in (data_dir(), sessions_dir(), jobs_dir(), logs_dir(), teams_dir(),
              uploads_tmp_dir()):
        secure_mkdir(d)
