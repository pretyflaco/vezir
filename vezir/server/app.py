"""FastAPI application factory.

Composes all routers and starts the background worker.

v0.7.0: HTML dashboard, login flow, and static assets removed.
All interaction is via JSON API (TUI, Android, CLI).
"""
from __future__ import annotations

import logging

from fastapi import FastAPI

from .. import __version__, config
from . import (
    labels,
    migrations,
    queue,
    sessions,
    teams,
    uploads,
    voiceprints,
    worker,
)


def create_app() -> FastAPI:
    config.harden_umask()
    config.configure_logging()
    log = logging.getLogger("vezir")

    config.ensure_dirs()
    migrations.run_pending_migrations()
    for _t in queue.list_teams():
        voiceprints.ensure_db_exists(_t["id"])

    app = FastAPI(
        title="vezir",
        description="Internal scribe service wrapping millet.",
        version=__version__,
    )

    @app.get("/health")
    def health():
        return {
            "status": "ok",
            "version": __version__,
            "data_dir": str(config.data_dir()),
        }

    app.include_router(uploads.router)
    app.include_router(sessions.router)
    app.include_router(labels.router)
    app.include_router(teams.router)

    @app.on_event("startup")
    def _startup():
        log.info("vezir %s starting up", __version__)
        log.info("data dir: %s", config.data_dir())
        # Sweep any resumable-upload staging left over from a crash/restart.
        try:
            uploads.sweep_abandoned_uploads()
        except Exception:
            log.exception("startup resumable-upload sweep failed (non-fatal)")
        worker.start_background_worker()

    @app.on_event("shutdown")
    def _shutdown():
        log.info("vezir shutting down")
        worker.stop_background_worker()

    return app


app = create_app()
