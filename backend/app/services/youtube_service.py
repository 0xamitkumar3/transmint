"""YouTube input: fetch existing captions, or download the audio track for Whisper.

No YouTube API key is involved. The official Data API cannot download captions or
media for videos you don't own, so Transmint uses:

* `youtube-transcript-api` to read caption tracks (fast, no audio download), and
* `yt-dlp` to download the audio-only stream when captions are unavailable.

Only canonical watch URLs built from a validated video ID are ever requested.
"""

from __future__ import annotations

import html
import importlib.util
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

from app.core.errors import DependencyUnavailableError, YouTubeError
from app.schemas.transcription import TranscriptionResult, TranscriptSegment, TranscriptSource
from app.utils.youtube_url import canonical_watch_url

logger = logging.getLogger(__name__)

# Caption lines that carry no speech, e.g. "[Music]" or "(applause)".
NOISE_RE = re.compile(r"^\s*[\[(][^\])]{1,30}[\])]\s*$")


@dataclass(frozen=True)
class VideoInfo:
    video_id: str
    title: str | None
    duration_seconds: float | None
    raw: dict  # full yt-dlp info dict


class YouTubeService:
    def __init__(
        self,
        *,
        caption_languages: list[str] | None = None,
        max_duration_seconds: int = 3 * 3600,
        max_download_bytes: int = 500 * 1024 * 1024,
        download_timeout_seconds: int = 600,
        js_runtimes: list[str] | None = None,
        cookies_file: Path | None = None,
    ) -> None:
        self.caption_languages = caption_languages or ["en"]
        self.max_duration_seconds = max_duration_seconds
        self.max_download_bytes = max_download_bytes
        self.download_timeout_seconds = download_timeout_seconds
        self.js_runtimes = js_runtimes or []
        self.cookies_file = cookies_file

    # --- availability ------------------------------------------------------

    @staticmethod
    def captions_supported() -> bool:
        return importlib.util.find_spec("youtube_transcript_api") is not None

    @staticmethod
    def download_supported() -> bool:
        return importlib.util.find_spec("yt_dlp") is not None

    def check_available(self) -> None:
        if not (self.captions_supported() or self.download_supported()):
            raise DependencyUnavailableError(
                "YouTube input is unavailable: install 'yt-dlp' and 'youtube-transcript-api'."
            )

    # --- captions ----------------------------------------------------------

    def fetch_captions(self, video_id: str) -> TranscriptionResult | None:
        """Return the video's captions as a transcript, or None if there are none usable.

        Prefers manually created tracks in the configured languages, then auto-generated
        ones, then any available track. Raises `YouTubeError` only if the video itself is
        unavailable; every other caption problem returns None so the caller can fall back
        to downloading audio.
        """
        if not self.captions_supported():
            return None
        import youtube_transcript_api as yta

        try:
            track_list = yta.YouTubeTranscriptApi().list(video_id)
            track = self._choose_track(track_list, yta.NoTranscriptFound)
            if track is None:
                return None
            fetched = track.fetch()
        except (yta.VideoUnavailable, yta.InvalidVideoId) as exc:
            raise YouTubeError("This YouTube video is unavailable (removed, private or invalid).") from exc
        except yta.TranscriptsDisabled:
            logger.info("Captions are disabled for %s", video_id)
            return None
        except Exception as exc:  # noqa: BLE001 - blocked IPs, age gates, network errors...
            logger.info("Could not fetch captions for %s (%s); will download audio", video_id, type(exc).__name__)
            return None

        segments = []
        for snippet in fetched.snippets:
            text = html.unescape(snippet.text).replace("\n", " ").strip()
            if not text or NOISE_RE.match(text):
                continue
            start = max(0.0, float(snippet.start))
            segments.append(TranscriptSegment(start=round(start, 2), end=round(start + float(snippet.duration), 2), text=text))
        if not segments:
            return None
        return TranscriptionResult(
            text=" ".join(s.text for s in segments),
            language=fetched.language_code,
            duration_seconds=segments[-1].end,
            segments=segments,
            transcript_source=TranscriptSource.YOUTUBE_AUTO_CAPTIONS if fetched.is_generated
            else TranscriptSource.YOUTUBE_CAPTIONS,
        )

    def _choose_track(self, track_list, not_found_exc):  # noqa: ANN001, ANN202
        for finder in (track_list.find_manually_created_transcript, track_list.find_generated_transcript):
            try:
                return finder(self.caption_languages)
            except not_found_exc:
                continue
        tracks = sorted(track_list, key=lambda t: t.is_generated)  # manual tracks first
        return tracks[0] if tracks else None

    # --- metadata & audio download -------------------------------------------

    def fetch_info(self, video_id: str) -> VideoInfo:
        """Look up the video with yt-dlp and enforce live-stream and duration limits."""
        if not self.download_supported():
            raise DependencyUnavailableError("Downloading YouTube audio requires the 'yt-dlp' package.")
        import yt_dlp

        try:
            with yt_dlp.YoutubeDL(self._ydl_options()) as ydl:
                info = ydl.extract_info(canonical_watch_url(video_id), download=False)
        except yt_dlp.utils.DownloadError as exc:
            raise YouTubeError(_friendly_ytdlp_error(str(exc))) from exc
        if not info:
            raise YouTubeError("YouTube returned no information for this video.")
        if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming"}:
            raise YouTubeError("Live streams and upcoming premieres can't be transcribed. Try again after it ends.")
        duration = info.get("duration")
        if duration and duration > self.max_duration_seconds:
            raise YouTubeError(
                f"The video is {duration / 60:.0f} minutes long; the limit is {self.max_duration_seconds // 60} minutes."
            )
        return VideoInfo(video_id=video_id, title=info.get("title"), duration_seconds=duration, raw=info)

    def download_audio(self, video: VideoInfo, dest_dir: Path, file_id: str) -> Path:
        """Download the best audio-only stream to `<dest_dir>/<file_id>.<ext>` and return its path."""
        import yt_dlp

        dest_dir.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()

        def enforce_timeout(progress: dict) -> None:
            if time.monotonic() - started > self.download_timeout_seconds:
                raise yt_dlp.utils.DownloadError("download timed out")

        opts = self._ydl_options(
            format="bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best[height<=360]",
            outtmpl=str(dest_dir / f"{file_id}.%(ext)s"),
            max_filesize=self.max_download_bytes,
            progress_hooks=[enforce_timeout],
            overwrites=True,
        )
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                # Re-extract in this session: stream URLs are bound to the session that
                # resolved them, so reusing `video.raw` from another session gets HTTP 403.
                result = ydl.extract_info(canonical_watch_url(video.video_id), download=True)
        except yt_dlp.utils.DownloadError as exc:
            self._remove_partials(dest_dir, file_id)
            if "timed out" in str(exc):
                raise YouTubeError(
                    f"Downloading the audio took longer than {self.download_timeout_seconds} seconds."
                ) from exc
            raise YouTubeError(_friendly_ytdlp_error(str(exc))) from exc

        downloads = (result or {}).get("requested_downloads") or []
        path = Path(downloads[0]["filepath"]).resolve() if downloads and downloads[0].get("filepath") else None
        if path is None or not path.is_file():
            self._remove_partials(dest_dir, file_id)
            raise YouTubeError("The audio could not be downloaded (it may exceed the size limit).")
        if path.parent != dest_dir.resolve():
            path.unlink(missing_ok=True)
            raise YouTubeError("Unexpected download location; aborted.")
        return path

    def _ydl_options(self, **extra: object) -> dict:
        try:
            from yt_dlp.globals import supported_js_runtimes

            supported = set(supported_js_runtimes.value)
        except ImportError:  # older yt-dlp without JS runtime support
            supported = set()
        opts: dict = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "noplaylist": True,
            "socket_timeout": 30,
            "retries": 3,
            "logger": _YtDlpLogger(),
        }
        runtimes = {name: {} for name in self.js_runtimes if name in supported}
        if runtimes:
            opts["js_runtimes"] = runtimes
        if self.cookies_file:
            opts["cookiefile"] = str(self.cookies_file)
        opts.update(extra)
        return opts

    @staticmethod
    def _remove_partials(dest_dir: Path, file_id: str) -> None:
        for leftover in dest_dir.glob(f"{file_id}.*"):
            leftover.unlink(missing_ok=True)


