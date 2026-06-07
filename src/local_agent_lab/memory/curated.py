from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable

from .observability import utc_now


CURATED_SCHEMA_VERSION = 4

VALID_RECORD_TYPES = {
    "project_fact",
    "decision",
    "preference",
    "workflow",
    "open_loop",
    "lesson",
    "contact_note",
    "research_note",
}
VALID_TRUST_LEVELS = {"low", "medium", "high", "canonical"}
VALID_RECORD_STATUSES = {"active", "stale", "superseded", "archived", "deleted"}


@dataclass(frozen=True)
class MemoryRecord:
    id: str
    record_type: str
    title: str
    body: str
    subject_id: str | None
    trust_level: str
    source_kind: str
    source_ref: str | None
    provenance: dict[str, Any]
    status: str
    valid_from: str | None
    valid_to: str | None
    last_verified_at: str | None
    created_by: str
    created_at: str
    updated_at: str
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "record_type": self.record_type,
            "title": self.title,
            "body": self.body,
            "subject_id": self.subject_id,
            "trust_level": self.trust_level,
            "source_kind": self.source_kind,
            "source_ref": self.source_ref,
            "provenance": self.provenance,
            "status": self.status,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "last_verified_at": self.last_verified_at,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class MemoryLink:
    from_kind: str
    from_id: str
    to_kind: str
    to_id: str
    link_type: str
    confidence: float | None
    notes: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "from_kind": self.from_kind,
            "from_id": self.from_id,
            "to_kind": self.to_kind,
            "to_id": self.to_id,
            "link_type": self.link_type,
            "confidence": self.confidence,
            "notes": self.notes,
        }


