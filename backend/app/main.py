"""FastAPI application factory and entrypoint.

Run from the `backend/` directory:

    uvicorn app.main:app --reload
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.deps import Services, build_services
from app.api.routes import health, summarization, transcription
from app.core.config import Settings, get_settings
from app.core.errors import TransmintError

logger = logging.getLogger("transmint")

# Allowance for multipart boundaries/headers on top of the raw file size.
MULTIPART_OVERHEAD_BYTES = 64 * 1024


def create_app(settings: Settings | None = None, services: Services | None = None) -> FastAPI:
    """Build the application. Tests pass `services` with fake AI backends."""
    settings = settings or (services.settings if services else get_settings())
    logging.basicConfig(level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        svc = services or build_services(settings)
        # Jobs from a previous run cannot resume (their in-memory queue is gone).
        stale = svc.transcriptions.fail_unfinished("Server restarted before the job finished. Please re-upload.")
        purged = svc.audio.purge_upload_dir()
        if stale or purged:
            logger.info("Startup cleanup: %d stale jobs marked failed, %d orphaned uploads removed", stale, purged)
        app.state.services = svc
        yield
        svc.jobs.shutdown()

    app = FastAPI(
        title=f"{settings.app_name} API",
        version=settings.app_version,
        description="Transcribe meeting and lecture recordings and generate structured summaries.",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def reject_oversized_uploads(request: Request, call_next):  # noqa: ANN001, ANN202
        """Reject uploads by Content-Length before the multipart body is read."""
        if request.method == "POST" and request.url.path.rstrip("/") == "/api/transcriptions":
            length = request.headers.get("content-length")
            if length and length.isdigit() and int(length) > settings.max_upload_bytes + MULTIPART_OVERHEAD_BYTES:
                return JSONResponse(
                    status_code=413,
                    content={"detail": f"File exceeds the {settings.max_upload_mb} MB upload limit.",
                             "code": "file_too_large"},
                )
        return await call_next(request)

    # Added last so it is the outermost middleware and also decorates early 413 responses.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "Accept"],
        expose_headers=["Content-Disposition", "Location", "Retry-After"],
    )

    @app.exception_handler(TransmintError)
    async def handle_transmint_error(_: Request, exc: TransmintError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code, content={"detail": exc.message, "code": exc.code}, headers=exc.headers
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled error", exc_info=exc)
        return JSONResponse(status_code=500, content={"detail": "Internal server error.", "code": "internal_error"})

    app.include_router(health.router)
    app.include_router(transcription.router)
    app.include_router(summarization.router)
    return app


app = create_app()
