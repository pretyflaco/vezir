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
    from vezir.server.enroll import PAYLOAD_VERSION, build_payload

    ca = "-----BEGIN CERTIFICATE-----\nABCDEF\n-----END CERTIFICATE-----\n"
    payload = build_payload("https://srv.ts.net", "vzr_abc", ca_pem=ca)
    obj = json.loads(payload)
    assert obj["v"] == PAYLOAD_VERSION == 2
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
