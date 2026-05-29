"""``vezir doctor`` — diagnose configuration and environment issues.

Auto-detects context:
  * **Client checks** always run (credential chain, SSL, connectivity,
    token auth, file permissions, teams.json schema).
  * **Server checks** run only when ``$VEZIR_DATA/vezir.sqlite`` exists
    locally (expired tokens, orphaned tokens, migration status, data dir
    permissions, millet binary, stale jobs).

Output: one line per check prefixed ``[OK]``, ``[WARN]``, or ``[ERROR]``.
Exit code: 0 if no errors, 1 if any ``[ERROR]``.

Added in v0.6.4.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import __version__, config
from .client import config as client_config

# ── result collector ────────────────────────────────────────────────────────

_OK = "OK"
_WARN = "WARN"
_ERROR = "ERROR"


class _Results:
    """Accumulates doctor check results for display and exit-code."""

    def __init__(self) -> None:
        self._rows: list[tuple[str, str]] = []

    def ok(self, msg: str) -> None:
        self._rows.append((_OK, msg))

    def warn(self, msg: str) -> None:
        self._rows.append((_WARN, msg))

    def error(self, msg: str) -> None:
        self._rows.append((_ERROR, msg))

    @property
    def has_errors(self) -> bool:
        return any(sev == _ERROR for sev, _ in self._rows)

    def print_all(self) -> None:
        for sev, msg in self._rows:
            print(f"  [{sev:<5s}] {msg}")

    @property
    def rows(self) -> list[tuple[str, str]]:
        return list(self._rows)


# ── client-side checks ─────────────────────────────────────────────────────


def _check_credential_resolution(
    r: _Results,
) -> tuple[str | None, str | None, str | None]:
    """C1 + C4: credential chain summary + coexistence warning.

    Returns ``(url, token, team_id)`` for use by downstream checks.
    ``team_id`` may be ``None`` if no team is configured locally; that
    is fine for /health checks but blocks team-scoped probes.
    """
    url, token, team_id, source = client_config.resolve_credentials()

    if not url and not token:
        r.error("no credentials configured (env / teams.json / client.json)")
        return None, None, None

    masked = (token[:8] + "***") if token and len(token) > 8 else "(none)"
    team_str = team_id or "(none -- /api/me only)"
    r.ok(
        f"credentials: source={source}  url={url}  "
        f"token={masked}  team={team_str}"
    )

    # C4: coexistence warning — if teams.json won, and client.json ALSO
    # has url+token, the operator might be confused about which one is
    # active.
    if source and source.startswith("teams:"):
        prefs = client_config.load_client_prefs()
        if prefs.get("url") and prefs.get("token"):
            r.warn(
                "client.json also has url + token but teams.json takes "
                "precedence.  Remove url/token from client.json to avoid "
                "confusion, or unset to let teams.json be the single source."
            )

    # Check for disagreement between env and teams.json.
    env_url = os.environ.get("VEZIR_URL")
    os.environ.get("VEZIR_TOKEN")
    if source and source.startswith("teams:") and env_url:
        # Env had URL but not token (partial), so teams.json won.
        if env_url != url:
            r.warn(
                f"VEZIR_URL env ({env_url}) differs from teams.json "
                f"active URL ({url}).  VEZIR_TOKEN is not set so "
                "teams.json wins.  If intentional, no action needed."
            )
    if source == "env":
        _tid, t_url, _t_tok = client_config.active_team_credentials()
        if t_url and t_url != env_url:
            r.warn(
                f"teams.json URL ({t_url}) differs from VEZIR_URL env "
                f"({env_url}).  Env wins.  Unset VEZIR_URL + VEZIR_TOKEN "
                "to let teams.json take effect."
            )

    return url, token, team_id


def _check_env_shadow(r: _Results) -> None:
    """C2: detect stale VEZIR_URL / VEZIR_TOKEN in shell init files."""
    current_url = os.environ.get("VEZIR_URL", "")
    targets = [
        Path("/etc/environment"),
        Path.home() / ".profile",
        Path.home() / ".bashrc",
    ]
    url_pattern = re.compile(r"""^\s*(?:export\s+)?VEZIR_URL\s*=\s*['"]?([^'"#\s]+)""", re.M)

    found: dict[str, str] = {}
    for target in targets:
        try:
            if not target.is_file():
                continue
            text = target.read_text(encoding="utf-8", errors="replace")
            for m in url_pattern.finditer(text):
                found[str(target)] = m.group(1)
        except PermissionError:
            continue

    if len(found) > 1:
        lines = "  ".join(f"{p}: {v}" for p, v in found.items())
        r.warn(
            f"VEZIR_URL set in multiple shell sources: {lines}.  "
            "The earliest-sourced one wins; check for stale entries."
        )
    elif len(found) == 1:
        path, val = next(iter(found.items()))
        if current_url and val != current_url:
            r.warn(
                f"VEZIR_URL in {path} ({val}) differs from current env "
                f"({current_url}).  Another source may be shadowing it."
            )


