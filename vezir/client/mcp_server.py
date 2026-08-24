"""``vezir mcp`` — MCP server exposing vezir sessions to AI harnesses.

A read-only Model Context Protocol server (stdio) that lets opencode,
Claude Code, and other MCP-capable harnesses pull meeting context —
session lists, summaries, transcripts — straight from the vezir server,
without manually pulling artifacts and pasting paths.

Wire it into opencode via ``~/.config/opencode/opencode.json``:

    "mcp": {"vezir": {"type": "local", "command": ["vezir", "mcp"]}}

Credentials come from the regular client config (``teams.json`` /
``VEZIR_URL`` / ``VEZIR_TOKEN`` / ``VEZIR_TEAM_ID``) — the same sources
``vezir pull`` uses.  No server-side changes; all traffic goes through
the existing session/artifact HTTP API.
"""
from __future__ import annotations

import logging

log = logging.getLogger("vezir.client.mcp")

_DEFAULT_MAX_TRANSCRIPT_CHARS = 24_000


def _client():
    """Build a VezirClient from the ambient client config, like `vezir pull`."""
    from .. import config
    from .api import VezirClient

    server_url = config.server_url()
    token = config.client_token()
    team_id = config.client_team_id()
    if not token:
        raise RuntimeError(
            "no vezir credentials configured — set VEZIR_TOKEN or run "
            "`vezir team config add` / `vezir login` first"
        )
    return VezirClient(server_url, token, team_id=team_id)


def _session_briefs(sessions, limit: int) -> list[dict]:
    return [
        {
            "id": s.id,
            "title": s.title,
            "status": s.status,
            "created_at": s.created_at,
            "github": s.github,
        }
        for s in sessions[:limit]
    ]


def list_sessions(limit: int = 20, status: str | None = None) -> list[dict]:
    """List recent vezir sessions (team meetings) with id/title/status/date.

    Use the session ``id`` with get_summary / get_transcript to pull the
    meeting's content as context.
    """
    api = _client()
    result = api.get_sessions(limit=min(max(limit, 1), 200))
    if not result.is_ok():
        raise RuntimeError(f"could not list sessions: {result.error_message()}")
    sessions = result.ok
    if status:
        sessions = [s for s in sessions if s.status == status]
    return _session_briefs(sessions, limit)


def search_sessions(query: str, limit: int = 20) -> list[dict]:
    """Search vezir sessions by title substring (case-insensitive)."""
    api = _client()
    result = api.get_sessions(limit=200)
    if not result.is_ok():
        raise RuntimeError(f"could not list sessions: {result.error_message()}")
    q = (query or "").strip().lower()
    sessions = [
        s for s in result.ok
        if q in (s.title or "").lower()
    ]
    return _session_briefs(sessions, limit)


def _fetch_artifact_text(session_id: str, key: str, suffix: str) -> str:
    """Download one artifact by dict key (falling back to suffix match)."""
    api = _client()
    result = api.get_session(session_id)
    if not result.is_ok():
        raise RuntimeError(f"session {session_id}: {result.error_message()}")
    session = result.ok
    name = session.artifacts.get(key)
    if name is None:
        name = next(
            (n for n in session.artifacts.values() if n.endswith(suffix)), None,
        )
    if name is None:
        raise RuntimeError(
            f"session {session_id} has no '{key}' artifact "
            f"(available: {sorted(session.artifacts) or 'none'})"
        )
    data = api.download_artifact(session_id, name)
    if not data.is_ok():
        raise RuntimeError(f"download failed: {data.error_message()}")
    return data.ok.decode("utf-8", errors="replace")


def get_summary(session_id: str) -> str:
    """Return the AI summary (markdown) for a vezir session."""
    return _fetch_artifact_text(session_id, "summary", ".summary.md")


def get_transcript(session_id: str, max_chars: int = 0) -> str:
    """Return the diarized transcript (plain text) for a vezir session.

    Transcripts are truncated to ``max_chars`` (default 24k) with a note
    appended, so huge meetings don't blow up the context window.
    """
    text = _fetch_artifact_text(session_id, "txt", ".txt")
    cap = max_chars if max_chars > 0 else _DEFAULT_MAX_TRANSCRIPT_CHARS
    if len(text) > cap:
        text = (
            text[:cap]
            + f"\n\n[… truncated: showing {cap} of {len(text)} chars; "
            "call get_transcript with a larger max_chars for more]"
        )
    return text


def serve() -> None:
    """Run the stdio MCP server (blocks).  Requires ``vezir[mcp]``."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - install-time guard
        raise SystemExit(
            "vezir mcp requires the 'mcp' extra:  pip install 'vezir[mcp]'"
        ) from exc

    server = FastMCP("vezir")
    server.tool()(list_sessions)
    server.tool()(search_sessions)
    server.tool()(get_summary)
    server.tool()(get_transcript)
    server.run()
