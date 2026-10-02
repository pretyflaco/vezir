"""`vezir upload` must resolve the active team and send X-Team-Id.

Regression for the 0.8.x bug where `vezir upload` (the after-the-fact CLI
upload) resolved credentials via the team-less ``config.server_url()`` /
``config.client_token()`` and called ``uploader.upload()`` without
``team_id=``.  The server requires X-Team-Id on every team-scoped endpoint
(v0.7.0+); a missing header is a hard HTTP 400.  scribe/TUI passed team_id
and worked; the CLI did not.

These tests drive the Click command and assert the team flows through to
the uploader, without any network.
"""
from __future__ import annotations

import json
import time
import wave
from pathlib import Path

import pytest
from click.testing import CliRunner

from vezir.cli import main


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Never read the developer's real ~/.config/vezir/teams.json."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


def _tiny_ogg(tmp_path: Path) -> Path:
    # The uploader validates extension + magic bytes; a minimal OggS header
    # is enough for validate_audio_path/_pick logic in our stubs (we stub
    # the uploader entirely, so content is irrelevant, but keep a real file).
    p = tmp_path / "meeting-20260617-123708.ogg"
    p.write_bytes(b"OggS" + b"\x00" * 64)
    return p


def _wav(tmp_path: Path) -> Path:
    p = tmp_path / "rec.wav"
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 1600)
    return p


def _stub_uploader(monkeypatch, captured: dict):
    """Stub uploader so no network happens; capture team_id + endpoint."""
    from vezir.client import uploader

    monkeypatch.setattr(uploader, "validate_audio_path", lambda p: Path(p))
    monkeypatch.setattr(
        uploader, "server_supports_resumable",
        lambda *a, **k: (captured.update(probe_team=k.get("team_id")) or True),
    )

    def fake_resumable(server_url, token, audio_path, **kw):
        captured["endpoint"] = "resumable"
        captured["team_id"] = kw.get("team_id")
        captured["server_url"] = server_url
        captured["token"] = token
        return {"session_id": "01TEST", "bytes": 64}

    def fake_oneshot(server_url, token, audio_path, **kw):
        captured["endpoint"] = "oneshot"
        captured["team_id"] = kw.get("team_id")
        return {"session_id": "01TEST", "bytes": 64}

    monkeypatch.setattr(uploader, "upload_resumable", fake_resumable)
    monkeypatch.setattr(uploader, "upload", fake_oneshot)


def test_upload_uses_active_team_from_teams_json(tmp_path, monkeypatch):
    """With teams.json active, `vezir upload` forwards that team_id."""
    captured: dict = {}
    _stub_uploader(monkeypatch, captured)

    # No env creds; rely on teams.json.
    monkeypatch.delenv("VEZIR_URL", raising=False)
    monkeypatch.delenv("VEZIR_TOKEN", raising=False)
    monkeypatch.delenv("VEZIR_TEAM_ID", raising=False)
    monkeypatch.setattr(
        "vezir.client.config.resolve_credentials",
        lambda: ("https://srv", "vzr_tok", "blink", "teams:blink"),
    )

    res = CliRunner().invoke(main, ["upload", str(_tiny_ogg(tmp_path))])
    assert res.exit_code == 0, res.output
    assert captured["team_id"] == "blink"
    assert captured["endpoint"] == "resumable"
    assert captured["probe_team"] == "blink"


def test_upload_team_flag_overrides(tmp_path, monkeypatch):
    """--team resolves a teams.json entry and wins over the active team."""
    captured: dict = {}
    _stub_uploader(monkeypatch, captured)
    monkeypatch.delenv("VEZIR_URL", raising=False)
    monkeypatch.delenv("VEZIR_TOKEN", raising=False)
    monkeypatch.setattr(
        "vezir.client.config.team_credentials",
        lambda team: ("twentyone", "https://t21", "vzr_t21")
        if team == "twentyone" else (None, None, None),
    )
    # active is blink, but --team twentyone must win
    monkeypatch.setattr(
        "vezir.client.config.resolve_credentials",
        lambda: ("https://srv", "vzr_tok", "blink", "teams:blink"),
    )

    res = CliRunner().invoke(
        main, ["upload", "--team", "twentyone", str(_tiny_ogg(tmp_path))]
    )
    assert res.exit_code == 0, res.output
    assert captured["team_id"] == "twentyone"
    assert captured["server_url"] == "https://t21"


def test_upload_unknown_team_passes_slug_through(tmp_path, monkeypatch):
    """--team not in teams.json still forwards the slug (server resolves it)."""
    captured: dict = {}
    _stub_uploader(monkeypatch, captured)
    monkeypatch.setenv("VEZIR_URL", "https://srv")
    monkeypatch.setenv("VEZIR_TOKEN", "vzr_tok")
    monkeypatch.setattr(
        "vezir.client.config.team_credentials",
        lambda team: (None, None, None),
    )

    res = CliRunner().invoke(
        main, ["upload", "--team", "newteam", str(_tiny_ogg(tmp_path))]
    )
    assert res.exit_code == 0, res.output
    assert captured["team_id"] == "newteam"


