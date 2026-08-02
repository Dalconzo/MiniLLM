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
SUBJECT_ALIASES = {
    "home-mcp": "Home Projects and Devices",
    "home-mcp-capabilities": "Home Projects and Devices",
    "memory-system": "AI Memory and Local LLMs",
    "ai-memory": "AI Memory and Local LLMs",
    "local-llm": "AI Memory and Local LLMs",
    "local-llms": "AI Memory and Local LLMs",
    "project-catalog": "Home Projects and Devices",
    "projects": "Home Projects and Devices",
    "health-supplements": "Health and Supplements",
    "recipes": "Recipes and Baking",
    "baking": "Recipes and Baking",
}


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
    explicit_conversation_count: int
    chunk_conversation_count: int
    message_count: int
    chunk_count: int
    latest_activity_at: str | None
    provenance_warnings: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.subject.to_dict(),
            "conversation_count": self.conversation_count,
            "explicit_conversation_count": self.explicit_conversation_count,
            "chunk_conversation_count": self.chunk_conversation_count,
            "message_count": self.message_count,
            "chunk_count": self.chunk_count,
            "latest_activity_at": self.latest_activity_at,
            "provenance_warnings": self.provenance_warnings,
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


def resolve_subject(
    connection: sqlite3.Connection,
    slug_or_name: str,
    *,
    kind: str = "subject",
) -> tuple[Subject, str | None]:
    """Resolve a subject input through exact slug/name and known aliases."""
    init_subject_schema(connection)
    normalized_kind = _normalize_kind(kind)
    requested_slug = normalize_subject_slug(slug_or_name)
    row = connection.execute(
        """
        SELECT id, slug, kind, name, description, created_at, updated_at, metadata_json
        FROM subjects
        WHERE kind = ? AND (slug = ? OR lower(name) = lower(?))
        """,
        (normalized_kind, requested_slug, slug_or_name.strip()),
    ).fetchone()
    if row is not None:
        return _subject_from_row(row), None

    alias_target = SUBJECT_ALIASES.get(requested_slug)
    if alias_target is None:
        raise KeyError(f"subject not found: {normalized_kind}/{requested_slug}")
    alias_slug = normalize_subject_slug(alias_target)
    row = connection.execute(
        """
        SELECT id, slug, kind, name, description, created_at, updated_at, metadata_json
        FROM subjects
        WHERE kind = ? AND slug = ?
        """,
        (normalized_kind, alias_slug),
    ).fetchone()
    if row is None:
        raise KeyError(f"subject alias target not found: {normalized_kind}/{requested_slug}->{alias_slug}")
    return _subject_from_row(row), alias_target


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
            (
                SELECT COUNT(DISTINCT conversation_id)
                FROM (
                    SELECT cs2.conversation_id AS conversation_id
                    FROM conversation_subjects cs2
                    JOIN conversations c2 ON c2.id = cs2.conversation_id AND c2.is_deleted = 0
                    WHERE cs2.subject_id = s.id
                    UNION
                    SELECT mc2.conversation_id AS conversation_id
                    FROM chunk_subjects chs2
                    JOIN message_chunks mc2 ON mc2.id = chs2.chunk_id AND mc2.is_deleted = 0
                    JOIN conversations c3 ON c3.id = mc2.conversation_id AND c3.is_deleted = 0
                    WHERE chs2.subject_id = s.id
                )
            ) AS conversation_count,
            (
                SELECT COUNT(DISTINCT cs3.conversation_id)
                FROM conversation_subjects cs3
                JOIN conversations c4 ON c4.id = cs3.conversation_id AND c4.is_deleted = 0
                WHERE cs3.subject_id = s.id
            ) AS explicit_conversation_count,
            (
                SELECT COUNT(DISTINCT mc3.conversation_id)
                FROM chunk_subjects chs3
                JOIN message_chunks mc3 ON mc3.id = chs3.chunk_id AND mc3.is_deleted = 0
                JOIN conversations c5 ON c5.id = mc3.conversation_id AND c5.is_deleted = 0
                WHERE chs3.subject_id = s.id
            ) AS chunk_conversation_count,
            (
                SELECT COUNT(DISTINCT mc4.message_id)
                FROM chunk_subjects chs4
                JOIN message_chunks mc4 ON mc4.id = chs4.chunk_id AND mc4.is_deleted = 0
                JOIN messages m4 ON m4.id = mc4.message_id AND m4.is_deleted = 0
                WHERE chs4.subject_id = s.id
            ) AS message_count,
            (
                SELECT COUNT(DISTINCT chs5.chunk_id)
                FROM chunk_subjects chs5
                JOIN message_chunks mc5 ON mc5.id = chs5.chunk_id AND mc5.is_deleted = 0
                WHERE chs5.subject_id = s.id
            ) AS chunk_count,
            (
                SELECT MAX(activity_at)
                FROM (
                    SELECT COALESCE(c6.updated_at, c6.last_message_at, c6.created_at) AS activity_at
                    FROM conversation_subjects cs6
                    JOIN conversations c6 ON c6.id = cs6.conversation_id AND c6.is_deleted = 0
                    WHERE cs6.subject_id = s.id
                    UNION ALL
                    SELECT COALESCE(c7.updated_at, c7.last_message_at, c7.created_at) AS activity_at
                    FROM chunk_subjects chs7
                    JOIN message_chunks mc7 ON mc7.id = chs7.chunk_id AND mc7.is_deleted = 0
                    JOIN conversations c7 ON c7.id = mc7.conversation_id AND c7.is_deleted = 0
                    WHERE chs7.subject_id = s.id
                )
            ) AS latest_activity_at
        FROM subjects s
        {where}
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
    conversation_count = int(row[8] or 0)
    explicit_conversation_count = int(row[9] or 0)
    chunk_conversation_count = int(row[10] or 0)
    message_count = int(row[11] or 0)
    chunk_count = int(row[12] or 0)
    return SubjectSummary(
        subject=_subject_from_row(row[:8]),
        conversation_count=conversation_count,
        explicit_conversation_count=explicit_conversation_count,
        chunk_conversation_count=chunk_conversation_count,
        message_count=message_count,
        chunk_count=chunk_count,
        latest_activity_at=row[13],
        provenance_warnings=_subject_provenance_warnings(
            conversation_count=conversation_count,
            explicit_conversation_count=explicit_conversation_count,
            chunk_conversation_count=chunk_conversation_count,
            message_count=message_count,
            chunk_count=chunk_count,
        ),
    )


