from fastapi.testclient import TestClient


def test_health_reports_status_and_version(client: TestClient) -> None:
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["app"] == "Transmint"
    assert body["version"]
    assert body["transcription_available"] is True
    assert body["summarization_configured"] is True


def test_health_never_exposes_secrets(client: TestClient) -> None:
    assert "test-key" not in client.get("/api/health").text


def test_health_reports_missing_groq_key(client: TestClient, llm) -> None:
    llm.configured = False
    assert client.get("/api/health").json()["summarization_configured"] is False


def test_openapi_docs_available(client: TestClient) -> None:
    assert client.get("/docs").status_code == 200
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/transcriptions" in paths and "/api/summaries" in paths


def test_cors_allows_configured_origin_only(client: TestClient) -> None:
    ok = client.get("/api/health", headers={"Origin": "http://127.0.0.1:5500"})
    assert ok.headers.get("access-control-allow-origin") == "http://127.0.0.1:5500"
    blocked = client.get("/api/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in blocked.headers