def _check_teams_json_schema(r: _Results) -> None:
    """C3: teams.json structural validation."""
    p = client_config.teams_config_path()
    if not p.exists():
        r.ok("teams.json: not present (single-team or env-only mode)")
        return

    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        r.error(f"teams.json: invalid JSON: {exc}")
        return

    if not isinstance(raw, dict):
        r.error("teams.json: top-level value is not an object")
        return

    teams = raw.get("teams", [])
    if not isinstance(teams, list):
        r.error("teams.json: 'teams' is not an array")
        return

    # Check for entries dropped by load_teams_config (missing fields).
    dropped = 0
    ids_seen: dict[str, int] = {}
    for t in teams:
        if not isinstance(t, dict):
            dropped += 1
            continue
        if not t.get("id") or not t.get("url") or not t.get("token"):
            dropped += 1
            continue
        tid = t["id"]
        ids_seen[tid] = ids_seen.get(tid, 0) + 1
    if dropped:
        r.warn(
            f"teams.json: {dropped} team entry(s) dropped (missing id/url/token)"
        )

    dupes = {k: v for k, v in ids_seen.items() if v > 1}
    if dupes:
        r.warn(f"teams.json: duplicate team ids: {dupes}")

    active = raw.get("active")
    if active and active not in ids_seen:
        r.warn(
            f"teams.json: active team '{active}' not in teams list.  "
            "Will auto-correct to first available team."
        )

    if not teams:
        r.ok("teams.json: present but empty (no teams configured)")
    else:
        clean = len(teams) - dropped
        r.ok(f"teams.json: {clean} team(s) configured, active={active}")


def _check_token_format(r: _Results, token: str | None) -> None:
    """C5: token format validation."""
    if not token:
        return  # already reported in C1

    prefix = "vzr_"
    if not token.startswith(prefix):
        if token.startswith("nvpn://"):
            r.error(
                "token looks like an nvpn invite, not a vezir bearer.  "
                "VEZIR_TOKEN should start with 'vzr_'."
            )
        else:
            r.warn(f"token does not start with '{prefix}'")
        return

    expected_len = 47  # "vzr_" (4) + token_urlsafe(32) (43)
    if len(token) != expected_len:
        r.warn(
            f"token length is {len(token)}, expected {expected_len} "
            "-- check for trailing whitespace or truncation"
        )

    body = token[len(prefix):]
    valid_chars = set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
    )
    for i, ch in enumerate(body):
        if ch not in valid_chars:
            r.warn(
                f"token has unexpected character at position {len(prefix) + i} "
                f"({ch!r}) -- check for copy-paste artifacts"
            )
            break


