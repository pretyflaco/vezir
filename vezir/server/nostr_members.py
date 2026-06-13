"""Nostr npub allowlist — humans who may authenticate via NIP-98.

Companion to ``auth.py``'s ``vzr_`` bearer tokens.  Where a bearer token
identifies a human by an opaque secret, a *nostr member* identifies the
same human by their public key.  A signed NIP-98 event (see
``nip98.py``) proves possession of the matching secret key; this table
maps the proven pubkey to a ``github`` handle + ``is_admin`` tier so the
rest of the auth chain (``require_team_context`` → memberships) works
unchanged.

Storage: the ``nostr_members`` table in ``~/vezir-data/vezir.sqlite``
(schema lives in ``queue.SCHEMA``; created idempotently on first
connection).  The key column ``npub`` holds the **64-char lowercase hex
x-only pubkey**, NOT the bech32 ``npub1…`` form — that is the shape the
NIP-98 verifier yields, so lookups need no decode.  The CLI accepts the
human-friendly ``npub1…`` form and decodes it via ``nip19`` before
calling :func:`add`.

Design mirrors ``auth.py``: thin functions over ``queue._conn()`` so we
inherit the global lock + WAL + busy_timeout, keeping every
read-modify-write atomic.
"""
from __future__ import annotations

import logging
import time

from . import queue

log = logging.getLogger("vezir.nostr_members")


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _normalize_pubkey(pubkey_hex: str) -> str:
    """Validate + lowercase a 64-char hex x-only pubkey.

    Raises ``ValueError`` if the input is not exactly 64 hex chars.  The
    canonical stored form is lowercase so lookups are exact-match.
    """
    pk = (pubkey_hex or "").strip().lower()
    if len(pk) != 64:
        raise ValueError(
            f"pubkey must be 64 hex chars (x-only); got {len(pk)}"
        )
    try:
        int(pk, 16)
    except ValueError as exc:
        raise ValueError("pubkey is not valid hex") from exc
    return pk


def add(
    pubkey_hex: str,
    github: str,
    *,
    is_admin: bool = False,
    label: str | None = None,
) -> None:
    """Add or update a nostr member.

    Upserts on ``npub`` so re-running with new ``github``/``is_admin``/
    ``label`` updates the existing row (use case: promoting to admin,
    correcting a handle).  ``added_at`` is preserved on update.
    """
    pk = _normalize_pubkey(pubkey_hex)
    with queue._conn() as c:
        existing = c.execute(
            "SELECT added_at FROM nostr_members WHERE npub = ?", (pk,)
        ).fetchone()
        added_at = existing["added_at"] if existing else _now_iso()
        c.execute(
            "INSERT INTO nostr_members (npub, github, is_admin, label, added_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(npub) DO UPDATE SET "
            "github=excluded.github, is_admin=excluded.is_admin, "
            "label=excluded.label",
            (pk, github, 1 if is_admin else 0, label or None, added_at),
        )


def remove(pubkey_hex: str) -> int:
    """Remove a nostr member by hex pubkey.  Returns count removed (0 or 1)."""
    pk = _normalize_pubkey(pubkey_hex)
    with queue._conn() as c:
        cur = c.execute("DELETE FROM nostr_members WHERE npub = ?", (pk,))
        return cur.rowcount


def list_members() -> list[dict]:
    """Return all nostr members ordered by ``added_at``.

    Each dict carries ``npub`` (hex), ``github``, ``is_admin`` (bool),
    ``label``, ``added_at``.
    """
    with queue._conn() as c:
        rows = c.execute(
            "SELECT npub, github, is_admin, label, added_at "
            "FROM nostr_members ORDER BY added_at"
        ).fetchall()
    return [
        {
            "npub": row["npub"],
            "github": row["github"],
            "is_admin": bool(row["is_admin"]),
            "label": row["label"],
            "added_at": row["added_at"],
        }
        for row in rows
    ]


def lookup_npub(pubkey_hex: str) -> tuple[str, bool] | None:
    """Resolve a hex x-only pubkey to ``(github, is_admin)``, or None.

    The pubkey is expected to already be a verified signer (the caller
    has checked a NIP-98 signature).  Returns None for unknown pubkeys
    — an unrecognized but cryptographically valid signer is NOT trusted.
    """
    try:
        pk = _normalize_pubkey(pubkey_hex)
    except ValueError:
        return None
    with queue._conn() as c:
        row = c.execute(
            "SELECT github, is_admin FROM nostr_members WHERE npub = ?",
            (pk,),
        ).fetchone()
    if row is None:
        return None
    return (row["github"] or "", bool(row["is_admin"]))
