"""Tests for vezir mcp (harness MCP server) and vezir ctx (v0.15.0)."""
from __future__ import annotations

import json
from pathlib import Path

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
                                    "txt": "01AAA.txt",
                                    "iteration_plan": "01AAA.iteration-plan.md"}),
            _FakeSession("01BBB", "Weekly Sync", status="needs_labeling"),
        ]
        self.attachments = {
            "01AAA": [
                {"name": "cue_00-01-05.png", "size": 12345,
                 "content_type": "image/png"},
            ],
        }

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
        if name.endswith(".iteration-plan.md"):
            return _FakeResult(ok=b"# Plan\n\n- [00:01:05] high: fix it\n")
        if name.endswith(".txt"):
            return _FakeResult(ok=b"[00:00] ALICE: welcome\n")
        if name.endswith(".mp4"):
            return _FakeResult(ok=b"\x00\x00\x00\x18ftypmp42VIDEOBYTES")
        return _FakeResult(ok=None)

    def list_attachments(self, sid):
        return _FakeResult(ok=self.attachments.get(sid, []))

    def download_attachment(self, sid, name):
        if name == "cue_00-01-05.png":
            return _FakeResult(ok=b"\x89PNG\r\n\x1a\nFAKEFRAME")
        return _FakeResult(ok=None, success=False)


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


def test_get_transcript_full_by_default(fake_client):
    """v0.15.1: no truncation unless max_chars is explicitly passed."""
    from vezir.client import mcp_server

    text = mcp_server.get_transcript("01AAA")
    assert text == "[00:00] ALICE: welcome\n"
    assert "truncated" not in text


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


# ── MCP artifact tools (v0.18.0) ─────────────────────────────────────────────


def test_list_artifacts_returns_artifacts_and_attachments(fake_client):
    from vezir.client import mcp_server

    out = mcp_server.list_artifacts("01AAA")
    assert out["artifacts"]["summary"] == "01AAA.summary.md"
    assert out["artifacts"]["iteration_plan"] == "01AAA.iteration-plan.md"
    assert out["attachments"][0]["name"] == "cue_00-01-05.png"


def test_list_artifacts_empty_attachments(fake_client):
    from vezir.client import mcp_server

    out = mcp_server.list_artifacts("01BBB")
    assert out["artifacts"] == {}
    assert out["attachments"] == []


def test_get_artifact_by_type_key(fake_client):
    from vezir.client import mcp_server

    assert "fix it" in mcp_server.get_artifact("01AAA", "iteration_plan")


def test_get_artifact_text_by_filename(fake_client):
    from vezir.client import mcp_server

    assert "Phoenix plan." in mcp_server.get_artifact("01AAA", "01AAA.summary.md")


def test_get_artifact_attachment_binary_requires_save_path(fake_client):
    from vezir.client import mcp_server

    with pytest.raises(RuntimeError, match="binary"):
        mcp_server.get_artifact("01AAA", "cue_00-01-05.png")


def test_get_artifact_binary_with_save_path(fake_client, tmp_path):
    from vezir.client import mcp_server

    dest = tmp_path / "frame.png"
    out = mcp_server.get_artifact("01AAA", "cue_00-01-05.png", save_path=str(dest))
    assert out == str(dest)
    assert dest.read_bytes() == b"\x89PNG\r\n\x1a\nFAKEFRAME"


def test_get_artifact_source_video_by_name(fake_client, tmp_path):
    from vezir.client import mcp_server

    dest = tmp_path / "demo.mp4"
    out = mcp_server.get_artifact("01AAA", "01AAA.mp4", save_path=str(dest))
    assert out == str(dest)
    assert dest.read_bytes().startswith(b"\x00\x00\x00\x18ftyp")


def test_get_artifact_missing_session_errors(fake_client):
    from vezir.client import mcp_server

    with pytest.raises(RuntimeError, match="session 01ZZZ"):
        mcp_server.get_artifact("01ZZZ", "summary")


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


# ── server wiring: concurrency + cancellation (v0.23.1) ──────────────────────
#
# FastMCP ran the blocking ``def`` tools inline on its event loop: concurrent
# calls serialized, and aborting a burst could kill the server (a cancel
# landing while a finished call's response was being sent → CancelledError
# escapes the handler → harness sees "Connection closed").  Tools are now
# offloaded to worker threads.  Driven end to end through the real SDK.


