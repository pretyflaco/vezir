"""Tests for the unauthenticated GET /ca.crt endpoint (v0.7.7).

Serves the PUBLIC Caddy internal CA certificate so onboarding
teammates can fetch it over the tunnel without an SSH login on the
server box.  Only the private key is sensitive (and never leaves the
server); the cert itself is safe to serve openly.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

_FAKE_PEM = (
    "-----BEGIN CERTIFICATE-----\n"
    "MIIBfakebase64contentforatestcertificatethatisnotreal==\n"
    "-----END CERTIFICATE-----\n"
)


@pytest.fixture
def tmp_data(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("VEZIR_DATA", d)
        yield Path(d)


def _client():
    from fastapi.testclient import TestClient

    from vezir.server.app import create_app
    return TestClient(create_app(), follow_redirects=False)


def test_ca_crt_served_when_configured(tmp_data, monkeypatch):
    pem_path = tmp_data / "vezir-internal-ca.crt"
    pem_path.write_text(_FAKE_PEM, encoding="utf-8")
    monkeypatch.setenv("VEZIR_CADDY_ROOT_CERT_PATH", str(pem_path))

    r = _client().get("/ca.crt")
    assert r.status_code == 200
    assert "BEGIN CERTIFICATE" in r.text
    assert r.text == _FAKE_PEM
    assert r.headers["content-type"].startswith("application/x-pem-file")
    assert "attachment" in r.headers.get("content-disposition", "")


def test_ca_crt_needs_no_auth(tmp_data, monkeypatch):
    """No Authorization header, no X-Team-Id — still 200."""
    pem_path = tmp_data / "vezir-internal-ca.crt"
    pem_path.write_text(_FAKE_PEM, encoding="utf-8")
    monkeypatch.setenv("VEZIR_CADDY_ROOT_CERT_PATH", str(pem_path))

    r = _client().get("/ca.crt")  # no headers at all
    assert r.status_code == 200


def test_ca_crt_404_when_unconfigured(tmp_data, monkeypatch):
    monkeypatch.delenv("VEZIR_CADDY_ROOT_CERT_PATH", raising=False)
    r = _client().get("/ca.crt")
    assert r.status_code == 404
