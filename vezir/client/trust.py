"""TLS trust resolution for vezir clients.

The vezir deployment mixes two TLS regimes:

  * The **public** front (e.g. ``vezir.twentyone.ist``) terminates TLS with
    a publicly-trusted certificate (Let's Encrypt) that validates against
    the system / certifi trust store.
  * **Internal** hosts served with Caddy's ``tls internal`` present a
    Caddy-local-CA certificate that is NOT in any public store.

A single client may talk to either, so the historical approach of pointing
``SSL_CERT_FILE`` at *only* the Caddy CA is wrong: it makes httpx trust the
internal CA while breaking validation of the public Let's Encrypt cert
(``unable to get local issuer certificate``).

``resolve_verify`` instead returns a value suitable for httpx's ``verify=``
that trusts BOTH: it starts from the default trust store and *appends* any
configured internal CA, rather than replacing the store.

Resolution:
  1. If ``explicit`` is given (not None), it wins (a bool, a path, or an
     already-built ``ssl.SSLContext``).
  2. Otherwise build an ``ssl.SSLContext`` from the system/certifi defaults
     and additionally load any CA file named by ``SSL_CERT_FILE`` or
     ``VEZIR_CADDY_ROOT_CERT_PATH`` (so internal hosts keep working).
  3. If no extra CA is configured, return ``True`` (httpx default), avoiding
     the overhead of building a context.
"""
from __future__ import annotations

import os
import ssl
from collections.abc import Iterable

# Env vars that may name an *additional* CA to trust (in priority order).
_CA_ENV_VARS = ("SSL_CERT_FILE", "VEZIR_CADDY_ROOT_CERT_PATH")


def _extra_ca_paths(env_vars: Iterable[str] = _CA_ENV_VARS) -> list[str]:
    """Return existing CA file paths named by the trust env vars (deduped)."""
    seen: set[str] = set()
    out: list[str] = []
    for var in env_vars:
        path = os.environ.get(var)
        if path and os.path.isfile(path) and path not in seen:
            seen.add(path)
            out.append(path)
    return out


def resolve_verify(explicit: bool | str | ssl.SSLContext | None = None):
    """Resolve an httpx ``verify`` value that trusts public + internal CAs.

    See module docstring for the resolution order.  Returns one of:
      * the ``explicit`` value, if provided;
      * an ``ssl.SSLContext`` (default store + extra internal CA(s)); or
      * ``True`` (no extra CA configured -> plain default trust).
    """
    if explicit is not None:
        return explicit

    extra = _extra_ca_paths()
    if not extra:
        return True

    # Build a context that trusts BOTH the public roots and the internal CA.
    #
    # IMPORTANT: ssl.create_default_context() / load_default_certs() consult
    # OpenSSL's default verify paths, which honor $SSL_CERT_FILE.  On boxes
    # where SSL_CERT_FILE names the Caddy-only CA (the very misconfiguration
    # this fixes), that would narrow the "default" store to just Caddy and
    # break the public Let's Encrypt front.  So we seed the public roots
    # explicitly from certifi (independent of SSL_CERT_FILE) and add the OS
    # trust directory, then append the internal CA(s) on top.
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    _load_public_roots(ctx)
    for path in extra:
        try:
            ctx.load_verify_locations(cafile=path)
        except (ssl.SSLError, OSError):
            # A malformed/unreadable extra CA must not blind us to the
            # public roots; skip it and keep public trust intact.
            continue
    return ctx


def _load_public_roots(ctx: ssl.SSLContext) -> None:
    """Load the public CA roots into ``ctx`` independent of $SSL_CERT_FILE.

    Prefers certifi (the same bundle httpx's default uses); also adds the OS
    capath directory when present so privately-installed public-equivalent
    roots still apply.  Best-effort: failures here leave whatever loaded so
    far intact.
    """
    loaded = False
    try:
        import certifi

        ctx.load_verify_locations(cafile=certifi.where())
        loaded = True
    except (ImportError, ssl.SSLError, OSError):
        pass

    # Also pick up the OS trust directory (capath) if discoverable, without
    # going through SSL_CERT_FILE.
    paths = ssl.get_default_verify_paths()
    capath = paths.capath or paths.openssl_capath
    if capath and os.path.isdir(capath):
        try:
            ctx.load_verify_locations(capath=capath)
            loaded = True
        except (ssl.SSLError, OSError):
            pass

    if not loaded:
        # Last resort: fall back to OpenSSL defaults (may honor SSL_CERT_FILE,
        # but better than an empty store).
        try:
            ctx.load_default_certs()
        except (ssl.SSLError, OSError):
            pass
