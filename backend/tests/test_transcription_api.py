from pathlib import Path

from fastapi.testclient import TestClient

from app.api.deps import Services
from app.core.errors import TranscriptionError
from tests.conftest import SAMPLE_TRANSCRIPT, FakeTranscriber, make_wav_bytes


def _upload(client: TestClient, content: bytes, name: str = "meeting.wav"):
    return client.post("/api/transcriptions", files={"file": (name, content, "application/octet-stream")})


def test_upload_creates_job_and_completes(client: TestClient, wav_bytes: bytes, services: Services) -> None:
    resp = _upload(client, wav_bytes)
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "queued"
    assert len(body["id"]) == 32
    assert body["original_filename"] == "meeting.wav"
    assert resp.headers["location"] == f"/api/transcriptions/{body['id']}"

    status = client.get(f"/api/transcriptions/{body['id']}/status").json()
    assert status["status"] == "completed"
    assert "text" not in status

    record = client.get(f"/api/transcriptions/{body['id']}").json()
    assert record["text"] == SAMPLE_TRANSCRIPT
    assert record["language"] == "en"
    assert record["duration_seconds"] == 1.0  # from the probe, since the fake returned None
    assert record["segments"][0] == {"start": 0.0, "end": 3.2, "text": "Good morning everyone."}

    # Temporary upload removed after processing.
    assert list(services.settings.upload_dir.iterdir()) == []


def test_rejects_unsupported_extension(client: TestClient, wav_bytes: bytes) -> None:
    resp = _upload(client, wav_bytes, "notes.txt")
    assert resp.status_code == 415
    assert resp.json()["code"] == "invalid_file"


def test_rejects_content_not_matching_extension(client: TestClient, services: Services) -> None:
    resp = _upload(client, b"this is definitely not audio data", "fake.wav")
    assert resp.status_code == 415
    assert list(services.settings.upload_dir.iterdir()) == []


def test_rejects_corrupt_audio_with_valid_header(client: TestClient, services: Services) -> None:
    corrupt = b"RIFF\x24\x00\x00\x00WAVE" + b"\x00garbage" * 50
    resp = _upload(client, corrupt, "broken.wav")
    assert resp.status_code == 415
    assert "decoded" in resp.json()["detail"] or "audio" in resp.json()["detail"]
    assert list(services.settings.upload_dir.iterdir()) == []


def test_rejects_empty_file(client: TestClient) -> None:
    assert _upload(client, b"", "empty.wav").status_code == 415


def test_rejects_file_over_size_limit(client: TestClient, services: Services) -> None:
    big = make_wav_bytes(seconds=40)  # ~1.28 MB > 1 MB limit in test settings
    resp = _upload(client, big)
    assert resp.status_code == 413
    assert resp.json()["code"] == "file_too_large"
    assert list(services.settings.upload_dir.iterdir()) == []


def test_missing_file_field_is_422(client: TestClient) -> None:
    assert client.post("/api/transcriptions").status_code == 422


def test_transcriber_unavailable_returns_503(
    client: TestClient, transcriber: FakeTranscriber, wav_bytes: bytes
) -> None:
    transcriber.available = False
    resp = _upload(client, wav_bytes)
    assert resp.status_code == 503
    assert resp.json()["code"] == "dependency_unavailable"


def test_transcription_failure_marks_job_failed_without_fake_text(
    client: TestClient, transcriber: FakeTranscriber, wav_bytes: bytes, services: Services
) -> None:
    transcriber.error = TranscriptionError("Transcription failed: RuntimeError.")
    job_id = _upload(client, wav_bytes).json()["id"]
    record = client.get(f"/api/transcriptions/{job_id}").json()
    assert record["status"] == "failed"
    assert record["error"] == "Transcription failed: RuntimeError."
    assert record["text"] is None
    assert list(services.settings.upload_dir.iterdir()) == []


def test_unexpected_error_is_not_leaked(client: TestClient, transcriber: FakeTranscriber, wav_bytes: bytes) -> None:
    transcriber.error = RuntimeError("secret internal detail /home/user/path")
    job_id = _upload(client, wav_bytes).json()["id"]
    record = client.get(f"/api/transcriptions/{job_id}").json()
    assert record["status"] == "failed"
    assert "secret" not in record["error"]


def test_unknown_transcription_is_404(client: TestClient) -> None:
    assert client.get("/api/transcriptions/doesnotexist").status_code == 404
    assert client.get("/api/transcriptions/doesnotexist/status").status_code == 404
    assert client.get("/api/transcriptions/doesnotexist/export").status_code == 404


def test_export_transcript_txt_and_md(client: TestClient, completed_transcription: dict) -> None:
    tid = completed_transcription["id"]
    txt = client.get(f"/api/transcriptions/{tid}/export?format=txt")
    assert txt.status_code == 200
    assert txt.headers["content-type"].startswith("text/plain")
    assert txt.headers["content-disposition"] == f'attachment; filename="transmint-transcript-{tid}.txt"'
    assert SAMPLE_TRANSCRIPT in txt.text
    assert "[00:00 - 00:03] Good morning everyone." in txt.text

    md = client.get(f"/api/transcriptions/{tid}/export?format=md")
    assert md.headers["content-type"].startswith("text/markdown")
    assert md.text.startswith("# Transcript")

    assert client.get(f"/api/transcriptions/{tid}/export?format=pdf").status_code == 422


def test_export_incomplete_transcription_is_409(client: TestClient, services: Services) -> None:
    services.transcriptions.create("a" * 32, "pending.wav")
    resp = client.get(f"/api/transcriptions/{'a' * 32}/export")
    assert resp.status_code == 409


def test_restart_marks_unfinished_jobs_failed(services: Services) -> None:
    from app.main import create_app

    services.transcriptions.create("b" * 32, "stuck.wav")
    stray = Path(services.settings.upload_dir) / "orphan.wav"
    stray.write_bytes(b"x")
    with TestClient(create_app(services=services)) as client:
        record = client.get(f"/api/transcriptions/{'b' * 32}").json()
    assert record["status"] == "failed"
    assert "restarted" in record["error"]
    assert not stray.exists()
