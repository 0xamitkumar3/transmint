import pytest
from fastapi.testclient import TestClient

from app.api.deps import Services
from app.core.errors import InvalidInputError, YouTubeError
from app.services.youtube_service import NOISE_RE, _friendly_ytdlp_error
from app.utils.youtube_url import canonical_watch_url, parse_youtube_video_id
from tests.conftest import SAMPLE_TRANSCRIPT, FakeTranscriber, FakeYouTube

VID = "jNQXAC9IVRw"


# --- URL parsing ---------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        f"https://www.youtube.com/watch?v={VID}",
        f"https://youtube.com/watch?v={VID}&t=42s&list=PL123",
        f"http://m.youtube.com/watch?feature=share&v={VID}",
        f"https://music.youtube.com/watch?v={VID}",
        f"https://youtu.be/{VID}?si=abc",
        f"youtu.be/{VID}",
        f"https://www.youtube.com/shorts/{VID}",
        f"https://www.youtube.com/live/{VID}?feature=share",
        f"https://www.youtube.com/embed/{VID}",
        f"https://www.youtube-nocookie.com/embed/{VID}",
        f"  https://WWW.YOUTUBE.COM/watch?v={VID}  ",
    ],
)
def test_parses_supported_links(url: str) -> None:
    assert parse_youtube_video_id(url) == VID


@pytest.mark.parametrize(
    "url",
    [
        "",
        "not a url",
        f"https://evil.com/watch?v={VID}",
        f"https://youtube.com.evil.com/watch?v={VID}",
        f"https://www.youtube.com/watch?v={VID}x",  # wrong length
        "https://www.youtube.com/watch?v=abc",
        "https://www.youtube.com/playlist?list=PL123",
        "https://www.youtube.com/@somechannel",
        "file:///etc/passwd",
        f"https://youtu.be/{VID[:-1]}%",
    ],
)
def test_rejects_unsupported_links(url: str) -> None:
    with pytest.raises(InvalidInputError):
        parse_youtube_video_id(url)


def test_canonical_url() -> None:
    assert canonical_watch_url(VID) == f"https://www.youtube.com/watch?v={VID}"


def test_noise_captions_and_error_messages() -> None:
    assert NOISE_RE.match("[Music]") and NOISE_RE.match("(applause)")
    assert not NOISE_RE.match("[Music] and then we talked")
    assert "YTDLP_COOKIES_FILE" in _friendly_ytdlp_error("ERROR: Sign in to confirm you're not a bot")
    assert "private" in _friendly_ytdlp_error("ERROR: [youtube] x: Private video")
    assert "unavailable" in _friendly_ytdlp_error("weird internal failure /tmp/path").lower()
    assert "/tmp/path" not in _friendly_ytdlp_error("weird internal failure /tmp/path")


# --- API + job flow -------------------------------------------------------------


def _submit(client: TestClient, **body):
    return client.post("/api/transcriptions/youtube", json={"url": f"https://youtu.be/{VID}", **body})


def test_youtube_uses_captions_when_available(
    client: TestClient, youtube: FakeYouTube, transcriber: FakeTranscriber
) -> None:
    resp = _submit(client)
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "queued"
    assert body["source_type"] == "youtube"
    assert body["source_url"] == f"https://www.youtube.com/watch?v={VID}"

    record = client.get(f"/api/transcriptions/{body['id']}").json()
    assert record["status"] == "completed"
    assert record["transcript_source"] == "youtube_captions"
    assert record["title"] == "A Test Lecture"
    assert record["duration_seconds"] == 42.0  # real video duration, not the last caption end
    assert record["text"].startswith("Captions say hello")
    assert transcriber.calls == []  # Whisper not needed
    assert not any(c.startswith("download") for c in youtube.calls)


