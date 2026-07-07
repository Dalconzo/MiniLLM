from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from .curated import get_memory_record, init_curated_memory_schema, list_memory_records, update_memory_record_status
from .observability import utc_now


FEEDBACK_SCHEMA_VERSION = 6
VALID_FEEDBACK_RATINGS = ("up", "down", "saved", "ignored", "resolved")


@dataclass(frozen=True)
class MemoryFeedback:
    id: str
    memory_record_id: str | None
    source_kind: str
    source_id: str
    run_id: str | None
    query: str | None
    rating: str
    note: str | None
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "memory_record_id": self.memory_record_id,
            "source_kind": self.source_kind,
            "source_id": self.source_id,
            "run_id": self.run_id,
            "query": self.query,
            "rating": self.rating,
            "note": self.note,
            "created_at": self.created_at,
        }


def init_feedback_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL,
            checksum TEXT NOT NULL
        );

        INSERT OR IGNORE INTO schema_migrations (version, name, applied_at, checksum)
        VALUES (6, 'chatgpt_memory_feedback', datetime('now'), 'feedback_tables_v1');

        CREATE TABLE IF NOT EXISTS memory_feedback (
            id TEXT PRIMARY KEY,
            memory_record_id TEXT REFERENCES memory_records(id) ON DELETE SET NULL,
            source_kind TEXT NOT NULL,
            source_id TEXT NOT NULL,
            run_id TEXT,
            query TEXT,
            rating TEXT NOT NULL,
            note TEXT,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_memory_feedback_record
            ON memory_feedback(memory_record_id);
        CREATE INDEX IF NOT EXISTS idx_memory_feedback_source
            ON memory_feedback(source_kind, source_id);
        CREATE INDEX IF NOT EXISTS idx_memory_feedback_rating
            ON memory_feedback(rating);
        """
    )
    connection.commit()


def record_memory_feedback(
    connection: sqlite3.Connection,
    *,
    source_kind: str,
    source_id: str,
    rating: str,
    memory_record_id: str | None = None,
    run_id: str | None = None,
    query: str | None = None,
    note: str | None = None,
) -> MemoryFeedback:
    init_curated_memory_schema(connection)
    init_feedback_schema(connection)
    normalized_rating = _validate_rating(rating)
    created_at = utc_now()
    feedback_id = "fbk_" + _short_hash(
        f"{memory_record_id or ''}:{source_kind}:{source_id}:{rating}:{run_id or ''}:{query or ''}:{note or ''}:{created_at}"
    )
    feedback = MemoryFeedback(
        id=feedback_id,
        memory_record_id=memory_record_id,
        source_kind=source_kind,
        source_id=source_id,
        run_id=run_id,
        query=query,
        rating=normalized_rating,
        note=note,
        created_at=created_at,
    )

    with connection:
        connection.execute(
            """
            INSERT INTO memory_feedback (
                id, memory_record_id, source_kind, source_id, run_id, query, rating, note, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                feedback.id,
                feedback.memory_record_id,
                feedback.source_kind,
                feedback.source_id,
                feedback.run_id,
                feedback.query,
                feedback.rating,
                feedback.note,
                feedback.created_at,
            ),
        )

    if memory_record_id is not None:
        _apply_feedback_to_record(connection, memory_record_id, normalized_rating)

    return feedback


def list_open_loops(connection: sqlite3.Connection, *, limit: int | None = None) -> list[dict[str, Any]]:
    init_curated_memory_schema(connection)
    records = list_memory_records(connection, record_type="open_loop", status="active", limit=limit)
    return [record.to_dict() for record in records]


def feedback_summary(connection: sqlite3.Connection, *, memory_record_id: str) -> dict[str, Any]:
    init_feedback_schema(connection)
    rows = connection.execute(
        """
        SELECT rating, COUNT(*)
        FROM memory_feedback
        WHERE memory_record_id = ?
        GROUP BY rating
        """,
        (memory_record_id,),
    ).fetchall()
    summary = {row[0]: int(row[1]) for row in rows}
    summary["memory_record_id"] = memory_record_id
    return summary


def _apply_feedback_to_record(connection: sqlite3.Connection, memory_record_id: str, rating: str) -> None:
    record = get_memory_record(connection, memory_record_id)
    metadata = dict(record.metadata)
    summary = metadata.get("feedback_summary") if isinstance(metadata.get("feedback_summary"), dict) else {}
    summary[rating] = int(summary.get(rating, 0)) + 1
    metadata["feedback_summary"] = summary
    metadata["last_feedback_at"] = utc_now()
    metadata["last_feedback_rating"] = rating

    down_count = int(summary.get("down", 0))
    up_count = int(summary.get("up", 0)) + int(summary.get("saved", 0))

    status = record.status
    if rating in {"saved", "resolved"}:
        status = "active"
    elif down_count >= 2 and status == "active":
        status = "stale"
    elif up_count >= 2 and status in {"stale", "superseded"}:
        status = "active"

    with connection:
        connection.execute(
            """
            UPDATE memory_records
            SET status = ?, updated_at = ?, metadata_json = ?
            WHERE id = ?
            """,
            (status, utc_now(), json.dumps(metadata, sort_keys=True), memory_record_id),
        )


def _validate_rating(rating: str) -> str:
    normalized = rating.strip().lower()
    if normalized not in VALID_FEEDBACK_RATINGS:
        raise ValueError(f"invalid memory feedback rating: {rating}")
    return normalized


def _short_hash(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
