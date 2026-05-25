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

v0.6.0 specifics (locked-in decisions from vezir_plan.md):

* Seed teams: ``blink`` (everyone except bettermorning) and
  ``twentyone`` (pretyflaco + bettermorning).
* Token assignment:
  - bettermorning's tokens -> twentyone
  - all other handles' tokens -> blink
  - pretyflaco's two UNLABELED tokens -> REVOKED (option γ-lite)
    (the labeled gpu-server admin token and android-galaxy token
    stay assigned to blink; you can manually re-issue twentyone
    tokens for whichever devices need cross-team capability).
* Job backfill:
  - jobs where github == 'bettermorning' -> twentyone
  - smoke-upload-test* jobs -> DELETED
  - all other jobs -> blink
* Files:
  - ~/vezir-data/team.json   -> ~/vezir-data/teams/blink/roster.json
  - ~/vezir-data/teams/twentyone/roster.json seeded with
    [{"github": "pretyflaco"}, {"github": "bettermorning"}]
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
    """Open a connection to the queue DB (same file used by queue.py)."""
    db_path = config.queue_db_path()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
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


# ── v0.6.0: seed blink + twentyone, backfill team_id, drop smokes ──────────


def _seed_teams(conn: sqlite3.Connection) -> None:
    """Insert the two seed teams if not present.

    Idempotent: a teams table with existing rows is left alone.  This
    matches the case where the operator manually pre-created teams
    before running the migration.
    """
    existing = {
        row["id"]
        for row in conn.execute("SELECT id FROM teams").fetchall()
    }
    seeds = [
        ("blink", "Blink", None, "sandbox"),
        ("twentyone", "Twentyone", None, "sandbox"),
    ]
    for slug, name, remote, mtype in seeds:
        if slug in existing:
            log.info("migration: team %r already exists; skipping seed", slug)
            continue
        conn.execute(
            "INSERT INTO teams (id, name, sync_remote, sync_meeting_type, "
            "created_at) VALUES (?, ?, ?, ?, ?)",
            (slug, name, remote, mtype, _now()),
        )
        log.info("migration: seeded team %r (%s)", slug, name)


def _backfill_jobs(conn: sqlite3.Connection) -> dict:
    """Backfill jobs.team_id and delete smoke-test rows.

    Returns a stats dict for the migration log.
    """
    # 1. Drop smoke-test jobs entirely (per user decision).
    cur = conn.execute(
        "DELETE FROM jobs WHERE github LIKE 'smoke-upload-test%'"
    )
    smoke_deleted = cur.rowcount

    # 2. Backfill team_id.
    #    bettermorning -> twentyone
    #    everyone else with team_id='' -> blink
    bm_cur = conn.execute(
        "UPDATE jobs SET team_id = 'twentyone', updated_at = ? "
        "WHERE github = 'bettermorning' AND team_id = ''",
        (_now(),),
    )
    bm_assigned = bm_cur.rowcount

    blink_cur = conn.execute(
        "UPDATE jobs SET team_id = 'blink', updated_at = ? "
        "WHERE team_id = ''",
        (_now(),),
    )
    blink_assigned = blink_cur.rowcount

    # 3. Sanity check: every job has a non-empty team_id now.
    orphans = conn.execute(
        "SELECT COUNT(*) AS n FROM jobs WHERE team_id = '' OR team_id IS NULL"
    ).fetchone()
    if orphans and orphans["n"]:
        raise RuntimeError(
            f"migration bug: {orphans['n']} jobs still have empty team_id"
        )

    stats = {
        "smoke_deleted": smoke_deleted,
        "bettermorning_to_twentyone": bm_assigned,
        "all_others_to_blink": blink_assigned,
    }
    log.info("migration: jobs backfill stats: %s", stats)
    return stats


def _backfill_tokens() -> dict:
    """Backfill ~/vezir-data/tokens.json with team_id, revoke unlabeled pretyflaco.

    Returns a stats dict.  Atomic write at the end (writes to a temp
    file, then renames).
    """
    p = config.tokens_json_path()
    if not p.exists():
        log.info("migration: no tokens.json found; nothing to backfill")
        return {"tokens_total": 0, "to_blink": 0, "to_twentyone": 0, "revoked": 0}

    data = json.loads(p.read_text(encoding="utf-8"))
    tokens = data.get("tokens", [])

    to_blink = 0
    to_twentyone = 0
    revoked = 0
    keep: list[dict] = []
    for entry in tokens:
        gh = entry.get("github") or ""
        team_existing = entry.get("team_id")
        label = entry.get("label") or ""

        # Option γ-lite: revoke pretyflaco tokens that have no label.
        # The labeled pretyflaco tokens (gpu-server, android-galaxy)
        # stay; the unlabeled ones get dropped.
        if (
            gh == "pretyflaco"
            and not team_existing
            and not label
        ):
            log.info(
                "migration: revoking unlabeled pretyflaco token "
                "(issued_at=%s)",
                entry.get("issued_at"),
            )
            revoked += 1
            continue

        # Already has team_id (e.g. a prior partial migration): keep as-is.
        if team_existing:
            keep.append(entry)
            continue

        if gh == "bettermorning":
            entry["team_id"] = "twentyone"
            to_twentyone += 1
        else:
            entry["team_id"] = "blink"
            to_blink += 1
        keep.append(entry)

    new_data = {"tokens": keep}
    config.secure_write_text(p, json.dumps(new_data, indent=2))

    stats = {
        "tokens_total": len(keep),
        "to_blink": to_blink,
        "to_twentyone": to_twentyone,
        "revoked": revoked,
    }
    log.info("migration: tokens backfill stats: %s", stats)
    return stats


