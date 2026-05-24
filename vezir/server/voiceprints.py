"""Per-team voiceprint DB management for vezir.

v0.6.2+: each team holds its own voiceprint DB at
``~/vezir-data/teams/<team_id>/speaker_profiles.json``.  The worker
exposes the caller's per-team DB to unmodified millet via the per-job
HOME shim (see meet_runner.build_home_shim).  The schema matches what
``meet/voiceprint.py`` (load_profiles) expects: a plain JSON dict
keyed by speaker name.

Pre-v0.6.2 vezir kept a single central DB at
``~/vezir-data/speaker_profiles.json``.  The v0.6.2 migration moves
that file under ``teams/blink/`` and seeds ``teams/twentyone/`` empty.

Helper functions here are used to seed each team's DB and to inspect
it from the web UI / CLI / labeling pipeline.  All accept ``team_id``
explicitly; there is no longer a single global default — callers must
pass the team they want to operate on.
"""
from __future__ import annotations

import json
from pathlib import Path

from .. import config


def ensure_db_exists(team_id: str) -> Path:
    """Create an empty per-team profile DB file if not present. Returns its path."""
    if not team_id:
        raise ValueError("ensure_db_exists requires team_id (added in v0.6.2)")
    p = config.team_speaker_profiles_path(team_id)
    config.secure_mkdir(p.parent)
    if not p.exists():
        config.secure_write_text(p, "{}")
    else:
        config.secure_chmod_file(p)
    return p


def list_known_names(team_id: str) -> list[str]:
    """Return sorted list of names enrolled in the team's profile DB."""
    if not team_id:
        raise ValueError("list_known_names requires team_id (added in v0.6.2)")
    p = config.team_speaker_profiles_path(team_id)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    return sorted(data.keys())


def seed_from(source: Path, team_id: str, *, merge: bool = False) -> dict:
    """Copy or merge an existing millet profiles file into a team's DB.

    Args:
        source: Path to the source profiles file.
        team_id: Slug of the team whose DB to seed (required v0.6.2+).
        merge: When True, merge into the existing team DB.  Per-name
            policy: the profile with the higher ``n_sessions`` wins (it
            has more training data).  When False (default), refuses if
            the team's DB is already populated.

    Returns:
        Dict with keys ``added``, ``updated``, ``kept``, ``total``.
    """
    if not team_id:
        raise ValueError("seed_from requires team_id (added in v0.6.2)")
    target = config.team_speaker_profiles_path(team_id)
    existing: dict = {}
    if target.exists():
        existing = json.loads(target.read_text(encoding="utf-8") or "{}")
        if existing and not merge:
            raise FileExistsError(
                f"team {team_id!r} profile DB already populated at {target}"
            )

    source_data = json.loads(source.read_text(encoding="utf-8"))
    stats = {"added": 0, "updated": 0, "kept": 0, "total": 0}

    for name, info in source_data.items():
        src_n = info.get("n_sessions", 1)
        if name not in existing:
            existing[name] = info
            stats["added"] += 1
        else:
            dst_n = existing[name].get("n_sessions", 1)
            if src_n > dst_n:
                existing[name] = info
                stats["updated"] += 1
            else:
                stats["kept"] += 1

    stats["total"] = len(existing)
    config.secure_mkdir(target.parent)
    config.secure_write_text(
        target,
        json.dumps(existing, indent=2, ensure_ascii=False),
    )
    return stats
