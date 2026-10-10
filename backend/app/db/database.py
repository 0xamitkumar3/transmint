"""SQLite connection management and schema creation.

All SQL lives in this package so the storage engine can be swapped (e.g. PostgreSQL)
without touching routes or services.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS transcriptions (
    id                TEXT PRIMARY KEY,
    status            TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    source_type       TEXT NOT NULL DEFAULT 'upload',
    source_url        TEXT,
    title             TEXT,
    transcript_source TEXT,
    text              TEXT,
    language          TEXT,
    duration_seconds  REAL,
    segments_json     TEXT,
    error             TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS summaries (
    id               TEXT PRIMARY KEY,
    transcription_id TEXT REFERENCES transcriptions(id) ON DELETE CASCADE,
    summary_type     TEXT NOT NULL,
    output_language  TEXT,
    model            TEXT NOT NULL,
    chunk_count      INTEGER NOT NULL,
    content_json     TEXT NOT NULL,
    created_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_summaries_transcription ON summaries(transcription_id);
"""

# Columns added after the first release: (table, column, definition). Applied idempotently
# so existing local databases keep working without a migration tool.
ADDED_COLUMNS = [
    ("transcriptions", "source_type", "TEXT NOT NULL DEFAULT 'upload'"),
    ("transcriptions", "source_url", "TEXT"),
    ("transcriptions", "title", "TEXT"),
    ("transcriptions", "transcript_source", "TEXT"),
]


class Database:
    """Thin wrapper that hands out short-lived SQLite connections.

    A new connection per unit of work keeps things safe across the API threadpool
    and the background transcription workers.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            for table, column, definition in ADDED_COLUMNS:
                existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
