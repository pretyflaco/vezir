"""Enrollment payload and QR utilities.

v0.7.0: HTML enrollment page removed. QR codes are now generated
by the CLI (``vezir token enroll``) directly in the terminal.
This module provides the shared payload-building and QR-rendering
functions used by the CLI.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import segno

log = logging.getLogger("vezir.enroll")


# Latest QR payload schema version. v1 omits ca_pem; v2 includes it.
PAYLOAD_VERSION = 2

# Hard cap on the embedded CA PEM length. A normal RSA-2048 or Ed25519
# Caddy root is ~1-2 KiB. Anything much larger is operator error and we
# bail rather than producing a multi-kilobyte QR that won't scan.
_MAX_CA_PEM_BYTES = 8 * 1024


def _load_caddy_root_cert() -> str | None:
    """Return the PEM-encoded Caddy internal CA cert, if configured.

    Configured via ``VEZIR_CADDY_ROOT_CERT_PATH``. Returns None when the
    env var is unset, the file is missing, or its contents are clearly
    not a PEM certificate.
    """
    path_env = os.environ.get("VEZIR_CADDY_ROOT_CERT_PATH")
    if not path_env:
        return None
    p = Path(path_env)
    if not p.exists():
        log.warning(
            "VEZIR_CADDY_ROOT_CERT_PATH=%s does not exist; falling back to v1 payload",
            path_env,
        )
        return None
    try:
        text = p.read_text(encoding="utf-8")
    except Exception as exc:
        log.warning("could not read %s: %s", path_env, exc)
        return None
    if "BEGIN CERTIFICATE" not in text:
        log.warning(
            "%s does not look like a PEM-encoded certificate; falling back to v1",
            path_env,
        )
        return None
    if len(text.encode("utf-8")) > _MAX_CA_PEM_BYTES:
        log.warning(
            "CA cert at %s is %d bytes (cap %d); falling back to v1",
            path_env, len(text), _MAX_CA_PEM_BYTES,
        )
        return None
    return text


def build_payload(server_url: str, token: str, ca_pem: str | None = None) -> str:
    """Return the canonical QR payload JSON string."""
    if ca_pem:
        obj = {
            "v": PAYLOAD_VERSION,
            "url": server_url,
            "token": token,
            "ca_pem": ca_pem,
        }
    else:
        obj = {"v": 1, "url": server_url, "token": token}
    return json.dumps(obj, separators=(",", ":"), sort_keys=True)


def render_qr_terminal(payload: str) -> str:
    """Render a QR code as UTF-8 terminal art.

    Uses segno's ``terminal()`` in ``compact`` mode: half-block
    characters (``\\u2584 \\u2580 \\u2588``) with NO ANSI color escapes,
    so the result is safe to embed in Textual widgets (v0.15.0 fixed the
    reauth modal, where the default non-compact output's raw ``\\x1b[7m``
    sequences rendered as literal garbage and the ~106-col block
    overflowed the modal, clipping the buttons out of reach).
    Returns a multi-line string ready for print().
    """
    import io
    qr = segno.make(payload, error="m")
    buf = io.StringIO()
    qr.terminal(out=buf, border=2, compact=True)
    return buf.getvalue()