def _check_ssl_cert(r: _Results, url: str | None) -> None:
    """C6: SSL certificate configuration."""
    if not url or not url.startswith("https://"):
        return  # not HTTPS, nothing to check

    ssl_cert = os.environ.get("SSL_CERT_FILE")
    caddy_cert = os.environ.get("VEZIR_CADDY_ROOT_CERT_PATH")

    if not ssl_cert and not caddy_cert:
        r.warn(
            f"server URL is HTTPS ({url}) but neither SSL_CERT_FILE nor "
            "VEZIR_CADDY_ROOT_CERT_PATH is set.  If the server uses an "
            "internal CA (e.g. Caddy), httpx will reject the certificate.  "
            "Set SSL_CERT_FILE=/path/to/caddy-root.pem"
        )
        return

    for var_name, path_str in [
        ("SSL_CERT_FILE", ssl_cert),
        ("VEZIR_CADDY_ROOT_CERT_PATH", caddy_cert),
    ]:
        if not path_str:
            continue
        p = Path(path_str)
        if not p.is_file():
            r.error(f"{var_name}={path_str} is set but the file does not exist")
        else:
            try:
                text = p.read_text(encoding="utf-8", errors="replace")[:200]
                if "BEGIN CERTIFICATE" not in text:
                    r.warn(
                        f"{var_name}={path_str} does not look like a PEM certificate "
                        "(missing 'BEGIN CERTIFICATE' header)"
                    )
                else:
                    r.ok(f"{var_name}: {path_str} (PEM)")
            except PermissionError:
                r.warn(f"{var_name}={path_str}: permission denied reading file")


def _check_file_perms(r: _Results, path: Path, label: str) -> None:
    """C7/S3 helper: check a file is mode 0600."""
    if not path.exists():
        return
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        return
    if mode != 0o600:
        r.warn(
            f"{label} ({path}) has mode {oct(mode)}, expected 0o600.  "
            f"Fix: chmod 600 {path}"
        )


def _check_dir_perms(r: _Results, path: Path, label: str) -> None:
    """S3 helper: check a directory is mode 0700."""
    if not path.exists():
        return
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        return
    if mode != 0o700:
        r.warn(
            f"{label} ({path}) has mode {oct(mode)}, expected 0o700.  "
            f"Fix: chmod 700 {path}"
        )


def _check_client_file_perms(r: _Results) -> None:
    """C7: client config file permissions."""
    _check_file_perms(r, client_config.teams_config_path(), "teams.json")
    _check_file_perms(r, client_config.client_config_path(), "client.json")


def _is_private_ip(host: str) -> bool:
    """True if *host* looks like a private/tunnel IP (10.x, 100.x, 192.168.x)."""
    return (
        host.startswith("10.")
        or host.startswith("100.")
        or host.startswith("192.168.")
        or host in ("127.0.0.1", "localhost")
    )


def _check_tunnel_reachability(r: _Results, url: str | None) -> None:
    """Probe raw TCP connectivity to the server before HTTP checks.

    Only runs when the server URL points to a private/tunnel IP
    (10.x, 100.x, 192.168.x).  A TCP timeout here usually means the
    VPN tunnel isn't established yet.
    """
    if not url:
        return
    from urllib.parse import urlparse
    parsed = urlparse(url)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 8000)
    if not host or not _is_private_ip(host):
        return  # skip for public hostnames / DNS names
    import socket
    try:
        sock = socket.create_connection((host, port), timeout=5)
        sock.close()
        r.ok(f"TCP {host}:{port}: reachable")
    except TimeoutError:
        r.error(
            f"TCP {host}:{port}: connection timed out.  "
            "VPN tunnel may not be established.  "
            "Check nvpn/tailscale status and restart if needed."
        )
    except OSError as exc:
        r.error(f"TCP {host}:{port}: {exc}.  VPN tunnel may be down.")


def _check_nvpn(r: _Results) -> None:
    """Report nvpn version if installed."""
    import shutil
    import subprocess
    nvpn = shutil.which("nvpn")
    if not nvpn:
        return  # not installed, skip silently
    try:
        proc = subprocess.run(
            [nvpn, "version"],
            capture_output=True, text=True, timeout=5,
        )
        version = proc.stdout.strip()
        if version:
            r.ok(f"nvpn: {version}")
        else:
            r.warn("nvpn: could not determine version")
    except Exception:
        r.warn("nvpn: could not determine version")