def init_curated_memory_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL,
            checksum TEXT NOT NULL
        );

        INSERT OR IGNORE INTO schema_migrations (version, name, applied_at, checksum)
        VALUES (4, 'chatgpt_memory_curated_records', datetime('now'), 'curated_memory_v1');

        CREATE TABLE IF NOT EXISTS memory_records (
            id TEXT PRIMARY KEY,
            record_type TEXT NOT NULL,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            subject_id TEXT REFERENCES subjects(id) ON DELETE SET NULL,
            trust_level TEXT NOT NULL,
            source_kind TEXT NOT NULL,
            source_ref TEXT,
            provenance_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'active',
            valid_from TEXT,
            valid_to TEXT,
            last_verified_at TEXT,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS memory_links (
            from_kind TEXT NOT NULL,
            from_id TEXT NOT NULL,
            to_kind TEXT NOT NULL,
            to_id TEXT NOT NULL,
            link_type TEXT NOT NULL,
            confidence REAL,
            notes TEXT,
            PRIMARY KEY(from_kind, from_id, to_kind, to_id, link_type)
        );

        CREATE INDEX IF NOT EXISTS idx_memory_records_type
            ON memory_records(record_type);
        CREATE INDEX IF NOT EXISTS idx_memory_records_subject
            ON memory_records(subject_id);
        CREATE INDEX IF NOT EXISTS idx_memory_records_status
            ON memory_records(status);
        CREATE INDEX IF NOT EXISTS idx_memory_records_trust
            ON memory_records(trust_level);
        CREATE INDEX IF NOT EXISTS idx_memory_records_updated
            ON memory_records(updated_at);
        CREATE INDEX IF NOT EXISTS idx_memory_links_to
            ON memory_links(to_kind, to_id);
        CREATE INDEX IF NOT EXISTS idx_memory_links_type
            ON memory_links(link_type);
        """
    )
    connection.commit()


def create_memory_record(
    connection: sqlite3.Connection,
    *,
    record_type: str,
    title: str,
    body: str,
    subject_id: str | None = None,
    trust_level: str = "medium",
    source_kind: str = "manual",
    source_ref: str | None = None,
    provenance: dict[str, Any] | None = None,
    status: str = "active",
    valid_from: str | None = None,
    valid_to: str | None = None,
    last_verified_at: str | None = None,
    created_by: str = "user",
    metadata: dict[str, Any] | None = None,
    record_id: str | None = None,
) -> MemoryRecord:
    init_curated_memory_schema(connection)
    normalized_type = _validate_record_type(record_type)
    normalized_trust = _validate_trust_level(trust_level)
    normalized_status = _validate_status(status)
    cleaned_title = _required_text(title, "title")
    cleaned_body = _required_text(body, "body")
    now = utc_now()
    final_record_id = record_id or _record_id(
        record_type=normalized_type,
        title=cleaned_title,
        body=cleaned_body,
        source_kind=source_kind,
        source_ref=source_ref,
    )

    with connection:
        connection.execute(
            """
            INSERT INTO memory_records (
                id, record_type, title, body, subject_id, trust_level, source_kind,
                source_ref, provenance_json, status, valid_from, valid_to,
                last_verified_at, created_by, created_at, updated_at, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                record_type = excluded.record_type,
                title = excluded.title,
                body = excluded.body,
                subject_id = excluded.subject_id,
                trust_level = excluded.trust_level,
                source_kind = excluded.source_kind,
                source_ref = excluded.source_ref,
                provenance_json = excluded.provenance_json,
                status = excluded.status,
                valid_from = excluded.valid_from,
                valid_to = excluded.valid_to,
                last_verified_at = excluded.last_verified_at,
                updated_at = excluded.updated_at,
                metadata_json = excluded.metadata_json
            """,
            (
                final_record_id,
                normalized_type,
                cleaned_title,
                cleaned_body,
                subject_id,
                normalized_trust,
                source_kind,
                source_ref,
                json.dumps(provenance or {}, sort_keys=True),
                normalized_status,
                valid_from,
                valid_to,
                last_verified_at,
                created_by,
                now,
                now,
                json.dumps(metadata or {}, sort_keys=True),
            ),
        )

    return get_memory_record(connection, final_record_id)


def promote_chunk_to_memory_record(
    connection: sqlite3.Connection,
    chunk_id: str,
    *,
    record_type: str,
    title: str | None = None,
    body: str | None = None,
    subject_id: str | None = None,
    trust_level: str = "medium",
    status: str = "active",
    created_by: str = "user",
    metadata: dict[str, Any] | None = None,
    provenance: dict[str, Any] | None = None,
    valid_from: str | None = None,
    valid_to: str | None = None,
    last_verified_at: str | None = None,
) -> MemoryRecord:
    init_curated_memory_schema(connection)
    chunk = _fetch_chunk_provenance(connection, chunk_id)
    if chunk is None:
        raise KeyError(f"message chunk not found: {chunk_id}")

    final_title = title or chunk["conversation_title"]
    final_body = body or chunk["text"]
    final_provenance = {
        "promoted_at": utc_now(),
        "source": {
            "chunk_id": chunk["chunk_id"],
            "message_id": chunk["message_id"],
            "conversation_id": chunk["conversation_id"],
            "import_id": chunk["import_id"],
            "conversation_title": chunk["conversation_title"],
            "role": chunk["role"],
            "turn_index": chunk["turn_index"],
            "chunk_index": chunk["chunk_index"],
            "text_sha256": chunk["text_sha256"],
        },
        **(provenance or {}),
    }
    record = create_memory_record(
        connection,
        record_type=record_type,
        title=final_title,
        body=final_body,
        subject_id=subject_id,
        trust_level=trust_level,
        source_kind="chatgpt_chunk",
        source_ref=chunk_id,
        provenance=final_provenance,
        status=status,
        valid_from=valid_from,
        valid_to=valid_to,
        last_verified_at=last_verified_at,
        created_by=created_by,
        metadata=metadata,
    )
    create_memory_link(
        connection,
        from_kind="memory_record",
        from_id=record.id,
        to_kind="message_chunk",
        to_id=chunk_id,
        link_type="derived_from",
        confidence=1.0,
        notes="Promoted from ChatGPT export chunk.",
    )
    create_memory_link(
        connection,
        from_kind="memory_record",
        from_id=record.id,
        to_kind="conversation",
        to_id=chunk["conversation_id"],
        link_type="derived_from",
        confidence=1.0,
        notes="Source conversation for promoted memory.",
    )
    return record


def get_memory_record(connection: sqlite3.Connection, record_id: str) -> MemoryRecord:
    init_curated_memory_schema(connection)
    row = connection.execute(
        """
        SELECT
            id, record_type, title, body, subject_id, trust_level, source_kind,
            source_ref, provenance_json, status, valid_from, valid_to,
            last_verified_at, created_by, created_at, updated_at, metadata_json
        FROM memory_records
        WHERE id = ?
        """,
        (record_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"memory record not found: {record_id}")
    return _record_from_row(row)


def list_memory_records(
    connection: sqlite3.Connection,
    *,
    record_type: str | None = None,
    trust_level: str | None = None,
    status: str | None = "active",
    subject_id: str | None = None,
    limit: int | None = None,
) -> list[MemoryRecord]:
    init_curated_memory_schema(connection)
    where: list[str] = []
    params: list[Any] = []
    if record_type is not None:
        where.append("record_type = ?")
        params.append(_validate_record_type(record_type))
    if trust_level is not None:
        where.append("trust_level = ?")
        params.append(_validate_trust_level(trust_level))
    if status is not None:
        where.append("status = ?")
        params.append(_validate_status(status))
    if subject_id is not None:
        where.append("subject_id = ?")
        params.append(subject_id)

    limit_clause = ""
    if limit is not None:
        if limit < 1:
            return []
        limit_clause = "LIMIT ?"
        params.append(limit)

    where_clause = f"WHERE {' AND '.join(where)}" if where else ""
    rows = connection.execute(
        f"""
        SELECT
            id, record_type, title, body, subject_id, trust_level, source_kind,
            source_ref, provenance_json, status, valid_from, valid_to,
            last_verified_at, created_by, created_at, updated_at, metadata_json
        FROM memory_records
        {where_clause}
        ORDER BY
            CASE trust_level
                WHEN 'canonical' THEN 0
                WHEN 'high' THEN 1
                WHEN 'medium' THEN 2
                WHEN 'low' THEN 3
                ELSE 4
            END,
            COALESCE(last_verified_at, updated_at, created_at) DESC,
            title COLLATE NOCASE ASC
        {limit_clause}
        """,
        params,
    ).fetchall()
    return [_record_from_row(row) for row in rows]


def update_memory_record_status(
    connection: sqlite3.Connection,
    record_id: str,
    status: str,
    *,
    metadata_patch: dict[str, Any] | None = None,
) -> MemoryRecord:
    init_curated_memory_schema(connection)
    normalized_status = _validate_status(status)
    record = get_memory_record(connection, record_id)
    metadata = {**record.metadata, **(metadata_patch or {})}
    with connection:
        connection.execute(
            """
            UPDATE memory_records
            SET status = ?, updated_at = ?, metadata_json = ?
            WHERE id = ?
            """,
            (normalized_status, utc_now(), json.dumps(metadata, sort_keys=True), record_id),
        )
    return get_memory_record(connection, record_id)


def create_memory_link(
    connection: sqlite3.Connection,
    *,
    from_kind: str,
    from_id: str,
    to_kind: str,
    to_id: str,
    link_type: str,
    confidence: float | None = None,
    notes: str | None = None,
) -> MemoryLink:
    init_curated_memory_schema(connection)
    with connection:
        connection.execute(
            """
            INSERT INTO memory_links (
                from_kind, from_id, to_kind, to_id, link_type, confidence, notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(from_kind, from_id, to_kind, to_id, link_type) DO UPDATE SET
                confidence = excluded.confidence,
                notes = excluded.notes
            """,
            (from_kind, from_id, to_kind, to_id, link_type, confidence, notes),
        )
    return MemoryLink(
        from_kind=from_kind,
        from_id=from_id,
        to_kind=to_kind,
        to_id=to_id,
        link_type=link_type,
        confidence=confidence,
        notes=notes,
    )


def list_memory_links(
    connection: sqlite3.Connection,
    *,
    from_kind: str | None = None,
    from_id: str | None = None,
    to_kind: str | None = None,
    to_id: str | None = None,
    link_type: str | None = None,
) -> list[MemoryLink]:
    init_curated_memory_schema(connection)
    filters = {
        "from_kind": from_kind,
        "from_id": from_id,
        "to_kind": to_kind,
        "to_id": to_id,
        "link_type": link_type,
    }
    where = [f"{key} = ?" for key, value in filters.items() if value is not None]
    params = [value for value in filters.values() if value is not None]
    where_clause = f"WHERE {' AND '.join(where)}" if where else ""
    rows = connection.execute(
        f"""
        SELECT from_kind, from_id, to_kind, to_id, link_type, confidence, notes
        FROM memory_links
        {where_clause}
        ORDER BY from_kind, from_id, link_type, to_kind, to_id
        """,
        params,
    ).fetchall()
    return [_link_from_row(row) for row in rows]


def record_type_options() -> tuple[str, ...]:
    return tuple(sorted(VALID_RECORD_TYPES))


def trust_level_options() -> tuple[str, ...]:
    return ("low", "medium", "high", "canonical")


def status_options() -> tuple[str, ...]:
    return ("active", "stale", "superseded", "archived", "deleted")


def _fetch_chunk_provenance(connection: sqlite3.Connection, chunk_id: str) -> dict[str, Any] | None:
    row = connection.execute(
        """
        SELECT
            message_chunks.id AS chunk_id,
            message_chunks.message_id,
            message_chunks.conversation_id,
            message_chunks.import_id,
            message_chunks.chunk_index,
            message_chunks.text,
            message_chunks.text_sha256,
            messages.role,
            messages.turn_index,
            conversations.title AS conversation_title
        FROM message_chunks
        JOIN messages ON messages.id = message_chunks.message_id
        JOIN conversations ON conversations.id = message_chunks.conversation_id
        WHERE message_chunks.id = ?
          AND message_chunks.is_deleted = 0
          AND messages.is_deleted = 0
          AND conversations.is_deleted = 0
        """,
        (chunk_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "chunk_id": row[0],
        "message_id": row[1],
        "conversation_id": row[2],
        "import_id": row[3],
        "chunk_index": row[4],
        "text": row[5],
        "text_sha256": row[6],
        "role": row[7],
        "turn_index": row[8],
        "conversation_title": row[9],
    }


def _record_from_row(row: sqlite3.Row | tuple[Any, ...]) -> MemoryRecord:
    return MemoryRecord(
        id=row[0],
        record_type=row[1],
        title=row[2],
        body=row[3],
        subject_id=row[4],
        trust_level=row[5],
        source_kind=row[6],
        source_ref=row[7],
        provenance=json.loads(row[8] or "{}"),
        status=row[9],
        valid_from=row[10],
        valid_to=row[11],
        last_verified_at=row[12],
        created_by=row[13],
        created_at=row[14],
        updated_at=row[15],
        metadata=json.loads(row[16] or "{}"),
    )


def _link_from_row(row: sqlite3.Row | tuple[Any, ...]) -> MemoryLink:
    return MemoryLink(
        from_kind=row[0],
        from_id=row[1],
        to_kind=row[2],
        to_id=row[3],
        link_type=row[4],
        confidence=row[5],
        notes=row[6],
    )


def _record_id(
    *,
    record_type: str,
    title: str,
    body: str,
    source_kind: str,
    source_ref: str | None,
) -> str:
    digest = hashlib.sha256(
        json.dumps(
            {
                "record_type": record_type,
                "title": title,
                "body": body,
                "source_kind": source_kind,
                "source_ref": source_ref,
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:16]
    return f"mem_{digest}"


def _validate_record_type(record_type: str) -> str:
    normalized = record_type.strip().lower().replace("-", "_")
    if normalized not in VALID_RECORD_TYPES:
        raise ValueError(f"invalid memory record type: {record_type}")
    return normalized


def _validate_trust_level(trust_level: str) -> str:
    normalized = trust_level.strip().lower().replace("-", "_")
    if normalized not in VALID_TRUST_LEVELS:
        raise ValueError(f"invalid memory trust level: {trust_level}")
    return normalized


def _validate_status(status: str) -> str:
    normalized = status.strip().lower().replace("-", "_")
    if normalized not in VALID_RECORD_STATUSES:
        raise ValueError(f"invalid memory record status: {status}")
    return normalized


def _required_text(value: str, field: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field} must not be empty")
    return cleaned
