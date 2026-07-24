"""Shared client-side config persistence.

Two files, two purposes:

* ``~/.config/vezir/client.json`` — non-secret user preferences (URL +
  token may also live here for back-compat with the single-team era).
  Same shape as in v0.5.x:
      - url, token              server identity (env vars override)
      - summary_preset          last-used preset id
      - auto_label              True/False
      - sync                    True/False
      - last_import_dir         last directory used by the Import picker

* ``~/.config/vezir/teams.json`` — v0.6.1+ client-side multi-team
  config.  Lets a thin client hold credentials for N teams and switch
  between them at runtime (TUI ``^t`` binding; CLI ``vezir team config
  use``).  Shape:
      {
        "teams": [
          {"id": "blink",     "url": "https://...", "token": "vzr_...",
           "label": "Blink"},
          {"id": "twentyone", "url": "https://...", "token": "vzr_...",
           "label": "Twentyone"}
        ],
        "active": "blink"
      }
  When ``teams.json`` exists and has an active entry, it WINS over
  env vars and over ``client.json``'s url/token.  This is the
  precedence order that callers expect; see :func:`resolve_credentials`.

Both files are mode 0600 (handled by ``config.secure_write_text``).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .. import config as _server_config


def client_config_path() -> Path:
    return Path.home() / ".config" / "vezir" / "client.json"


def teams_config_path() -> Path:
    """v0.6.1+: multi-team credentials store.

    Lives next to ``client.json`` and follows the same 0600 perms.
    """
    return Path.home() / ".config" / "vezir" / "teams.json"


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


# ── v0.6.1: multi-team client config ────────────────────────────────────────


def load_teams_config() -> dict:
    """Return the on-disk teams config, or ``{"teams": [], "active": None}``.

    Defensive: malformed JSON, wrong shape, etc. all return the empty
    shape so callers can do ``cfg["teams"]`` without a KeyError.
    """
    p = teams_config_path()
    if not p.exists():
        return {"teams": [], "active": None}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"teams": [], "active": None}
    if not isinstance(data, dict):
        return {"teams": [], "active": None}
    teams = data.get("teams")
    if not isinstance(teams, list):
        teams = []
    # Filter out entries that don't have the required fields.
    clean_teams = []
    for t in teams:
        if not isinstance(t, dict):
            continue
        if not t.get("id") or not t.get("url") or not t.get("token"):
            continue
        clean_teams.append(t)
    active = data.get("active")
    if active and not any(t["id"] == active for t in clean_teams):
        active = clean_teams[0]["id"] if clean_teams else None
    # Preserve any unknown top-level keys (e.g. fields added by a newer
    # client version) so the load→mutate→save round-trip every mutator does
    # doesn't silently erase them (L-5).  ``teams``/``active`` are always
    # overwritten with the cleaned values above.
    result = {k: v for k, v in data.items() if k not in ("teams", "active")}
    result["teams"] = clean_teams
    result["active"] = active
    return result


def save_teams_config(data: dict) -> None:
    """Atomically overwrite teams.json (mode 0600)."""
    p = teams_config_path()
    _server_config.secure_write_text(p, json.dumps(data, indent=2))


def active_team_credentials() -> tuple[str | None, str | None, str | None]:
    """Return ``(team_id, url, token)`` for the active team in teams.json,
    or ``(None, None, None)`` if teams.json is missing / empty / has no
    active entry.

    Used by :func:`resolve_credentials` to honor teams.json precedence
    when env vars are not set.  Callers should NEVER persist the
    returned token; treat it as a per-request snapshot.
    """
    cfg = load_teams_config()
    active = cfg.get("active")
    if not active:
        return None, None, None
    for t in cfg["teams"]:
        if t["id"] == active:
            return t["id"], t.get("url"), t.get("token")
    return None, None, None


def active_team_expiry() -> float | None:
    """Return the active team's stored session expiry (unix seconds), or None.

    Used by clients (TUI) to proactively warn before a session JWT expires
    (0.8.9).  ``None`` when teams.json has no active entry or the entry predates
    expiry tracking (older logins).
    """
    cfg = load_teams_config()
    active = cfg.get("active")
    if not active:
        return None
    for t in cfg["teams"]:
        if t["id"] == active:
            exp = t.get("expires_at")
            return float(exp) if exp is not None else None
    return None


def team_credentials(team: str) -> tuple[str | None, str | None, str | None]:
    """Resolve a named team (slug/id) from teams.json to ``(team_id, url, token)``.

    Returns ``(None, None, None)`` when *team* is not configured locally.
    The match is on the teams.json entry ``id`` (which is the slug, e.g.
    ``"blink"``).  Callers should NEVER persist the returned token.
    """
    cfg = load_teams_config()
    for t in cfg["teams"]:
        if t["id"] == team:
            return t["id"], t.get("url"), t.get("token")
    return None, None, None


def resolve_credentials() -> tuple[
    str | None, str | None, str | None, str | None,
]:
    """Return ``(url, token, team_id, source)`` using precedence rules.

    v0.7.0: adds ``team_id`` to the tuple.  Every team-scoped HTTP
    request sends an ``X-Team-Id`` header naming this team.  ``None``
    is returned when no team has been configured -- the caller can
    still hit /api/me to discover memberships and prompt the user.

    Precedence (highest first):
      1. ``VEZIR_URL`` + ``VEZIR_TOKEN`` env vars (BOTH must be set).
         Optional ``VEZIR_TEAM_ID`` supplies the team scope.
         Source label: ``"env"``.
      2. teams.json active entry.  Source label: ``"teams:<id>"``.
      3. client.json ``url`` + ``token`` keys.  Source label:
         ``"client"``.  No team_id available from this source.
      4. Nothing.  Returns ``(None, None, None, None)``.
    """
    env_url = os.environ.get("VEZIR_URL")
    env_token = os.environ.get("VEZIR_TOKEN")
    env_team = os.environ.get("VEZIR_TEAM_ID") or None
    if env_url and env_token:
        return env_url, env_token, env_team, "env"

    team_id, url, token = active_team_credentials()
    if url and token:
        return url, token, team_id, f"teams:{team_id}"

    prefs = load_client_prefs()
    p_url = prefs.get("url")
    p_token = prefs.get("token")
    if p_url and p_token:
        return p_url, p_token, prefs.get("team_id"), "client"

    return None, None, None, None


def set_active_team(team_id: str) -> dict:
    """Mark a team as active in teams.json; persist; return the new config.

    Raises ``ValueError`` if no team with that id is configured.
    """
    cfg = load_teams_config()
    if not any(t["id"] == team_id for t in cfg["teams"]):
        raise ValueError(
            f"team {team_id!r} is not configured locally; "
            f"add it with `vezir team config add` first"
        )
    cfg["active"] = team_id
    save_teams_config(cfg)
    return cfg


def add_team_credentials(
    team_id: str,
    url: str,
    token: str,
    label: str | None = None,
    *,
    activate: bool = False,
) -> dict:
    """Add or update a team entry in teams.json.

    If a team with the same id already exists, its url/token/label are
    replaced (use case: re-issued token).  When ``activate=True`` (or
    when this is the first team being added), the team becomes the
    active team automatically.
    """
    cfg = load_teams_config()
    teams: list = cfg["teams"]
    found = False
    for t in teams:
        if t["id"] == team_id:
            t["url"] = url
            t["token"] = token
            if label is not None:
                t["label"] = label
            found = True
            break
    if not found:
        teams.append({
            "id": team_id,
            "url": url,
            "token": token,
            "label": label or team_id,
        })
    if activate or not cfg.get("active"):
        cfg["active"] = team_id
    save_teams_config(cfg)
    return cfg


def set_team_session(
    team_id: str,
    url: str,
    session_jwt: str,
    npub: str,
    label: str | None = None,
    *,
    activate: bool = True,
    expires_at: float | None = None,
    refresh_token: str | None = None,
    refresh_expires_at: float | None = None,
) -> dict:
    """Store a nostr-login session for a team in teams.json.

    The session (access) JWT is stored in the same ``token`` field that
    bearer tokens use, so :func:`resolve_credentials` and
    ``api.VezirClient._headers()`` send it as ``Authorization: Bearer``
    with no special-casing.  We additionally record ``auth="nostr"`` and
    the ``npub`` so ``vezir login`` can recognize a re-login candidate and
    show whose key is bound.  Upserts on ``team_id`` like
    :func:`add_team_credentials`.

    ``expires_at`` (unix seconds, optional): the access JWT's expiry, stored
    so clients can proactively warn/refresh before a request 401s (0.8.9).
    ``None`` leaves any existing value untouched on update.

    ``refresh_token`` (optional): the rotating refresh token (``vzrt_…``)
    exchanged at ``POST /api/auth/refresh`` for a fresh pair.  Rotated on
    every refresh, so this is upserted each time.  ``refresh_expires_at``
    is its idle-expiry (unix seconds).  ``None`` for either leaves the
    stored value untouched (so a plain expiry-only update doesn't wipe a
    stored refresh token).
    """
    cfg = load_teams_config()
    teams: list = cfg["teams"]
    for t in teams:
        if t["id"] == team_id:
            t["url"] = url
            t["token"] = session_jwt
            t["auth"] = "nostr"
            t["npub"] = npub
            if label is not None:
                t["label"] = label
            if expires_at is not None:
                t["expires_at"] = expires_at
            if refresh_token is not None:
                t["refresh_token"] = refresh_token
            if refresh_expires_at is not None:
                t["refresh_expires_at"] = refresh_expires_at
            break
    else:
        entry = {
            "id": team_id,
            "url": url,
            "token": session_jwt,
            "auth": "nostr",
            "npub": npub,
            "label": label or team_id,
        }
        if expires_at is not None:
            entry["expires_at"] = expires_at
        if refresh_token is not None:
            entry["refresh_token"] = refresh_token
        if refresh_expires_at is not None:
            entry["refresh_expires_at"] = refresh_expires_at
        teams.append(entry)
    if activate or not cfg.get("active"):
        cfg["active"] = team_id
    save_teams_config(cfg)
    return cfg


def active_team_refresh_token() -> str | None:
    """Return the active team's stored refresh token (``vzrt_…``), or None.

    ``None`` when there's no active team, the entry predates refresh
    tokens (pre-refresh logins), or a ``vzr_`` bearer is in use.
    """
    cfg = load_teams_config()
    active = cfg.get("active")
    if not active:
        return None
    for t in cfg["teams"]:
        if t["id"] == active:
            return t.get("refresh_token")
    return None


def remove_team_credentials(team_id: str) -> dict:
    """Remove a team entry from teams.json.  Idempotent if missing.

    If the removed team was active, the new active is the first
    remaining team (or ``None`` if the list is now empty).
    """
    cfg = load_teams_config()
    cfg["teams"] = [t for t in cfg["teams"] if t["id"] != team_id]
    if cfg.get("active") == team_id:
        cfg["active"] = cfg["teams"][0]["id"] if cfg["teams"] else None
    save_teams_config(cfg)
    return cfg


def next_team_id(current: str | None) -> str | None:
    """Cycle helper: return the slug of the team after ``current``.

    Wraps around at the end of the list.  Returns ``None`` if no
    teams are configured.  When ``current`` isn't in the list,
    returns the first configured team.

    Hardened for v0.7.4's team-UUID migration: if ``current`` is a
    team UUID (e.g. it leaked in from /api/me's UUID-keyed membership
    list) rather than a configured slug, resolve it back to the
    matching slug via any ``uuid``/``team_id`` field a team entry
    happens to carry, so the cycle starts from the right position
    instead of snapping to the first team.
    """
    cfg = load_teams_config()
    teams = cfg["teams"]
    if not teams:
        return None
    ids = [t["id"] for t in teams]
    if current not in ids:
        # Maybe ``current`` is a UUID; map it back to a slug if any
        # entry records one.
        for t in teams:
            if current and current in (t.get("uuid"), t.get("team_id")):
                current = t["id"]
                break
    if current not in ids:
        return ids[0]
    idx = ids.index(current)
    return ids[(idx + 1) % len(ids)]
