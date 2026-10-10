"""Audio file handling: streaming uploads to disk, probing, conversion and cleanup.

Decoding is done with PyAV (bundled FFmpeg libraries, installed with faster-whisper).
If PyAV cannot open a file and the `ffmpeg`/`ffprobe` CLI tools are on PATH, they are
used as a fallback to probe the file and convert it to 16 kHz mono WAV.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from fastapi import UploadFile

from app.core.errors import FileTooLargeError, InvalidFileError, TranscriptionError
from app.utils.file_validation import SNIFF_BYTES, validate_signature

logger = logging.getLogger(__name__)

CHUNK_SIZE = 1024 * 1024
FFMPEG_TIMEOUT_SECONDS = 600


@dataclass(frozen=True)
class AudioInfo:
    duration_seconds: float | None
    needs_conversion: bool  # True when only the ffmpeg CLI can decode the file


class AudioService:
    def __init__(self, upload_dir: Path, max_upload_bytes: int) -> None:
        self.upload_dir = upload_dir
        self.max_upload_bytes = max_upload_bytes

    def ensure_upload_dir(self) -> None:
        self.upload_dir.mkdir(parents=True, exist_ok=True)

    async def save_upload(self, upload: UploadFile, file_id: str, ext: str) -> Path:
        """Stream an upload to `<upload_dir>/<file_id><ext>`, enforcing size and signature.

        The destination name is server-generated, so the client filename never touches
        the filesystem. The partial file is removed on any failure.
        """
        self.ensure_upload_dir()
        dest = self.upload_dir / f"{file_id}{ext}"
        written = 0
        try:
            with dest.open("xb") as out:
                first = True
                while chunk := await upload.read(CHUNK_SIZE):
                    if first:
                        validate_signature(chunk[:SNIFF_BYTES], ext)
                        first = False
                    written += len(chunk)
                    if written > self.max_upload_bytes:
                        limit_mb = self.max_upload_bytes // (1024 * 1024)
                        raise FileTooLargeError(f"File exceeds the {limit_mb} MB upload limit.")
                    out.write(chunk)
            if written == 0:
                raise InvalidFileError("The uploaded file is empty.")
        except BaseException:
            self.cleanup(dest)
            raise
        return dest

    def probe(self, path: Path) -> AudioInfo:
        """Verify the file contains decodable audio and return its duration if known."""
        try:
            return AudioInfo(duration_seconds=self._probe_pyav(path), needs_conversion=False)
        except Exception as exc:  # noqa: BLE001 - any PyAV failure falls through to ffprobe
            logger.info("PyAV could not decode %s (%s); trying ffprobe", path.name, exc)
        if shutil.which("ffprobe") and shutil.which("ffmpeg"):
            duration = self._probe_ffprobe(path)
            if duration is not None:
                return AudioInfo(duration_seconds=duration, needs_conversion=True)
        raise InvalidFileError("The file could not be decoded as audio. It may be corrupt or unsupported.")

    def prepare_for_transcription(self, path: Path, info: AudioInfo) -> Path:
        """Return a path the transcriber can decode, converting with ffmpeg if required."""
        if not info.needs_conversion:
            return path
        if not shutil.which("ffmpeg"):
            raise TranscriptionError("FFmpeg is required to convert this file but is not installed.")
        target = path.with_suffix(".converted.wav")
        cmd = ["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-i", str(path), "-ac", "1", "-ar", "16000", str(target)]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=FFMPEG_TIMEOUT_SECONDS)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            self.cleanup(target)
            raise TranscriptionError("FFmpeg failed to convert the audio file.") from exc
        return target

    def purge_upload_dir(self) -> int:
        """Delete leftover uploads (e.g. after a crash). Only call when no jobs are running."""
        if not self.upload_dir.is_dir():
            return 0
        files = [p for p in self.upload_dir.iterdir() if p.is_file()]
        self.cleanup(*files)
        return len(files)

    @staticmethod
    def cleanup(*paths: Path | None) -> None:
        """Delete temporary files, ignoring ones that are already gone."""
        for path in paths:
            if path is None:
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.warning("Could not delete temporary file %s", path, exc_info=True)

    # --- internals ---------------------------------------------------------

    @staticmethod
    def _probe_pyav(path: Path) -> float | None:
        import av  # imported lazily: provided by faster-whisper

        with av.open(str(path)) as container:
            if not container.streams.audio:
                raise InvalidFileError("The file does not contain an audio stream.")
            stream = container.streams.audio[0]
            next(container.decode(stream))  # raises if the first frame cannot be decoded
            if container.duration:
                return round(container.duration / av.time_base, 3)
            if stream.duration and stream.time_base:
                return round(float(stream.duration * stream.time_base), 3)
            return None

    @staticmethod
    def _probe_ffprobe(path: Path) -> float | None:
        cmd = ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "format=duration",
               "-of", "json", str(path)]
        try:
            result = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=60)
            data = json.loads(result.stdout or "{}")
        except (subprocess.SubprocessError, json.JSONDecodeError):
            return None
        try:
            return round(float(data["format"]["duration"]), 3)
        except (KeyError, TypeError, ValueError):
            return None
