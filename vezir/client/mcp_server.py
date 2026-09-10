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
    meeting's content as context.  ``limit`` may go up to 500 (the
    server's clamp); raise it to reach older sessions.
    """
    api = _client()
    result = api.get_sessions(limit=min(max(limit, 1), 500))
    if not result.is_ok():
        raise RuntimeError(f"could not list sessions: {result.error_message()}")
    sessions = result.ok
    if status:
        sessions = [s for s in sessions if s.status == status]
    return _session_briefs(sessions, limit)


def search_sessions(query: str, limit: int = 20) -> list[dict]:
    """Search vezir sessions by title substring (case-insensitive).

    Searches up to 500 recent sessions (the server's clamp).
    """
    api = _client()
    result = api.get_sessions(limit=500)
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


def list_artifacts(session_id: str) -> dict:
    """List every downloadable file for a session.

    Returns ``{"artifacts": {<type>: <filename>}, "attachments": [...]}``:
    the artifact dict (transcript/summary/pdf/iteration_plan/… keyed by
    type) plus the attachments list (cue frames ``cue_HH-MM-SS.png`` and
    any user-uploaded files).  Use a filename (or an artifact type key)
    with get_artifact to pull the content.  The source video of a video
    session (``.mp4``/``.mov``) sits at the session root — it is not in
    either list but get_artifact can fetch it by name.
    """
    api = _client()
    result = api.get_session(session_id)
    if not result.is_ok():
        raise RuntimeError(f"session {session_id}: {result.error_message()}")
    session = result.ok
    att = api.list_attachments(session_id)
    # A server too old for the attachments route 404s — treat as none.
    attachments = att.ok if att.is_ok() else []
    return {
        "session_id": session_id,
        "artifacts": dict(session.artifacts),
        "attachments": attachments,
    }


def get_artifact(session_id: str, name: str, save_path: str | None = None) -> str:
    """Download one file of a session by name (or artifact type key).

    ``name`` may be an artifact type (``txt``, ``summary``,
    ``iteration_plan``, ``json``, ``srt``, ``pdf``), an exact filename
    from list_artifacts (e.g. ``cue_00-03-12.png``), or a session-root
    file (the source video).  Attachments (frames, user files) are
    resolved automatically.

    Text content is returned decoded.  Binary content (png, pdf, mp4)
    requires ``save_path``: the file is written there and the path is
    returned — pass the path to an image/PDF-capable tool next.
    """
    from pathlib import Path

    api = _client()
    result = api.get_session(session_id)
    if not result.is_ok():
        raise RuntimeError(f"session {session_id}: {result.error_message()}")
    session = result.ok

    # Resolve an artifact type key to its filename.
    filename = session.artifacts.get(name, name)

    # Attachments live on a different route; prefer it when the name is
    # listed there (frames, user-uploaded files).
    att = api.list_attachments(session_id)
    att_names = {a.get("name") for a in att.ok} if att.is_ok() else set()
    if filename in att_names:
        data = api.download_attachment(session_id, filename)
    else:
        data = api.download_artifact(session_id, filename)
    if not data.is_ok():
        raise RuntimeError(f"download failed: {data.error_message()}")
    raw: bytes = data.ok

    if save_path:
        dest = Path(save_path).expanduser()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(raw)
        return str(dest)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise RuntimeError(
            f"'{filename}' is binary ({len(raw)} bytes) — call get_artifact "
            "again with save_path set to a local file path"
        ) from None


def get_transcript(session_id: str, max_chars: int = 0) -> str:
    """Return the complete diarized transcript (plain text) for a session.

    Full by default (v0.15.1): when you ask for a session's transcript you
    want all of it — no silent truncation.  Pass ``max_chars`` only when
    you explicitly want just a snippet (e.g. a preview for a very long
    meeting); a truncation note is appended in that case.
    """
    text = _fetch_artifact_text(session_id, "txt", ".txt")
    cap = max_chars  # 0 / negative = no cap
    if cap > 0 and len(text) > cap:
        text = (
            text[:cap]
            + f"\n\n[… truncated: showing {cap} of {len(text)} chars; "
            "call get_transcript without max_chars (or a larger value) "
            "for the full transcript]"
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
    server.tool()(list_artifacts)
    server.tool()(get_artifact)
    server.run()
