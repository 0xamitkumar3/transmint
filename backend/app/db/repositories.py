"""Repositories: the only place that reads/writes rows. They speak in Pydantic models."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from app.db.database import Database
from app.schemas.summary import SummaryContent, SummaryResponse, SummaryType
from app.schemas.transcription import (
    JobStatus,
    SourceType,
    TranscriptSource,
    TranscriptionResponse,
    TranscriptionResult,
    TranscriptSegment,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TranscriptionRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create(
        self,
        transcription_id: str,
        original_filename: str,
        *,
        source_type: SourceType = SourceType.UPLOAD,
        source_url: str | None = None,
    ) -> TranscriptionResponse:
        now = _now()
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO transcriptions (id, status, original_filename, source_type, source_url, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (transcription_id, JobStatus.QUEUED.value, original_filename, source_type.value, source_url, now, now),
            )
        record = self.get(transcription_id)
        assert record is not None
        return record

    def get(self, transcription_id: str) -> TranscriptionResponse | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM transcriptions WHERE id = ?", (transcription_id,)).fetchone()
        return self._to_model(row) if row else None

    def mark_processing(self, transcription_id: str) -> None:
        self._update(transcription_id, status=JobStatus.PROCESSING.value, error=None)

    def set_metadata(self, transcription_id: str, *, title: str | None, duration_seconds: float | None) -> None:
        """Store source metadata (e.g. a YouTube title) discovered while processing."""
        fields: dict[str, object] = {}
        if title:
            fields["title"] = title
        if duration_seconds:
            fields["duration_seconds"] = duration_seconds
        if fields:
            self._update(transcription_id, **fields)

    def mark_completed(self, transcription_id: str, result: TranscriptionResult) -> None:
        self._update(
            transcription_id,
            status=JobStatus.COMPLETED.value,
            transcript_source=result.transcript_source.value,
            text=result.text,
            language=result.language,
            duration_seconds=result.duration_seconds,
            segments_json=json.dumps([s.model_dump() for s in result.segments]),
            error=None,
        )

    def mark_failed(self, transcription_id: str, error: str) -> None:
        self._update(transcription_id, status=JobStatus.FAILED.value, error=error)

    def fail_unfinished(self, reason: str) -> int:
        """Mark jobs left queued/processing by a previous process as failed. Returns count."""
        with self.db.connect() as conn:
            cur = conn.execute(
                "UPDATE transcriptions SET status = ?, error = ?, updated_at = ? WHERE status IN (?, ?)",
                (JobStatus.FAILED.value, reason, _now(), JobStatus.QUEUED.value, JobStatus.PROCESSING.value),
            )
            return cur.rowcount

    def _update(self, transcription_id: str, **fields: object) -> None:
        fields["updated_at"] = _now()
        assignments = ", ".join(f"{name} = ?" for name in fields)
        with self.db.connect() as conn:
            conn.execute(
                f"UPDATE transcriptions SET {assignments} WHERE id = ?",  # noqa: S608 - names are internal
                (*fields.values(), transcription_id),
            )

    @staticmethod
    def _to_model(row: sqlite3.Row) -> TranscriptionResponse:
        segments = json.loads(row["segments_json"]) if row["segments_json"] else []
        return TranscriptionResponse(
            id=row["id"],
            status=JobStatus(row["status"]),
            original_filename=row["original_filename"],
            source_type=SourceType(row["source_type"] or SourceType.UPLOAD.value),
            source_url=row["source_url"],
            title=row["title"],
            transcript_source=TranscriptSource(row["transcript_source"]) if row["transcript_source"] else None,
            text=row["text"],
            language=row["language"],
            duration_seconds=row["duration_seconds"],
            segments=[TranscriptSegment(**s) for s in segments],
            error=row["error"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )


class SummaryRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create(
        self,
        summary_id: str,
        *,
        transcription_id: str | None,
        summary_type: SummaryType,
        output_language: str | None,
        model: str,
        chunk_count: int,
        content: SummaryContent,
    ) -> SummaryResponse:
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO summaries (id, transcription_id, summary_type, output_language, model, "
                "chunk_count, content_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    summary_id,
                    transcription_id,
                    summary_type.value,
                    output_language,
                    model,
                    chunk_count,
                    content.model_dump_json(),
                    _now(),
                ),
            )
        record = self.get(summary_id)
        assert record is not None
        return record

    def get(self, summary_id: str) -> SummaryResponse | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM summaries WHERE id = ?", (summary_id,)).fetchone()
        if not row:
            return None
        return SummaryResponse(
            id=row["id"],
            transcription_id=row["transcription_id"],
            summary_type=SummaryType(row["summary_type"]),
            output_language=row["output_language"],
            model=row["model"],
            chunk_count=row["chunk_count"],
            content=SummaryContent.model_validate_json(row["content_json"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )
