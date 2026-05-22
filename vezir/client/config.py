"""Shared client-side config persistence (~/.config/vezir/client.json).

The file holds non-secret user preferences that should stick across
sessions and across the CLI <-> GUI boundary:

  - url, token              server identity (also overridable via env vars)
  - summary_preset          last-used preset id
  - auto_label              True/False — toggle for the auto-label opt-out
  - sync                    True/False — toggle for the sync opt-out

URL and token are read/written by the GUI today (gui.py) for backward
compatibility with the existing on-disk schema; this module mirrors that
shape so callers can update one key without losing the others.

File mode is 0600 (handled by config.secure_write_text).
"""
from __future__ import annotations

import json
from pathlib import Path

from .. import config as _server_config


def client_config_path() -> Path:
    return Path.home() / ".config" / "vezir" / "client.json"


def load_client_prefs() -> dict:
    """Return the on-disk client config, or {} if it doesn't exist / is
    unreadable.  Always returns a dict (never None) so callers can do
    ``prefs.get(...)`` without a guard."""
    p = client_config_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def save_client_prefs(data: dict) -> None:
    """Atomically overwrite the client config with `data` (mode 0600).

    Callers should usually load -> mutate -> save to avoid stomping
    keys they don't know about.
    """
    p = client_config_path()
    _server_config.secure_write_text(p, json.dumps(data, indent=2))
