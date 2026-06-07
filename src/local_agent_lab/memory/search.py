from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

from .audit import blocked_source_ids, init_audit_schema
from .curated import init_curated_memory_schema
from .observability import MemoryObservationError, memory_db_path
from .privacy import redact_obvious_secrets
from .ranking import rank_memory_hits


def search_chatgpt_memory(
    *,
    memory_dir: Path,
    query: str,
    limit: int = 8,
    subject: str | None = None,
    title: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    exclude_source_ids: list[str] | None = None,
    exclude_subjects: list[str] | None = None,
    depth: str = "medium",
) -> dict[str, Any]:
    sqlite_path = memory_db_path(memory_dir)
    if not sqlite_path.exists():
        raise MemoryObservationError(
            f"ChatGPT memory database does not exist: {sqlite_path}",
            stage="retrieve_candidates",
            error_code="memory_database_not_found",
            source_ref=str(sqlite_path),
        )

    fts_query = _fts_query(query)
    if not fts_query:
        raise MemoryObservationError(
            "memory search query did not contain searchable terms",
            stage="retrieve_candidates",
            error_code="empty_search_query",
            source_ref=None,
        )

    with sqlite3.connect(sqlite_path) as connection:
        connection.row_factory = sqlite3.Row
        init_audit_schema(connection)
        filters_applied: list[dict[str, Any]] = []
        where = [
            "chatgpt_chunks_fts MATCH ?",
            "message_chunks.is_deleted = 0",
            "messages.is_deleted = 0",
            "conversations.is_deleted = 0",
        ]
        params: list[Any] = [fts_query]
        excluded_ids = sorted(set(exclude_source_ids or []) | blocked_source_ids(connection))
        if excluded_ids:
            placeholders = ", ".join("?" for _ in excluded_ids)
            where.append(f"message_chunks.id NOT IN ({placeholders})")
            where.append(f"messages.id NOT IN ({placeholders})")
            where.append(f"conversations.id NOT IN ({placeholders})")
            params.extend(excluded_ids)
            params.extend(excluded_ids)
            params.extend(excluded_ids)
            filters_applied.append({"field": "exclude_source", "value": excluded_ids})
        joins = [
            "JOIN message_chunks ON message_chunks.id = chatgpt_chunks_fts.chunk_id",
            "JOIN messages ON messages.id = chatgpt_chunks_fts.message_id",
            "JOIN conversations ON conversations.id = chatgpt_chunks_fts.conversation_id",
        ]

        if title:
            where.append("conversations.title LIKE ?")
            params.append(f"%{title}%")
            filters_applied.append({"field": "title", "value": title})
        if date_from:
            where.append("COALESCE(messages.created_at, conversations.created_at, '') >= ?")
            params.append(date_from)
            filters_applied.append({"field": "date_from", "value": date_from})
        if date_to:
            where.append("COALESCE(messages.created_at, conversations.created_at, '') <= ?")
            params.append(date_to)
            filters_applied.append({"field": "date_to", "value": date_to})
        if subject:
            if _table_exists(connection, "subjects") and _table_exists(connection, "chunk_subjects"):
                joins.extend(
                    [
                        "JOIN chunk_subjects ON chunk_subjects.chunk_id = message_chunks.id",
                        "JOIN subjects ON subjects.id = chunk_subjects.subject_id",
                    ]
                )
                where.append("(subjects.slug = ? OR subjects.name LIKE ?)")
                params.extend([_slug(subject), f"%{subject}%"])
                filters_applied.append({"field": "subject", "value": subject})
            else:
                return _empty_result(
                    query=query,
                    ranking_profile="fts_bm25_v1",
                    filters_applied=[
                        *filters_applied,
                        {
                            "field": "subject",
                            "value": subject,
                            "warning": "subject tables do not exist yet",
                        },
                    ],
                )
        if exclude_subjects:
            if _table_exists(connection, "subjects") and _table_exists(connection, "chunk_subjects"):
                subject_slugs = [_slug(item) for item in exclude_subjects]
                placeholders = ", ".join("?" for _ in subject_slugs)
                where.append(
                    f"""
                    NOT EXISTS (
                        SELECT 1
                        FROM chunk_subjects excluded_chunk_subjects
                        JOIN subjects excluded_subjects
                          ON excluded_subjects.id = excluded_chunk_subjects.subject_id
                        WHERE excluded_chunk_subjects.chunk_id = message_chunks.id
                          AND excluded_subjects.slug IN ({placeholders})
                    )
                    """
                )
                params.extend(subject_slugs)
            filters_applied.append({"field": "exclude_subject", "value": exclude_subjects})

        sql = f"""
            SELECT
                conversations.id AS conversation_id,
                conversations.source_conversation_id,
                conversations.title,
                messages.id AS message_id,
                messages.role,
                messages.turn_index,
                messages.created_at AS message_created_at,
                message_chunks.id AS chunk_id,
                message_chunks.chunk_index,
                message_chunks.source_kind,
                bm25(chatgpt_chunks_fts) AS bm25_score,
                snippet(chatgpt_chunks_fts, 2, '[', ']', '...', 24) AS snippet
            FROM chatgpt_chunks_fts
            {' '.join(joins)}
            WHERE {' AND '.join(where)}
            ORDER BY bm25_score ASC
            LIMIT ?
        """
        params.append(limit)
        rows = connection.execute(sql, params).fetchall()
        fts_results = [_row_to_hit(index, row) for index, row in enumerate(rows, start=1)]
        curated_results = _curated_hits(
            connection,
            query=query,
            subject=subject,
            exclude_source_ids=excluded_ids,
            limit=limit,
        )

    results = rank_memory_hits([*fts_results, *curated_results], depth=depth)[:limit]
    return {
        "status": "ok",
        "query": query,
        "ranking_profile": "hybrid_memory_v1",
        "candidate_counts": {
            "fts": len(fts_results),
            "vector": 0,
            "curated": len(curated_results),
            "after_filters": len(results),
        },
        "filters_applied": filters_applied,
        "results": results,
        "count": len(results),
    }