def test_youtube_falls_back_to_whisper_without_captions(
    client: TestClient, youtube: FakeYouTube, transcriber: FakeTranscriber, services: Services
) -> None:
    youtube.captions = None
    job_id = _submit(client).json()["id"]
    record = client.get(f"/api/transcriptions/{job_id}").json()
    assert record["status"] == "completed", record
    assert record["transcript_source"] == "whisper"
    assert record["text"] == SAMPLE_TRANSCRIPT
    assert record["title"] == "A Test Lecture"
    assert youtube.calls == [f"captions:{VID}", f"info:{VID}", f"download:{VID}"]
    assert len(transcriber.calls) == 1
    assert list(services.settings.upload_dir.iterdir()) == []  # downloaded audio removed


def test_prefer_captions_false_forces_whisper(client: TestClient, youtube: FakeYouTube) -> None:
    job_id = _submit(client, prefer_captions=False).json()["id"]
    record = client.get(f"/api/transcriptions/{job_id}").json()
    assert record["transcript_source"] == "whisper"
    assert not any(c.startswith("captions") for c in youtube.calls)


def test_youtube_errors_mark_job_failed(client: TestClient, youtube: FakeYouTube, services: Services) -> None:
    youtube.captions = None
    youtube.info_error = YouTubeError("The video is 300 minutes long; the limit is 180 minutes.")
    record = client.get(f"/api/transcriptions/{_submit(client).json()['id']}").json()
    assert record["status"] == "failed"
    assert "limit is 180 minutes" in record["error"]
    assert record["text"] is None

    youtube.info_error = None
    youtube.download_error = YouTubeError("This YouTube video is private.")
    record = client.get(f"/api/transcriptions/{_submit(client).json()['id']}").json()
    assert record["status"] == "failed" and record["error"] == "This YouTube video is private."
    assert list(services.settings.upload_dir.iterdir()) == []


def test_metadata_failure_does_not_fail_caption_jobs(client: TestClient, youtube: FakeYouTube) -> None:
    youtube.info_error = YouTubeError("bot check")
    record = client.get(f"/api/transcriptions/{_submit(client).json()['id']}").json()
    assert record["status"] == "completed"
    assert record["title"] is None
    assert record["duration_seconds"] == 8.0


def test_invalid_youtube_link_is_422(client: TestClient) -> None:
    resp = client.post("/api/transcriptions/youtube", json={"url": "https://vimeo.com/123"})
    assert resp.status_code == 422 and resp.json()["code"] == "invalid_input"
    assert client.post("/api/transcriptions/youtube", json={}).status_code == 422


def test_whisper_unavailable_with_captions_disabled_is_503(client: TestClient, transcriber: FakeTranscriber) -> None:
    transcriber.available = False
    assert _submit(client, prefer_captions=False).status_code == 503
    assert _submit(client).status_code == 202  # captions path still possible


def test_youtube_transcript_can_be_summarized_and_exported(client: TestClient) -> None:
    job_id = _submit(client).json()["id"]
    md = client.get(f"/api/transcriptions/{job_id}/export?format=md").text
    assert "**YouTube video:** A Test Lecture" in md
    assert f"**URL:** https://www.youtube.com/watch?v={VID}" in md
    assert "youtube captions" in md
    resp = client.post("/api/summaries", json={"transcription_id": job_id, "summary_type": "brief"})
    assert resp.status_code == 201
    assert resp.json()["transcription_id"] == job_id


def test_health_reports_youtube(client: TestClient) -> None:
    assert client.get("/api/health").json()["youtube_available"] is True


def test_old_database_is_migrated(tmp_path) -> None:
    import sqlite3

    from app.db.database import Database
    from app.db.repositories import TranscriptionRepository

    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE transcriptions (id TEXT PRIMARY KEY, status TEXT NOT NULL, original_filename TEXT NOT NULL, "
            "text TEXT, language TEXT, duration_seconds REAL, segments_json TEXT, error TEXT, "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        conn.execute("INSERT INTO transcriptions VALUES ('old1','completed','a.wav','hi',NULL,NULL,NULL,NULL,"
                     "'2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')")
    db = Database(path)
    db.initialize()
    db.initialize()  # idempotent
    record = TranscriptionRepository(db).get("old1")
    assert record.source_type.value == "upload" and record.text == "hi"