def _check_server_connectivity(
    r: _Results,
    url: str | None,
    token: str | None,
    team_id: str | None = None,
) -> None:
    """C8 + C9: server reachability and token auth."""
    if not url:
        r.error("cannot check server: no URL resolved")
        return

    # C8: /health probe
    try:
        import httpx  # noqa: F401 — runtime availability check
    except ImportError:
        r.warn("httpx not installed; skipping connectivity checks")
        return

    try:
        from .client.api import VezirClient
        client = VezirClient(url, token or "", team_id=team_id, verify=None)
        result = client.health()
    except Exception as exc:
        # Catch SSL errors, connection errors, etc.
        exc_str = str(exc)
        if "SSL" in exc_str or "certificate" in exc_str.lower():
            r.error(
                f"server {url}: SSL error: {exc_str[:120]}.  "
                "Check SSL_CERT_FILE / VEZIR_CADDY_ROOT_CERT_PATH."
            )
        else:
            r.error(f"server {url}: unreachable: {exc_str[:120]}")
        return

    if not result.is_ok():
        r.error(f"server {url}: /health returned error: {result.error_message()[:120]}")
        return

    health_data = result.ok or {}
    server_version = health_data.get("version", "?")
    if server_version != __version__:
        r.warn(
            f"server version {server_version} != client version {__version__}"
        )
    else:
        r.ok(f"server {url}: reachable (version {server_version})")

    # C9: /api/me with token
    if not token:
        r.warn("no token configured; skipping auth check")
        return
    try:
        me_result = client.get_me()
    except Exception as exc:
        r.warn(f"/api/me failed: {exc}")
        return

    if not me_result.is_ok():
        if me_result.http_error:
            code, msg = me_result.http_error
            if code == 401:
                r.error(f"token rejected (401): {msg[:120]}")
            elif code == 403:
                r.error(f"token forbidden (403): {msg[:120]}")
            else:
                r.warn(f"/api/me returned {code}: {msg[:80]}")
        else:
            r.warn(f"/api/me error: {me_result.error_message()[:120]}")
        return

    me_data = me_result.ok or {}
    github = me_data.get("github", "?")
    role = "admin" if me_data.get("is_admin") else "scribe"
    mems = me_data.get("memberships") or []
    _check_token_membership(r, team_id, mems, github=github, role=role)


def _check_token_membership(
    r: _Results,
    team_id: str | None,
    mems: list,
    *,
    github: str = "?",
    role: str = "?",
) -> None:
    """C9 sub-check: report token memberships and flag a configured
    team that isn't among them.

    v0.7.4 keys memberships by team UUID (``team_id``) but users
    configure teams by slug; each membership also carries ``slug``.
    Match the configured ``team_id`` against EITHER the UUID or the
    slug so a slug-configured client isn't flagged with a false 403,
    and show the slug (human-friendly) in the summary.
    """
    if mems:
        team_summary = ", ".join(
            f"{m.get('slug') or m.get('team_id', '?')}({m.get('role', '?')})"
            for m in mems
        )
    else:
        team_summary = "(no team memberships)"
    r.ok(
        f"token accepted: github={github}  role={role}  teams=[{team_summary}]"
    )
    if team_id and not any(
        m.get("team_id") == team_id or m.get("slug") == team_id
        for m in mems
    ):
        r.error(
            f"configured team_id={team_id!r} is not in this token's "
            "memberships; team-scoped requests will return 403."
        )


def _check_deprecated_env_vars(r: _Results) -> None:
    """C10: deprecated VEZIR_MEET_* env vars."""
    for legacy, new in config._DEPRECATED_ENV_ALIASES.items():
        if os.environ.get(legacy):
            r.warn(
                f"{legacy} is set (deprecated).  "
                f"Rename to {new}."
            )


# ── server-side checks ─────────────────────────────────────────────────────


def _is_server_local() -> bool:
    """True if the local machine looks like a vezir server (queue DB exists)."""
    return bool(config.queue_db_path().is_file())


