"""FastAPI application factory.

Composes all routers, mounts static files, starts the background worker.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .. import __version__, config
from . import enroll, labels, login, queue as _queue, sessions, uploads, voiceprints, worker


def create_app() -> FastAPI:
    config.harden_umask()
    logging.basicConfig(
        level=getattr(logging, config.log_level(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("vezir")

    config.ensure_dirs()
    voiceprints.ensure_db_exists()

    app = FastAPI(
        title="vezir",
        description="Internal scribe service wrapping meetscribe.",
        version=__version__,
    )

    @app.get("/health")
    def health():
        return {
            "status": "ok",
            "version": __version__,
            "data_dir": str(config.data_dir()),
        }

    app.include_router(login.router)
    app.include_router(uploads.router)
    app.include_router(sessions.router)
    app.include_router(labels.router)
    app.include_router(enroll.router)

    static_dir = Path(__file__).resolve().parent.parent / "web" / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.on_event("startup")
    def _startup():
        log.info("vezir %s starting up", __version__)
        log.info("data dir: %s", config.data_dir())
        worker.start_background_worker()

    @app.on_event("shutdown")
    def _shutdown():
        log.info("vezir shutting down")
        worker.stop_background_worker()

    return app


app = create_app()
