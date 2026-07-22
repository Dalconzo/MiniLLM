from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

from .audit import blocked_source_ids, init_audit_schema
from .curated import init_curated_memory_schema
from .domain_scoping import (
    apply_governance_policy,
    classify_text_domains,
    detect_query_domains,
    dominant_domain,
    high_risk_domains,
    high_risk_lenses,
    scope_candidate_domains,
    select_lenses_for_query,
)
from .observability import MemoryObservationError, memory_db_path
from .privacy import redact_obvious_secrets
from .ranking import rank_memory_hits
from .subjects import normalize_subject_slug


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
    effort: int = 2,
    allow_cross_domain: bool = False,
) -> dict[str, Any]:
    sqlite_path = memory_db_path(memory_dir)
    if not sqlite_path.exists():
        raise MemoryObservationError(
            f"ChatGPT memory database does not exist: {sqlite_path}",
            stage="retrieve_candidates",
            error_code="memory_database_not_found",
            source_ref=str(sqlite_path),
        )

    fts_queries = _fts_queries(query)
    if not fts_queries:
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
        query_domains = detect_query_domains(query)
        where = [
            "chatgpt_chunks_fts MATCH ?",
            "message_chunks.is_deleted = 0",
            "messages.is_deleted = 0",
            "conversations.is_deleted = 0",
        ]
        params: list[Any] = [fts_queries[0][1]]
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
                where.append(
                    """
                    (
                        EXISTS (
                            SELECT 1
                            FROM chunk_subjects
                            JOIN subjects ON subjects.id = chunk_subjects.subject_id
                            WHERE chunk_subjects.chunk_id = message_chunks.id
                              AND (subjects.slug = ? OR subjects.name LIKE ?)
                        )
                        OR EXISTS (
                            SELECT 1
                            FROM conversation_subjects
                            JOIN subjects ON subjects.id = conversation_subjects.subject_id
                            WHERE conversation_subjects.conversation_id = conversations.id
                              AND (subjects.slug = ? OR subjects.name LIKE ?)
                        )
                    )
                    """
                )
                subject_slug = normalize_subject_slug(subject)
                params.extend([subject_slug, f"%{subject}%", subject_slug, f"%{subject}%"])
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
                subject_slugs = [normalize_subject_slug(item) for item in exclude_subjects]
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
                    AND NOT EXISTS (
                        SELECT 1
                        FROM conversation_subjects excluded_conversation_subjects
                        JOIN subjects excluded_subjects
                          ON excluded_subjects.id = excluded_conversation_subjects.subject_id
                        WHERE excluded_conversation_subjects.conversation_id = conversations.id
                          AND excluded_subjects.slug IN ({placeholders})
                    )
                    """
                )
                params.extend(subject_slugs)
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
            JOIN message_chunks ON message_chunks.id = chatgpt_chunks_fts.chunk_id
            JOIN messages ON messages.id = chatgpt_chunks_fts.message_id
            JOIN conversations ON conversations.id = chatgpt_chunks_fts.conversation_id
            WHERE {' AND '.join(where)}
            ORDER BY bm25_score ASC
            LIMIT ?
        """
        rows = []
        fts_strategy = fts_queries[0][0]
        for strategy, candidate_fts_query in fts_queries:
            candidate_params = list(params)
            candidate_params[0] = candidate_fts_query
            rows = connection.execute(sql, [*candidate_params, limit]).fetchall()
            fts_strategy = strategy
            if rows:
                break
        if fts_strategy != "precise_all_terms":
            filters_applied.append({"field": "fts_strategy", "value": fts_strategy})
        fts_results = [_row_to_hit(index, row) for index, row in enumerate(rows, start=1)]
        curated_results = _curated_hits(
            connection,
            query=query,
            subject=subject,
            exclude_source_ids=excluded_ids,
            exclude_subjects=exclude_subjects,
            title=title,
            date_from=date_from,
            date_to=date_to,
            limit=limit,
        )

        scoped_results = []
        for hit in [*fts_results, *curated_results]:
            hit_domains = classify_text_domains(
                hit.get("title"),
                hit.get("snippet"),
                hit.get("role"),
            )
            allowed, relation = scope_candidate_domains(
                query_domains,
                hit_domains,
                effort=effort,
                allow_cross_domain=allow_cross_domain,
            )
            if not allowed:
                continue
            governance_allowed, governance_reason, governance_labels = apply_governance_policy(
                query_domains,
                hit_domains,
                effort=effort,
                allow_cross_domain=allow_cross_domain,
                candidate_status=str(hit.get("status") or "") or None,
                candidate_trust_level=str(hit.get("trust_level") or "") or None,
                source_role=str(hit.get("source_role") or hit.get("role") or "") or None,
                domain_relation=relation,
            )
            if not governance_allowed:
                continue
            scoped_hit = dict(hit)
            scoped_hit["domains"] = hit_domains
            scoped_hit["domain_primary"] = dominant_domain(hit_domains)
            scoped_hit["domain_relation"] = relation
            scoped_hit["domain_reason"] = "query_domain_match" if relation in {"primary", "broad"} else relation
            scoped_hit["governance_reason"] = governance_reason
            scoped_hit["governance_labels"] = governance_labels
            scoped_results.append(scoped_hit)

        effective_depth = _effort_depth_cap(depth, effort)
        results = rank_memory_hits(scoped_results, depth=effective_depth)[:limit]
        results = [_project_hit_for_depth(result) for result in results]
        lenses = select_lenses_for_query(query_domains, effort)
        results = [_attach_effort_checks(result, effort=effort, query=query, query_domains=query_domains) for result in results]
        return {
            "status": "ok",
            "query": query,
            "ranking_profile": "hybrid_memory_v1",
            "domain_detection": {
                "primary_domain": dominant_domain(query_domains),
                "domains": query_domains,
                "effort": effort,
                "allow_cross_domain": allow_cross_domain,
                "policy": "heuristic_domain_scoping_v1",
            },
            "lenses": lenses,
            "governance": {
                "high_risk": bool(high_risk_domains(query_domains)),
                "domains": high_risk_domains(query_domains),
                "labels": high_risk_lenses(query_domains),
                "policy": "conservative_high_risk_v1" if high_risk_domains(query_domains) else "standard_v1",
            },
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
    source_role = row["role"]
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
        "title": redact_obvious_secrets(row["title"] or "").text,
        "role": source_role,
        "source_role": source_role,
        "epistemic_status": "assistant_suggested" if source_role == "assistant" else "user_reported",
        "confidence_basis": "assistant_suggestion_only" if source_role == "assistant" else "single_user_statement",
        "status": None,
        "trust_level": None,
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
    exclude_subjects: list[str] | None,
    title: str | None,
    date_from: str | None,
    date_to: str | None,
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
    if title:
        where.append("memory_records.title LIKE ?")
        params.append(f"%{title}%")
    if date_from:
        where.append("COALESCE(memory_records.valid_from, memory_records.updated_at, memory_records.created_at, '') >= ?")
        params.append(date_from)
    if date_to:
        where.append("COALESCE(memory_records.valid_from, memory_records.updated_at, memory_records.created_at, '') <= ?")
        params.append(date_to)
    if exclude_source_ids:
        placeholders = ", ".join("?" for _ in exclude_source_ids)
        where.append(f"memory_records.id NOT IN ({placeholders})")
        params.extend(exclude_source_ids)
        where.append(f"(memory_records.source_ref IS NULL OR memory_records.source_ref NOT IN ({placeholders}))")
        params.extend(exclude_source_ids)
        where.append(
            f"""
            NOT EXISTS (
                SELECT 1
                FROM memory_links blocked_links
                WHERE blocked_links.from_kind = 'memory_record'
                  AND blocked_links.from_id = memory_records.id
                  AND blocked_links.to_id IN ({placeholders})
            )
            """
        )
        params.extend(exclude_source_ids)
    if subject and _table_exists(connection, "subjects"):
        where.append(
            """
            memory_records.subject_id IN (
                SELECT id FROM subjects WHERE slug = ? OR name LIKE ?
            )
            """
        )
        params.extend([normalize_subject_slug(subject), f"%{subject}%"])
    if exclude_subjects and _table_exists(connection, "subjects"):
        subject_slugs = [normalize_subject_slug(item) for item in exclude_subjects]
        placeholders = ", ".join("?" for _ in subject_slugs)
        where.append(
            f"""
            (memory_records.subject_id IS NULL OR memory_records.subject_id NOT IN (
                SELECT id FROM subjects WHERE slug IN ({placeholders})
            ))
            """
        )
        params.extend(subject_slugs)

    rows = connection.execute(
        f"""
        SELECT
            id, record_type, title, body, subject_id, trust_level,
            source_kind, source_ref, status, updated_at
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
                "title": redact_obvious_secrets(str(row[2]) or "").text,
                "role": row[1],
                "source_role": None,
                "epistemic_status": "confirmed" if trust_level in {"canonical", "high"} else "user_reported",
                    "confidence_basis": "multiple_sources_agree" if trust_level == "canonical" else "single_user_statement",
                    "status": row[8],
                    "trust_level": trust_level,
                    "turn_index": None,
                    "chunk_index": None,
                    "message_created_at": row[9],
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


def _project_hit_for_depth(hit: dict[str, Any]) -> dict[str, Any]:
    projected = dict(hit)
    exposed_fields = set(projected.get("exposed_fields", []))
    if "snippet" not in exposed_fields:
        projected.pop("snippet", None)
    if "role" not in exposed_fields:
        projected.pop("role", None)
    if "chunk_text" not in exposed_fields:
        projected.pop("chunk_text", None)
    if "nearby_turn_refs" not in exposed_fields:
        projected.pop("nearby_turn_refs", None)
    if "conversation_window" not in exposed_fields:
        projected.pop("conversation_window", None)
    if "related_curated_memories" not in exposed_fields:
        projected.pop("related_curated_memories", None)
    projected["title"] = redact_obvious_secrets(str(projected.get("title") or "")).text
    return projected


def _attach_effort_checks(hit: dict[str, Any], *, effort: int, query: str, query_domains: list[str]) -> dict[str, Any]:
    projected = dict(hit)
    validation_checks = {
        "temporal": {"status": "not_run" if effort < 4 else _temporal_status(projected)},
        "contradiction": {"status": "not_run" if effort < 4 else _contradiction_status(projected, query, query_domains)},
    }
    projected["validation_checks"] = validation_checks
    projected["effort"] = effort
    return projected


FTS_STOP_WORDS = {
    "a",
    "about",
    "after",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "before",
    "do",
    "for",
    "from",
    "how",
    "i",
    "in",
    "is",
    "it",
    "know",
    "me",
    "my",
    "of",
    "on",
    "or",
    "should",
    "that",
    "the",
    "this",
    "to",
    "what",
    "when",
    "where",
    "with",
}


def _fts_queries(query: str) -> list[tuple[str, str]]:
    tokens = _search_tokens(query)
    if not tokens:
        return []
    precise = " ".join(_quoted_fts_token(token) for token in tokens)
    if len(tokens) == 1:
        return [("precise_all_terms", precise)]
    broad = " OR ".join(_quoted_fts_token(token) for token in tokens)
    return [("precise_all_terms", precise), ("broad_any_terms", broad)]


def _search_tokens(query: str) -> list[str]:
    tokens = re.findall(r"[A-Za-z0-9_][A-Za-z0-9_-]*", query)
    filtered = [token for token in tokens if token.lower() not in FTS_STOP_WORDS]
    return filtered or tokens


def _quoted_fts_token(token: str) -> str:
    return f'"{token.replace(chr(34), chr(34) + chr(34))}"'


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'virtual table') AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _slug(value: str) -> str:
    return normalize_subject_slug(value)


def _effort_depth_cap(depth: str, effort: int) -> str:
    effort_depth = {
        1: "far",
        2: "medium",
        3: "close",
    }.get(effort, "full")
    tiers = ("far", "medium", "close", "full")
    if depth not in tiers:
        raise ValueError(f"depth must be one of: {', '.join(tiers)}")
    return tiers[min(tiers.index(depth), tiers.index(effort_depth))]


def _temporal_status(hit: dict[str, Any]) -> str:
    status = str(hit.get("status") or "").lower()
    if status in {"stale", "superseded", "archived", "deleted"}:
        return status
    valid_to = hit.get("valid_to")
    if valid_to:
        return "stale"
    return "current"


def _contradiction_status(hit: dict[str, Any], query: str, query_domains: list[str]) -> str:
    text = " ".join(str(hit.get(field) or "") for field in ("title", "snippet", "role")).lower()
    query_text = query.lower()
    if any(token in text for token in ("not ", "never", "instead", "avoid", "failed", "wrong", "contradict")) and any(
        token in query_text for token in ("not", "avoid", "should", "wrong", "fail")
    ):
        return "possible"
    if query_domains and hit.get("domain_relation") in {"transfer", "analogy"}:
        return "review"
    return "not_detected"