def _check_tokens_json(r: _Results) -> None:
    """S1 + S2: expired tokens + leftover legacy token store.

    v0.7.2: tokens live in the ``tokens`` table inside ``vezir.sqlite``.
    This check reads the table for expired rows (S2) and warns if a
    stale ``tokens.json`` (not the post-migration ``.migrated`` backstop)
    is still present (S1) — that would mean the migration hasn't run or
    someone hand-restored the old file.
    """
    import sqlite3
    import time

    # S1: a live tokens.json should not exist post-0.7.2.
    p = config.tokens_json_path()
    if p.exists():
        r.warn(
            "tokens.json still present.  v0.7.2 moved tokens into "
            "vezir.sqlite; restart the server to run the migration "
            "(it will be renamed to tokens.json.migrated)."
        )

    db_path = config.queue_db_path()
    if not db_path.exists():
        r.ok("tokens: no vezir.sqlite yet (no tokens issued)")
        return

    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT github, expires_at, label FROM tokens"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.OperationalError:
        # tokens table not created yet (fresh DB before first auth call).
        r.ok("tokens: table not yet created (no tokens issued)")
        return
    except Exception as exc:
        r.error(f"tokens: cannot read vezir.sqlite: {exc}")
        return

    if not rows:
        r.ok("tokens: 0 rows (no tokens issued)")
        return

    # S2: expired tokens still in the table.
    now = time.time()
    expired = []
    for t in rows:
        exp = t["expires_at"]
        if not exp:
            continue
        try:
            exp_epoch = (
                time.mktime(time.strptime(exp, "%Y-%m-%dT%H:%M:%SZ"))
                - time.timezone
            )
            if now >= exp_epoch:
                expired.append(t)
        except Exception:
            continue
    if expired:
        names = ", ".join(
            f"{t['github'] or '?'} ({t['label'] or '-'})"
            for t in expired
        )
        r.warn(
            f"{len(expired)} expired token(s) in vezir.sqlite: {names}.  "
            "Consider revoking to clean up: "
            "`vezir token revoke --github <handle> --label <label>`."
        )
    else:
        r.ok(f"tokens: {len(rows)} active token(s)")


def _check_server_data_perms(r: _Results) -> None:
    """S3: data directory and DB/token store permissions."""
    _check_dir_perms(r, config.data_dir(), "VEZIR_DATA")
    _check_file_perms(r, config.queue_db_path(), "vezir.sqlite")
    # The legacy file may still exist as a .migrated backstop.
    if config.tokens_json_path().exists():
        _check_file_perms(r, config.tokens_json_path(), "tokens.json")
    teams_root = config.teams_dir()
    if teams_root.is_dir():
        for child in teams_root.iterdir():
            if child.is_dir():
                _check_file_perms(
                    r,
                    child / "speaker_profiles.json",
                    f"teams/{child.name}/speaker_profiles.json",
                )


def _check_migrations(r: _Results) -> None:
    """S4: migration status."""
    import sqlite3

    db = config.queue_db_path()
    if not db.is_file():
        r.warn("queue DB does not exist; migrations have not run")
        return

    try:
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
    except Exception as exc:
        r.error(f"cannot open queue DB: {exc}")
        return

    try:
        # Check if schema_migrations table exists.
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='schema_migrations'"
        )
        if not cur.fetchone():
            r.warn("schema_migrations table missing; migrations have not run")
            conn.close()
            return

        applied = conn.execute(
            "SELECT version, applied_at FROM schema_migrations "
            "ORDER BY applied_at"
        ).fetchall()
        conn.close()
    except Exception as exc:
        r.error(f"error reading schema_migrations: {exc}")
        try:
            conn.close()
        except Exception:
            pass
        return

    applied_versions = {row["version"] for row in applied}

    # Known migrations (from migrations.py:ALL_MIGRATIONS).
    expected = ["0.6.0-multi-team", "0.6.2-per-team-voiceprints"]

    for version in expected:
        if version in applied_versions:
            ts = next(
                (r["applied_at"] for r in applied if r["version"] == version),
                "?",
            )
            r.ok(f"migration {version}: applied ({ts})")
        else:
            r.error(
                f"migration {version}: NOT applied.  "
                "Run `vezir serve` once to trigger automatic migration."
            )


def _check_millet_binary(r: _Results) -> None:
    """S5: millet binary availability."""
    try:
        binary = config.meet_binary()
        r.ok(f"millet binary: {binary}")
    except RuntimeError:
        r.warn(
            "millet binary not found.  Install millet-pipeline "
            "or set VEZIR_MILLET_BIN.  The worker cannot process jobs "
            "without it."
        )


