"""Transcription endpoints: upload (async job), status, retrieval and export."""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends, File, Query, Response, UploadFile, status
from fastapi.concurrency import run_in_threadpool

from app.api.deps import Services, get_services, rate_limit
from app.core.errors import ConflictError, DependencyUnavailableError, NotFoundError
from app.schemas.transcription import (
    JobStatus,
    SourceType,
    TranscriptionResponse,
    TranscriptionStatusResponse,
    YouTubeTranscriptionRequest,
)
from app.services.export_service import ExportFormat, render_transcript
from app.utils.file_validation import safe_download_name, sanitize_filename, validate_extension
from app.utils.youtube_url import canonical_watch_url, parse_youtube_video_id

router = APIRouter(prefix="/api/transcriptions", tags=["transcriptions"])


def _get_or_404(services: Services, transcription_id: str) -> TranscriptionResponse:
    record = services.transcriptions.get(transcription_id)
    if record is None:
        raise NotFoundError(f"Transcription '{transcription_id}' not found.")
    return record


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=TranscriptionResponse,
    dependencies=[Depends(rate_limit)],
    responses={413: {"description": "File too large"}, 415: {"description": "Unsupported or invalid audio"},
               503: {"description": "Transcription engine unavailable"}},
)
async def create_transcription(
    response: Response,
    file: UploadFile = File(..., description="Audio file (wav, mp3, m4a, flac, ogg, webm)."),
    services: Services = Depends(get_services),
) -> TranscriptionResponse:
    """Upload audio and start a background transcription job.

    Returns immediately with status `queued`. Poll `GET /api/transcriptions/{id}/status`
    until it is `completed` or `failed`, then fetch the full result.
    """
    ext = validate_extension(file.filename, services.settings.allowed_extension_set)
    services.transcriber.check_available()

    transcription_id = uuid4().hex
    path = await services.audio.save_upload(file, transcription_id, ext)
    try:
        info = await run_in_threadpool(services.audio.probe, path)
        record = services.transcriptions.create(transcription_id, sanitize_filename(file.filename))
    except BaseException:
        services.audio.cleanup(path)
        raise
    services.jobs.submit(transcription_id, path, info)

    response.headers["Location"] = f"/api/transcriptions/{transcription_id}"
    return record


@router.post(
    "/youtube",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=TranscriptionResponse,
    dependencies=[Depends(rate_limit)],
    responses={422: {"description": "Not a valid YouTube video link"},
               503: {"description": "YouTube support or transcription engine unavailable"}},
)
def create_youtube_transcription(
    body: YouTubeTranscriptionRequest, response: Response, services: Services = Depends(get_services)
) -> TranscriptionResponse:
    """Start a background job that transcribes a YouTube video.

    The video's own captions are used when available (fast, no download). Otherwise
    the audio-only stream is downloaded and transcribed with Whisper. Poll the status
    endpoint exactly as for uploads; `transcript_source` tells you which path was used.
    """
    if services.youtube is None:
        raise DependencyUnavailableError("YouTube input is disabled on this server (YOUTUBE_ENABLED=false).")
    services.youtube.check_available()
    video_id = parse_youtube_video_id(body.url)
    prefer_captions = (
        services.settings.youtube_prefer_captions if body.prefer_captions is None else body.prefer_captions
    )
    if not prefer_captions:
        services.transcriber.check_available()

    transcription_id = uuid4().hex
    record = services.transcriptions.create(
        transcription_id,
        f"youtube-{video_id}",
        source_type=SourceType.YOUTUBE,
        source_url=canonical_watch_url(video_id),
    )
    services.jobs.submit_youtube(transcription_id, video_id, prefer_captions)
    response.headers["Location"] = f"/api/transcriptions/{transcription_id}"
    return record


@router.get("/{transcription_id}", response_model=TranscriptionResponse)
def get_transcription(transcription_id: str, services: Services = Depends(get_services)) -> TranscriptionResponse:
    """Return the transcription record, including the transcript once completed."""
    return _get_or_404(services, transcription_id)


@router.get("/{transcription_id}/status", response_model=TranscriptionStatusResponse)
def get_transcription_status(
    transcription_id: str, services: Services = Depends(get_services)
) -> TranscriptionStatusResponse:
    """Lightweight status check for polling."""
    record = _get_or_404(services, transcription_id)
    return TranscriptionStatusResponse(**record.model_dump(include=set(TranscriptionStatusResponse.model_fields)))


@router.get(
    "/{transcription_id}/export",
    response_class=Response,
    responses={200: {"content": {"text/plain": {}, "text/markdown": {}}}},
)
def export_transcription(
    transcription_id: str,
    format: ExportFormat = Query(ExportFormat.TXT, description="txt or md"),  # noqa: A002
    services: Services = Depends(get_services),
) -> Response:
    """Download the transcript as a TXT or Markdown file."""
    record = _get_or_404(services, transcription_id)
    if record.status is not JobStatus.COMPLETED:
        raise ConflictError(f"Transcription is '{record.status.value}', not completed.")
    filename = safe_download_name(f"transmint-transcript-{record.id}", format.value)
    return Response(
        content=render_transcript(record, format),
        media_type=format.media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