class _YtDlpLogger:
    """Route yt-dlp output to our logger instead of stdout."""

    def debug(self, msg: str) -> None:
        logger.debug(msg)

    def info(self, msg: str) -> None:
        logger.debug(msg)

    def warning(self, msg: str) -> None:
        logger.info("yt-dlp: %s", msg)

    def error(self, msg: str) -> None:
        logger.warning("yt-dlp: %s", msg)


def _friendly_ytdlp_error(message: str) -> str:
    lowered = message.lower()
    if "sign in to confirm" in lowered or "not a bot" in lowered:
        return ("YouTube is asking for a sign-in/bot check for this server. Set YTDLP_COOKIES_FILE to a "
                "cookies.txt exported from your browser, or try again later.")
    if "private video" in lowered:
        return "This YouTube video is private."
    if "age" in lowered and ("restrict" in lowered or "confirm your age" in lowered):
        return "This video is age-restricted. Set YTDLP_COOKIES_FILE with a signed-in account's cookies to access it."
    if "not available in your country" in lowered or "geo" in lowered:
        return "This video is not available in the server's region."
    if "members-only" in lowered or "join this channel" in lowered:
        return "This video is for channel members only."
    if "video unavailable" in lowered or "has been removed" in lowered:
        return "This YouTube video is unavailable (removed, private or invalid)."
    if "max_filesize" in lowered or "larger than max-filesize" in lowered:
        return "The audio stream exceeds the download size limit."
    return "Could not download this YouTube video. It may be private, region-locked or temporarily unavailable."
