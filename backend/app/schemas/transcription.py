"""Pydantic schemas for transcription resources."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class JobStatus(str, Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class SourceType(str, Enum):
    UPLOAD = "upload"
    YOUTUBE = "youtube"


class TranscriptSource(str, Enum):
    """How the transcript text was produced."""

    WHISPER = "whisper"
    YOUTUBE_CAPTIONS = "youtube_captions"
    YOUTUBE_AUTO_CAPTIONS = "youtube_auto_captions"


class TranscriptSegment(BaseModel):
    """A timestamped piece of the transcript (seconds from start)."""

    start: float = Field(ge=0)
    end: float = Field(ge=0)
    text: str


class TranscriptionResult(BaseModel):
    """Output of the transcription service for one audio file."""

    text: str
    language: str | None = None
    language_probability: float | None = None
    duration_seconds: float | None = None
    segments: list[TranscriptSegment] = Field(default_factory=list)
    transcript_source: TranscriptSource = TranscriptSource.WHISPER


class YouTubeTranscriptionRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"examples": [{"url": "https://www.youtube.com/watch?v=jNQXAC9IVRw", "prefer_captions": True}]}
    )

    url: str = Field(min_length=5, max_length=2048, description="A YouTube video link (watch, youtu.be, shorts, live).")
    prefer_captions: bool | None = Field(
        default=None,
        description="Use the video's existing captions when available instead of running Whisper. "
        "Defaults to the server setting YOUTUBE_PREFER_CAPTIONS.",
    )


class TranscriptionStatusResponse(BaseModel):
    id: str
    status: JobStatus
    error: str | None = None
    created_at: datetime
    updated_at: datetime


class TranscriptionResponse(TranscriptionStatusResponse):
    """Full transcription record. Transcript fields are null until `completed`."""

    source_type: SourceType = SourceType.UPLOAD
    original_filename: str = Field(description="Uploaded filename, or the video title for YouTube sources.")
    source_url: str | None = None
    title: str | None = None
    transcript_source: TranscriptSource | None = None
    text: str | None = None
    language: str | None = None
    duration_seconds: float | None = None
    segments: list[TranscriptSegment] = Field(default_factory=list)
