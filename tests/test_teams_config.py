"""v0.6.1 client-side teams.json + GET /api/me tests."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

# ── /api/me ─────────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_data(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("VEZIR_DATA", d)
        yield Path(d)


@pytest.fixture
def client_factory(tmp_data):
    from fastapi.testclient import TestClient

    from vezir.server.app import create_app

    def _make():
        return TestClient(create_app(), follow_redirects=False)
    return _make


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_api_me_returns_memberships(client_factory):
    """v0.7.0: /api/me returns identity + a list of every team the
    user is a member of (with role + team_name)."""
    from vezir.server import auth, queue
    client = client_factory()
    tok = auth.issue("alice", team_id="blink")  # shim adds blink membership
    queue.add_membership("alice", "twentyone", role="admin")
    r = client.get("/api/me", headers=_bearer(tok))
    assert r.status_code == 200
    body = r.json()
    assert body["github"] == "alice"
    assert body["is_admin"] is False
    # v0.7.4: memberships are keyed by uuid (team_id); slug is the
    # human identifier the client shows.
    by_team = {m["slug"]: m for m in body["memberships"]}
    assert "blink" in by_team
    assert "twentyone" in by_team
    assert by_team["twentyone"]["role"] == "admin"
    assert by_team["blink"]["team_name"] == "Blink"
    # team_id is the uuid the client sends back in X-Team-Id.
    assert by_team["blink"]["team_id"] != "blink"


def test_api_me_returns_admin_flag(client_factory):
    from vezir.server import auth
    client = client_factory()
    tok = auth.issue("alice", is_admin=True)
    r = client.get("/api/me", headers=_bearer(tok))
    assert r.status_code == 200
    assert r.json()["is_admin"] is True


def test_api_me_empty_memberships(client_factory):
    """A token whose handle has no memberships still gets a 200 with
    an empty memberships list (so the client can display 'no teams').
    """
    from vezir.server import auth
    client = client_factory()
    # Use the raw issue path so the shim doesn't auto-membership.
    tok = auth._issue_raw("orphan")
    r = client.get("/api/me", headers=_bearer(tok))
    assert r.status_code == 200
    body = r.json()
    assert body["github"] == "orphan"
    assert body["memberships"] == []


def test_api_me_rejects_no_bearer(client_factory):
    client = client_factory()
    r = client.get("/api/me")
    assert r.status_code == 401


def test_api_me_rejects_invalid_token(client_factory):
    client = client_factory()
    r = client.get("/api/me", headers=_bearer("vzr_bogus"))
    assert r.status_code == 401


# ── client-side teams.json store ────────────────────────────────────────────


@pytest.fixture
def tmp_home(monkeypatch):
    """Pin Path.home() at a temp dir so teams.json + client.json land in isolation."""
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("HOME", d)
        monkeypatch.delenv("VEZIR_URL", raising=False)
        monkeypatch.delenv("VEZIR_TOKEN", raising=False)
        # Path.home() reads $HOME on Linux; make sure nothing has cached it.
        yield Path(d)


def test_load_teams_config_empty(tmp_home):
    from vezir.client.config import load_teams_config
    cfg = load_teams_config()
    assert cfg == {"teams": [], "active": None}


def test_add_team_credentials_creates_file(tmp_home):
    from vezir.client.config import add_team_credentials, load_teams_config
    add_team_credentials(
        "blink", "https://muscle/", "vzr_blink_abc", label="Blink",
    )
    cfg = load_teams_config()
    assert len(cfg["teams"]) == 1
    assert cfg["teams"][0]["id"] == "blink"
    assert cfg["teams"][0]["label"] == "Blink"
    # First team auto-activates.
    assert cfg["active"] == "blink"


def test_add_team_credentials_updates_existing(tmp_home):
    from vezir.client.config import add_team_credentials, load_teams_config
    add_team_credentials("blink", "https://old/", "vzr_old", label="Blink")
    add_team_credentials("blink", "https://new/", "vzr_new", label="Blink v2")
    cfg = load_teams_config()
    assert len(cfg["teams"]) == 1
    assert cfg["teams"][0]["url"] == "https://new/"
    assert cfg["teams"][0]["token"] == "vzr_new"
    assert cfg["teams"][0]["label"] == "Blink v2"


def test_add_second_team_does_not_auto_activate(tmp_home):
    from vezir.client.config import add_team_credentials, load_teams_config
    add_team_credentials("blink", "https://m/", "vzr_b")
    add_team_credentials("twentyone", "https://m/", "vzr_t")
    cfg = load_teams_config()
    assert cfg["active"] == "blink"


def test_add_with_activate_overrides(tmp_home):
    from vezir.client.config import add_team_credentials, load_teams_config
    add_team_credentials("blink", "https://m/", "vzr_b")
    add_team_credentials("twentyone", "https://m/", "vzr_t", activate=True)
    cfg = load_teams_config()
    assert cfg["active"] == "twentyone"


def test_set_active_team(tmp_home):
    from vezir.client.config import (
        add_team_credentials,
        load_teams_config,
        set_active_team,
    )
    add_team_credentials("blink", "https://m/", "vzr_b")
    add_team_credentials("twentyone", "https://m/", "vzr_t")
    set_active_team("twentyone")
    cfg = load_teams_config()
    assert cfg["active"] == "twentyone"


def test_set_active_team_unknown_raises(tmp_home):
    from vezir.client.config import add_team_credentials, set_active_team
    add_team_credentials("blink", "https://m/", "vzr_b")
    with pytest.raises(ValueError, match="not configured locally"):
        set_active_team("nonexistent")


def test_remove_team_credentials(tmp_home):
    from vezir.client.config import (
        add_team_credentials,
        load_teams_config,
        remove_team_credentials,
    )
    add_team_credentials("blink", "https://m/", "vzr_b")
    add_team_credentials("twentyone", "https://m/", "vzr_t")
    remove_team_credentials("blink")
    cfg = load_teams_config()
    ids = {t["id"] for t in cfg["teams"]}
    assert ids == {"twentyone"}
    # blink was active; new active should be twentyone (the remaining one).
    assert cfg["active"] == "twentyone"


def test_remove_team_empties_active_when_last_removed(tmp_home):
    from vezir.client.config import (
        add_team_credentials,
        load_teams_config,
        remove_team_credentials,
    )
    add_team_credentials("blink", "https://m/", "vzr_b")
    remove_team_credentials("blink")
    cfg = load_teams_config()
    assert cfg["teams"] == []
    assert cfg["active"] is None


def test_next_team_id_cycles(tmp_home):
    from vezir.client.config import add_team_credentials, next_team_id
    add_team_credentials("blink", "https://m/", "vzr_b")
    add_team_credentials("twentyone", "https://m/", "vzr_t")
    assert next_team_id("blink") == "twentyone"
    assert next_team_id("twentyone") == "blink"  # wrap


def test_next_team_id_empty_returns_none(tmp_home):
    from vezir.client.config import next_team_id
    assert next_team_id("blink") is None


def test_next_team_id_unknown_current_returns_first(tmp_home):
    from vezir.client.config import add_team_credentials, next_team_id
    add_team_credentials("blink", "https://m/", "vzr_b")
    assert next_team_id("never-configured") == "blink"


# ── resolve_credentials precedence ──────────────────────────────────────────


def test_resolve_credentials_env_wins(tmp_home, monkeypatch):
    from vezir.client.config import add_team_credentials, resolve_credentials
    add_team_credentials("blink", "https://teamsjson/", "vzr_fromteamsjson")
    monkeypatch.setenv("VEZIR_URL", "https://envwins/")
    monkeypatch.setenv("VEZIR_TOKEN", "vzr_envwins")
    monkeypatch.delenv("VEZIR_TEAM_ID", raising=False)
    url, token, team_id, source = resolve_credentials()
    assert source == "env"
    assert url == "https://envwins/"
    assert token == "vzr_envwins"
    assert team_id is None  # not set in env -> None


def test_resolve_credentials_env_team_id_supported(tmp_home, monkeypatch):
    """v0.7.0: VEZIR_TEAM_ID supplies the team scope for env-creds mode."""
    from vezir.client.config import resolve_credentials
    monkeypatch.setenv("VEZIR_URL", "https://m/")
    monkeypatch.setenv("VEZIR_TOKEN", "vzr_x")
    monkeypatch.setenv("VEZIR_TEAM_ID", "blink")
    url, token, team_id, source = resolve_credentials()
    assert source == "env"
    assert team_id == "blink"


def test_resolve_credentials_teams_json_when_no_env(tmp_home, monkeypatch):
    from vezir.client.config import add_team_credentials, resolve_credentials
    add_team_credentials("blink", "https://teams/", "vzr_teams")
    monkeypatch.delenv("VEZIR_URL", raising=False)
    monkeypatch.delenv("VEZIR_TOKEN", raising=False)
    monkeypatch.delenv("VEZIR_TEAM_ID", raising=False)
    url, token, team_id, source = resolve_credentials()
    assert source == "teams:blink"
    assert url == "https://teams/"
    assert token == "vzr_teams"
    assert team_id == "blink"


def test_resolve_credentials_client_json_when_no_env_no_teams(tmp_home, monkeypatch):
    from vezir.client.config import resolve_credentials, save_client_prefs
    monkeypatch.delenv("VEZIR_URL", raising=False)
    monkeypatch.delenv("VEZIR_TOKEN", raising=False)
    monkeypatch.delenv("VEZIR_TEAM_ID", raising=False)
    save_client_prefs({"url": "https://client/", "token": "vzr_client"})
    url, token, team_id, source = resolve_credentials()
    assert source == "client"
    assert url == "https://client/"
    assert token == "vzr_client"
    # client.json doesn't store team_id by default.
    assert team_id is None


def test_resolve_credentials_returns_nones_when_nothing_set(tmp_home, monkeypatch):
    from vezir.client.config import resolve_credentials
    monkeypatch.delenv("VEZIR_URL", raising=False)
    monkeypatch.delenv("VEZIR_TOKEN", raising=False)
    monkeypatch.delenv("VEZIR_TEAM_ID", raising=False)
    url, token, team_id, source = resolve_credentials()
    assert url is None
    assert token is None
    assert team_id is None
    assert source is None


def test_resolve_credentials_env_partial_falls_through(tmp_home, monkeypatch):
    """Setting only VEZIR_URL (no token) should fall through to teams.json."""
    from vezir.client.config import add_team_credentials, resolve_credentials
    add_team_credentials("blink", "https://teams/", "vzr_teams")
    monkeypatch.setenv("VEZIR_URL", "https://only-url/")
    monkeypatch.delenv("VEZIR_TOKEN", raising=False)
    url, token, team_id, source = resolve_credentials()
    assert source == "teams:blink"
    assert url == "https://teams/"
    assert team_id == "blink"


# ── teams.json file format defensive parsing ────────────────────────────────


def test_load_teams_config_handles_malformed_json(tmp_home):
    from vezir.client.config import load_teams_config, teams_config_path
    p = teams_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not valid json")
    cfg = load_teams_config()
    assert cfg == {"teams": [], "active": None}


def test_load_teams_config_drops_entries_missing_fields(tmp_home):
    from vezir.client.config import load_teams_config, teams_config_path
    p = teams_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "teams": [
            {"id": "good", "url": "u", "token": "t"},
            {"id": "no_url", "token": "t"},
            {"url": "u", "token": "t"},  # no id
            {"id": "no_token", "url": "u"},
        ],
        "active": "good",
    }))
    cfg = load_teams_config()
    ids = {t["id"] for t in cfg["teams"]}
    assert ids == {"good"}
    assert cfg["active"] == "good"


def test_load_teams_config_corrects_active_when_orphaned(tmp_home):
    from vezir.client.config import load_teams_config, teams_config_path
    p = teams_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "teams": [{"id": "blink", "url": "u", "token": "t"}],
        "active": "ghost",  # not in teams list
    }))
    cfg = load_teams_config()
    # Active was orphaned; should fall back to first team.
    assert cfg["active"] == "blink"
