"""Service container and FastAPI dependencies.

Services are created once at startup and stored on `app.state.services`. Tests can
build a container with fake transcribers / LLM clients and inject it the same way.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request

from app.core.config import Settings
from app.core.rate_limit import RateLimiter
from app.db.database import Database
from app.db.repositories import SummaryRepository, TranscriptionRepository
from app.services.audio_service import AudioService
from app.services.summarization_service import GroqClient, LLMClient, SummarizationService
from app.services.transcription_service import Transcriber, TranscriptionJobRunner, WhisperTranscriber
from app.services.youtube_service import YouTubeService


@dataclass
class Services:
    settings: Settings
    database: Database
    transcriptions: TranscriptionRepository
    summaries: SummaryRepository
    audio: AudioService
    transcriber: Transcriber
    youtube: YouTubeService | None
    jobs: TranscriptionJobRunner
    summarizer: SummarizationService
    rate_limiter: RateLimiter


def build_services(
    settings: Settings,
    *,
    transcriber: Transcriber | None = None,
    llm: LLMClient | None = None,
    youtube: YouTubeService | None = None,
    job_runner_kwargs: dict | None = None,
) -> Services:
    database = Database(settings.database_path)
    database.initialize()
    transcriptions = TranscriptionRepository(database)
    audio = AudioService(settings.upload_dir, settings.max_upload_bytes)
    audio.ensure_upload_dir()
    transcriber = transcriber or WhisperTranscriber(
        settings.whisper_model_size,
        device=settings.whisper_device,
        compute_type=settings.whisper_compute_type,
        language=settings.whisper_language,
        beam_size=settings.whisper_beam_size,
        vad_filter=settings.whisper_vad_filter,
    )
    if youtube is None and settings.youtube_enabled:
        youtube = YouTubeService(
            caption_languages=settings.youtube_caption_language_list,
            max_duration_seconds=settings.youtube_max_duration_minutes * 60,
            max_download_bytes=settings.youtube_max_download_mb * 1024 * 1024,
            download_timeout_seconds=settings.youtube_download_timeout_seconds,
            js_runtimes=settings.ytdlp_js_runtime_list,
            cookies_file=settings.ytdlp_cookies_file,
            supadata_api_key=settings.supadata_api_key,
            supadata_mode=settings.supadata_mode,
        )
    llm = llm or GroqClient(
        settings.groq_api_key,
        settings.groq_model,
        timeout=settings.groq_timeout_seconds,
        max_retries=settings.groq_max_retries,
        temperature=settings.groq_temperature,
    )
    return Services(
        settings=settings,
        database=database,
        transcriptions=transcriptions,
        summaries=SummaryRepository(database),
        audio=audio,
        transcriber=transcriber,
        youtube=youtube,
        jobs=TranscriptionJobRunner(
            transcriptions,
            audio,
            transcriber,
            youtube=youtube,
            max_workers=settings.max_concurrent_transcriptions,
            **(job_runner_kwargs or {}),
        ),
        summarizer=SummarizationService(
            llm,
            chunk_chars=settings.summary_chunk_chars,
            overlap_chars=settings.summary_chunk_overlap_chars,
            max_transcript_chars=settings.max_transcript_chars,
        ),
        rate_limiter=RateLimiter(settings.rate_limit_per_minute),
    )


def get_services(request: Request) -> Services:
    return request.app.state.services


def rate_limit(request: Request) -> None:
    """Dependency applied to expensive endpoints (uploads, LLM calls)."""
    client = request.client.host if request.client else "unknown"
    get_services(request).rate_limiter.check(client)
