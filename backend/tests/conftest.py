"""Shared fixtures. External AI services are always faked; no network or model downloads."""

from __future__ import annotations

import io
import json
import math
import struct
import wave
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.deps import Services, build_services
from app.core.config import Settings
from app.core.errors import DependencyUnavailableError, TranscriptionError
from app.main import create_app
from app.core.errors import YouTubeError
from app.schemas.transcription import TranscriptionResult, TranscriptSegment, TranscriptSource
from app.services.transcription_service import InlineExecutor
from app.services.youtube_service import VideoInfo

SAMPLE_TRANSCRIPT = (
    "Good morning everyone. Today we will review the quarterly roadmap. "
    "Priya will prepare the migration plan by Friday. We agreed to postpone the mobile release. "
    "Someone suggested adding dark mode but no decision was made."
)

VALID_SUMMARY = {
    "overview": "The team reviewed the quarterly roadmap.",
    "key_points": ["Roadmap reviewed", "Mobile release postponed"],
    "important_concepts": [{"term": "Migration plan", "explanation": "Plan for moving systems."}],
    "decisions": ["Postpone the mobile release"],
    "suggestions": ["Add dark mode"],
    "action_items": [{"task": "Prepare the migration plan", "owner": "Priya", "deadline": "by Friday"}],
    "follow_up_questions": ["When will the mobile release happen?"],
    "uncertainties": [],
}


class FakeTranscriber:
    """Stands in for faster-whisper."""

    def __init__(self) -> None:
        self.available = True
        self.error: Exception | None = None
        self.calls: list[Path] = []

    def check_available(self) -> None:
        if not self.available:
            raise DependencyUnavailableError("Transcription is unavailable: test.")

    def transcribe(self, audio_path: Path) -> TranscriptionResult:
        self.calls.append(audio_path)
        assert audio_path.exists(), "upload must exist while transcribing"
        if self.error:
            raise self.error
        return TranscriptionResult(
            text=SAMPLE_TRANSCRIPT,
            language="en",
            language_probability=0.99,
            duration_seconds=None,  # runner should fall back to the probed duration
            segments=[
                TranscriptSegment(start=0.0, end=3.2, text="Good morning everyone."),
                TranscriptSegment(start=3.2, end=7.5, text="Today we will review the quarterly roadmap."),
            ],
        )


class FakeLLM:
    """Stands in for the Groq client. Responses are a list of strings or a callable."""

    model = "fake-model"

    def __init__(self) -> None:
        self.configured = True
        self.responses: list[str] | None = None
        self.handler: Callable[[list[dict[str, str]]], str] | None = None
        self.error: Exception | None = None
        self.calls: list[list[dict[str, str]]] = []

    def check_available(self) -> None:
        if not self.configured:
            raise DependencyUnavailableError("Summarization is unavailable: GROQ_API_KEY is not configured.")

    def complete_json(self, messages: list[dict[str, str]], *, max_tokens: int) -> str:
        self.calls.append(messages)
        if self.error:
            raise self.error
        if self.handler:
            return self.handler(messages)
        if self.responses:
            return self.responses.pop(0)
        return json.dumps(VALID_SUMMARY)


class FakeYouTube:
    """Stands in for YouTubeService: no network access."""

    def __init__(self) -> None:
        self.captions: TranscriptionResult | None = TranscriptionResult(
            text="Captions say hello. Priya will prepare the plan by Friday.",
            language="en",
            duration_seconds=8.0,
            segments=[TranscriptSegment(start=0.0, end=8.0, text="Captions say hello. Priya will prepare the plan by Friday.")],
            transcript_source=TranscriptSource.YOUTUBE_CAPTIONS,
        )
        self.info_error: Exception | None = None
        self.download_error: Exception | None = None
        self.calls: list[str] = []

    @staticmethod
    def download_supported() -> bool:
        return True

    def check_available(self) -> None:
        return None

    def fetch_captions(self, video_id: str) -> TranscriptionResult | None:
        self.calls.append(f"captions:{video_id}")
        return self.captions.model_copy(deep=True) if self.captions else None

    def fetch_info(self, video_id: str) -> VideoInfo:
        self.calls.append(f"info:{video_id}")
        if self.info_error:
            raise self.info_error
        return VideoInfo(video_id=video_id, title="A Test Lecture", duration_seconds=42.0, raw={})

    def download_audio(self, video: VideoInfo, dest_dir: Path, file_id: str) -> Path:
        self.calls.append(f"download:{video.video_id}")
        if self.download_error:
            raise self.download_error
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / f"{file_id}.wav"
        path.write_bytes(make_wav_bytes())
        return path


def make_wav_bytes(seconds: float = 1.0, rate: int = 16_000) -> bytes:
    """A short 440 Hz mono PCM WAV that PyAV can decode."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = (int(8000 * math.sin(2 * math.pi * 440 * i / rate)) for i in range(int(seconds * rate)))
        w.writeframes(b"".join(struct.pack("<h", f) for f in frames))
    return buf.getvalue()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        database_path=tmp_path / "test.db",
        upload_dir=tmp_path / "uploads",
        groq_api_key="test-key",
        max_upload_mb=1,
        rate_limit_per_minute=0,
        summary_chunk_chars=2_000,
        summary_chunk_overlap_chars=50,
    )


@pytest.fixture
def transcriber() -> FakeTranscriber:
    return FakeTranscriber()


@pytest.fixture
def llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def youtube() -> FakeYouTube:
    return FakeYouTube()


@pytest.fixture
def services(settings: Settings, transcriber: FakeTranscriber, llm: FakeLLM, youtube: FakeYouTube) -> Services:
    return build_services(
        settings, transcriber=transcriber, llm=llm, youtube=youtube, job_runner_kwargs={"executor": InlineExecutor()}
    )


@pytest.fixture
def client(services: Services) -> Iterator[TestClient]:
    app = create_app(services=services)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def wav_bytes() -> bytes:
    return make_wav_bytes()


@pytest.fixture
def completed_transcription(client: TestClient, wav_bytes: bytes) -> dict:
    resp = client.post("/api/transcriptions", files={"file": ("meeting.wav", wav_bytes, "audio/wav")})
    assert resp.status_code == 202, resp.text
    record = client.get(f"/api/transcriptions/{resp.json()['id']}").json()
    assert record["status"] == "completed"
    return record


__all__ = ["FakeLLM", "FakeTranscriber", "FakeYouTube", "SAMPLE_TRANSCRIPT", "VALID_SUMMARY", "TranscriptionError", "YouTubeError"]
