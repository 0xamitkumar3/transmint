"""Domain exceptions mapped to HTTP responses by handlers registered in `main.py`."""

from __future__ import annotations


class TransmintError(Exception):
    """Base class for errors that should become a clean JSON HTTP response."""

    status_code: int = 500
    code: str = "internal_error"

    def __init__(self, message: str, *, headers: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.headers = headers


class NotFoundError(TransmintError):
    status_code = 404
    code = "not_found"


class InvalidFileError(TransmintError):
    status_code = 415
    code = "invalid_file"


class FileTooLargeError(TransmintError):
    status_code = 413
    code = "file_too_large"


class InvalidInputError(TransmintError):
    status_code = 422
    code = "invalid_input"


class ConflictError(TransmintError):
    """The resource exists but is not in a state that allows the operation."""

    status_code = 409
    code = "conflict"


class DependencyUnavailableError(TransmintError):
    """A required local dependency (model, library, binary) or credential is missing."""

    status_code = 503
    code = "dependency_unavailable"


class YouTubeError(TransmintError):
    """The YouTube video could not be used (private, too long, blocked, ...)."""

    status_code = 422
    code = "youtube_error"


class TranscriptionError(TransmintError):
    status_code = 500
    code = "transcription_failed"


class UpstreamRateLimitError(TransmintError):
    status_code = 429
    code = "upstream_rate_limited"


class UpstreamTimeoutError(TransmintError):
    status_code = 504
    code = "upstream_timeout"


class SummarizationError(TransmintError):
    """The LLM provider failed or returned output that could not be validated."""

    status_code = 502
    code = "summarization_failed"


class RateLimitExceededError(TransmintError):
    status_code = 429
    code = "rate_limited"
