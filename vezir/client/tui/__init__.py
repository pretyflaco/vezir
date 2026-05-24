"""Vezir Textual TUI -- desktop thin client.

Entry point: ``vezir tui`` (see vezir/cli.py).

Screens:

  RecordScreen   record audio, pause/resume, optional personal flag,
                 upload + status badge
  SessionsScreen DataTable of own + team-visible sessions
  DetailScreen   session metadata, artifact list, retry-summary, share
  ArtifactScreen text artifacts inline; PDF/binary handed to OS opener
  LabelScreen    speaker labeling with autocomplete + ffplay clips

All screens consume vezir.client.api.VezirClient, which is the shared
HTTP layer (mirrors vezir-android's net/SessionApi.kt etc).  No business
logic lives in the screens -- they call into api.py for everything and
spawn worker threads via Textual's @work for blocking I/O.

The TUI imports heavyweight deps (meet_record, textual, etc.) lazily
so that ``vezir --help`` and ``vezir token list`` stay snappy on boxes
that don't have millet-record installed.
"""
from __future__ import annotations

# Public re-exports are limited on purpose -- callers should construct
# the App through ``launch_tui()`` so we control the import ordering.

__all__ = ["launch_tui"]


def launch_tui(*, serve: bool = False, host: str = "127.0.0.1", port: int = 8800) -> int:
    """Run the Textual app.  Lazy-imports textual + screens.

    ``serve=True`` publishes the TUI over HTTPS via ``textual serve``
    so it can be opened in a browser (drop-in for the web dashboard
    once the v0.5 deprecation lands).
    """
    from .app import VezirTuiApp

    if serve:
        # textual serve is provided as a CLI helper; from within Python
        # we wire the equivalent path via textual.serve when present in
        # this version of textual.  Fall back to a friendly error.
        try:
            from textual_serve.server import Server  # type: ignore
        except ImportError:
            print(
                "vezir: textual-serve is not installed; install it "
                "with `pip install textual-serve` (Python 3.11+).",
            )
            return 1
        server = Server(command="vezir tui", host=host, port=port)
        server.serve()
        return 0

    app = VezirTuiApp()
    app.run()
    return 0
