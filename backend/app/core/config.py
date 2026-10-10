"""Application configuration loaded from environment variables and `.env`."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# `backend/` directory. Relative paths in settings are resolved against it so the
# app behaves the same regardless of the current working directory.
BACKEND_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Typed runtime settings. Every field can be overridden by an env variable."""

    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- General -----------------------------------------------------------
    app_name: str = "Transmint"
    app_version: str = "0.1.0"
    log_level: str = "INFO"

    # --- Storage -----------------------------------------------------------
    database_path: Path = Path("data/transmint.db")
    upload_dir: Path = Path("data/uploads")

    # --- Uploads -----------------------------------------------------------
    max_upload_mb: int = Field(default=100, gt=0)
    allowed_extensions: str = ".wav,.mp3,.m4a,.flac,.ogg,.webm"

    # --- Transcription (faster-whisper) -------------------------------------
    whisper_model_size: str = "base"
    whisper_device: str = "auto"  # "cpu", "cuda" or "auto"
    whisper_compute_type: str = "int8"
    whisper_language: str | None = None  # e.g. "en"; empty = auto-detect
    whisper_beam_size: int = Field(default=5, ge=1)
    whisper_vad_filter: bool = True
    max_concurrent_transcriptions: int = Field(default=1, ge=1)

    # --- YouTube input (no API key required) --------------------------------
    youtube_enabled: bool = True
    # Try the video's existing captions before downloading audio for Whisper.
    youtube_prefer_captions: bool = True
    # Preferred caption languages, in order; any available track is used as a fallback.
    youtube_caption_languages: str = "en"
    youtube_max_duration_minutes: int = Field(default=180, gt=0)
    youtube_max_download_mb: int = Field(default=500, gt=0)
    youtube_download_timeout_seconds: int = Field(default=600, gt=0)
    # JavaScript runtimes yt-dlp may use for YouTube challenges (deno, node>=22, bun, quickjs).
    ytdlp_js_runtimes: str = "deno,node,bun"
    # Optional Netscape cookies.txt for videos that need sign-in / bot checks.
    ytdlp_cookies_file: Path | None = None
    # Optional Supadata API key (https://supadata.ai). Fetches captions via Supadata's
    # servers, which works on cloud hosts where YouTube blocks the server's IP.
    supadata_api_key: str | None = None
    # "native" = existing captions only; "auto" = also AI-generate when there are none
    # (uses more Supadata credits); "generate" = always AI-generate.
    supadata_mode: Literal["native", "auto", "generate"] = "native"

    # --- Summarization (Groq) ----------------------------------------------
    groq_api_key: str | None = None
    groq_model: str = "openai/gpt-oss-120b"
    groq_timeout_seconds: float = Field(default=60.0, gt=0)
    groq_max_retries: int = Field(default=2, ge=0)
    groq_temperature: float = Field(default=0.2, ge=0, le=2)
    summary_chunk_chars: int = Field(default=12_000, ge=1_000)
    summary_chunk_overlap_chars: int = Field(default=300, ge=0)
    max_transcript_chars: int = Field(default=400_000, ge=1_000)

    # --- Basic abuse protection ---------------------------------------------
    rate_limit_per_minute: int = Field(default=20, ge=0)  # 0 disables the limiter

    @field_validator("whisper_language", "groq_api_key", "supadata_api_key", mode="before")
    @classmethod
    def _blank_to_none(cls, value: str | None) -> str | None:
        if isinstance(value, str) and (not value.strip() or value.strip().startswith("your-")):
            return None  # blank or the .env.example placeholder
        return value

    @field_validator("ytdlp_cookies_file", mode="before")
    @classmethod
    def _blank_path_to_none(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("database_path", "upload_dir", "ytdlp_cookies_file", mode="after")
    @classmethod
    def _resolve_path(cls, value: Path | None) -> Path | None:
        if value is None:
            return None
        return value if value.is_absolute() else (BACKEND_DIR / value).resolve()

    @property
    def youtube_caption_language_list(self) -> list[str]:
        return [lang.strip() for lang in self.youtube_caption_languages.split(",") if lang.strip()]

    @property
    def ytdlp_js_runtime_list(self) -> list[str]:
        return [r.strip().lower() for r in self.ytdlp_js_runtimes.split(",") if r.strip()]

    @property
    def allowed_extension_set(self) -> frozenset[str]:
        exts = (e.strip().lower() for e in self.allowed_extensions.split(","))
        return frozenset(e if e.startswith(".") else f".{e}" for e in exts if e)

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    """Return the cached settings instance."""
    return Settings()
