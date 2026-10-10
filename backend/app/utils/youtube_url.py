"""Strict parsing of YouTube video links.

Only the 11-character video ID is extracted. Downstream code rebuilds a canonical
`https://www.youtube.com/watch?v=<id>` URL, so arbitrary user-supplied URLs are never
fetched by the server (no SSRF through the YouTube downloader).
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from app.core.errors import InvalidInputError

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtube-nocookie.com",
                 "www.youtube-nocookie.com"}
SHORT_HOSTS = {"youtu.be", "www.youtu.be"}
PATH_PREFIXES = ("/shorts/", "/live/", "/embed/", "/v/", "/e/")


def parse_youtube_video_id(url: str) -> str:
    """Return the video ID from a YouTube link, or raise `InvalidInputError`."""
    raw = (url or "").strip()
    if len(raw) > 2048:
        raise InvalidInputError("The YouTube URL is too long.")
    if not re.match(r"^https?://", raw, re.IGNORECASE):
        raw = "https://" + raw
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()

    candidate: str | None = None
    if host in SHORT_HOSTS:
        candidate = parsed.path.lstrip("/").split("/")[0]
    elif host in YOUTUBE_HOSTS:
        if parsed.path in ("/watch", "/watch/"):
            candidate = (parse_qs(parsed.query).get("v") or [None])[0]
        else:
            for prefix in PATH_PREFIXES:
                if parsed.path.startswith(prefix):
                    candidate = parsed.path[len(prefix):].split("/")[0]
                    break
    else:
        raise InvalidInputError("Only youtube.com and youtu.be video links are supported.")

    if not candidate or not VIDEO_ID_RE.match(candidate):
        raise InvalidInputError("Could not find a valid YouTube video ID in the link (playlists and channels are not supported).")
    return candidate


def canonical_watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"
