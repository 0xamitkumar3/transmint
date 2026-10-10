import httpx

from app.schemas.transcription import TranscriptSource
from app.services import youtube_service
from app.services.youtube_service import YouTubeService

TRANSCRIPT = {
    "content": [
        {"text": "Hello &amp; welcome.", "offset": 0, "duration": 2500, "lang": "en"},
        {"text": "[Music]", "offset": 2500, "duration": 1000, "lang": "en"},
        {"text": "Priya will send the plan.", "offset": 3500, "duration": 2000, "lang": "en"},
    ],
    "lang": "en",
    "availableLangs": ["en"],
}


def _service(handler, **kwargs) -> YouTubeService:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return YouTubeService(supadata_api_key="sd-key", supadata_client=client, **kwargs)


def test_supadata_transcript_is_parsed() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=TRANSCRIPT)

    result = _service(handler, caption_languages=["en", "hi"]).fetch_captions("dQw4w9WgXcQ")

    assert result is not None
    assert result.text == "Hello & welcome. Priya will send the plan."
    assert [(s.start, s.end) for s in result.segments] == [(0.0, 2.5), (3.5, 5.5)]
    assert result.language == "en"
    assert result.transcript_source == TranscriptSource.YOUTUBE_CAPTIONS
    req = seen[0]
    assert req.headers["x-api-key"] == "sd-key"
    assert req.url.params["url"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert req.url.params["lang"] == "en"
    assert req.url.params["mode"] == "native"


def test_supadata_async_job_is_polled(monkeypatch) -> None:
    monkeypatch.setattr(youtube_service, "SUPADATA_POLL_INTERVAL_SECONDS", 0)
    polls = iter([{"status": "queued"}, {"status": "active"}, {"status": "completed", **TRANSCRIPT}])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/transcript"):
            return httpx.Response(202, json={"jobId": "job-1"})
        assert request.url.path.endswith("/transcript/job-1")
        return httpx.Response(200, json=next(polls))

    result = _service(handler).fetch_supadata_captions("dQw4w9WgXcQ")
    assert result is not None and "Priya" in result.text


def test_supadata_failures_return_none() -> None:
    for status, body in [(401, {"error": "unauthorized"}), (404, {"error": "transcript-unavailable"}),
                         (429, {"error": "limit-exceeded"}), (500, {"error": "internal-error"})]:
        service = _service(lambda _req, s=status, b=body: httpx.Response(s, json=b))
        assert service.fetch_supadata_captions("dQw4w9WgXcQ") is None


def test_supadata_failed_job_returns_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/transcript"):
            return httpx.Response(202, json={"jobId": "job-1"})
        return httpx.Response(200, json={"status": "failed", "error": {"message": "no audio"}})

    assert _service(handler).fetch_supadata_captions("dQw4w9WgXcQ") is None


def test_supadata_network_error_returns_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    assert _service(handler).fetch_supadata_captions("dQw4w9WgXcQ") is None


def test_supadata_not_called_without_key(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not be called
        raise AssertionError("Supadata should not be called")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    service = YouTubeService(supadata_client=client)
    monkeypatch.setattr(YouTubeService, "captions_supported", staticmethod(lambda: False))
    assert service.fetch_captions("dQw4w9WgXcQ") is None
