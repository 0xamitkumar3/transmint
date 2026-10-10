"""Summary endpoints: create, retrieve and export."""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends, Query, Response, status

from app.api.deps import Services, get_services, rate_limit
from app.core.errors import ConflictError, NotFoundError
from app.schemas.summary import SummaryRequest, SummaryResponse
from app.schemas.transcription import JobStatus
from app.services.export_service import ExportFormat, render_summary
from app.utils.file_validation import safe_download_name

router = APIRouter(prefix="/api/summaries", tags=["summaries"])


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=SummaryResponse,
    dependencies=[Depends(rate_limit)],
    responses={404: {"description": "Transcription not found"}, 409: {"description": "Transcription not completed"},
               429: {"description": "Rate limited"}, 502: {"description": "LLM failure"},
               503: {"description": "Groq not configured"}, 504: {"description": "LLM timeout"}},
)
def create_summary(
    body: SummaryRequest, response: Response, services: Services = Depends(get_services)
) -> SummaryResponse:
    """Summarize a stored transcription (`transcription_id`) or raw `text` with Groq.

    Runs synchronously (in FastAPI's threadpool); long transcripts are chunked and
    summarized hierarchically.
    """
    if body.transcription_id is not None:
        record = services.transcriptions.get(body.transcription_id)
        if record is None:
            raise NotFoundError(f"Transcription '{body.transcription_id}' not found.")
        if record.status is not JobStatus.COMPLETED:
            raise ConflictError(f"Transcription is '{record.status.value}'; wait until it is completed.")
        text = record.text
    else:
        text = body.text

    outcome = services.summarizer.summarize(text or "", body.summary_type, body.output_language)
    summary = services.summaries.create(
        uuid4().hex,
        transcription_id=body.transcription_id,
        summary_type=body.summary_type,
        output_language=body.output_language,
        model=outcome.model,
        chunk_count=outcome.chunk_count,
        content=outcome.content,
    )
    response.headers["Location"] = f"/api/summaries/{summary.id}"
    return summary


def _get_or_404(services: Services, summary_id: str) -> SummaryResponse:
    summary = services.summaries.get(summary_id)
    if summary is None:
        raise NotFoundError(f"Summary '{summary_id}' not found.")
    return summary


@router.get("/{summary_id}", response_model=SummaryResponse)
def get_summary(summary_id: str, services: Services = Depends(get_services)) -> SummaryResponse:
    return _get_or_404(services, summary_id)


@router.get(
    "/{summary_id}/export",
    response_class=Response,
    responses={200: {"content": {"text/plain": {}, "text/markdown": {}}}},
)
def export_summary(
    summary_id: str,
    format: ExportFormat = Query(ExportFormat.MD, description="txt or md"),  # noqa: A002
    services: Services = Depends(get_services),
) -> Response:
    """Download the summary as a TXT or Markdown file."""
    summary = _get_or_404(services, summary_id)
    filename = safe_download_name(f"transmint-summary-{summary.id}", format.value)
    return Response(
        content=render_summary(summary, format),
        media_type=format.media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
