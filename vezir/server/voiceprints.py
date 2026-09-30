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


# ── Surgery on a team DB: remove / merge (v0.23.2) ──────────────────────────
#
# A profile is a running average with no record of what went into it, so a
# polluted one can't be cleaned — only removed (the person is re-learned the
# next time they're labeled) or, for one person enrolled under two names,
# merged.  Both back up the DB first; the backup path is returned.


def _load_team_db(team_id: str) -> tuple[Path, dict]:
    if not team_id:
        raise ValueError("team_id is required")
    p = config.team_speaker_profiles_path(team_id)
    if not p.exists():
        raise FileNotFoundError(f"team {team_id!r} has no voiceprint DB at {p}")
    return p, json.loads(p.read_text(encoding="utf-8") or "{}")


def backup_db(team_id: str, reason: str) -> Path:
    """Copy the team DB to ``speaker_profiles.json.bak-<ts>-<reason>`` (0600)."""
    import time

    p, data = _load_team_db(team_id)
    slug = "".join(c if c.isalnum() else "-" for c in reason).strip("-") or "backup"
    bak = p.with_name(f"{p.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}-{slug}")
    config.secure_write_text(bak, json.dumps(data, indent=2, ensure_ascii=False))
    return bak


def remove_profile(team_id: str, name: str) -> Path:
    """Delete profile ``name`` from the team DB.  Returns the backup path."""
    p, data = _load_team_db(team_id)
    if name not in data:
        raise KeyError(f"no profile named {name!r} (known: {', '.join(sorted(data))})")
    bak = backup_db(team_id, f"remove-{name}")
    del data[name]
    config.secure_write_text(p, json.dumps(data, indent=2, ensure_ascii=False))
    return bak


def merge_profiles(team_id: str, source: str, target: str) -> tuple[Path, int]:
    """Fold profile ``source`` into ``target`` (same person, two names).

    The embeddings are averaged weighted by ``n_sessions`` and re-normalized
    — exactly what millet would have produced had every session been
    enrolled under ``target`` — and ``source`` is removed.  Returns
    ``(backup_path, merged_n_sessions)``.
    """
    import math

    p, data = _load_team_db(team_id)
    for n in (source, target):
        if n not in data:
            raise KeyError(f"no profile named {n!r} (known: {', '.join(sorted(data))})")
    if source == target:
        raise ValueError("source and target are the same profile")
    src, dst = data[source], data[target]
    ns, nt = int(src.get("n_sessions", 1)), int(dst.get("n_sessions", 1))
    es, et = src["embedding"], dst["embedding"]
    if len(es) != len(et):
        raise ValueError("embedding sizes differ; not the same model")

    def unit(v: list) -> list:
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]

    es, et = unit(es), unit(et)
    merged = unit([(a * nt + b * ns) / (nt + ns) for a, b in zip(et, es, strict=True)])
    bak = backup_db(team_id, f"merge-{source}-into-{target}")
    data[target] = {**dst, "embedding": merged, "n_sessions": nt + ns}
    del data[source]
    config.secure_write_text(p, json.dumps(data, indent=2, ensure_ascii=False))
    return bak, nt + ns
