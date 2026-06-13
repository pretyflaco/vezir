"""Tests for vezir.client.trust.resolve_verify.

The key invariant (regression guard): a configured internal CA must be
*added to* the default trust store, never *replace* it -- otherwise the
public Let's Encrypt front fails with 'unable to get local issuer
certificate' while the internal CA env var is set.
"""
from __future__ import annotations

import datetime
import ssl

import pytest

from vezir.client import trust


@pytest.fixture
def ca_pem(tmp_path):
    """Write a throwaway self-signed CA cert to disk; return its path."""
    crypto = pytest.importorskip("cryptography")
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "vezir-test-ca")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    p = tmp_path / "vezir-test-ca.crt"
    p.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    _ = crypto  # silence unused
    return p


def _ca_count(ctx: ssl.SSLContext) -> int:
    return len(ctx.get_ca_certs())


def _default_baseline(monkeypatch) -> int:
    """CA count of a default context with NO trust env vars set.

    ``ssl.create_default_context()`` honors SSL_CERT_FILE at call time, so the
    baseline must be measured with those vars cleared to isolate what the
    internal CA adds.
    """
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("VEZIR_CADDY_ROOT_CERT_PATH", raising=False)
    return _ca_count(ssl.create_default_context())


# ── explicit wins ────────────────────────────────────────────────────────────


def test_explicit_true_wins(monkeypatch, ca_pem):
    monkeypatch.setenv("SSL_CERT_FILE", str(ca_pem))
    assert trust.resolve_verify(True) is True


def test_explicit_path_wins(monkeypatch, ca_pem):
    monkeypatch.setenv("SSL_CERT_FILE", str(ca_pem))
    assert trust.resolve_verify("/some/explicit/path") == "/some/explicit/path"


def test_explicit_context_wins(monkeypatch, ca_pem):
    monkeypatch.setenv("SSL_CERT_FILE", str(ca_pem))
    ctx = ssl.create_default_context()
    assert trust.resolve_verify(ctx) is ctx


# ── no extra CA -> default True ──────────────────────────────────────────────


def test_no_env_returns_true(monkeypatch):
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("VEZIR_CADDY_ROOT_CERT_PATH", raising=False)
    assert trust.resolve_verify() is True


def test_env_points_at_missing_file_returns_true(monkeypatch, tmp_path):
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "nope.crt"))
    monkeypatch.delenv("VEZIR_CADDY_ROOT_CERT_PATH", raising=False)
    assert trust.resolve_verify() is True


# ── the core regression: APPEND, don't replace ───────────────────────────────


def test_internal_ca_is_appended_not_replacing(monkeypatch, ca_pem):
    """Context must contain the default roots PLUS the internal CA."""
    baseline = _default_baseline(monkeypatch)
    monkeypatch.setenv("SSL_CERT_FILE", str(ca_pem))

    ctx = trust.resolve_verify()
    assert isinstance(ctx, ssl.SSLContext)
    # Default store preserved (public certs still validate) ...
    # ... and the internal CA was added on top.
    assert _ca_count(ctx) == baseline + 1
    subjects = [
        dict(x for rdn in c["subject"] for x in rdn).get("commonName")
        for c in ctx.get_ca_certs()
    ]
    assert "vezir-test-ca" in subjects


def test_both_env_vars_deduped(monkeypatch, ca_pem):
    """Same path in both env vars is loaded once, not twice."""
    baseline = _default_baseline(monkeypatch)
    monkeypatch.setenv("SSL_CERT_FILE", str(ca_pem))
    monkeypatch.setenv("VEZIR_CADDY_ROOT_CERT_PATH", str(ca_pem))
    ctx = trust.resolve_verify()
    assert isinstance(ctx, ssl.SSLContext)
    assert _ca_count(ctx) == baseline + 1


def test_malformed_ca_skipped_keeps_default_store(monkeypatch, tmp_path):
    """A garbage 'CA' file must not blind us to the default store."""
    baseline = _default_baseline(monkeypatch)
    bad = tmp_path / "bad.crt"
    bad.write_text("not a certificate\n")
    monkeypatch.setenv("SSL_CERT_FILE", str(bad))
    ctx = trust.resolve_verify()
    assert isinstance(ctx, ssl.SSLContext)
    # Default roots intact; malformed extra simply skipped.
    assert _ca_count(ctx) == baseline
