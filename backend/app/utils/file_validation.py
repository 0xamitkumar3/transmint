"""Upload validation helpers: extension checks, content sniffing and safe filenames."""

from __future__ import annotations

import re
import unicodedata
from pathlib import PurePath

from app.core.errors import InvalidFileError

# Number of leading bytes needed to sniff every supported container.
SNIFF_BYTES = 16


def _is_wav(head: bytes) -> bool:
    return head[:4] == b"RIFF" and head[8:12] == b"WAVE"


def _is_mp3(head: bytes) -> bool:
    # ID3v2 tag, or a raw MPEG audio frame sync (11 set bits).
    return head[:3] == b"ID3" or (len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0)


def _is_flac(head: bytes) -> bool:
    return head[:4] == b"fLaC" or head[:3] == b"ID3"


def _is_mp4(head: bytes) -> bool:
    return head[4:8] == b"ftyp"


def _is_ogg(head: bytes) -> bool:
    return head[:4] == b"OggS"


def _is_webm(head: bytes) -> bool:
    return head[:4] == b"\x1a\x45\xdf\xa3"


SIGNATURES = {
    ".wav": _is_wav,
    ".mp3": _is_mp3,
    ".flac": _is_flac,
    ".m4a": _is_mp4,
    ".mp4": _is_mp4,
    ".ogg": _is_ogg,
    ".webm": _is_webm,
}


def get_extension(filename: str | None) -> str:
    """Return the lower-cased extension (including the dot) of a client filename."""
    if not filename:
        return ""
    return PurePath(filename.replace("\\", "/")).suffix.lower()


def validate_extension(filename: str | None, allowed: frozenset[str]) -> str:
    """Ensure the filename has a supported extension and return it."""
    ext = get_extension(filename)
    if not ext or ext not in allowed:
        supported = ", ".join(sorted(allowed))
        raise InvalidFileError(f"Unsupported file type '{ext or 'none'}'. Supported: {supported}.")
    return ext


def validate_signature(head: bytes, ext: str) -> None:
    """Check the file's leading bytes match the container implied by its extension."""
    if not head:
        raise InvalidFileError("The uploaded file is empty.")
    check = SIGNATURES.get(ext)
    if check is not None and not check(head):
        raise InvalidFileError(f"File content does not look like a valid '{ext}' audio file.")


def sanitize_filename(filename: str | None, *, max_length: int = 120) -> str:
    """Reduce a client-supplied filename to a safe display name (never used as a path)."""
    name = PurePath((filename or "").replace("\\", "/")).name
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" .")
    if len(name) > max_length:
        stem, dot, ext = name.rpartition(".")
        name = (stem[: max_length - len(ext) - 1] + dot + ext) if dot else name[:max_length]
    return name or "audio"


def safe_download_name(stem: str, extension: str) -> str:
    """Build a filename for Content-Disposition from trusted parts only."""
    safe_stem = re.sub(r"[^A-Za-z0-9_-]+", "-", stem).strip("-") or "transmint"
    return f"{safe_stem}.{extension.lstrip('.')}"
