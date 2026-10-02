"""`vezir move` (0.26.0): the member-facing client for POST /api/sessions/{id}/move."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from vezir.cli import main
from vezir.client.api import ApiResult, Session


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    base = tmp_path / "rec"
    (base / "blink").mkdir(parents=True)
    (base / "twentyone").mkdir()
    monkeypatch.setenv("VEZIR_RECORD_DIR", str(base))
    monkeypatch.setenv("VEZIR_URL", "https://srv")
    monkeypatch.setenv("VEZIR_TOKEN", "vzr_tok")
    monkeypatch.setenv("VEZIR_TEAM_ID", "startups")
    return base


@pytest.fixture
def server(monkeypatch):
    """Fake VezirClient: the session lives in `blink`; records calls."""
    from vezir.client import api

    state = {"home": "blink", "moves": [], "move_result": None, "synced": True}

    def get_me(self):
        return ApiResult.success({"memberships": [
            {"slug": "startups"}, {"slug": "blink"}, {"slug": "twentyone"},
        ]})

    def get_session(self, sid):
        if self.team_id != state["home"]:
            return ApiResult.http(404, "session not found")
        return ApiResult.success(Session.from_dict({
            "id": sid, "status": "done", "title": "Bounty",
            "sync_enabled": 1 if state["synced"] else 0,
        }))

    def move_session(self, sid, to_team, *, sync=False):
        state["moves"].append((self.team_id, sid, to_team, sync))
        if state["move_result"] is not None:
            return state["move_result"]
        return ApiResult.success({
            "ok": True, "from_team": self.team_id, "to_team": to_team,
            "moved": True, "was_synced": state["synced"], "sync_queued": sync,
            "warning": "this session was already synced to blink's git repo; "
                       "that copy stays there — remove it from the repo manually."
            if state["synced"] else None,
        })

    monkeypatch.setattr(api.VezirClient, "get_me", get_me)
    monkeypatch.setattr(api.VezirClient, "get_session", get_session)
    monkeypatch.setattr(api.VezirClient, "move_session", move_session)
    return state


def _uploaded_folder(base: Path, sid: str) -> Path:
    d = base / "blink" / "meeting-20261002-123316"
    d.mkdir()
    (d / "meeting-20261002-123316.ogg").write_bytes(b"OggS")
    (d / "session.json").write_text(json.dumps({"session_id": sid, "team_id": "blink"}))
    return d


def test_move_finds_source_team_moves_and_follows_locally(env, server):
    d = _uploaded_folder(env, "01S")
    res = CliRunner().invoke(main, ["move", "01S", "--to-team", "twentyone", "-y"])
    assert res.exit_code == 0, res.output
    # Probed the active team (startups) first, found it in blink.
    assert server["moves"] == [("blink", "01S", "twentyone", False)]
    assert "moves blink → twentyone" in res.output
    assert "STAYS there" in res.output  # warned before
    assert "remove it from the repo manually" in res.output  # and after
    new = env / "twentyone" / d.name
    assert new.is_dir() and not d.exists()
    assert json.loads((new / "session.json").read_text())["team_id"] == "twentyone"


def test_move_sync_flag(env, server):
    res = CliRunner().invoke(main, ["move", "01S", "--to-team", "twentyone", "--sync", "-y"])
    assert res.exit_code == 0, res.output
    assert server["moves"][0][3] is True
    assert "sync to twentyone queued" in res.output


def test_move_confirmation_abort(env, server):
    res = CliRunner().invoke(main, ["move", "01S", "--to-team", "twentyone"], input="n\n")
    assert res.exit_code == 1
    assert server["moves"] == []


def test_move_not_found_anywhere(env, server):
    server["home"] = "elsewhere"
    res = CliRunner().invoke(main, ["move", "01S", "--to-team", "twentyone", "-y"])
    assert res.exit_code == 2
    assert "not found in startups, blink, twentyone" in res.output


def test_move_explains_old_server(env, server):
    server["move_result"] = ApiResult.http(404, '{"detail":"Not Found"}')
    res = CliRunner().invoke(main, ["move", "01S", "--to-team", "twentyone", "-y"])
    assert res.exit_code == 1
    assert "older than 0.26.0" in res.output
    assert "vezir session move 01S --to-team twentyone" in res.output


def test_move_surfaces_server_refusal(env, server):
    server["move_result"] = ApiResult.http(409, "session is being processed (transcribing)")
    res = CliRunner().invoke(main, ["move", "01S", "--to-team", "twentyone", "-y"])
    assert res.exit_code == 1
    assert "being processed" in res.output
