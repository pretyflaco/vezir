"""Session-level glue for `vezir clips`: fetch the cut list, find the source.

The cut list comes from the server (the MCP ``get_clips`` tool, words
included) or a local ``clips.json``.  The source video is a local file when
given, otherwise it is streamed once from the server into the output
directory and reused by later renders.
"""
from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ... import config


class ClipsError(RuntimeError):
    pass


@contextmanager
def _team(team: str | None) -> Iterator[None]:
    """Scope ``VEZIR_TEAM_ID`` to the server calls of this command."""
    if not team:
        yield
        return
    old = os.environ.get("VEZIR_TEAM_ID")
    os.environ["VEZIR_TEAM_ID"] = team
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("VEZIR_TEAM_ID", None)
        else:
            os.environ["VEZIR_TEAM_ID"] = old


def load_doc(session_id: str | None, clips_file: str | Path | None,
             team: str | None = None) -> dict[str, Any]:
    if clips_file:
        try:
            doc = json.loads(Path(clips_file).expanduser().read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ClipsError(f"cannot read {clips_file}: {exc}") from None
    elif session_id:
        from ..mcp_server import get_clips

        with _team(team):
            doc = get_clips(session_id, include_words=True)
    else:
        raise ClipsError("give a SESSION_ID or --clips-file")
    if not isinstance(doc, dict) or not isinstance(doc.get("clips"), list):
        raise ClipsError("not a clips document (no 'clips' list)")
    return doc


def select(doc: dict[str, Any], ids: str | None) -> list[dict[str, Any]]:
    clips = [c for c in doc["clips"] if isinstance(c, dict) and c.get("id")]
    if not ids:
        return clips
    wanted = [x.strip() for x in ids.split(",") if x.strip()]
    by_id = {c["id"]: c for c in clips}
    missing = [x for x in wanted if x not in by_id]
    if missing:
        raise ClipsError(f"unknown clip id(s) {missing}; available: {sorted(by_id)}")
    return [by_id[x] for x in wanted]


def ensure_source(doc: dict[str, Any], session_id: str | None, source: str | Path | None,
                  out_dir: Path, team: str | None = None) -> Path:
    """The source media: ``source`` if given, else downloaded once from the server."""
    if source:
        path = Path(source).expanduser()
        if not path.is_file():
            raise ClipsError(f"source not found: {path}")
        return path
    media = (doc.get("source") or {}).get("media")
    if not session_id:
        raise ClipsError("--source is required with --clips-file")
    # Server-supplied name -> local path: refuse anything but one plain name.
    name = config.safe_path_component(media)
    if name is None or name.startswith("."):
        raise ClipsError(f"server named an unusable source media {media!r}; pass --source")
    dest = out_dir / "_source" / name
    if not dest.is_file():
        from ..mcp_server import get_artifact

        with _team(team):
            get_artifact(session_id, name, save_path=str(dest))
    return dest


def summary_table(doc: dict[str, Any]) -> list[str]:
    rows = []
    for c in doc["clips"]:
        if not isinstance(c, dict):
            continue
        score = c.get("score")
        rows.append(
            f"{c.get('id', '?'):9} {c.get('start_ts', '?')} -> {c.get('end_ts', '?')} "
            f"{float(c.get('duration_s') or 0):6.1f}s  "
            f"{(str(score) + '/10') if score is not None else '  - '}  {c.get('title') or ''}"
        )
    for d in doc.get("dropped") or []:
        if isinstance(d, dict):
            rows.append(f"dropped: {d.get('title') or '(untitled)'}: {d.get('reason')}")
    if doc.get("error"):
        rows.append(f"error: {doc['error']}")
    return rows
