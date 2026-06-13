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


def _subjects(ctx: ssl.SSLContext) -> set[str]:
    """CommonNames of the certs ``ctx`` enumerates via get_ca_certs().

    NOTE: get_ca_certs() only enumerates certs loaded from a *cafile*, not a
    *capath* directory.  Whether the system default store is enumerable thus
    varies by platform (Debian/local uses a capath -> not enumerable; CI's
    image uses a cafile -> enumerable).  Tests must therefore assert on the
    *delta* the internal CA introduces, never on absolute counts.
    """
    out: set[str] = set()
    for c in ctx.get_ca_certs():
        cn = dict(x for rdn in c["subject"] for x in rdn).get("commonName")
        if cn:
            out.add(cn)
    return out


def _public_subjects() -> set[str]:
    """Subjects of the public roots the resolver seeds (same loader).

    Built via the resolver's own _load_public_roots so the comparison
    enumerates the exact same certs the resolver does, independent of how
    the platform's default store happens to be configured.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    trust._load_public_roots(ctx)
    return _subjects(ctx)


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
    """Context must contain the default roots PLUS the internal CA.

    Asserted as a *delta* over the default store (see _subjects docstring):
    the resolver's enumerable subjects == default enumerable subjects ∪
    {our test CA}.  This holds whether or not the platform's default store
    is itself enumerable.
    """
    public_subjects = _public_subjects()
    monkeypatch.setenv("SSL_CERT_FILE", str(ca_pem))

    ctx = trust.resolve_verify()
    assert isinstance(ctx, ssl.SSLContext)
    # The internal CA was added ...
    assert "vezir-test-ca" in _subjects(ctx)
    # ... on top of the default store (nothing dropped).
    assert public_subjects <= _subjects(ctx)
    assert _subjects(ctx) == public_subjects | {"vezir-test-ca"}


def test_both_env_vars_deduped(monkeypatch, ca_pem):
    """Same path in both env vars is loaded once, not twice."""
    public_subjects = _public_subjects()
    monkeypatch.setenv("SSL_CERT_FILE", str(ca_pem))
    monkeypatch.setenv("VEZIR_CADDY_ROOT_CERT_PATH", str(ca_pem))
    ctx = trust.resolve_verify()
    assert isinstance(ctx, ssl.SSLContext)
    # Loaded exactly once -> the delta is a single new subject.
    assert _subjects(ctx) == public_subjects | {"vezir-test-ca"}


def test_malformed_ca_skipped_keeps_default_store(monkeypatch, tmp_path):
    """A garbage 'CA' file must not blind us to the default store."""
    public_subjects = _public_subjects()
    bad = tmp_path / "bad.crt"
    bad.write_text("not a certificate\n")
    monkeypatch.setenv("SSL_CERT_FILE", str(bad))
    ctx = trust.resolve_verify()
    assert isinstance(ctx, ssl.SSLContext)
    # Default roots intact; malformed extra simply skipped (no delta).
    assert _subjects(ctx) == public_subjects
