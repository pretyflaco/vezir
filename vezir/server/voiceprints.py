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


def seed_from(source: Path) -> int:
    """One-shot copy of an existing meetscribe profiles file into vezir.

    Returns the number of profiles copied. Will not overwrite an existing
    central DB; raises FileExistsError if one is already present.
    """
    target = config.speaker_profiles_path()
    if target.exists():
        existing = json.loads(target.read_text(encoding="utf-8") or "{}")
        if existing:
            raise FileExistsError(
                f"central profile DB already populated at {target}"
            )
    config.secure_mkdir(target.parent)
    data = json.loads(source.read_text(encoding="utf-8"))
    config.secure_write_text(
        target,
        json.dumps(data, indent=2, ensure_ascii=False),
    )
    return len(data)
