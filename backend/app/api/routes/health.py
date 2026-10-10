"""Health check endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.deps import Services, get_services
from app.core.errors import DependencyUnavailableError

router = APIRouter(prefix="/api/health", tags=["health"])


class HealthResponse(BaseModel):
    status: str
    app: str
    version: str
    transcription_available: bool
    youtube_available: bool
    summarization_configured: bool


@router.get("", response_model=HealthResponse)
def health(services: Services = Depends(get_services)) -> HealthResponse:
    """Report that the API is running. Exposes capability flags only, never configuration values."""

    def available(check) -> bool:  # noqa: ANN001
        try:
            check()
            return True
        except DependencyUnavailableError:
            return False

    return HealthResponse(
        status="ok",
        app=services.settings.app_name,
        version=services.settings.app_version,
        transcription_available=available(services.transcriber.check_available),
        youtube_available=services.youtube is not None and available(services.youtube.check_available),
        summarization_configured=available(services.summarizer.check_available),
    )
