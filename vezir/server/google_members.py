"""Google email allowlist — humans who may authenticate via Google sign-in.

Companion to ``nostr_members.py``.  Where a nostr member is keyed by a
public key, a *google member* is keyed by a verified Google **email**
(lowercased).  A verified Google ID token (see ``google_auth.py``) proves
ownership of the email; this table maps it to a ``github`` handle +
``is_admin`` tier so the rest of the auth chain (``require_team_context``
→ memberships) works unchanged.

A valid Google account is NOT sufficient on its own: the email must be on
this allowlist (mirrors the npub allowlist).  ``google_auth`` additionally
requires the email's domain to be one of
``config.google_allowed_domains()`` (default ``blinkbtc.com``) and
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
            "SELECT github, is_admin, added_at FROM google_members WHERE email = ?", (em,)
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
    # A demotion or a re-pointed handle must not survive in live sessions
    # (security audit 2026-10-30, H-2); see nostr_members.add.
    if existing is not None and (
        (bool(existing["is_admin"]) and not is_admin) or existing["github"] != github
    ):
        _revoke_sessions(existing["github"], reason="google member changed")


def remove(email: str) -> int:
    """Remove a google member by email.  Returns count removed (0 or 1)."""
    em = _normalize_email(email)
    with queue._conn() as c:
        row = c.execute(
            "SELECT github FROM google_members WHERE email = ?", (em,)
        ).fetchone()
        cur = c.execute("DELETE FROM google_members WHERE email = ?", (em,))
        removed = cur.rowcount
    if removed and row is not None:
        _revoke_sessions(row["github"], reason="google member removed")
    return removed


def _revoke_sessions(github: str, *, reason: str) -> None:
    if not github:
        return
    from . import sessions_auth  # lazy: sessions_auth imports this module

    n = sessions_auth.revoke_all_for(github)
    if n:
        log.info("revoked %d session(s) for github=%s (%s)", n, github, reason)


def lookup_github(github: str) -> bool | None:
    """Current admin bit for ``github`` if any google row still names it.

    Used by session rotation to re-check an identity whose session row
    has no email (Google sessions store ``npub = ""``).  ``None`` means
    the handle is no longer allowlisted via Google at all.
    """
    if not github:
        return None
    with queue._conn() as c:
        rows = c.execute(
            "SELECT is_admin FROM google_members WHERE github = ?", (github,)
        ).fetchall()
    if not rows:
        return None
    return any(bool(r["is_admin"]) for r in rows)


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
