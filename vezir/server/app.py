"""FastAPI application factory.

Composes all routers and starts the background worker.

v0.7.0: HTML dashboard, login flow, and static assets removed.
All interaction is via JSON API (TUI, Android, CLI).
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Response

from .. import __version__, config
from . import (
    google_auth,
    labels,
    migrations,
    nostr_auth,
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

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # ── startup ──
        log.info("vezir %s starting up", __version__)
        log.info("data dir: %s", config.data_dir())
        # Sweep any resumable-upload staging left over from a crash/restart.
        try:
            uploads.sweep_abandoned_uploads()
        except Exception:
            log.exception("startup resumable-upload sweep failed (non-fatal)")
        worker.start_background_worker()
        yield
        # ── shutdown ──
        log.info("vezir shutting down")
        worker.stop_background_worker()

    app = FastAPI(
        title="vezir",
        description="Internal scribe service wrapping millet.",
        version=__version__,
        lifespan=lifespan,
    )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {
            "status": "ok",
            "version": __version__,
            "data_dir": str(config.data_dir()),
        }

    @app.get("/ca.crt")
    def ca_cert() -> Response:
        """Serve the internal Caddy CA certificate (v0.7.7).

        Unauthenticated by design: this is the PUBLIC CA cert (only the
        private key is sensitive, and that never leaves the server).
        Lets onboarding teammates fetch it over the tunnel with a plain
        ``curl -k https://<server>/ca.crt`` instead of needing an SSH
        login on the server box.  Served from the same path the QR
        enrollment payload embeds (``VEZIR_CADDY_ROOT_CERT_PATH``).
        """
        from .enroll import _load_caddy_root_cert
        pem = _load_caddy_root_cert()
        if not pem:
            raise HTTPException(
                status_code=404,
                detail="CA certificate not configured on this server.",
            )
        return Response(
            content=pem,
            media_type="application/x-pem-file",
            headers={
                "Content-Disposition": 'attachment; filename="vezir-ca.crt"',
            },
        )

    app.include_router(uploads.router)
    app.include_router(sessions.router)
    app.include_router(labels.router)
    app.include_router(teams.router)
    app.include_router(nostr_auth.router)
    app.include_router(google_auth.router)

    return app


app = create_app()