def _subject_provenance_warnings(
    *,
    conversation_count: int,
    explicit_conversation_count: int,
    chunk_conversation_count: int,
    message_count: int,
    chunk_count: int,
) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    if chunk_count > 0 and conversation_count == 0:
        warnings.append(
            {
                "code": "chunk_subject_without_conversation_provenance",
                "message": "Subject has chunk assignments but no live source conversations.",
            }
        )
    if chunk_count > 0 and explicit_conversation_count == 0:
        warnings.append(
            {
                "code": "chunk_only_subject_assignment",
                "message": "Subject count comes from chunk-level assignments, not explicit conversation labels.",
            }
        )
    if conversation_count == 1 and chunk_count >= 500:
        warnings.append(
            {
                "code": "single_conversation_concentration",
                "message": "Subject has many chunks from one conversation; inspect for over-broad assignment.",
            }
        )
    if chunk_count > 0 and message_count == 0:
        warnings.append(
            {
                "code": "chunk_subject_without_message_provenance",
                "message": "Subject has chunk assignments that do not resolve to live messages.",
            }
        )
    return warnings


def _assignment_from_row(row: sqlite3.Row | tuple[Any, ...]) -> dict[str, Any]:
    return {
        "subject": _subject_from_row(row[:8]).to_dict(),
        "confidence": row[8],
        "source": row[9],
        "assigned_at": row[10],
        "notes": row[11],
    }