def _move_roster() -> dict:
    """Move legacy team.json -> teams/blink/roster.json, seed teams/twentyone/roster.json.

    Idempotent: if the source doesn't exist or the destination already does,
    skip without raising.
    """
    legacy = config.team_json_path()
    blink_dst = config.team_roster_path("blink")
    twentyone_dst = config.team_roster_path("twentyone")

    stats = {"blink_roster_moved": False, "twentyone_roster_seeded": False}

    config.secure_mkdir(blink_dst.parent)
    config.secure_mkdir(twentyone_dst.parent)

    if legacy.exists() and not blink_dst.exists():
        shutil.move(str(legacy), str(blink_dst))
        config.secure_chmod_file(blink_dst)
        stats["blink_roster_moved"] = True
        log.info("migration: moved %s -> %s", legacy, blink_dst)
    elif blink_dst.exists():
        log.info("migration: blink roster already at %s; skipping move", blink_dst)
    else:
        log.info("migration: no legacy team.json found; blink roster left empty")

    if not twentyone_dst.exists():
        seed = [
            {"github": "pretyflaco"},
            {"github": "bettermorning"},
        ]
        config.secure_write_text(
            twentyone_dst,
            json.dumps(seed, indent=2),
        )
        stats["twentyone_roster_seeded"] = True
        log.info("migration: seeded %s", twentyone_dst)
    else:
        log.info(
            "migration: twentyone roster already at %s; skipping seed",
            twentyone_dst,
        )

    return stats


def migrate_0_6_0() -> dict:
    """Apply the v0.6.0 multi-team migration.

    Steps (each idempotent):

    1. Seed teams 'blink' and 'twentyone'.
    2. Backfill jobs.team_id (bettermorning -> twentyone, smoke-tests
       deleted, everything else -> blink).
    3. Backfill tokens.json team_id; revoke unlabeled pretyflaco tokens.
    4. Move ~/vezir-data/team.json -> teams/blink/roster.json; seed
       teams/twentyone/roster.json with pretyflaco + bettermorning.

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
        from . import queue as _queue  # noqa: WPS433 (local-by-design)
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

        _seed_teams(c)
        job_stats = _backfill_jobs(c)
        c.commit()

    token_stats = _backfill_tokens()
    roster_stats = _move_roster()

    _mark_applied(version)

    summary = {
        "version": version,
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
    """Move legacy central speaker_profiles.json -> teams/blink/speaker_profiles.json.

    Per the v0.6.2 design (matching the v0.6.0 job-backfill default
    "everything-not-bettermorning -> blink"), the existing central
    voiceprint training surface migrates into blink.  Twentyone starts
    with an empty DB (locked-in user decision: clean slate, no
    cross-team contamination).

    Idempotent: skips when blink already has its per-team DB; always
    ensures twentyone has at least an empty file so the per-job HOME
    shim's symlink target resolves.
    """
    legacy = config.speaker_profiles_path()
    blink_dst = config.team_speaker_profiles_path("blink")
    twentyone_dst = config.team_speaker_profiles_path("twentyone")

    stats = {
        "blink_voiceprints_moved": False,
        "twentyone_voiceprints_seeded": False,
    }

    config.secure_mkdir(blink_dst.parent)
    config.secure_mkdir(twentyone_dst.parent)

    # 1. Blink: move the legacy DB into place (or seed empty if missing).
    if not blink_dst.exists():
        if legacy.exists():
            shutil.move(str(legacy), str(blink_dst))
            config.secure_chmod_file(blink_dst)
            stats["blink_voiceprints_moved"] = True
            log.info(
                "migration 0.6.2: moved %s -> %s", legacy, blink_dst,
            )
        else:
            config.secure_write_text(blink_dst, "{}")
            log.info(
                "migration 0.6.2: no legacy voiceprint DB; "
                "seeded empty %s", blink_dst,
            )
    else:
        log.info(
            "migration 0.6.2: blink voiceprint DB already at %s; "
            "skipping move", blink_dst,
        )

    # 2. Twentyone: seed empty (user decision: clean slate, no
    #    cross-team contamination of voiceprints).
    if not twentyone_dst.exists():
        config.secure_write_text(twentyone_dst, "{}")
        stats["twentyone_voiceprints_seeded"] = True
        log.info(
            "migration 0.6.2: seeded empty %s "
            "(twentyone starts with no voiceprints)",
            twentyone_dst,
        )
    else:
        log.info(
            "migration 0.6.2: twentyone voiceprint DB already at %s; "
            "skipping seed", twentyone_dst,
        )

    return stats


def migrate_0_6_2() -> dict:
    """Apply the v0.6.2 per-team-voiceprint migration.

    Single step (idempotent): move the legacy central voiceprint DB
    (``~/vezir-data/speaker_profiles.json``) under
    ``~/vezir-data/teams/blink/speaker_profiles.json`` and seed an
    empty DB at ``~/vezir-data/teams/twentyone/speaker_profiles.json``.

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


# ── registry ────────────────────────────────────────────────────────────────


ALL_MIGRATIONS = [migrate_0_6_0, migrate_0_6_2]


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