def _row_to_hit(rank: int, row: sqlite3.Row) -> dict[str, Any]:
    score = float(row["bm25_score"])
    redacted = redact_obvious_secrets(row["snippet"] or "")
    return {
        "rank": rank,
        "score": score,
        "score_breakdown": {
            "fts_bm25": score,
            "semantic_similarity": None,
            "source_trust": None,
            "personal_feedback": None,
        },
        "source_kind": row["source_kind"],
        "disclosure_tier": "medium",
        "exposed_fields": ["title", "role", "snippet", "message_created_at", "ids"],
        "conversation_id": row["conversation_id"],
        "source_conversation_id": row["source_conversation_id"],
        "message_id": row["message_id"],
        "chunk_id": row["chunk_id"],
        "title": row["title"],
        "role": row["role"],
        "turn_index": row["turn_index"],
        "chunk_index": row["chunk_index"],
        "message_created_at": row["message_created_at"],
        "snippet": redacted.text,
        "redacted_secret_count": redacted.redacted_count,
    }


def _curated_hits(
    connection: sqlite3.Connection,
    *,
    query: str,
    subject: str | None,
    exclude_source_ids: list[str],
    limit: int,
) -> list[dict[str, Any]]:
    if not _table_exists(connection, "memory_records"):
        return []
    init_curated_memory_schema(connection)
    tokens = re.findall(r"[A-Za-z0-9_][A-Za-z0-9_-]*", query)
    if not tokens:
        return []

    where = ["memory_records.status = 'active'"]
    params: list[Any] = []
    token_clauses = []
    for token in tokens:
        token_clauses.append("(memory_records.title LIKE ? OR memory_records.body LIKE ?)")
        params.extend([f"%{token}%", f"%{token}%"])
    where.append(f"({' OR '.join(token_clauses)})")
    if exclude_source_ids:
        placeholders = ", ".join("?" for _ in exclude_source_ids)
        where.append(f"memory_records.id NOT IN ({placeholders})")
        params.extend(exclude_source_ids)
    if subject and _table_exists(connection, "subjects"):
        where.append(
            """
            memory_records.subject_id IN (
                SELECT id FROM subjects WHERE slug = ? OR name LIKE ?
            )
            """
        )
        params.extend([_slug(subject), f"%{subject}%"])

    rows = connection.execute(
        f"""
        SELECT
            id, record_type, title, body, subject_id, trust_level,
            source_kind, source_ref, updated_at
        FROM memory_records
        WHERE {' AND '.join(where)}
        ORDER BY updated_at DESC, title COLLATE NOCASE ASC
        LIMIT ?
        """,
        [*params, limit],
    ).fetchall()

    trust_scores = {"canonical": 1.0, "high": 0.8, "medium": 0.55, "low": 0.25}
    hits: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        redacted = redact_obvious_secrets(str(row[3])[:280])
        trust_level = str(row[5])
        hits.append(
            {
                "rank": index,
                "score": trust_scores.get(trust_level, 0.5),
                "score_breakdown": {"fts_bm25": None},
                "keyword_relevance": 0.75,
                "source_kind": "curated_memory",
                "disclosure_tier": "medium",
                "exposed_fields": ["title", "record_type", "snippet", "ids"],
                "conversation_id": None,
                "source_conversation_id": None,
                "message_id": None,
                "chunk_id": row[0],
                "title": row[2],
                "role": row[1],
                "turn_index": None,
                "chunk_index": None,
                "message_created_at": row[8],
                "snippet": redacted.text,
                "redacted_secret_count": redacted.redacted_count,
                "curated_trust": trust_scores.get(trust_level, 0.5),
            }
        )
    return hits


def _empty_result(*, query: str, ranking_profile: str, filters_applied: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "status": "ok",
        "query": query,
        "ranking_profile": ranking_profile,
        "candidate_counts": {
            "fts": 0,
            "vector": 0,
            "curated": 0,
            "after_filters": 0,
        },
        "filters_applied": filters_applied,
        "results": [],
        "count": 0,
    }


def _fts_query(query: str) -> str:
    tokens = re.findall(r"[A-Za-z0-9_][A-Za-z0-9_-]*", query)
    return " ".join(f'"{token.replace(chr(34), chr(34) + chr(34))}"' for token in tokens)


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'virtual table') AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _slug(value: str) -> str:
    return "-".join(re.findall(r"[a-z0-9]+", value.lower()))
