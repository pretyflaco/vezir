"""Tests for ``vezir doctor`` — v0.6.4.

Tests the individual check functions directly with controlled
fixtures rather than driving the full ``run_doctor`` entry point
(which prints to stdout and hits the real network).  A single
integration-style test at the end runs ``run_doctor`` in a fully
mocked environment.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def tmp_data(monkeypatch):
    """Isolated VEZIR_DATA + config dir for each test."""
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("VEZIR_DATA", d)
        config_dir = Path(d) / "config"
        config_dir.mkdir()
        # Redirect client config paths.
        monkeypatch.setattr(
            "vezir.client.config.client_config_path",
            lambda: config_dir / "client.json",
        )
        monkeypatch.setattr(
            "vezir.client.config.teams_config_path",
            lambda: config_dir / "teams.json",
        )
        yield Path(d)


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


# ── C1: credential resolution ──────────────────────────────────────────────


def test_c1_no_credentials(monkeypatch, tmp_data):
    from vezir.doctor import _check_credential_resolution, _Results

    monkeypatch.delenv("VEZIR_URL", raising=False)
    monkeypatch.delenv("VEZIR_TOKEN", raising=False)
    r = _Results()
    url, token = _check_credential_resolution(r)
    assert url is None
    assert token is None
    assert any(sev == "ERROR" for sev, _ in r.rows)


def test_c1_env_wins(monkeypatch, tmp_data):
    from vezir.doctor import _check_credential_resolution, _Results

    monkeypatch.setenv("VEZIR_URL", "https://env.example")
    monkeypatch.setenv("VEZIR_TOKEN", "vzr_test123456789012345678901234567890123")
    r = _Results()
    url, token = _check_credential_resolution(r)
    assert url == "https://env.example"
    assert any("source=env" in msg for _, msg in r.rows)


def test_c1_teams_json_wins_over_client(monkeypatch, tmp_data):
    from vezir.client.config import client_config_path, teams_config_path
    from vezir.doctor import _check_credential_resolution, _Results

    monkeypatch.delenv("VEZIR_URL", raising=False)
    monkeypatch.delenv("VEZIR_TOKEN", raising=False)
    _write_json(teams_config_path(), {
        "teams": [{"id": "blink", "url": "https://teams.example",
                   "token": "vzr_teams12345678901234567890123456789012"}],
        "active": "blink",
    })
    _write_json(client_config_path(), {
        "url": "https://client.example",
        "token": "vzr_client1234567890123456789012345678901",
    })
    r = _Results()
    url, token = _check_credential_resolution(r)
    assert url == "https://teams.example"
    # Should warn about coexistence.
    assert any("client.json also has url" in msg for _, msg in r.rows)


def test_c1_url_disagreement_env_vs_teams(monkeypatch, tmp_data):
    from vezir.client.config import teams_config_path
    from vezir.doctor import _check_credential_resolution, _Results

    monkeypatch.setenv("VEZIR_URL", "https://env.example")
    monkeypatch.setenv("VEZIR_TOKEN", "vzr_env12345678901234567890123456789012x")
    _write_json(teams_config_path(), {
        "teams": [{"id": "blink", "url": "https://different.example",
                   "token": "vzr_teams12345678901234567890123456789012"}],
        "active": "blink",
    })
    r = _Results()
    _check_credential_resolution(r)
    # Should warn about disagreement.
    assert any("differs from VEZIR_URL" in msg for _, msg in r.rows)


# ── C3: teams.json schema ──────────────────────────────────────────────────


def test_c3_teams_json_missing(monkeypatch, tmp_data):
    from vezir.doctor import _check_teams_json_schema, _Results

    r = _Results()
    _check_teams_json_schema(r)
    assert any("not present" in msg for _, msg in r.rows)
    assert all(sev != "ERROR" for sev, _ in r.rows)


def test_c3_teams_json_orphaned_active(monkeypatch, tmp_data):
    from vezir.client.config import teams_config_path
    from vezir.doctor import _check_teams_json_schema, _Results

    _write_json(teams_config_path(), {
        "teams": [{"id": "blink", "url": "u", "token": "t"}],
        "active": "ghost",
    })
    r = _Results()
    _check_teams_json_schema(r)
    assert any("ghost" in msg and "not in teams list" in msg
               for _, msg in r.rows)


def test_c3_teams_json_dropped_entries(monkeypatch, tmp_data):
    from vezir.client.config import teams_config_path
    from vezir.doctor import _check_teams_json_schema, _Results

    _write_json(teams_config_path(), {
        "teams": [
            {"id": "ok", "url": "u", "token": "t"},
            {"id": "bad"},  # missing url + token
            "not_a_dict",
        ],
        "active": "ok",
    })
    r = _Results()
    _check_teams_json_schema(r)
    assert any("2 team entry" in msg and "dropped" in msg
               for _, msg in r.rows)


def test_c3_teams_json_invalid_json(monkeypatch, tmp_data):
    from vezir.client.config import teams_config_path
    from vezir.doctor import _check_teams_json_schema, _Results

    teams_config_path().parent.mkdir(parents=True, exist_ok=True)
    teams_config_path().write_text("{invalid", encoding="utf-8")
    r = _Results()
    _check_teams_json_schema(r)
    assert any(sev == "ERROR" and "invalid JSON" in msg
               for sev, msg in r.rows)


# ── C5: token format ───────────────────────────────────────────────────────


def test_c5_valid_token(tmp_data):
    from vezir.doctor import _check_token_format, _Results

    r = _Results()
    # 47 chars: "vzr_" (4) + 43 chars of urlsafe base64
    _check_token_format(r, "vzr_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopq")
    assert not r.rows  # no warnings


def test_c5_nvpn_invite(tmp_data):
    from vezir.doctor import _check_token_format, _Results

    r = _Results()
    _check_token_format(r, "nvpn://invite/...")
    assert any(sev == "ERROR" and "nvpn invite" in msg
               for sev, msg in r.rows)


def test_c5_wrong_prefix(tmp_data):
    from vezir.doctor import _check_token_format, _Results

    r = _Results()
    _check_token_format(r, "abc_1234567890")
    assert any("does not start with" in msg for _, msg in r.rows)


def test_c5_wrong_length(tmp_data):
    from vezir.doctor import _check_token_format, _Results

    r = _Results()
    _check_token_format(r, "vzr_tooshort")
    assert any("length" in msg for _, msg in r.rows)


# ── C6: SSL cert config ───────────────────────────────────────────────────


def test_c6_https_no_cert(monkeypatch, tmp_data):
    from vezir.doctor import _check_ssl_cert, _Results

    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("VEZIR_CADDY_ROOT_CERT_PATH", raising=False)
    r = _Results()
    _check_ssl_cert(r, "https://example.com")
    assert any("neither SSL_CERT_FILE" in msg for _, msg in r.rows)


def test_c6_http_url_no_check(monkeypatch, tmp_data):
    from vezir.doctor import _check_ssl_cert, _Results

    r = _Results()
    _check_ssl_cert(r, "http://localhost:8000")
    assert not r.rows


def test_c6_cert_file_exists(monkeypatch, tmp_data):
    from vezir.doctor import _check_ssl_cert, _Results

    cert = tmp_data / "cert.pem"
    cert.write_text("-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n")
    monkeypatch.setenv("SSL_CERT_FILE", str(cert))
    monkeypatch.delenv("VEZIR_CADDY_ROOT_CERT_PATH", raising=False)
    r = _Results()
    _check_ssl_cert(r, "https://example.com")
    assert any(sev == "OK" and "PEM" in msg for sev, msg in r.rows)


def test_c6_cert_file_missing(monkeypatch, tmp_data):
    from vezir.doctor import _check_ssl_cert, _Results

    monkeypatch.setenv("SSL_CERT_FILE", "/tmp/nonexistent.pem")
    monkeypatch.delenv("VEZIR_CADDY_ROOT_CERT_PATH", raising=False)
    r = _Results()
    _check_ssl_cert(r, "https://example.com")
    assert any(sev == "ERROR" and "does not exist" in msg
               for sev, msg in r.rows)


# ── C7: file perms ─────────────────────────────────────────────────────────


def test_c7_file_perms_ok(tmp_data):
    from vezir.doctor import _check_file_perms, _Results

    f = tmp_data / "private.json"
    f.write_text("{}")
    f.chmod(0o600)
    r = _Results()
    _check_file_perms(r, f, "test")
    assert not r.rows


def test_c7_file_perms_wrong(tmp_data):
    from vezir.doctor import _check_file_perms, _Results

    f = tmp_data / "world.json"
    f.write_text("{}")
    f.chmod(0o644)
    r = _Results()
    _check_file_perms(r, f, "test")
    assert any("0o644" in msg for _, msg in r.rows)


# ── C10: deprecated env vars ──────────────────────────────────────────────


def test_c10_no_deprecated(monkeypatch, tmp_data):
    from vezir.doctor import _check_deprecated_env_vars, _Results

    for var in ("VEZIR_MEET_BIN", "VEZIR_MEET_DEVICE"):
        monkeypatch.delenv(var, raising=False)
    r = _Results()
    _check_deprecated_env_vars(r)
    assert not r.rows


def test_c10_deprecated_present(monkeypatch, tmp_data):
    from vezir.doctor import _check_deprecated_env_vars, _Results

    monkeypatch.setenv("VEZIR_MEET_BIN", "/usr/bin/millet")
    r = _Results()
    _check_deprecated_env_vars(r)
    assert any("VEZIR_MEET_BIN" in msg and "deprecated" in msg
               for _, msg in r.rows)


# ── S1 + S2: tokens.json ──────────────────────────────────────────────────


def test_s1_orphaned_tokens(monkeypatch, tmp_data):
    from vezir.doctor import _check_tokens_json, _Results

    _write_json(tmp_data / "tokens.json", {"tokens": [
        {"github": "alice", "token_hash": "abc", "team_id": "blink",
         "issued_at": "2025-01-01T00:00:00Z"},
        {"github": "bob", "token_hash": "def",
         "issued_at": "2025-01-01T00:00:00Z"},  # no team_id!
    ]})
    monkeypatch.setenv("VEZIR_DATA", str(tmp_data))
    r = _Results()
    _check_tokens_json(r)
    assert any(sev == "ERROR" and "missing team_id" in msg
               for sev, msg in r.rows)


def test_s2_expired_tokens(monkeypatch, tmp_data):
    from vezir.doctor import _check_tokens_json, _Results

    _write_json(tmp_data / "tokens.json", {"tokens": [
        {"github": "alice", "token_hash": "abc", "team_id": "blink",
         "issued_at": "2020-01-01T00:00:00Z",
         "expires_at": "2020-06-01T00:00:00Z",
         "label": "old-phone"},
    ]})
    monkeypatch.setenv("VEZIR_DATA", str(tmp_data))
    r = _Results()
    _check_tokens_json(r)
    assert any(sev == "WARN" and "expired" in msg
               for sev, msg in r.rows)


def test_s12_clean_tokens(monkeypatch, tmp_data):
    from vezir.doctor import _check_tokens_json, _Results

    _write_json(tmp_data / "tokens.json", {"tokens": [
        {"github": "alice", "token_hash": "abc", "team_id": "blink",
         "issued_at": "2025-01-01T00:00:00Z",
         "expires_at": "2099-01-01T00:00:00Z"},
    ]})
    monkeypatch.setenv("VEZIR_DATA", str(tmp_data))
    r = _Results()
    _check_tokens_json(r)
    assert all(sev == "OK" for sev, _ in r.rows)


# ── S4: migrations ─────────────────────────────────────────────────────────


def test_s4_migrations_applied(monkeypatch, tmp_data):
    import sqlite3

    from vezir.doctor import _check_migrations, _Results

    db_path = tmp_data / "vezir.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, "
        "applied_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO schema_migrations VALUES "
        "('0.6.0-multi-team', '2025-01-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO schema_migrations VALUES "
        "('0.6.2-per-team-voiceprints', '2025-02-01T00:00:00Z')"
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("VEZIR_DATA", str(tmp_data))
    r = _Results()
    _check_migrations(r)
    assert all(sev == "OK" for sev, _ in r.rows)
    assert len(r.rows) == 2


def test_s4_migration_missing(monkeypatch, tmp_data):
    import sqlite3

    from vezir.doctor import _check_migrations, _Results

    db_path = tmp_data / "vezir.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, "
        "applied_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO schema_migrations VALUES "
        "('0.6.0-multi-team', '2025-01-01T00:00:00Z')"
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("VEZIR_DATA", str(tmp_data))
    r = _Results()
    _check_migrations(r)
    assert any(sev == "ERROR" and "0.6.2-per-team-voiceprints" in msg
               and "NOT applied" in msg for sev, msg in r.rows)


# ── S5: millet binary ──────────────────────────────────────────────────────


def test_s5_millet_found(monkeypatch, tmp_data):
    from vezir.doctor import _check_millet_binary, _Results

    monkeypatch.setattr("vezir.config.meet_binary", lambda: "/usr/bin/millet")
    r = _Results()
    _check_millet_binary(r)
    assert any(sev == "OK" and "/usr/bin/millet" in msg
               for sev, msg in r.rows)


def test_s5_millet_not_found(monkeypatch, tmp_data):
    from vezir.doctor import _check_millet_binary, _Results

    def _raise():
        raise RuntimeError("not found")
    monkeypatch.setattr("vezir.config.meet_binary", _raise)
    r = _Results()
    _check_millet_binary(r)
    assert any(sev == "WARN" and "not found" in msg
               for sev, msg in r.rows)


# ── S7: stale jobs ─────────────────────────────────────────────────────────


def test_s7_stale_job_detected(monkeypatch, tmp_data):
    import sqlite3
    import time

    from vezir.doctor import _check_stale_jobs, _Results

    db_path = tmp_data / "vezir.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE jobs (id TEXT PRIMARY KEY, status TEXT, "
        "updated_at TEXT, github TEXT, team_id TEXT)"
    )
    # Job stuck for 2 hours.
    old_ts = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 7200)
    )
    conn.execute(
        "INSERT INTO jobs VALUES (?, ?, ?, ?, ?)",
        ("STALE01", "transcribing", old_ts, "alice", "blink"),
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("VEZIR_DATA", str(tmp_data))
    r = _Results()
    _check_stale_jobs(r)
    assert any(sev == "WARN" and "STALE01" in msg
               for sev, msg in r.rows)


def test_s7_no_stale_jobs(monkeypatch, tmp_data):
    import sqlite3
    import time

    from vezir.doctor import _check_stale_jobs, _Results

    db_path = tmp_data / "vezir.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE jobs (id TEXT PRIMARY KEY, status TEXT, "
        "updated_at TEXT, github TEXT, team_id TEXT)"
    )
    # Job recently updated.
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    conn.execute(
        "INSERT INTO jobs VALUES (?, ?, ?, ?, ?)",
        ("FRESH01", "transcribing", now, "alice", "blink"),
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("VEZIR_DATA", str(tmp_data))
    r = _Results()
    _check_stale_jobs(r)
    assert not r.rows


# ── Integration: run_doctor with mocked network ────────────────────────────


def test_run_doctor_all_clean_no_server(monkeypatch, tmp_data, capsys):
    """Client-only run with valid env credentials, no server checks."""
    from vezir.doctor import run_doctor

    monkeypatch.setenv("VEZIR_URL", "http://localhost:9999")
    monkeypatch.setenv("VEZIR_TOKEN", "vzr_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopq")
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("VEZIR_CADDY_ROOT_CERT_PATH", raising=False)
    # Remove the queue DB so server checks don't run.
    db = tmp_data / "vezir.sqlite"
    if db.exists():
        db.unlink()
    # Mock out connectivity to avoid real network.
    monkeypatch.setattr(
        "vezir.doctor._check_tunnel_reachability",
        lambda r, u: None,
    )
    monkeypatch.setattr(
        "vezir.doctor._check_nvpn",
        lambda r: None,
    )
    monkeypatch.setattr(
        "vezir.doctor._check_server_connectivity",
        lambda r, u, t: r.ok(f"server {u}: mocked ok"),
    )
    code = run_doctor()
    captured = capsys.readouterr().out
    assert code == 0
    assert "all" in captured and "passed" in captured
