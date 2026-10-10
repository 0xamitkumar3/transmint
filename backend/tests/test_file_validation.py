import pytest

from app.core.errors import InvalidFileError
from app.core.rate_limit import RateLimiter
from app.core.errors import RateLimitExceededError
from app.utils.file_validation import (
    safe_download_name,
    sanitize_filename,
    validate_extension,
    validate_signature,
)

ALLOWED = frozenset({".wav", ".mp3", ".m4a", ".flac"})


@pytest.mark.parametrize("name", ["a.wav", "LECTURE.MP3", "x.y.m4a", "song.flac"])
def test_valid_extensions(name: str) -> None:
    assert validate_extension(name, ALLOWED) == "." + name.rsplit(".", 1)[1].lower()


@pytest.mark.parametrize("name", ["notes.txt", "audio", "", None, "evil.wav.exe"])
def test_invalid_extensions(name) -> None:
    with pytest.raises(InvalidFileError):
        validate_extension(name, ALLOWED)


@pytest.mark.parametrize(
    ("head", "ext"),
    [
        (b"RIFF\x00\x00\x00\x00WAVEfmt ", ".wav"),
        (b"ID3\x04\x00\x00\x00\x00\x00\x00", ".mp3"),
        (b"\xff\xfb\x90\x00", ".mp3"),
        (b"fLaC\x00\x00\x00\x22", ".flac"),
        (b"\x00\x00\x00\x20ftypM4A ", ".m4a"),
    ],
)
def test_signature_accepts_real_headers(head: bytes, ext: str) -> None:
    validate_signature(head, ext)


@pytest.mark.parametrize(("head", "ext"), [(b"hello world text", ".wav"), (b"%PDF-1.7", ".mp3"), (b"", ".flac")])
def test_signature_rejects_mismatched_content(head: bytes, ext: str) -> None:
    with pytest.raises(InvalidFileError):
        validate_signature(head, ext)


def test_sanitize_filename_strips_paths_and_unsafe_chars() -> None:
    assert sanitize_filename("../../etc/passwd") == "passwd"
    assert sanitize_filename("C:\\Users\\me\\talk<1>.mp3") == "talk_1_.mp3"
    assert sanitize_filename("") == "audio"
    assert len(sanitize_filename("a" * 500 + ".wav")) <= 120
    assert sanitize_filename("a" * 500 + ".wav").endswith(".wav")


def test_safe_download_name() -> None:
    assert safe_download_name("transmint-summary-abc", "md") == "transmint-summary-abc.md"
    assert safe_download_name('../"bad"\r\nname', ".txt") == "bad-name.txt"


def test_rate_limiter_blocks_after_limit() -> None:
    limiter = RateLimiter(max_requests=2, window_seconds=60)
    limiter.check("1.2.3.4")
    limiter.check("1.2.3.4")
    with pytest.raises(RateLimitExceededError) as exc:
        limiter.check("1.2.3.4")
    assert exc.value.headers and "Retry-After" in exc.value.headers
    limiter.check("5.6.7.8")  # other clients unaffected


def test_rate_limiter_disabled_when_zero() -> None:
    limiter = RateLimiter(max_requests=0)
    for _ in range(100):
        limiter.check("x")