def _check_voiceprint_dbs(r: _Results) -> None:
    """S6: per-team voiceprint DB existence."""
    teams_root = config.teams_dir()
    if not teams_root.is_dir():
        return

    for child in sorted(teams_root.iterdir()):
        if not child.is_dir():
            continue
        db = child / "speaker_profiles.json"
        if not db.exists():
            r.warn(
                f"team '{child.name}': no voiceprint DB at {db}.  "
                "The worker will create an empty one on next job."
            )


def _check_server_json(r: _Results) -> None:
    """S8: validate server.json if present."""
    p = config.server_json_path()
    if not p.is_file():
        return  # optional file, skip silently
    try:
        import json as _json
        data = _json.loads(p.read_text())
    except Exception as exc:
        r.error(f"server.json: invalid JSON: {exc}")
        return
    if not isinstance(data, dict):
        r.error("server.json: top-level value must be a JSON object")
        return
    alt = data.get("alternate_urls")
    if alt is None:
        r.ok("server.json: present (no alternate_urls)")
        return
    if not isinstance(alt, list):
        r.error("server.json: alternate_urls must be a list of URL strings")
        return
    bad = [u for u in alt if not isinstance(u, str) or not u.startswith("http")]
    if bad:
        r.error(f"server.json: invalid entries in alternate_urls: {bad}")
        return
    r.ok(f"server.json: {len(alt)} alternate URL(s) configured")


def _check_stale_jobs(r: _Results) -> None:
    """S7: jobs stuck in non-terminal status for >30 minutes."""
    import sqlite3
    import time

    db = config.queue_db_path()
    if not db.is_file():
        return

    try:
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, status, updated_at, github, team_id FROM jobs "
            "WHERE status IN ('transcribing', 'summarizing', 'syncing', "
            "'labeling') ORDER BY updated_at"
        ).fetchall()
        conn.close()
    except Exception:
        return

    if not rows:
        return

    threshold = time.time() - (30 * 60)  # 30 minutes ago
    stale = []
    for row in rows:
        updated = row["updated_at"]
        if not updated:
            stale.append(row)
            continue
        try:
            ts = (
                time.mktime(time.strptime(updated, "%Y-%m-%dT%H:%M:%SZ"))
                - time.timezone
            )
            if ts < threshold:
                stale.append(row)
        except Exception:
            stale.append(row)

    if stale:
        ids = ", ".join(
            f"{row['id'][:12]} ({row['status']})"
            for row in stale
        )
        r.warn(
            f"{len(stale)} job(s) stuck in non-terminal status for >30min: "
            f"{ids}.  The worker may have crashed; check "
            "`vezir status` and server logs."
        )


# ── main entry point ───────────────────────────────────────────────────────


def run_doctor() -> int:
    """Run all doctor checks, print results, return exit code (0 or 1)."""
    r = _Results()

    print(f"vezir doctor  (client {__version__})")
    print()

    # ── client checks ──
    print("Client:")
    url, token, team_id = _check_credential_resolution(r)
    _check_env_shadow(r)
    _check_teams_json_schema(r)
    _check_token_format(r, token)
    _check_ssl_cert(r, url)
    _check_client_file_perms(r)
    _check_deprecated_env_vars(r)
    _check_nvpn(r)
    _check_tunnel_reachability(r, url)
    _check_server_connectivity(r, url, token, team_id)
    r.print_all()

    # ── server checks (only if local data exists) ──
    if _is_server_local():
        r_server = _Results()
        print()
        print("Server (local data detected):")
        _check_tokens_json(r_server)
        _check_server_data_perms(r_server)
        _check_migrations(r_server)
        _check_millet_binary(r_server)
        _check_voiceprint_dbs(r_server)
        _check_server_json(r_server)
        _check_stale_jobs(r_server)
        r_server.print_all()
        # Merge into the main results for exit code.
        r._rows.extend(r_server._rows)

    # ── summary ──
    print()
    errors = sum(1 for sev, _ in r.rows if sev == _ERROR)
    warns = sum(1 for sev, _ in r.rows if sev == _WARN)
    oks = sum(1 for sev, _ in r.rows if sev == _OK)

    if errors:
        print(f"{errors} error(s), {warns} warning(s), {oks} ok")
    elif warns:
        print(f"{warns} warning(s), {oks} ok  (no errors)")
    else:
        print(f"all {oks} checks passed")

    return 1 if errors else 0
