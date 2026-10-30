"""TLS trust resolution for vezir clients: public roots only.

Until 0.27.0 ``resolve_verify`` built a context that trusted the public
roots *plus* any CA named by ``SSL_CERT_FILE`` or
``VEZIR_CADDY_ROOT_CERT_PATH`` — for the nvpn-era deployment where internal
hosts were served by Caddy's ``tls internal``.  That context was then used
for **every** host, including the public front: if the Caddy root key ever
leaked, each client that had the env var set would accept a forged
``vezir.twentyone.ist`` certificate (security audit 2026-10-30, L-20).  And
``SSL_CERT_FILE`` is honoured by other tools as the *only* store, which mid
CA-incident looked like an attack (see AGENTS.md).

Nobody is on the internal-CA path any more, so the union is gone: the
client verifies against certifi + the OS trust directory, independent of
``SSL_CERT_FILE``.  The env vars are still *read* for one release only to
warn that they are no longer honoured.

Resolution:
  1. If ``explicit`` is given (not None), it wins (a bool, a path, or an
     already-built ``ssl.SSLContext``) — tests and the operator's own
     ``--ca`` style overrides.
  2. Otherwise an ``ssl.SSLContext`` seeded from certifi and the OS capath.
     (Never ``True``: httpx's default would consult OpenSSL's default
     paths, which honour ``SSL_CERT_FILE``.)
"""

from __future__ import annotations

import logging
import os
import ssl

log = logging.getLogger("vezir.trust")

# Read only to warn.  Not honoured since 0.27.0.
_LEGACY_CA_ENV_VARS = ("SSL_CERT_FILE", "SSL_CERT_DIR", "VEZIR_CADDY_ROOT_CERT_PATH")
_warned_legacy = False


def resolve_verify(explicit: bool | str | ssl.SSLContext | None = None):
    """Resolve an httpx ``verify`` value: ``explicit`` if given, else a
    public-roots-only ``ssl.SSLContext``."""
    if explicit is not None:
        return explicit
    _warn_legacy_env_once()
    return public_context()


def public_context() -> ssl.SSLContext:
    """A client context trusting certifi + the OS trust directory only."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    _load_public_roots(ctx)
    return ctx


def _warn_legacy_env_once() -> None:
    global _warned_legacy
    if _warned_legacy:
        return
    _warned_legacy = True
    for var in _LEGACY_CA_ENV_VARS:
        if os.environ.get(var):
            log.warning(
                "%s is set but no longer used for vezir client TLS (since "
                "0.27.0 the client trusts public roots only); unset it",
                var,
            )


def _load_public_roots(ctx: ssl.SSLContext) -> None:
    """Load certifi and the compiled OS capath, ignoring environment paths.

    ``DefaultVerifyPaths.cafile`` and ``.capath`` are environment-derived
    when ``SSL_CERT_FILE`` / ``SSL_CERT_DIR`` are set.  Only
    ``openssl_capath`` is safe to consult here.  There is deliberately no
    ``load_default_certs`` fallback: that would re-enable both variables.
    """
    try:
        import certifi

        ctx.load_verify_locations(cafile=certifi.where())
    except (ImportError, ssl.SSLError, OSError):
        pass

    paths = ssl.get_default_verify_paths()
    capath = paths.openssl_capath
    if capath and os.path.isdir(capath):
        try:
            ctx.load_verify_locations(capath=capath)
        except (ssl.SSLError, OSError):
            pass
