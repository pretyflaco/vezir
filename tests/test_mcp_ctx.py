"""Tests for vezir mcp (harness MCP server) and vezir ctx (v0.15.0)."""
from __future__ import annotations

import json

import pytest


class _FakeResult:
    def __init__(self, ok=None, success=True):
        self.ok = ok
        self._success = success

    def is_ok(self):
        return self._success

    def error_message(self):
        return "boom"


class _FakeSession:
    def __init__(self, sid, title, artifacts=None, status="done",
                 created_at="2026-08-24T10:00:00Z", github="alice"):
        self.id = sid
        self.title = title
        self.status = status
        self.created_at = created_at
        self.github = github
        self.artifacts = artifacts or {}


class _FakeApi:
    """Minimal VezirClient stand-in for the MCP/ctx plumbing."""

    def __init__(self):
        self.sessions = [
            _FakeSession("01AAA", "Brainstorm Phoenix",
                         artifacts={"summary": "01AAA.summary.md",
                                    "txt": "01AAA.txt"}),
            _FakeSession("01BBB", "Weekly Sync", status="needs_labeling"),
        ]

    def get_sessions(self, limit=50, since=None):
        return _FakeResult(ok=self.sessions)

    def get_session(self, sid):
        for s in self.sessions:
            if s.id == sid:
                return _FakeResult(ok=s)
        return _FakeResult(ok=None, success=False)

    def download_artifact(self, sid, name):
        if name.endswith(".summary.md"):
            return _FakeResult(ok=b"# Summary\n\nPhoenix plan.\n")
        if name.endswith(".txt"):
            return _FakeResult(ok=b"[00:00] ALICE: welcome\n")
        return _FakeResult(ok=None)


@pytest.fixture
def fake_client(monkeypatch):
    from vezir.client import mcp_server

    monkeypatch.setattr(mcp_server, "_client", lambda: _FakeApi())
    return _FakeApi()


# ── MCP tools ────────────────────────────────────────────────────────────────


def test_list_sessions_returns_briefs(fake_client):
    from vezir.client import mcp_server

    sessions = mcp_server.list_sessions()
    assert len(sessions) == 2
    assert sessions[0]["id"] == "01AAA"
    assert sessions[0]["title"] == "Brainstorm Phoenix"
    # No heavy payload fields in the brief.
    assert "artifacts" not in sessions[0]


def test_list_sessions_status_filter(fake_client):
    from vezir.client import mcp_server

    sessions = mcp_server.list_sessions(status="needs_labeling")
    assert [s["id"] for s in sessions] == ["01BBB"]


def test_search_sessions_by_title(fake_client):
    from vezir.client import mcp_server

    assert [s["id"] for s in mcp_server.search_sessions("phoenix")] == ["01AAA"]
    assert mcp_server.search_sessions("nonexistent") == []


def test_get_summary_returns_markdown(fake_client):
    from vezir.client import mcp_server

    assert "Phoenix plan." in mcp_server.get_summary("01AAA")


def test_get_transcript_truncates_with_note(fake_client, monkeypatch):
    from vezir.client import mcp_server

    text = mcp_server.get_transcript("01AAA", max_chars=10)
    assert text.startswith("[00:00] A")
    assert "truncated" in text


def test_get_summary_missing_session_errors(fake_client):
    from vezir.client import mcp_server

    with pytest.raises(RuntimeError, match="session 01ZZZ"):
        mcp_server.get_summary("01ZZZ")


def test_get_summary_missing_artifact_errors(fake_client):
    from vezir.client import mcp_server

    with pytest.raises(RuntimeError, match="no 'summary' artifact"):
        mcp_server.get_summary("01BBB")


# ── vezir ctx ────────────────────────────────────────────────────────────────


def _ctx_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("VEZIR_TOKEN", "vzr_" + "x" * 43)
    monkeypatch.setenv("VEZIR_TEAM_ID", "blink")
    monkeypatch.setenv("VEZIR_RECORD_DIR", str(tmp_path / "vezir-meetings"))


def test_ctx_by_title_prints_context(monkeypatch, tmp_path):
    from click.testing import CliRunner

    from vezir import cli

    _ctx_home(monkeypatch, tmp_path)

    api = _FakeApi()
    monkeypatch.setattr(cli, "config", cli.config)  # keep module ref
    monkeypatch.setattr(
        "vezir.client.api.VezirClient", lambda *a, **k: api,
    )
    # Pre-seed the pulled artifacts so no pull happens.
    out = tmp_path / "vezir-meetings" / "blink"
    sess_dir = out / "meeting-20260824-100000_BRAINSTORM_PHOENIX"
    sess_dir.mkdir(parents=True)
    (sess_dir / "session.json").write_text(json.dumps({"session_id": "01AAA"}))
    (sess_dir / "20260824_brainstorm_phoenix.md").write_text("# Sum\n\nCtx body.")
    (sess_dir / "20260824_brainstorm_phoenix.txt").write_text("[00:00] ALICE: hi")

    monkeypatch.setattr(
        "vezir.client.pull.pull_team_sessions", lambda *a, **k: 0,
    )

    runner = CliRunner()
    result = runner.invoke(cli.main, ["ctx", "phoenix"])
    assert result.exit_code == 0, result.output
    assert "# Meeting context: Brainstorm Phoenix" in result.output
    assert "Ctx body." in result.output
    assert "[00:00] ALICE: hi" in result.output


def test_ctx_ambiguous_match_errors(monkeypatch, tmp_path):
    from click.testing import CliRunner

    from vezir import cli

    _ctx_home(monkeypatch, tmp_path)

    api = _FakeApi()
    monkeypatch.setattr(
        "vezir.client.api.VezirClient", lambda *a, **k: api,
    )

    runner = CliRunner()
    result = runner.invoke(cli.main, ["ctx", "01"])
    assert result.exit_code == 1
    assert "matches 2 sessions" in result.stderr


def test_ctx_no_match_errors(monkeypatch, tmp_path):
    from click.testing import CliRunner

    from vezir import cli

    _ctx_home(monkeypatch, tmp_path)

    api = _FakeApi()
    monkeypatch.setattr(
        "vezir.client.api.VezirClient", lambda *a, **k: api,
    )

    runner = CliRunner()
    result = runner.invoke(cli.main, ["ctx", "nope-nothing"])
    assert result.exit_code == 1
    assert "no session matches" in result.stderr


def test_ctx_path_flag_prints_dir(monkeypatch, tmp_path):
    from click.testing import CliRunner

    from vezir import cli

    _ctx_home(monkeypatch, tmp_path)
    monkeypatch.setenv("VEZIR_RECORD_DIR", str(tmp_path / "vezir-meetings"))

    api = _FakeApi()
    monkeypatch.setattr(
        "vezir.client.api.VezirClient", lambda *a, **k: api,
    )
    sess_dir = (
        tmp_path / "vezir-meetings" / "blink"
        / "meeting-20260824-100000_BRAINSTORM_PHOENIX"
    )
    sess_dir.mkdir(parents=True)
    (sess_dir / "session.json").write_text(json.dumps({"session_id": "01AAA"}))
    (sess_dir / "20260824_brainstorm_phoenix.md").write_text("# Sum")
    monkeypatch.setattr(
        "vezir.client.pull.pull_team_sessions", lambda *a, **k: 0,
    )

    runner = CliRunner()
    result = runner.invoke(cli.main, ["ctx", "01AAA", "--path"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == str(sess_dir)