def _server_with(*tools):
    from mcp.server.fastmcp import FastMCP

    from vezir.client.mcp_server import offloaded

    server = FastMCP("test")
    holder: dict = {}
    for fn in tools:
        server.tool()(offloaded(fn, holder))
    return server


_STDIO_SERVER = """
import sys, time
from mcp.server.fastmcp import FastMCP
from vezir.client.mcp_server import offloaded

def slow(x: int) -> str:
    time.sleep(1.5)
    return "late"

def fast(x: int) -> str:
    return f"ok {x}"

server = FastMCP("test")
holder = {}
for fn in (slow, fast):
    server.tool()(offloaded(fn, holder) if sys.argv[1] == "offloaded" else fn)
server.run()
"""


async def _cancel_then_call(tmp_path, mode: str) -> str:
    """Real stdio subprocess (a separate process, like opencode's): cancel a
    burst of in-flight blocking calls, then call again on the same connection."""
    import asyncio
    import os
    import sys

    from mcp import ClientSession, StdioServerParameters, types
    from mcp.client.stdio import stdio_client

    import vezir

    script = tmp_path / "server.py"
    script.write_text(_STDIO_SERVER)
    repo = str(Path(vezir.__file__).resolve().parents[1])
    params = StdioServerParameters(
        command=sys.executable, args=[str(script), mode],
        env={**os.environ, "PYTHONPATH": repo},
    )
    with open(tmp_path / "stderr.log", "w") as errlog:
        async with stdio_client(params, errlog=errlog) as (r, w):
            async with ClientSession(r, w) as client:
                await client.initialize()
                # A burst, as a harness fans out, then an abort.  Inline, the
                # loop only runs between blocking calls — while a finished
                # call's response is being sent — so the cancel for it lands
                # mid-send and the escaping CancelledError kills the session
                # (~1 in 3 runs; offloaded: never, see _STDIO_SERVER modes).
                first = client._request_id
                pending = [
                    asyncio.create_task(client.call_tool("slow", {"x": i}))
                    for i in range(4)
                ]
                await asyncio.sleep(0.5)
                for rid in range(first, first + len(pending)):
                    await client.send_notification(types.ClientNotification(
                        types.CancelledNotification(
                            method="notifications/cancelled",
                            params=types.CancelledNotificationParams(requestId=rid),
                        )
                    ))
                await asyncio.sleep(2.0)  # the blocking calls return meanwhile
                for t in pending:
                    t.cancel()
                try:
                    res = await asyncio.wait_for(client.call_tool("fast", {"x": 2}), 15)
                except Exception as exc:  # McpError("Connection closed")
                    return f"dead: {exc}"
                return res.content[0].text


async def test_mcp_cancelled_call_does_not_kill_server(tmp_path):
    pytest.importorskip("mcp")
    assert await _cancel_then_call(tmp_path, "offloaded") == "ok 2"


async def test_mcp_calls_run_concurrently():
    pytest.importorskip("mcp")
    import asyncio
    import threading

    from mcp.shared.memory import create_connected_server_and_client_session

    # Both calls must be inside the tool at once to pass the barrier; inline
    # (serialized) execution would break it after the timeout.
    barrier = threading.Barrier(2, timeout=5)

    def meet(x: int) -> str:
        barrier.wait()
        return f"met {x}"

    async with create_connected_server_and_client_session(
        _server_with(meet)
    ) as client:
        results = await asyncio.gather(
            client.call_tool("meet", {"x": 1}), client.call_tool("meet", {"x": 2}),
        )
    assert [r.content[0].text for r in results] == ["met 1", "met 2"]


async def test_mcp_build_server_keeps_tool_contract():
    pytest.importorskip("mcp")
    from mcp.shared.memory import create_connected_server_and_client_session

    from vezir.client.mcp_server import build_server

    async with create_connected_server_and_client_session(build_server()) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == {
        "list_sessions", "search_sessions", "get_summary", "get_transcript",
        "list_artifacts", "get_artifact",
    }
    # Offloading must not hide the real signature or docs from the harness.
    assert set(tools["get_artifact"].inputSchema["properties"]) == {
        "session_id", "name", "save_path",
    }
    assert tools["get_artifact"].inputSchema["required"] == ["session_id", "name"]
    assert "Download one file" in (tools["get_artifact"].description or "")
