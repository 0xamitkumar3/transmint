"""Speech-to-text using faster-whisper, plus the background job runner that drives it."""

from __future__ import annotations

import importlib.util
import logging
import threading
from collections.abc import Callable
from concurrent.futures import Executor, ThreadPoolExecutor
from pathlib import Path
from typing import Protocol

from app.core.errors import DependencyUnavailableError, InvalidFileError, TranscriptionError, TransmintError
from app.db.repositories import TranscriptionRepository
from app.schemas.transcription import TranscriptionResult, TranscriptSegment
from app.services.audio_service import AudioInfo, AudioService
from app.services.youtube_service import YouTubeService

logger = logging.getLogger(__name__)


class Transcriber(Protocol):
    """Anything that turns an audio file into a `TranscriptionResult`."""

    def check_available(self) -> None:
        """Raise `DependencyUnavailableError` if transcription cannot run."""

    def transcribe(self, audio_path: Path) -> TranscriptionResult: ...


class WhisperTranscriber:
    """faster-whisper backed transcriber. The model is loaded lazily on first use."""

    def __init__(
        self,
        model_size: str,
        device: str = "auto",
        compute_type: str = "int8",
        language: str | None = None,
        beam_size: int = 5,
        vad_filter: bool = True,
    ) -> None:
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.language = language
        self.beam_size = beam_size
        self.vad_filter = vad_filter
        self._model = None
        self._lock = threading.Lock()

    def check_available(self) -> None:
        if importlib.util.find_spec("faster_whisper") is None:
            raise DependencyUnavailableError(
                "Transcription is unavailable: the 'faster-whisper' package is not installed."
            )

    def _get_model(self):  # noqa: ANN202 - faster_whisper.WhisperModel, imported lazily
        with self._lock:
            if self._model is None:
                self.check_available()
                from faster_whisper import WhisperModel

                logger.info("Loading Whisper model '%s' (device=%s, compute=%s)",
                            self.model_size, self.device, self.compute_type)
                try:
                    self._model = WhisperModel(
                        self.model_size, device=self.device, compute_type=self.compute_type
                    )
                except Exception as exc:
                    raise DependencyUnavailableError(
                        f"Could not load Whisper model '{self.model_size}'. The first run downloads "
                        "it from Hugging Face, so check your network connection and model name."
                    ) from exc
            return self._model

    def transcribe(self, audio_path: Path) -> TranscriptionResult:
        model = self._get_model()
        try:
            segments_iter, info = model.transcribe(
                str(audio_path),
                language=self.language,
                beam_size=self.beam_size,
                vad_filter=self.vad_filter,
            )
            # Segments are generated lazily: decoding happens while iterating.
            segments = [
                TranscriptSegment(start=round(s.start, 2), end=round(s.end, 2), text=s.text.strip())
                for s in segments_iter
                if s.text.strip()
            ]
        except Exception as exc:
            raise TranscriptionError(f"Transcription failed: {exc.__class__.__name__}.") from exc

        return TranscriptionResult(
            text=" ".join(s.text for s in segments),
            language=info.language,
            language_probability=round(info.language_probability, 3)
            if info.language_probability is not None
            else None,
            duration_seconds=round(info.duration, 3) if info.duration else None,
            segments=segments,
        )


