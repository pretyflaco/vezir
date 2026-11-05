"""One-shot data migrations.

Each migration is identified by a version label and recorded in the
``schema_migrations`` table inside ``vezir.sqlite`` after it succeeds.
A migration runs at most once per data dir.

The v0.6.0 migration is the only one in this module today.  Future
migrations append new functions and add their names to ``ALL_MIGRATIONS``.

Order of operations matters: the v0.6.0 migration must run BEFORE any
endpoint can serve traffic, so ``app.create_app`` invokes
``run_pending_migrations`` early in startup.

Failure mode: any migration that raises leaves the
``schema_migrations`` row unwritten, so the next process restart
retries from a clean state.  Migrations must be re-entrant — if half
of the work happened before a crash, the second attempt must finish
without producing duplicates or losing prior work.

v0.6.0 / v0.6.2 (single-team -> multi-team):

* A fresh data dir has no legacy data, so nothing is seeded: no teams, no
  rosters, no voiceprint files.  Operators create their teams with
  ``vezir team create``.
* A pre-0.6.0 data dir has single-team data (jobs without a team, tokens
  without a team, ``team.json``, a central ``speaker_profiles.json``).
  All of it moves into one team, :data:`LEGACY_TEAM_SLUG` (created if
  missing).  Smoke-test jobs (``github LIKE 'smoke-upload-test%'``) are
  deleted.
* A data dir where these migrations already ran is not touched: they are
  recorded in ``schema_migrations``.
"""
from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import time

from .. import config

log = logging.getLogger("vezir.migrations")


