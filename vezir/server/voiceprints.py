"""Central voiceprint DB management for vezir.

Vezir owns its own profile DB at ~/vezir-data/speaker_profiles.json. The
worker exposes this DB to unmodified meetscribe via the per-job HOME shim
(see meet_runner.build_home_shim). The schema matches what
meet/voiceprint.py:88 (load_profiles) expects.

Helper functions here are used to seed the DB and to inspect it from the
web UI.
"""
from __future__ import annotations

import json
from pathlib import Path

from .. import config


def ensure_db_exists() -> Path:
    """Create an empty profile DB file if not present. Returns its path."""
    p = config.speaker_profiles_path()
    config.secure_mkdir(p.parent)
    if not p.exists():
        config.secure_write_text(p, "{}")
    else:
        config.secure_chmod_file(p)
    return p


def list_known_names() -> list[str]:
    """Return sorted list of names enrolled in the central profile DB."""
    p = config.speaker_profiles_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    return sorted(data.keys())


def seed_from(source: Path, *, merge: bool = False) -> dict:
    """Copy or merge an existing meetscribe profiles file into vezir.

    Args:
        source: Path to the source profiles file.
        merge: When True, merge into the existing central DB.  Per-name
            policy: the profile with the higher ``n_sessions`` wins (it
            has more training data).  When False (default), refuses if
            the central DB is already populated.

    Returns:
        Dict with keys ``added``, ``updated``, ``kept``, ``total``.
    """
    target = config.speaker_profiles_path()
    existing: dict = {}
    if target.exists():
        existing = json.loads(target.read_text(encoding="utf-8") or "{}")
        if existing and not merge:
            raise FileExistsError(
                f"central profile DB already populated at {target}"
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