class TranscriptionJobRunner:
    """Runs transcription jobs off the request thread and records their status.

    A bounded thread pool keeps CPU/GPU usage predictable; jobs beyond the pool size
    wait in the executor queue with status `queued`.
    """

    def __init__(
        self,
        repository: TranscriptionRepository,
        audio_service: AudioService,
        transcriber: Transcriber,
        youtube: YouTubeService | None = None,
        executor: Executor | None = None,
        max_workers: int = 1,
    ) -> None:
        self.repository = repository
        self.audio_service = audio_service
        self.transcriber = transcriber
        self.youtube = youtube
        self._executor = executor or ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="transcribe")

    def submit(self, transcription_id: str, audio_path: Path, info: AudioInfo) -> None:
        """Queue transcription of an uploaded file (the file is deleted afterwards)."""
        self._executor.submit(
            self._run, transcription_id, lambda temp: self._transcribe_file(audio_path, info, temp), [audio_path]
        )

    def submit_youtube(self, transcription_id: str, video_id: str, prefer_captions: bool) -> None:
        """Queue a YouTube job: captions if available, otherwise download audio and transcribe."""
        self._executor.submit(
            self._run, transcription_id, lambda temp: self._process_youtube(transcription_id, video_id,
                                                                            prefer_captions, temp), []
        )

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _run(
        self,
        transcription_id: str,
        work: Callable[[list[Path]], TranscriptionResult],
        temp_paths: list[Path],
    ) -> None:
        """Common job lifecycle: status updates, error recording and temp-file cleanup.

        `work` appends any temporary files it creates to `temp_paths`.
        """
        try:
            self.repository.mark_processing(transcription_id)
            result = work(temp_paths)
            self.repository.mark_completed(transcription_id, result)
            logger.info("Transcription %s completed via %s (%d segments)",
                        transcription_id, result.transcript_source.value, len(result.segments))
        except TransmintError as exc:
            logger.warning("Transcription %s failed: %s", transcription_id, exc.message)
            self._safe_fail(transcription_id, exc.message)
        except Exception:
            logger.exception("Transcription %s failed unexpectedly", transcription_id)
            self._safe_fail(transcription_id, "Transcription failed due to an internal error.")
        finally:
            self.audio_service.cleanup(*temp_paths)

    def _transcribe_file(self, audio_path: Path, info: AudioInfo, temp_paths: list[Path]) -> TranscriptionResult:
        decodable = self.audio_service.prepare_for_transcription(audio_path, info)
        if decodable != audio_path:
            temp_paths.append(decodable)
        result = self.transcriber.transcribe(decodable)
        if result.duration_seconds is None:
            result.duration_seconds = info.duration_seconds
        return result

    def _process_youtube(
        self, transcription_id: str, video_id: str, prefer_captions: bool, temp_paths: list[Path]
    ) -> TranscriptionResult:
        if self.youtube is None:
            raise DependencyUnavailableError("YouTube input is not enabled on this server.")

        if prefer_captions:
            captions = self.youtube.fetch_captions(video_id)
            if captions is not None:
                duration = self._store_youtube_metadata(transcription_id, video_id)
                if duration:
                    captions.duration_seconds = float(duration)
                return captions
            logger.info("No usable captions for %s; downloading audio for Whisper", video_id)

        self.transcriber.check_available()
        video = self.youtube.fetch_info(video_id)
        self.repository.set_metadata(transcription_id, title=video.title, duration_seconds=video.duration_seconds)
        audio_path = self.youtube.download_audio(video, self.audio_service.upload_dir, transcription_id)
        temp_paths.append(audio_path)
        try:
            info = self.audio_service.probe(audio_path)
        except InvalidFileError as exc:
            raise TranscriptionError("The downloaded YouTube audio could not be decoded.") from exc
        return self._transcribe_file(audio_path, info, temp_paths)

    def _store_youtube_metadata(self, transcription_id: str, video_id: str) -> float | None:
        """Best-effort title/duration lookup for caption-based jobs. Returns the duration.

        Failures (including the duration limit) don't fail the job: the captions are
        already available and cost nothing to keep.
        """
        if not self.youtube.download_supported():
            return None
        try:
            video = self.youtube.fetch_info(video_id)
        except Exception as exc:  # noqa: BLE001
            logger.info("Could not fetch metadata for %s: %s", video_id, exc)
            return None
        self.repository.set_metadata(transcription_id, title=video.title, duration_seconds=video.duration_seconds)
        return video.duration_seconds

    def _safe_fail(self, transcription_id: str, message: str) -> None:
        try:
            self.repository.mark_failed(transcription_id, message)
        except Exception:
            logger.exception("Could not record failure for transcription %s", transcription_id)


class InlineExecutor(Executor):
    """Executor that runs work immediately in the caller's thread (used by tests)."""

    def submit(self, fn: Callable, /, *args, **kwargs):  # type: ignore[override]
        from concurrent.futures import Future

        future: Future = Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except BaseException as exc:  # noqa: BLE001
            future.set_exception(exc)
        return future
