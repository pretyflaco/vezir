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


def test_no_env_returns_public_context(monkeypatch):
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("VEZIR_CADDY_ROOT_CERT_PATH", raising=False)
    ctx = trust.resolve_verify()
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.check_hostname and ctx.verify_mode == ssl.CERT_REQUIRED
    assert _subjects(ctx) == _public_subjects()


# ── 0.27.0 (audit L-20): the internal-CA union is gone ───────────────────────


def test_internal_ca_env_is_no_longer_trusted(monkeypatch, ca_pem):
    """A CA named by SSL_CERT_FILE / VEZIR_CADDY_ROOT_CERT_PATH used to be
    appended to the context used for EVERY host — a leaked Caddy root key
    could then forge the public front for any opted-in client.  Public
    roots only now; the env vars merely warn."""
    public_subjects = _public_subjects()
    monkeypatch.setenv("SSL_CERT_FILE", str(ca_pem))
    monkeypatch.setenv("VEZIR_CADDY_ROOT_CERT_PATH", str(ca_pem))
    ctx = trust.resolve_verify()
    assert isinstance(ctx, ssl.SSLContext)
    assert "vezir-test-ca" not in _subjects(ctx)
    assert _subjects(ctx) == public_subjects


def test_legacy_env_warns_once(monkeypatch, ca_pem):
    """Patched on the logger itself: other tests' create_app() resets the
    root handlers, which would blind caplog depending on test order."""
    seen: list[str] = []
    monkeypatch.setattr(trust.log, "warning", lambda msg, *a, **k: seen.append(msg % a))
    monkeypatch.setenv("VEZIR_CADDY_ROOT_CERT_PATH", str(ca_pem))
    monkeypatch.setattr(trust, "_warned_legacy", False)
    trust.resolve_verify()
    trust.resolve_verify()
    assert sum("no longer used for vezir client TLS" in m for m in seen) == 1


def test_ssl_cert_file_pointing_at_only_the_caddy_ca_does_not_blind_public_trust(
    monkeypatch, ca_pem,
):
    """The muscle incident: SSL_CERT_FILE naming ONLY the Caddy CA made
    tools that honour it fail every public host.  vezir must not."""
    public_subjects = _public_subjects()
    monkeypatch.setenv("SSL_CERT_FILE", str(ca_pem))
    assert public_subjects <= _subjects(trust.resolve_verify())
