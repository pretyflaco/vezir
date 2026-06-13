"""Google email allowlist — humans who may authenticate via Google sign-in.

Companion to ``nostr_members.py``.  Where a nostr member is keyed by a
public key, a *google member* is keyed by a verified Google **email**
(lowercased).  A verified Google ID token (see ``google_auth.py``) proves
ownership of the email; this table maps it to a ``github`` handle +
``is_admin`` tier so the rest of the auth chain (``require_team_context``
→ memberships) works unchanged.

A valid Google account is NOT sufficient on its own: the email must be on
this allowlist (mirrors the npub allowlist).  ``google_auth`` additionally
requires the email's Workspace domain to match
``config.google_allowed_domain()`` (default ``blinkbtc.com``) and
``email_verified`` to be true.

Storage: the ``google_members`` table in ``~/vezir-data/vezir.sqlite``
(schema in ``queue.SCHEMA``).  Thin functions over ``queue._conn()`` so we
inherit the global lock + WAL + busy_timeout (atomic read-modify-write).
"""
from __future__ import annotations

import logging
import time

from . import queue

log = logging.getLogger("vezir.google_members")


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _normalize_email(email: str) -> str:
    """Validate + lowercase an email.  Raises ValueError if obviously bad.

    Google emails are matched case-insensitively; we store the lowercased
    form so lookups are exact-match.
    """
    e = (email or "").strip().lower()
    if "@" not in e or e.startswith("@") or e.endswith("@") or " " in e:
        raise ValueError(f"not a valid email: {email!r}")
    return e


def add(
    email: str,
    github: str,
    *,
    is_admin: bool = False,
    label: str | None = None,
) -> None:
    """Add or update a google member.

    Upserts on ``email`` so re-running with new ``github``/``is_admin``/
    ``label`` updates the existing row.  ``added_at`` is preserved on
    update.
    """
    em = _normalize_email(email)
    with queue._conn() as c:
        existing = c.execute(
            "SELECT added_at FROM google_members WHERE email = ?", (em,)
        ).fetchone()
        added_at = existing["added_at"] if existing else _now_iso()
        c.execute(
            "INSERT INTO google_members (email, github, is_admin, label, added_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(email) DO UPDATE SET "
            "github=excluded.github, is_admin=excluded.is_admin, "
            "label=excluded.label",
            (em, github, 1 if is_admin else 0, label or None, added_at),
        )


def remove(email: str) -> int:
    """Remove a google member by email.  Returns count removed (0 or 1)."""
    em = _normalize_email(email)
    with queue._conn() as c:
        cur = c.execute("DELETE FROM google_members WHERE email = ?", (em,))
        return cur.rowcount


def list_members() -> list[dict]:
    """Return all google members ordered by ``added_at``.

    Each dict carries ``email``, ``github``, ``is_admin`` (bool),
    ``label``, ``added_at``.
    """
    with queue._conn() as c:
        rows = c.execute(
            "SELECT email, github, is_admin, label, added_at "
            "FROM google_members ORDER BY added_at"
        ).fetchall()
    return [
        {
            "email": row["email"],
            "github": row["github"],
            "is_admin": bool(row["is_admin"]),
            "label": row["label"],
            "added_at": row["added_at"],
        }
        for row in rows
    ]


def lookup_email(email: str) -> tuple[str, bool] | None:
    """Resolve a verified email to ``(github, is_admin)``, or None.

    The email is expected to already be verified by the caller (a checked
    Google ID token with ``email_verified=true``).  Returns None for
    unknown emails — a verified-but-unauthorized account is NOT trusted.
    """
    try:
        em = _normalize_email(email)
    except ValueError:
        return None
    with queue._conn() as c:
        row = c.execute(
            "SELECT github, is_admin FROM google_members WHERE email = ?",
            (em,),
        ).fetchone()
    if row is None:
        return None
    return (row["github"] or "", bool(row["is_admin"]))
