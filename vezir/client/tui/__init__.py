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

from pathlib import Path

# Public re-exports are limited on purpose -- callers should construct
# the App through ``launch_tui()`` so we control the import ordering.

__all__ = ["launch_tui"]


def _install_crash_log() -> Path | None:
    """Log vezir records to a persistent file while the TUI runs (0.23.0).

    The TUI otherwise logs to stderr, which dies with the terminal — the
    2026-09-28 crash left no traceback anywhere, making the root cause
    unrecoverable.  A rotating file under the XDG state dir fixes that.
    Returns the log path (for the crash message), or None on failure.
    """
    import logging
    import os
    from logging.handlers import RotatingFileHandler

    try:
        state_home = Path(
            os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")
        )
        log_dir = state_home / "vezir"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "tui.log"
        handler = RotatingFileHandler(
            log_path, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        vezir_log = logging.getLogger("vezir")
        vezir_log.addHandler(handler)
        if vezir_log.level > logging.INFO or vezir_log.level == logging.NOTSET:
            vezir_log.setLevel(logging.INFO)
        return log_path
    except OSError:
        return None


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

    import logging

    log_path = _install_crash_log()
    app = VezirTuiApp()
    try:
        app.run()
    except BaseException:
        # Persist the traceback before the terminal state is lost — the
        # recording survives a TUI crash by design, but without a log the
        # crash itself is undebuggable.
        logging.getLogger("vezir.client.tui").exception(
            "TUI crashed (unhandled exception escaped app.run)"
        )
        if log_path is not None:
            print(f"vezir: crash traceback written to {log_path}")
        raise
    return 0
