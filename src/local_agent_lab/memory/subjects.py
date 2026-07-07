from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable

from .ontology import validate_subject_kind
from .observability import utc_now


SUBJECT_SCHEMA_VERSION = 2


@dataclass(frozen=True)
class Subject:
    id: str
    slug: str
    kind: str
    name: str
    description: str | None
    created_at: str
    updated_at: str
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "slug": self.slug,
            "kind": self.kind,
            "name": self.name,
            "description": self.description,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class SubjectSummary:
    subject: Subject
    conversation_count: int
    chunk_count: int
    latest_activity_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.subject.to_dict(),
            "conversation_count": self.conversation_count,
            "chunk_count": self.chunk_count,
            "latest_activity_at": self.latest_activity_at,
        }


def init_subject_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL,
            checksum TEXT NOT NULL
        );

        INSERT OR IGNORE INTO schema_migrations (version, name, applied_at, checksum)
        VALUES (2, 'chatgpt_memory_subjects', datetime('now'), 'subjects_v1');

        CREATE TABLE IF NOT EXISTS subjects (
            id TEXT PRIMARY KEY,
            slug TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('subject', 'project', 'workflow')),
            name TEXT NOT NULL,
            description TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(kind, slug)
        );

        CREATE TABLE IF NOT EXISTS conversation_subjects (
            conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            subject_id TEXT NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
            confidence REAL NOT NULL DEFAULT 1.0,
            source TEXT NOT NULL DEFAULT 'manual',
            assigned_at TEXT NOT NULL,
            notes TEXT,
            PRIMARY KEY (conversation_id, subject_id)
        );

        CREATE TABLE IF NOT EXISTS chunk_subjects (
            chunk_id TEXT NOT NULL REFERENCES message_chunks(id) ON DELETE CASCADE,
            subject_id TEXT NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
            confidence REAL NOT NULL DEFAULT 1.0,
            source TEXT NOT NULL DEFAULT 'manual',
            assigned_at TEXT NOT NULL,
            notes TEXT,
            PRIMARY KEY (chunk_id, subject_id)
        );

        CREATE INDEX IF NOT EXISTS idx_subjects_kind_slug ON subjects(kind, slug);
        CREATE INDEX IF NOT EXISTS idx_conversation_subjects_subject ON conversation_subjects(subject_id);
        CREATE INDEX IF NOT EXISTS idx_conversation_subjects_conversation ON conversation_subjects(conversation_id);
        CREATE INDEX IF NOT EXISTS idx_chunk_subjects_subject ON chunk_subjects(subject_id);
        CREATE INDEX IF NOT EXISTS idx_chunk_subjects_chunk ON chunk_subjects(chunk_id);
        """
    )
    connection.commit()


def normalize_subject_slug(value: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    return slug or "untitled"


def upsert_subject(
    connection: sqlite3.Connection,
    name: str,
    *,
    kind: str = "subject",
    slug: str | None = None,
    description: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Subject:
    init_subject_schema(connection)
    normalized_kind = _normalize_kind(kind)
    normalized_slug = normalize_subject_slug(slug or name)
    subject_id = _subject_id(normalized_kind, normalized_slug)
    now = utc_now()
    metadata_json = json.dumps(metadata or {}, sort_keys=True)

    with connection:
        connection.execute(
            """
            INSERT INTO subjects (
                id, slug, kind, name, description, created_at, updated_at, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(kind, slug) DO UPDATE SET
                name = excluded.name,
                description = COALESCE(excluded.description, subjects.description),
                updated_at = excluded.updated_at,
                metadata_json = excluded.metadata_json
            """,
            (
                subject_id,
                normalized_slug,
                normalized_kind,
                name.strip() or normalized_slug,
                description,
                now,
                now,
                metadata_json,
            ),
        )

    return get_subject(connection, normalized_slug, kind=normalized_kind)


def get_subject(connection: sqlite3.Connection, slug_or_name: str, *, kind: str = "subject") -> Subject:
    init_subject_schema(connection)
    normalized_kind = _normalize_kind(kind)
    slug = normalize_subject_slug(slug_or_name)
    row = connection.execute(
        """
        SELECT id, slug, kind, name, description, created_at, updated_at, metadata_json
        FROM subjects
        WHERE kind = ? AND slug = ?
        """,
        (normalized_kind, slug),
    ).fetchone()
    if row is None:
        raise KeyError(f"subject not found: {normalized_kind}/{slug}")
    return _subject_from_row(row)


def assign_conversation_subject(
    connection: sqlite3.Connection,
    conversation_id: str,
    subject_name: str,
    *,
    kind: str = "subject",
    confidence: float = 1.0,
    source: str = "manual",
    notes: str | None = None,
    include_chunks: bool = False,
) -> Subject:
    subject = upsert_subject(connection, subject_name, kind=kind)
    now = utc_now()
    with connection:
        connection.execute(
            """
            INSERT INTO conversation_subjects (
                conversation_id, subject_id, confidence, source, assigned_at, notes
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(conversation_id, subject_id) DO UPDATE SET
                confidence = excluded.confidence,
                source = excluded.source,
                assigned_at = excluded.assigned_at,
                notes = excluded.notes
            """,
            (conversation_id, subject.id, confidence, source, now, notes),
        )
        if include_chunks:
            chunk_ids = [
                row[0]
                for row in connection.execute(
                    "SELECT id FROM message_chunks WHERE conversation_id = ? AND is_deleted = 0",
                    (conversation_id,),
                ).fetchall()
            ]
            _assign_chunks(connection, chunk_ids, subject.id, confidence=confidence, source=source, notes=notes, assigned_at=now)
    return subject


def assign_chunk_subject(
    connection: sqlite3.Connection,
    chunk_id: str,
    subject_name: str,
    *,
    kind: str = "subject",
    confidence: float = 1.0,
    source: str = "manual",
    notes: str | None = None,
) -> Subject:
    subject = upsert_subject(connection, subject_name, kind=kind)
    with connection:
        _assign_chunks(
            connection,
            [chunk_id],
            subject.id,
            confidence=confidence,
            source=source,
            notes=notes,
            assigned_at=utc_now(),
        )
    return subject


def remove_conversation_subject(
    connection: sqlite3.Connection,
    conversation_id: str,
    subject_name: str,
    *,
    kind: str = "subject",
    remove_chunk_assignments: bool = False,
) -> int:
    subject = get_subject(connection, subject_name, kind=kind)
    with connection:
        cursor = connection.execute(
            "DELETE FROM conversation_subjects WHERE conversation_id = ? AND subject_id = ?",
            (conversation_id, subject.id),
        )
        deleted = cursor.rowcount
        if remove_chunk_assignments:
            connection.execute(
                """
                DELETE FROM chunk_subjects
                WHERE subject_id = ?
                  AND chunk_id IN (SELECT id FROM message_chunks WHERE conversation_id = ?)
                """,
                (subject.id, conversation_id),
            )
    return deleted


def list_subjects(
    connection: sqlite3.Connection,
    *,
    kind: str | None = None,
    limit: int | None = None,
) -> list[SubjectSummary]:
    init_subject_schema(connection)
    params: list[Any] = []
    where = ""
    if kind is not None:
        where = "WHERE s.kind = ?"
        params.append(_normalize_kind(kind))

    limit_clause = ""
    if limit is not None:
        if limit < 1:
            return []
        limit_clause = "LIMIT ?"
        params.append(limit)

    rows = connection.execute(
        f"""
        SELECT
            s.id,
            s.slug,
            s.kind,
            s.name,
            s.description,
            s.created_at,
            s.updated_at,
            s.metadata_json,
            COUNT(DISTINCT cs.conversation_id) AS conversation_count,
            COUNT(DISTINCT chs.chunk_id) AS chunk_count,
            MAX(
                COALESCE(c.updated_at, c.last_message_at, c.created_at),
                COALESCE(mc_conv.updated_at, mc_conv.last_message_at, mc_conv.created_at)
            ) AS latest_activity_at
        FROM subjects s
        LEFT JOIN conversation_subjects cs ON cs.subject_id = s.id
        LEFT JOIN conversations c ON c.id = cs.conversation_id AND c.is_deleted = 0
        LEFT JOIN chunk_subjects chs ON chs.subject_id = s.id
        LEFT JOIN message_chunks mc ON mc.id = chs.chunk_id AND mc.is_deleted = 0
        LEFT JOIN conversations mc_conv ON mc_conv.id = mc.conversation_id AND mc_conv.is_deleted = 0
        {where}
        GROUP BY s.id
        ORDER BY latest_activity_at DESC NULLS LAST, s.kind ASC, s.name COLLATE NOCASE ASC
        {limit_clause}
        """,
        params,
    ).fetchall()
    return [_summary_from_row(row) for row in rows]


def list_conversation_subjects(connection: sqlite3.Connection, conversation_id: str) -> list[dict[str, Any]]:
    init_subject_schema(connection)
    rows = connection.execute(
        """
        SELECT
            s.id, s.slug, s.kind, s.name, s.description, s.created_at, s.updated_at, s.metadata_json,
            cs.confidence, cs.source, cs.assigned_at, cs.notes
        FROM conversation_subjects cs
        JOIN subjects s ON s.id = cs.subject_id
        WHERE cs.conversation_id = ?
        ORDER BY s.kind ASC, s.name COLLATE NOCASE ASC
        """,
        (conversation_id,),
    ).fetchall()
    return [_assignment_from_row(row) for row in rows]


def list_subject_conversations(
    connection: sqlite3.Connection,
    subject_name: str,
    *,
    kind: str = "subject",
    limit: int | None = None,
) -> list[dict[str, Any]]:
    init_subject_schema(connection)
    subject = get_subject(connection, subject_name, kind=kind)
    params: list[Any] = [subject.id]
    limit_clause = ""
    if limit is not None:
        if limit < 1:
            return []
        limit_clause = "LIMIT ?"
        params.append(limit)

    rows = connection.execute(
        f"""
        SELECT
            c.id, c.title, c.created_at, c.updated_at, c.message_count,
            cs.confidence, cs.source, cs.assigned_at, cs.notes
        FROM conversation_subjects cs
        JOIN conversations c ON c.id = cs.conversation_id
        WHERE cs.subject_id = ? AND c.is_deleted = 0
        ORDER BY COALESCE(c.updated_at, c.last_message_at, c.created_at) DESC NULLS LAST,
                 c.title COLLATE NOCASE ASC
        {limit_clause}
        """,
        params,
    ).fetchall()
    return [
        {
            "id": row[0],
            "title": row[1],
            "created_at": row[2],
            "updated_at": row[3],
            "message_count": row[4],
            "confidence": row[5],
            "source": row[6],
            "assigned_at": row[7],
            "notes": row[8],
        }
        for row in rows
    ]


def _assign_chunks(
    connection: sqlite3.Connection,
    chunk_ids: Iterable[str],
    subject_id: str,
    *,
    confidence: float,
    source: str,
    notes: str | None,
    assigned_at: str,
) -> None:
    connection.executemany(
        """
        INSERT INTO chunk_subjects (chunk_id, subject_id, confidence, source, assigned_at, notes)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(chunk_id, subject_id) DO UPDATE SET
            confidence = excluded.confidence,
            source = excluded.source,
            assigned_at = excluded.assigned_at,
            notes = excluded.notes
        """,
        [(chunk_id, subject_id, confidence, source, assigned_at, notes) for chunk_id in chunk_ids],
    )


def _normalize_kind(kind: str) -> str:
    return validate_subject_kind(kind)


def _subject_id(kind: str, slug: str) -> str:
    return f"{kind}_{slug}"


def _subject_from_row(row: sqlite3.Row | tuple[Any, ...]) -> Subject:
    return Subject(
        id=row[0],
        slug=row[1],
        kind=row[2],
        name=row[3],
        description=row[4],
        created_at=row[5],
        updated_at=row[6],
        metadata=json.loads(row[7] or "{}"),
    )


def _summary_from_row(row: sqlite3.Row | tuple[Any, ...]) -> SubjectSummary:
    return SubjectSummary(
        subject=_subject_from_row(row[:8]),
        conversation_count=int(row[8] or 0),
        chunk_count=int(row[9] or 0),
        latest_activity_at=row[10],
    )


def _assignment_from_row(row: sqlite3.Row | tuple[Any, ...]) -> dict[str, Any]:
    return {
        "subject": _subject_from_row(row[:8]).to_dict(),
        "confidence": row[8],
        "source": row[9],
        "assigned_at": row[10],
        "notes": row[11],
    }