def test_upload_no_team_errors_clearly(tmp_path, monkeypatch):
    """No team anywhere -> fail fast with a helpful message, not a 400."""
    captured: dict = {}
    _stub_uploader(monkeypatch, captured)
    monkeypatch.delenv("VEZIR_URL", raising=False)
    monkeypatch.delenv("VEZIR_TEAM_ID", raising=False)
    # creds present but team_id is None (e.g. client.json source)
    monkeypatch.setenv("VEZIR_TOKEN", "vzr_tok")
    monkeypatch.setattr(
        "vezir.client.config.resolve_credentials",
        lambda: ("https://srv", "vzr_tok", None, "client"),
    )
    monkeypatch.setattr("vezir.config.server_url", lambda: "https://srv")
    monkeypatch.setattr("vezir.config.client_token", lambda: "vzr_tok")

    res = CliRunner().invoke(main, ["upload", str(_tiny_ogg(tmp_path))])
    assert res.exit_code == 1
    assert "no team selected" in res.output
    assert "team_id" not in captured  # never reached the uploader


# ── 0.24.0: identity-wide credentials ──────────────────────────────────────


def _write_teams(home: Path, teams: list, active: str) -> None:
    from vezir import config as server_config

    cfgdir = home / ".config" / "vezir"
    cfgdir.mkdir(parents=True, exist_ok=True)
    server_config.secure_write_text(
        cfgdir / "teams.json", json.dumps({"teams": teams, "active": active}),
    )


def _nostr(team: str, token: str, exp: float, refresh: str = "vzrt_x",
           npub: str = "npub1me", url: str = "https://srv") -> dict:
    return {
        "id": team, "url": url, "token": token, "auth": "nostr",
        "npub": npub, "expires_at": exp, "refresh_token": refresh,
    }


def test_upload_stale_team_borrows_same_identity_token(
    tmp_path, monkeypatch, _isolated_home,
):
    """Incident 2026-10-02: --team twentyone held an expired JWT while the
    same person's blink entry was fresh; the upload must use the fresh one
    (the JWT identifies the person; team scope is X-Team-Id)."""
    captured: dict = {}
    _stub_uploader(monkeypatch, captured)
    monkeypatch.delenv("VEZIR_URL", raising=False)
    monkeypatch.delenv("VEZIR_TOKEN", raising=False)
    now = time.time()
    _write_teams(_isolated_home, [
        _nostr("blink", "eyJ.fresh", now + 600),
        _nostr("twentyone", "eyJ.stale", now - 600),
        # A different identity's fresher token must never be borrowed.
        _nostr("startups", "eyJ.other", now + 9999, npub="npub1someoneelse"),
    ], active="blink")

    res = CliRunner().invoke(
        main, ["upload", "--team", "twentyone", str(_tiny_ogg(tmp_path))]
    )
    assert res.exit_code == 0, res.output
    assert captured["team_id"] == "twentyone"
    assert captured["token"] == "eyJ.fresh"


def test_upload_passes_refresh_cb_for_session_entries(
    tmp_path, monkeypatch, _isolated_home,
):
    """A 401 mid-upload refreshes the TARGET team's entry (not just active)."""
    from vezir.client import uploader

    monkeypatch.delenv("VEZIR_URL", raising=False)
    monkeypatch.delenv("VEZIR_TOKEN", raising=False)
    now = time.time()
    _write_teams(_isolated_home, [
        _nostr("blink", "eyJ.b", now + 600, refresh="vzrt_b"),
        _nostr("twentyone", "eyJ.t", now + 600, refresh="vzrt_t"),
    ], active="blink")
    monkeypatch.setattr(uploader, "validate_audio_path", lambda p: Path(p))
    monkeypatch.setattr(uploader, "server_supports_resumable", lambda *a, **k: True)

    seen: dict = {}

    def fake_refresh(base_url, verify=None, team=None):
        seen["team"] = team
        return "eyJ.new"

    monkeypatch.setattr("vezir.client.api.refresh_session", fake_refresh)

    def fake_resumable(server_url, token, audio_path, **kw):
        seen["new"] = kw["refresh_cb"]()
        return {"session_id": "01TEST", "bytes": 1}

    monkeypatch.setattr(uploader, "upload_resumable", fake_resumable)
    res = CliRunner().invoke(
        main, ["upload", "--team", "twentyone", str(_tiny_ogg(tmp_path))]
    )
    assert res.exit_code == 0, res.output
    assert seen == {"team": "twentyone", "new": "eyJ.new"}


def test_upload_env_bearer_gets_no_refresh_cb(tmp_path, monkeypatch):
    """An env/--token bearer must never be swapped for a stored identity."""
    captured: dict = {}
    _stub_uploader(monkeypatch, captured)
    from vezir.client import uploader

    def fake_resumable(server_url, token, audio_path, **kw):
        captured["refresh_cb"] = kw.get("refresh_cb")
        return {"session_id": "01TEST", "bytes": 1}

    monkeypatch.setattr(uploader, "upload_resumable", fake_resumable)
    monkeypatch.setenv("VEZIR_URL", "https://srv")
    monkeypatch.setenv("VEZIR_TOKEN", "vzr_tok")
    monkeypatch.setenv("VEZIR_TEAM_ID", "ci")
    res = CliRunner().invoke(main, ["upload", str(_tiny_ogg(tmp_path))])
    assert res.exit_code == 0, res.output
    assert captured["refresh_cb"] is None