SCHEMA_TRACKING = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);
"""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _conn() -> sqlite3.Connection:
    """Open a connection to the queue DB (same file used by queue.py).

    Applies the same connection PRAGMAs as ``queue._conn`` (WAL,
    busy_timeout, synchronous, foreign_keys) so the startup migration
    path is consistent with the running server.  These are set before
    any statement, as required for per-connection PRAGMAs.
    """
    db_path = config.queue_db_path()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA_TRACKING)
    return conn


def _already_applied(version: str) -> bool:
    with _conn() as c:
        row = c.execute(
            "SELECT 1 FROM schema_migrations WHERE version = ?",
            (version,),
        ).fetchone()
        return row is not None


def _mark_applied(version: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, applied_at) "
            "VALUES (?, ?)",
            (version, _now()),
        )
        c.commit()


# ── v0.6.0: move single-team data into one team, drop smokes ────────────────

# The one team pre-0.6.0 (single-team) data is moved into.
LEGACY_TEAM_SLUG = "default"
LEGACY_TEAM_NAME = "Default"


def _team_keys(conn: sqlite3.Connection) -> set[str]:
    """Every id and slug in ``teams`` (pre- and post-0.7.4 layouts)."""
    has_slug = any(
        r[1] == "slug"
        for r in conn.execute("PRAGMA table_info(teams)").fetchall()
    )
    cols = "id, slug" if has_slug else "id"
    keys: set[str] = set()
    for row in conn.execute(f"SELECT {cols} FROM teams").fetchall():
        keys.add(row["id"])
        if has_slug and row["slug"]:
            keys.add(row["slug"])
    return keys


def _ensure_legacy_team(conn: sqlite3.Connection) -> bool:
    """Create :data:`LEGACY_TEAM_SLUG` if missing.  Returns True if created."""
    if LEGACY_TEAM_SLUG in _team_keys(conn):
        return False
    # Legacy layout: id=slug (the 0.7.4 migration later remaps to a uuid).
    conn.execute(
        "INSERT INTO teams (id, name, sync_remote, sync_meeting_type, "
        "created_at) VALUES (?, ?, ?, ?, ?)",
        (LEGACY_TEAM_SLUG, LEGACY_TEAM_NAME, None, "sandbox", _now()),
    )
    log.info("migration: created team %r for single-team data", LEGACY_TEAM_SLUG)
    return True


def _legacy_tokens() -> list[dict]:
    """Token rows in ``tokens.json`` that predate teams (no ``team_id``)."""
    p = config.tokens_json_path()
    if not p.exists():
        return []
    data = json.loads(p.read_text(encoding="utf-8"))
    return [t for t in data.get("tokens", []) if not t.get("team_id")]


def _has_legacy_data(conn: sqlite3.Connection) -> bool:
    """True if the data dir holds pre-0.6.0 single-team data."""
    orphan_jobs = conn.execute(
        "SELECT COUNT(*) AS n FROM jobs "
        "WHERE (team_id = '' OR team_id IS NULL) "
        "AND github NOT LIKE 'smoke-upload-test%'"
    ).fetchone()["n"]
    return bool(orphan_jobs or _legacy_tokens() or config.team_json_path().exists())


def _backfill_jobs(conn: sqlite3.Connection) -> dict:
    """Move team-less jobs into the legacy team; delete smoke-test rows."""
    cur = conn.execute(
        "DELETE FROM jobs WHERE github LIKE 'smoke-upload-test%'"
    )
    smoke_deleted = cur.rowcount

    cur = conn.execute(
        "UPDATE jobs SET team_id = ?, updated_at = ? "
        "WHERE team_id = '' OR team_id IS NULL",
        (LEGACY_TEAM_SLUG, _now()),
    )
    assigned = cur.rowcount

    stats = {"smoke_deleted": smoke_deleted, "to_legacy_team": assigned}
    log.info("migration: jobs backfill stats: %s", stats)
    return stats


def _backfill_tokens() -> dict:
    """Give every team-less token in ``tokens.json`` the legacy team.

    Atomic write (temp file + rename) via ``config.secure_write_text``.
    """
    p = config.tokens_json_path()
    if not p.exists():
        log.info("migration: no tokens.json found; nothing to backfill")
        return {"tokens_total": 0, "to_legacy_team": 0}

    data = json.loads(p.read_text(encoding="utf-8"))
    tokens = data.get("tokens", [])
    assigned = 0
    for entry in tokens:
        if not entry.get("team_id"):
            entry["team_id"] = LEGACY_TEAM_SLUG
            assigned += 1
    if assigned:
        config.secure_write_text(p, json.dumps({"tokens": tokens}, indent=2))

    stats = {"tokens_total": len(tokens), "to_legacy_team": assigned}
    log.info("migration: tokens backfill stats: %s", stats)
    return stats


def _move_roster() -> dict:
    """Move legacy ``team.json`` into the legacy team's ``roster.json``.

    Idempotent: if the source doesn't exist or the destination already
    does, skip without raising.
    """
    legacy = config.team_json_path()
    dst = config.team_roster_path(LEGACY_TEAM_SLUG)
    stats = {"roster_moved": False}
    if legacy.exists() and not dst.exists():
        config.secure_mkdir(dst.parent)
        shutil.move(str(legacy), str(dst))
        config.secure_chmod_file(dst)
        stats["roster_moved"] = True
        log.info("migration: moved %s -> %s", legacy, dst)
    return stats


def migrate_0_6_0() -> dict:
    """Apply the v0.6.0 multi-team migration.

    On a fresh data dir this only deletes smoke-test jobs and records
    itself.  With pre-0.6.0 data (each step idempotent):

    1. Create the :data:`LEGACY_TEAM_SLUG` team.
    2. Move team-less jobs into it (smoke-test jobs deleted).
    3. Give team-less tokens in ``tokens.json`` that team.
    4. Move ``~/vezir-data/team.json`` to its ``roster.json``.

    Returns a stats dict suitable for the migration log + smoke-test
    assertions.
    """
    version = "0.6.0-multi-team"
    if _already_applied(version):
        log.info("migration %s already applied; nothing to do", version)
        return {"already_applied": True}

    # Ensure data dir + teams dir exist before any file operations.
    config.ensure_dirs()

    with _conn() as c:
        # The queue module owns the jobs/teams table DDL; we just need
        # to confirm those tables exist before we touch them.  Touching
        # the queue connection (via queue.list_recent or similar) would
        # be enough but introduces an import cycle, so we materialize
        # the schema directly.
        from . import queue as _queue
        c.executescript(_queue.SCHEMA)
        # Re-run the column-add migrations queue.py does on open.
        for ddl in (
            "ALTER TABLE jobs ADD COLUMN summary_preset TEXT",
            "ALTER TABLE jobs ADD COLUMN auto_label_enabled INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE jobs ADD COLUMN sync_enabled INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE jobs ADD COLUMN personal INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE jobs ADD COLUMN summary_error TEXT",
            "ALTER TABLE jobs ADD COLUMN sync_error TEXT",
            "ALTER TABLE jobs ADD COLUMN team_id TEXT NOT NULL DEFAULT ''",
        ):
            try:
                c.execute(ddl)
            except sqlite3.OperationalError:
                pass
        try:
            c.execute("CREATE INDEX IF NOT EXISTS idx_jobs_team ON jobs(team_id)")
        except sqlite3.OperationalError:
            pass
        c.commit()

        legacy = _has_legacy_data(c)
        team_created = _ensure_legacy_team(c) if legacy else False
        job_stats = _backfill_jobs(c)
        c.commit()

    if legacy:
        token_stats = _backfill_tokens()
        roster_stats = _move_roster()
    else:
        token_stats = {"tokens_total": 0, "to_legacy_team": 0}
        roster_stats = {"roster_moved": False}

    _mark_applied(version)

    summary = {
        "version": version,
        "legacy_data": legacy,
        "legacy_team_created": team_created,
        "jobs": job_stats,
        "tokens": token_stats,
        "roster": roster_stats,
    }

    # Audit log: write a JSON summary to ~/vezir-data/logs/
    log_dir = config.logs_dir()
    config.secure_mkdir(log_dir)
    log_file = log_dir / f"migration-{version}.log"
    config.secure_write_text(
        log_file,
        json.dumps(summary, indent=2) + "\n",
    )
    log.info("migration %s complete; audit at %s", version, log_file)

    return summary


# ── v0.6.2: per-team voiceprint DB ─────────────────────────────────────────


def _move_voiceprints() -> dict:
    """Move the legacy central voiceprint DB into the legacy team.

    Pre-0.6.2 vezir kept one ``~/vezir-data/speaker_profiles.json``.  It
    belongs with the pre-0.6.0 data, so it moves to
    ``teams/<LEGACY_TEAM_SLUG>/speaker_profiles.json``.  Nothing is seeded
    otherwise: the per-job HOME shim creates an empty team DB on first use.

    Idempotent: skips when there is no legacy DB or the destination exists.
    """
    legacy = config.speaker_profiles_path()
    dst = config.team_speaker_profiles_path(LEGACY_TEAM_SLUG)
    stats = {"voiceprints_moved": False}
    if legacy.exists() and not dst.exists():
        config.secure_mkdir(dst.parent)
        shutil.move(str(legacy), str(dst))
        config.secure_chmod_file(dst)
        stats["voiceprints_moved"] = True
        log.info("migration 0.6.2: moved %s -> %s", legacy, dst)
    return stats


def migrate_0_6_2() -> dict:
    """Apply the v0.6.2 per-team-voiceprint migration.

    Single step (idempotent): move the legacy central voiceprint DB
    (``~/vezir-data/speaker_profiles.json``), if any, into the
    :data:`LEGACY_TEAM_SLUG` team's directory.

    No schema changes are needed for v0.6.2: per-team sync_remote was
    already a schema slot in v0.6.0; per-team voiceprint DBs are pure
    on-disk reshuffles.

    Returns a stats dict suitable for the migration log.
    """
    version = "0.6.2-per-team-voiceprints"
    if _already_applied(version):
        log.info("migration %s already applied; nothing to do", version)
        return {"already_applied": True}

    config.ensure_dirs()
    if config.speaker_profiles_path().exists():
        # The central DB is single-team data: make sure its team exists.
        with _conn() as c:
            from . import queue as _queue
            c.executescript(_queue.SCHEMA)
            _ensure_legacy_team(c)
            c.commit()
    vp_stats = _move_voiceprints()
    _mark_applied(version)

    summary = {
        "version": version,
        "voiceprints": vp_stats,
    }

    log_dir = config.logs_dir()
    config.secure_mkdir(log_dir)
    log_file = log_dir / f"migration-{version}.log"
    config.secure_write_text(
        log_file,
        json.dumps(summary, indent=2) + "\n",
    )
    log.info("migration %s complete; audit at %s", version, log_file)

    return summary


# ── v0.7.0: token team_id -> memberships table ─────────────────────────────


def _backfill_memberships(conn: sqlite3.Connection) -> dict:
    """Populate ``memberships`` from each token's ``team_id`` field.

    For every (github, team_id) pair found in ``tokens.json``, insert a
    row into the ``memberships`` table with role ``scribe`` (or
    ``admin`` if the token row has ``is_admin=true``).  When the same
    handle owns multiple tokens for the same team, the highest role
    wins (admin > scribe).

    The token store is then rewritten with ``team_id`` stripped from
    every row — v0.7.0 derives team context from an ``X-Team-Id``
    header validated against the memberships table, not from the token
    itself.

    Idempotent: if a (github, team_id) row already exists in
    ``memberships`` it's left alone via ``INSERT OR IGNORE``.  Re-running
    on an already-stripped tokens.json is a no-op.
    """
    p = config.tokens_json_path()
    if not p.exists():
        log.info("migration 0.7.0: no tokens.json; nothing to backfill")
        return {"memberships_added": 0, "tokens_stripped": 0}

    data = json.loads(p.read_text(encoding="utf-8"))
    tokens = data.get("tokens", [])

    # Aggregate the best role per (github, team_id) before writing.
    best: dict[tuple[str, str], str] = {}
    for entry in tokens:
        gh = entry.get("github") or ""
        team = entry.get("team_id") or ""
        if not gh or not team:
            continue
        role = "admin" if entry.get("is_admin") else "scribe"
        key = (gh, team)
        if best.get(key) == "admin":
            continue  # already promoted to admin; can't go higher
        best[key] = role

    added = 0
    for (gh, team), role in best.items():
        cur = conn.execute(
            "INSERT OR IGNORE INTO memberships "
            "(github, team_id, role, added_at, added_by) "
            "VALUES (?, ?, ?, ?, ?)",
            (gh, team, role, _now(), "migration-0.7.0"),
        )
        added += cur.rowcount
    conn.commit()

    # Strip team_id from every token row.
    stripped = 0
    new_tokens: list[dict] = []
    for entry in tokens:
        if "team_id" in entry:
            entry = {k: v for k, v in entry.items() if k != "team_id"}
            stripped += 1
        new_tokens.append(entry)
    if stripped:
        config.secure_write_text(p, json.dumps({"tokens": new_tokens}, indent=2))

    stats = {
        "memberships_added": added,
        "tokens_stripped": stripped,
        "unique_pairs": len(best),
    }
    log.info("migration 0.7.0: memberships backfill stats: %s", stats)
    return stats


def migrate_0_7_0() -> dict:
    """Apply the v0.7.0 memberships migration.

    Steps (idempotent):

    1. Materialize the v0.7.0 schema (memberships, session_teams).  The
       queue module's ``SCHEMA_SQL`` includes ``CREATE TABLE IF NOT
       EXISTS`` for both, so we just run ``executescript`` again.
    2. For each token row in ``tokens.json``, ensure a corresponding
       membership exists.  Strip ``team_id`` from every token row.

    Returns a stats dict suitable for the audit log.
    """
    version = "0.7.0-memberships"
    if _already_applied(version):
        log.info("migration %s already applied; nothing to do", version)
        return {"already_applied": True}

    config.ensure_dirs()

    with _conn() as c:
        from . import queue as _queue
        c.executescript(_queue.SCHEMA)
        c.commit()
        mem_stats = _backfill_memberships(c)

    _mark_applied(version)

    summary = {"version": version, "memberships": mem_stats}

    log_dir = config.logs_dir()
    config.secure_mkdir(log_dir)
    log_file = log_dir / f"migration-{version}.log"
    config.secure_write_text(
        log_file,
        json.dumps(summary, indent=2) + "\n",
    )
    log.info("migration %s complete; audit at %s", version, log_file)

    return summary


# ── v0.7.2: tokens.json -> sqlite tokens table ────────────────────────────


def _import_tokens_to_sqlite(conn: sqlite3.Connection) -> dict:
    """Import every row from ``tokens.json`` into the ``tokens`` table.

    The old flat file did an unlocked full-file read-modify-write with a
    lost-update race.  v0.7.2 stores tokens in ``vezir.sqlite`` instead.

    Idempotent: ``INSERT OR IGNORE`` keyed on ``token_hash`` means a
    re-run never duplicates rows.  After a successful import the source
    file is renamed to ``tokens.json.migrated`` so it's preserved as a
    backstop but no longer treated as authoritative.  If the file is
    already absent (fresh install, or a prior run renamed it) this is a
    no-op.
    """
    p = config.tokens_json_path()
    if not p.exists():
        log.info("migration 0.7.2: no tokens.json; nothing to import")
        return {"tokens_imported": 0, "file_renamed": False}

    data = json.loads(p.read_text(encoding="utf-8"))
    tokens = data.get("tokens", [])

    imported = 0
    for entry in tokens:
        token_hash = entry.get("token_hash")
        github = entry.get("github")
        if not token_hash or not github:
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO tokens "
            "(token_hash, github, issued_at, expires_at, last_used_at, "
            "is_admin, label) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                token_hash,
                github,
                entry.get("issued_at") or _now(),
                entry.get("expires_at"),
                entry.get("last_used_at"),
                1 if entry.get("is_admin") else 0,
                entry.get("label"),
            ),
        )
        imported += cur.rowcount
    conn.commit()

    # Preserve the old file as a backstop rather than deleting it.
    migrated_path = p.with_suffix(p.suffix + ".migrated")
    p.rename(migrated_path)

    stats = {
        "tokens_imported": imported,
        "rows_in_file": len(tokens),
        "file_renamed": True,
        "backstop": str(migrated_path),
    }
    log.info("migration 0.7.2: token import stats: %s", stats)
    return stats


def migrate_0_7_2() -> dict:
    """Apply the v0.7.2 tokens-to-sqlite migration.

    Steps (idempotent):

    1. Materialize the schema (the ``tokens`` table is part of the queue
       module's ``SCHEMA``).
    2. Import every row from ``tokens.json`` into the ``tokens`` table
       via ``INSERT OR IGNORE`` (keyed on ``token_hash``), then rename
       ``tokens.json`` -> ``tokens.json.migrated``.

    Returns a stats dict suitable for the audit log.
    """
    version = "0.7.2-tokens-to-sqlite"
    if _already_applied(version):
        log.info("migration %s already applied; nothing to do", version)
        return {"already_applied": True}

    config.ensure_dirs()

    with _conn() as c:
        from . import queue as _queue
        c.executescript(_queue.SCHEMA)
        c.commit()
        import_stats = _import_tokens_to_sqlite(c)

    _mark_applied(version)

    summary = {"version": version, "tokens": import_stats}

    log_dir = config.logs_dir()
    config.secure_mkdir(log_dir)
    log_file = log_dir / f"migration-{version}.log"
    config.secure_write_text(
        log_file,
        json.dumps(summary, indent=2) + "\n",
    )
    log.info("migration %s complete; audit at %s", version, log_file)

    return summary


# ── v0.7.4: team slug -> UUID primary key ─────────────────────────────────


def _migrate_teams_to_uuid(conn: sqlite3.Connection) -> dict:
    """Give every team a stable UUID id; slugs become mutable display names.

    Pre-0.7.4 the team slug WAS the primary key (``teams.id``) and the FK
    discriminator on ``jobs``/``memberships``/``session_teams``.  This
    rewrites each team's ``id`` to a fresh uuid, sets ``slug`` to the old
    id, rewrites the child FKs, and renames the on-disk ``teams/<slug>/``
    dir to ``teams/<uuid>/``.

    Idempotent: a team whose ``slug`` column is already populated (the
    v0.7.4 marker) is skipped.
    """
    import uuid as _uuid

    rows = conn.execute("SELECT * FROM teams").fetchall()
    remapped = 0
    dirs_renamed = 0
    teams_root = config.teams_dir()

    # FK-safe ordering under foreign_keys=ON: for each legacy team,
    # INSERT a new uuid-keyed row, repoint every child to it, then
    # DELETE the old slug-keyed row.  No PK is ever mutated in place, so
    # children never transiently dangle and we don't touch the
    # connection's foreign_keys pragma (which would leak to pooled
    # state).
    for row in rows:
        old_id = row["id"]
        keys = row.keys()
        existing_slug = row["slug"] if "slug" in keys else None
        # Already migrated if the slug column is populated — that's the
        # definitive marker of the v0.7.4 model.  (We must NOT rely on
        # the id's *shape*: a uuid4 hex like "ed89...fcfb" happens to
        # satisfy the slug regex — lowercase alnum, starts with a
        # letter, <=32 chars — so shape-matching would re-migrate an
        # already-uuid team and clobber its slug.)
        if existing_slug:
            continue
        new_uuid = _uuid.uuid4().hex
        name = row["name"] if "name" in keys else old_id
        sync_remote = row["sync_remote"] if "sync_remote" in keys else None
        sync_meeting_type = (
            row["sync_meeting_type"] if "sync_meeting_type" in keys else "sandbox"
        )
        created_at = row["created_at"] if "created_at" in keys else _now()

        conn.execute(
            "INSERT INTO teams (id, slug, name, sync_remote, "
            "sync_meeting_type, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (new_uuid, old_id, name, sync_remote, sync_meeting_type, created_at),
        )
        conn.execute(
            "UPDATE jobs SET team_id = ? WHERE team_id = ?", (new_uuid, old_id)
        )
        conn.execute(
            "UPDATE memberships SET team_id = ? WHERE team_id = ?",
            (new_uuid, old_id),
        )
        conn.execute(
            "UPDATE session_teams SET team_id = ? WHERE team_id = ?",
            (new_uuid, old_id),
        )
        conn.execute("DELETE FROM teams WHERE id = ?", (old_id,))
        remapped += 1

        # Rename on-disk dir teams/<slug>/ -> teams/<uuid>/.
        old_dir = teams_root / old_id
        new_dir = teams_root / new_uuid
        if old_dir.is_dir() and not new_dir.exists():
            old_dir.rename(new_dir)
            dirs_renamed += 1
    conn.commit()

    stats = {"teams_remapped": remapped, "dirs_renamed": dirs_renamed}
    log.info("migration 0.7.4: team uuid remap stats: %s", stats)
    return stats


def migrate_0_7_4() -> dict:
    """Apply the v0.7.4 team-slug-to-uuid migration."""
    version = "0.7.4-team-uuid"
    if _already_applied(version):
        log.info("migration %s already applied; nothing to do", version)
        return {"already_applied": True}

    config.ensure_dirs()

    with _conn() as c:
        from . import queue as _queue
        c.executescript(_queue.SCHEMA)
        # Ensure the slug column exists on pre-0.7.4 DBs before the remap.
        try:
            c.execute("ALTER TABLE teams ADD COLUMN slug TEXT")
        except sqlite3.OperationalError:
            pass
        c.commit()
        remap_stats = _migrate_teams_to_uuid(c)

    _mark_applied(version)

    summary = {"version": version, "teams": remap_stats}

    log_dir = config.logs_dir()
    config.secure_mkdir(log_dir)
    log_file = log_dir / f"migration-{version}.log"
    config.secure_write_text(
        log_file,
        json.dumps(summary, indent=2) + "\n",
    )
    log.info("migration %s complete; audit at %s", version, log_file)

    return summary


def migrate_0_10_0() -> dict:
    """Create the ``sessions`` table for rotating refresh-token sessions.

    Purely additive: materializes ``queue.SCHEMA`` (which now includes the
    ``sessions`` table and its indexes) so an existing data dir gains the
    new table without a server restart race.  Idempotent — the schema uses
    ``CREATE TABLE IF NOT EXISTS`` throughout, so re-running is a no-op.
    No data is moved; existing 24h session JWTs and ``vzr_`` tokens are
    untouched and keep working until they expire.
    """
    version = "0.10.0-sessions"
    if _already_applied(version):
        log.info("migration %s already applied; nothing to do", version)
        return {"already_applied": True}

    config.ensure_dirs()

    with _conn() as c:
        from . import queue as _queue
        c.executescript(_queue.SCHEMA)
        c.commit()

    _mark_applied(version)
    log.info("migration %s complete; sessions table ready", version)
    return {"version": version, "sessions_table": "ready"}


def migrate_0_14_0() -> dict:
    """Add ``jobs.summary_fallback`` for fallback-summarizer provenance.

    Purely additive: when millet falls back to a different summary backend
    (opt-in via ``MILLET_SUMMARY_PRESET_FALLBACK``), the worker records the
    actual ``<backend>/<model>`` here so the UI can show the summary was
    *not* produced by the requested preset.  NULL for all pre-existing
    rows.  Idempotent — the column add is also part of queue schema
    bring-up, so a fresh DB already has it and the ALTER is a no-op.
    """
    version = "0.14.0-summary-fallback"
    if _already_applied(version):
        log.info("migration %s already applied; nothing to do", version)
        return {"already_applied": True}

    config.ensure_dirs()

    with _conn() as c:
        from . import queue as _queue
        c.executescript(_queue.SCHEMA)
        try:
            c.execute("ALTER TABLE jobs ADD COLUMN summary_fallback TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists (fresh schema bring-up)
        c.commit()

    _mark_applied(version)
    log.info("migration %s complete; jobs.summary_fallback ready", version)
    return {"version": version, "summary_fallback_column": "ready"}


def migrate_0_18_0() -> dict:
    """Add ``jobs.video`` and ``jobs.summary_template`` (screenrecording loop).

    Purely additive: ``video`` flags uploads whose source file is video
    (.mp4/.mov) so the worker extracts the audio track with ffmpeg before
    transcription; ``summary_template`` carries the millet summary template
    name (e.g. "iteration-plan") forwarded as millet's --summary-template.
    Defaults: video=0, summary_template=NULL for all pre-existing rows.
    Idempotent — both column adds are also part of queue schema bring-up,
    so on a fresh DB the ALTERs are no-ops.
    """
    version = "0.18.0-video-template"
    if _already_applied(version):
        log.info("migration %s already applied; nothing to do", version)
        return {"already_applied": True}

    config.ensure_dirs()

    with _conn() as c:
        from . import queue as _queue
        c.executescript(_queue.SCHEMA)
        for ddl in (
            "ALTER TABLE jobs ADD COLUMN video INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE jobs ADD COLUMN summary_template TEXT",
        ):
            try:
                c.execute(ddl)
            except sqlite3.OperationalError:
                pass  # column already exists (fresh schema bring-up)
        c.commit()

    _mark_applied(version)
    log.info("migration %s complete; jobs.video + jobs.summary_template ready", version)
    return {"version": version, "video_template_columns": "ready"}


def migrate_0_20_0() -> dict:
    """Add ``jobs.summary_provenance`` and backfill it from sidecars.

    Records "<backend>/<model>" for every summary, unconditionally, so a
    client can state whether a summary came from a hardware-attested TEE.
    (``summary_fallback`` only records a backend when a *fallback* fired,
    so it can't answer that question on the normal path.)

    Unlike every earlier migration this one also **reads data**: it walks
    existing jobs with a NULL provenance and parses millet's
    ``.summary.meta.json`` sidecar for each.  Without that, the 500+
    historical sessions summarized by the old cloud backends would show
    no provenance at all, and the "unattested" signal would only ever fire
    for new jobs — exactly the rows where it matters least.

    Safety: only NULL rows are touched (so re-running cannot overwrite
    live values), every row is wrapped individually, and any failure is
    logged and skipped.  A missing or corrupt sidecar leaves NULL.  The
    backfill must never be able to stop the server from starting.
    """
    version = "0.20.0-summary-provenance"
    if _already_applied(version):
        log.info("migration %s already applied; nothing to do", version)
        return {"already_applied": True}

    config.ensure_dirs()

    backfilled = 0
    skipped = 0
    with _conn() as c:
        from . import queue as _queue
        c.executescript(_queue.SCHEMA)
        try:
            c.execute("ALTER TABLE jobs ADD COLUMN summary_provenance TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists (fresh schema bring-up)
        c.commit()

        # Backfill.  Imported lazily: worker imports config/queue, and this
        # module runs during app startup before the worker thread exists.
        from .worker import _session_dir, _summary_provenance

        rows = c.execute(
            "SELECT id FROM jobs WHERE summary_provenance IS NULL"
        ).fetchall()
        for row in rows:
            job_id = row["id"] if isinstance(row, sqlite3.Row) else row[0]
            try:
                prov = _summary_provenance(_session_dir(job_id))
            except Exception:
                log.debug("backfill: unreadable sidecar for %s", job_id, exc_info=True)
                skipped += 1
                continue
            if not prov:
                skipped += 1
                continue
            try:
                c.execute(
                    "UPDATE jobs SET summary_provenance = ? "
                    "WHERE id = ? AND summary_provenance IS NULL",
                    (prov, job_id),
                )
                backfilled += 1
            except sqlite3.Error:
                log.debug("backfill: update failed for %s", job_id, exc_info=True)
                skipped += 1
        c.commit()

    _mark_applied(version)
    log.info(
        "migration %s complete; jobs.summary_provenance ready "
        "(backfilled %d, skipped %d of %d candidate rows)",
        version, backfilled, skipped, backfilled + skipped,
    )
    return {
        "version": version,
        "summary_provenance_column": "ready",
        "backfilled": backfilled,
        "skipped": skipped,
    }


def migrate_0_27_0() -> dict:
    """Create ``session_refresh_history`` and ``enroll_codes`` (0.27.0).

    * ``session_refresh_history`` — refresh-token reuse memory (M-11).
    * ``enroll_codes`` — short-lived single-use enrollment codes so the QR
      no longer embeds a 90-day bearer (M-8).

    Security audit 2026-10-30, M-11: reuse detection remembered only the
    previous generation (``sessions.prev_refresh_hash``), so a refresh
    token two or more rotations old was indistinguishable from a random
    one — a quiet 401, no family revocation, no security log.  RFC 9700
    §4.14.2 expects *any* previously issued token of the family to trip
    revocation.  Purely additive DDL; no data is read or rewritten.
    Idempotent — the table is also part of the queue schema bring-up.
    """
    version = "0.27.0-refresh-history"
    if _already_applied(version):
        log.info("migration %s already applied; nothing to do", version)
        return {"already_applied": True}

    config.ensure_dirs()
    with _conn() as c:
        from . import queue as _queue
        c.executescript(_queue.SCHEMA)
        c.commit()

    _mark_applied(version)
    log.info("migration %s complete; session_refresh_history + enroll_codes ready", version)
    return {"version": version, "session_refresh_history": "ready", "enroll_codes": "ready"}


# ── registry ────────────────────────────────────────────────────────────────


ALL_MIGRATIONS = [
    migrate_0_6_0, migrate_0_6_2, migrate_0_7_0, migrate_0_7_2, migrate_0_7_4,
    migrate_0_10_0, migrate_0_14_0, migrate_0_18_0, migrate_0_20_0,
    migrate_0_27_0,
]


def run_pending_migrations() -> list[dict]:
    """Run every migration that hasn't been marked applied yet.

    Called from ``app.create_app`` during startup, before any router
    that touches user data.  Returns a list of summary dicts (one per
    migration that ran this call).
    """
    results: list[dict] = []
    for fn in ALL_MIGRATIONS:
        try:
            summary = fn()
        except Exception:
            log.exception("migration %s FAILED; refusing to start", fn.__name__)
            raise
        results.append(summary)
    return results
