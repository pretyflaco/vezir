from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from vezir.cli import main


def test_team_config_add_reads_token_from_stdin_without_network(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    def no_network(*args, **kwargs):
        raise AssertionError("token stdin must not exchange an enrollment code")

    monkeypatch.setattr("vezir.client.api.exchange_enroll_code", no_network)
    token = "vzr_stdin_secret"
    result = CliRunner().invoke(
        main,
        [
            "team", "config", "add",
            "--id", "blink",
            "--url", "https://example.test",
            "--token-stdin",
        ],
        input=token + "\n",
    )

    assert result.exit_code == 0, result.output
    assert token not in result.output
    from vezir.client.config import load_teams_config

    cfg = load_teams_config()
    assert cfg["active"] == "blink"
    assert cfg["teams"][0]["token"] == token
