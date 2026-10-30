"""Tests for the enrollment QR payload + terminal renderer.

v0.7.0: the HTML ``/admin/enroll`` page was removed.  QR codes are
now built by the CLI (``vezir token enroll``) directly in the
terminal via ``vezir.server.enroll.render_qr_terminal``.  The shared
``build_payload`` + ``_load_caddy_root_cert`` helpers stay covered
here because they're still the canonical payload schema for the
Android/iOS clients.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

# ── payload schema is canonical ─────────────────────────────────────────────


def test_payload_v1_when_no_ca(monkeypatch):
    """Without a CA cert configured, build_payload emits the v1 shape so
    pre-0.1.12 Android/iOS clients keep parsing it unchanged."""
    monkeypatch.delenv("VEZIR_CADDY_ROOT_CERT_PATH", raising=False)
    from vezir.server.enroll import build_payload

    payload = build_payload("http://server.example:8000", "vzr_abc123")
    obj = json.loads(payload)
    assert obj == {
        "v": 1,
        "url": "http://server.example:8000",
        "token": "vzr_abc123",
    }
    # Compact (no spaces) so QR is smaller.
    assert " " not in payload


def test_payload_v2_when_ca_provided():
    """With a CA cert, build_payload bumps to v2 and embeds the PEM."""
    from vezir.server.enroll import LEGACY_PAYLOAD_VERSION, build_payload

    ca = "-----BEGIN CERTIFICATE-----\nABCDEF\n-----END CERTIFICATE-----\n"
    payload = build_payload("https://srv.ts.net", "vzr_abc", ca_pem=ca)
    obj = json.loads(payload)
    assert obj["v"] == LEGACY_PAYLOAD_VERSION == 2
    assert obj["url"] == "https://srv.ts.net"
    assert obj["token"] == "vzr_abc"
    assert obj["ca_pem"] == ca


def test_payload_ca_loaded_from_env(monkeypatch, tmp_path):
    """When VEZIR_CADDY_ROOT_CERT_PATH points at a real PEM,
    _load_caddy_root_cert returns it; the CLI's ``token enroll``
    command then upgrades the payload to v2.
    """
    cert = tmp_path / "vezir-root.crt"
    cert.write_text(
        "-----BEGIN CERTIFICATE-----\nMOCKED\n-----END CERTIFICATE-----\n"
    )
    monkeypatch.setenv("VEZIR_CADDY_ROOT_CERT_PATH", str(cert))

    from vezir.server.enroll import _load_caddy_root_cert
    loaded = _load_caddy_root_cert()
    assert loaded is not None
    assert "MOCKED" in loaded


def test_payload_ca_silently_falls_back_on_bad_path(monkeypatch):
    """A misconfigured cert path must NOT break enrollment.  Falls
    back to v1 and just logs a warning."""
    monkeypatch.setenv("VEZIR_CADDY_ROOT_CERT_PATH", "/does/not/exist.crt")
    from vezir.server.enroll import _load_caddy_root_cert, build_payload

    assert _load_caddy_root_cert() is None
    payload = build_payload("http://srv:8000", "vzr_t")
    assert json.loads(payload)["v"] == 1


def test_payload_ca_rejected_when_not_pem(monkeypatch, tmp_path):
    """If the configured file isn't a PEM cert, fall back to v1 rather
    than dumping garbage into the QR.
    """
    fake = tmp_path / "garbage"
    fake.write_text("this is not a certificate")
    monkeypatch.setenv("VEZIR_CADDY_ROOT_CERT_PATH", str(fake))
    from vezir.server.enroll import _load_caddy_root_cert
    assert _load_caddy_root_cert() is None


def test_payload_ca_rejected_when_too_large(monkeypatch, tmp_path):
    """Hard cap protects QR scannability."""
    big = tmp_path / "huge.crt"
    big.write_text(
        "-----BEGIN CERTIFICATE-----\n"
        + ("A" * 100_000)
        + "\n-----END CERTIFICATE-----\n"
    )
    monkeypatch.setenv("VEZIR_CADDY_ROOT_CERT_PATH", str(big))
    from vezir.server.enroll import _load_caddy_root_cert
    assert _load_caddy_root_cert() is None


# ── terminal QR rendering ────────────────────────────────────────────────────


def test_render_qr_terminal_produces_ansi_output():
    """``render_qr_terminal`` returns a multi-line string that the CLI
    prints during ``vezir token enroll``.

    v0.15.0: segno's terminal() runs in ``compact`` mode — half-block
    Unicode characters with NO ANSI escape sequences, so the same art is
    safe to embed in Textual widgets (the reauth modal's QR was rendered
    as literal ``\\x1b[7m`` garbage before).
    """
    from vezir.server.enroll import build_payload, render_qr_terminal

    payload = build_payload("http://srv:8000", "vzr_abc")
    art = render_qr_terminal(payload)
    assert isinstance(art, str)
    assert "\n" in art
    # Compact mode: no ANSI escapes, half-block characters present.
    assert "\x1b[" not in art
    assert any(ch in art for ch in "▄▀█")


def test_render_qr_terminal_handles_v2_payload(tmp_path, monkeypatch):
    """The QR also renders cleanly with the larger v2 payload
    (URL + token + CA PEM)."""
    ca = "-----BEGIN CERTIFICATE-----\nABCDEF\n-----END CERTIFICATE-----\n"
    from vezir.server.enroll import build_payload, render_qr_terminal

    payload = build_payload("https://srv.ts.net", "vzr_abc", ca_pem=ca)
    art = render_qr_terminal(payload)
    assert "\n" in art


# Suppress an unused-import warning if pytest fixtures are dropped.
_ = pytest



# ── v3 enrollment codes (0.27.0, security audit M-8) ─────────────────────────


@pytest.fixture
def tmp_data(monkeypatch):
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("VEZIR_DATA", d)
        yield Path(d)


def test_payload_v3_carries_a_code_not_a_token():
    from vezir.server.enroll import PAYLOAD_VERSION, build_payload

    obj = json.loads(build_payload("https://srv", enroll_code="vzre_abc"))
    assert obj == {"v": PAYLOAD_VERSION, "url": "https://srv", "enroll_code": "vzre_abc"}
    assert PAYLOAD_VERSION == 3
    with pytest.raises(ValueError):
        build_payload("https://srv")


def test_code_is_single_use_and_issues_the_token_only_on_exchange(tmp_data):
    from vezir.server import auth, enroll

    code, expires = enroll.create_code("alice", label="phone", token_ttl_seconds=3600)
    assert code.startswith("vzre_") and len(code) > 24
    # No bearer exists yet.
    assert auth.list_tokens() == [] or all(t["github"] != "alice" for t in auth.list_tokens())

    out = enroll.exchange_code(code, client_ip="203.0.113.5")
    assert out["github"] == "alice" and out["label"] == "phone"
    assert out["token"].startswith("vzr_")
    assert auth.lookup_identity(out["token"]) == ("alice", False)
    assert out["expires_at"] is not None

    # Second use fails identically to an unknown code.
    with pytest.raises(enroll.EnrollError):
        enroll.exchange_code(code)
    with pytest.raises(enroll.EnrollError):
        enroll.exchange_code("vzre_" + "x" * 32)


def test_code_expires(tmp_data, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from vezir.server import enroll

    code, _ = enroll.create_code("bob")
    later = datetime.now(timezone.utc) + timedelta(seconds=enroll.ENROLL_CODE_TTL_SECONDS + 1)
    monkeypatch.setattr(enroll, "_now", lambda: later)
    with pytest.raises(enroll.EnrollError):
        enroll.exchange_code(code)


def test_concurrent_exchange_only_one_wins(tmp_data):
    import threading

    from vezir.server import enroll

    code, _ = enroll.create_code("carol")
    wins: list = []
    losses: list = []
    barrier = threading.Barrier(5)

    def go():
        barrier.wait()
        try:
            wins.append(enroll.exchange_code(code))
        except enroll.EnrollError:
            losses.append(1)

    ts = [threading.Thread(target=go) for _ in range(5)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(wins) == 1 and len(losses) == 4


def test_exchange_endpoint(tmp_data, monkeypatch):
    from fastapi.testclient import TestClient

    from vezir.server import enroll
    from vezir.server.app import create_app

    client = TestClient(create_app())
    code, _ = enroll.create_code("dave", token_ttl_seconds=None)
    r = client.post("/api/enroll/exchange", json={"code": code})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["github"] == "dave" and body["token"].startswith("vzr_")
    assert body["expires_at"] is None
    assert "ca_pem" not in body
    # The token works, and the code is spent.
    me = client.get("/api/me", headers={"Authorization": f"Bearer {body['token']}"})
    assert me.status_code == 200
    assert client.post("/api/enroll/exchange", json={"code": code}).status_code == 401
    assert client.post("/api/enroll/exchange", json={"code": "nope"}).status_code == 401


def test_exchange_endpoint_is_on_the_login_bucket(tmp_data):
    from fastapi.routing import APIRoute

    from vezir.server import ratelimit
    from vezir.server.app import create_app

    app = create_app()
    route = next(
        r for r in app.routes if isinstance(r, APIRoute) and r.path == "/api/enroll/exchange"
    )
    assert ratelimit.limit_login in [d.dependency for d in route.dependencies]


def test_cli_enroll_default_is_v3_and_legacy_still_available(tmp_data, monkeypatch):
    from click.testing import CliRunner

    from vezir import cli as cli_mod
    from vezir.server import auth, enroll

    runner = CliRunner()
    r = runner.invoke(cli_mod.main, ["token", "enroll", "--github", "erin", "--server", "https://s"])
    assert r.exit_code == 0, r.output
    assert "Enrollment code issued" in r.output
    assert "--enroll-code vzre_" in r.output
    assert "vzr_" not in r.output.replace("vzre_", "")  # no bearer printed
    # Nothing issued until exchange.
    assert all(t["github"] != "erin" for t in auth.list_tokens())
    code = next(w for w in r.output.split() if w.startswith("vzre_"))
    assert enroll.exchange_code(code)["github"] == "erin"

    r = runner.invoke(cli_mod.main, ["token", "enroll", "--github", "frank", "--server", "https://s",
                                    "--legacy"])
    assert r.exit_code == 0, r.output
    assert "legacy QR" in r.output and "VEZIR_TOKEN=" not in r.output
    r = runner.invoke(cli_mod.main, ["token", "enroll", "--github", "frank", "--server", "https://s",
                                    "--legacy", "--print-token"])
    assert "VEZIR_TOKEN=vzr_" in r.output
