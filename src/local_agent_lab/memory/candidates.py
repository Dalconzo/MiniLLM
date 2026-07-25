from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

from .observability import utc_now
from .ontology import validate_subject_kind
from .subjects import init_subject_schema, normalize_subject_slug

if TYPE_CHECKING:
    from .chatgpt_ingest import ParsedExport


CANDIDATE_SCHEMA_VERSION = 5
VALID_REVIEW_STATUSES = ("pending", "approved", "rejected", "merged")
VALID_QUALITY_FILTERS = ("all", "user_only", "high_signal")


@dataclass(frozen=True)
class CandidateMemory:
    id: str
    content: str
    memory_type: str
    reason_type: str
    domains: list[str]
    confidence: float
    valid_from: str | None
    valid_to: str | None
    last_confirmed_at: str | None
    source_kind: str
    source_ref: str
    source_links: dict[str, Any]
    review_status: str
    review_notes: str | None
    origin: str
    assistant_suggestion: bool
    created_at: str
    updated_at: str
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "content": self.content,
            "memory_type": self.memory_type,
            "reason_type": self.reason_type,
            "domain_primary": self.domain_primary,
            "domains": self.domains,
            "confidence": self.confidence,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "last_confirmed_at": self.last_confirmed_at,
            "source_kind": self.source_kind,
            "source_ref": self.source_ref,
            "source_links": self.source_links,
            "review_status": self.review_status,
            "review_notes": self.review_notes,
            "origin": self.origin,
            "assistant_suggestion": self.assistant_suggestion,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": self.metadata,
        }

    @property
    def domain_primary(self) -> str:
        return self.domains[0] if self.domains else "misc"


def init_candidate_memory_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL,
            checksum TEXT NOT NULL
        );

        INSERT OR IGNORE INTO schema_migrations (version, name, applied_at, checksum)
        VALUES (5, 'chatgpt_memory_candidate_memories', datetime('now'), 'candidate_memory_v1');

        CREATE TABLE IF NOT EXISTS candidate_memories (
            id TEXT PRIMARY KEY,
            import_id TEXT NOT NULL REFERENCES imports(id) ON DELETE CASCADE,
            conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
            chunk_id TEXT NOT NULL REFERENCES message_chunks(id) ON DELETE CASCADE,
            source_kind TEXT NOT NULL,
            source_ref TEXT NOT NULL,
            source_role TEXT NOT NULL,
            memory_type TEXT NOT NULL,
            reason_type TEXT NOT NULL,
            domain_primary TEXT NOT NULL,
            domains_json TEXT NOT NULL DEFAULT '[]',
            confidence REAL NOT NULL,
            valid_from TEXT,
            valid_to TEXT,
            last_confirmed_at TEXT,
            review_status TEXT NOT NULL DEFAULT 'pending',
            review_notes TEXT,
            origin TEXT NOT NULL,
            assistant_suggestion INTEGER NOT NULL DEFAULT 0,
            source_links_json TEXT NOT NULL DEFAULT '{}',
            content TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_candidate_memories_status
            ON candidate_memories(review_status);
        CREATE INDEX IF NOT EXISTS idx_candidate_memories_domain
            ON candidate_memories(domain_primary);
        CREATE INDEX IF NOT EXISTS idx_candidate_memories_type
            ON candidate_memories(memory_type);
        CREATE INDEX IF NOT EXISTS idx_candidate_memories_source
            ON candidate_memories(source_kind, source_ref);
        CREATE INDEX IF NOT EXISTS idx_candidate_memories_import
            ON candidate_memories(import_id);
        CREATE INDEX IF NOT EXISTS idx_candidate_memories_updated
            ON candidate_memories(updated_at);
        """
    )
    connection.commit()


def extract_candidate_memories(parsed: "ParsedExport") -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for chunk in parsed.chunks:
        candidate = _candidate_from_chunk(chunk)
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def replace_candidate_memories(connection: sqlite3.Connection, parsed: "ParsedExport") -> None:
    init_candidate_memory_schema(connection)
    import_id = parsed.import_record["id"]
    candidates = extract_candidate_memories(parsed)
    with connection:
        connection.execute("DELETE FROM candidate_memories WHERE import_id = ?", (import_id,))
        connection.executemany(
            """
            INSERT INTO candidate_memories (
                id, import_id, conversation_id, message_id, chunk_id, source_kind, source_ref,
                source_role, memory_type, reason_type, domain_primary, domains_json, confidence,
                valid_from, valid_to, last_confirmed_at, review_status, review_notes, origin,
                assistant_suggestion, source_links_json, content, metadata_json, created_at, updated_at
            )
            VALUES (
                :id, :import_id, :conversation_id, :message_id, :chunk_id, :source_kind, :source_ref,
                :source_role, :memory_type, :reason_type, :domain_primary, :domains_json, :confidence,
                :valid_from, :valid_to, :last_confirmed_at, :review_status, :review_notes, :origin,
                :assistant_suggestion, :source_links_json, :content, :metadata_json, :created_at, :updated_at
            )
            """,
            candidates,
        )


def list_candidate_memories(
    connection: sqlite3.Connection,
    *,
    review_status: str | None = "pending",
    domain: str | None = None,
    source_role: str | None = None,
    assistant_suggestion: bool | None = None,
    subject: str | None = None,
    subject_kind: str | None = None,
    quality_filter: str = "all",
    limit: int | None = None,
) -> list[CandidateMemory]:
    init_candidate_memory_schema(connection)
    if subject is not None:
        init_subject_schema(connection)
    where: list[str] = []
    params: list[Any] = []
    if review_status is not None:
        where.append("review_status = ?")
        params.append(review_status)
    if domain is not None:
        where.append("domain_primary = ?")
        params.append(domain)
    if source_role is not None:
        where.append("source_role = ?")
        params.append(source_role)
    if assistant_suggestion is not None:
        where.append("assistant_suggestion = ?")
        params.append(1 if assistant_suggestion else 0)
    quality_clause, quality_params = _candidate_quality_clause(quality_filter)
    if quality_clause:
        where.append(quality_clause)
        params.extend(quality_params)
    if subject is not None:
        subject_clause, subject_params = _subject_filter_clause(
            connection,
            subject,
            subject_kind=subject_kind,
        )
        where.append(subject_clause)
        params.extend(subject_params)

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
            id, content, memory_type, reason_type, domains_json, confidence,
            valid_from, valid_to, last_confirmed_at, source_kind, source_ref,
            source_links_json, review_status, review_notes, origin,
            assistant_suggestion, created_at, updated_at, metadata_json
        FROM candidate_memories
        {where_clause}
        ORDER BY
            CASE review_status
                WHEN 'pending' THEN 0
                WHEN 'approved' THEN 1
                WHEN 'merged' THEN 2
                WHEN 'rejected' THEN 3
                ELSE 4
            END,
            confidence DESC,
            updated_at DESC,
            id ASC
        {limit_clause}
        """,
        params,
    ).fetchall()
    return [_candidate_from_row(row) for row in rows]


def list_candidate_subjects(
    connection: sqlite3.Connection,
    *,
    review_status: str | None = "pending",
    source_role: str | None = None,
    assistant_suggestion: bool | None = None,
    quality_filter: str = "all",
    kind: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    init_candidate_memory_schema(connection)
    init_subject_schema(connection)
    match_parts: list[str] = []
    match_params: list[Any] = []
    if review_status is not None:
        match_parts.append("cm.review_status = ?")
        match_params.append(review_status)
    if source_role is not None:
        match_parts.append("cm.source_role = ?")
        match_params.append(source_role)
    if assistant_suggestion is not None:
        match_parts.append("cm.assistant_suggestion = ?")
        match_params.append(1 if assistant_suggestion else 0)
    quality_clause, quality_params = _candidate_quality_clause(quality_filter, table_alias="cm")
    if quality_clause:
        match_parts.append(quality_clause)
        match_params.extend(quality_params)
    if kind is not None:
        match_parts.append("s.kind = ?")
        match_params.append(_normalize_kind(kind))
    match_clause = f"WHERE {' AND '.join(match_parts)}" if match_parts else ""

    union_parts: list[str] = []
    union_params: list[Any] = []
    if _table_exists(connection, "conversation_subjects"):
        union_parts.append(
            f"""
            SELECT
                s.id AS subject_id,
                cm.id AS candidate_id,
                cm.review_status AS review_status,
                cm.assistant_suggestion AS assistant_suggestion,
                cm.updated_at AS updated_at
            FROM candidate_memories cm
            JOIN conversation_subjects cs ON cs.conversation_id = cm.conversation_id
            JOIN subjects s ON s.id = cs.subject_id
            {match_clause}
            """
        )
        union_params.extend(match_params)
    if _table_exists(connection, "chunk_subjects"):
        union_parts.append(
            f"""
            SELECT
                s.id AS subject_id,
                cm.id AS candidate_id,
                cm.review_status AS review_status,
                cm.assistant_suggestion AS assistant_suggestion,
                cm.updated_at AS updated_at
            FROM candidate_memories cm
            JOIN chunk_subjects cs ON cs.chunk_id = cm.chunk_id
            JOIN subjects s ON s.id = cs.subject_id
            {match_clause}
            """
        )
        union_params.extend(match_params)

    if not union_parts:
        return []

    subject_where_parts: list[str] = []
    subject_where_params: list[Any] = []
    if kind is not None:
        subject_where_parts.append("s.kind = ?")
        subject_where_params.append(_normalize_kind(kind))
    subject_where_clause = f"WHERE {' AND '.join(subject_where_parts)}" if subject_where_parts else ""

    limit_clause = ""
    if limit is not None:
        if limit < 1:
            return []
        limit_clause = "LIMIT ?"

    rows = connection.execute(
        f"""
        WITH subject_matches AS (
            {" UNION ALL ".join(union_parts)}
        ),
        distinct_subject_matches AS (
            SELECT DISTINCT
                subject_id,
                candidate_id,
                review_status,
                assistant_suggestion,
                updated_at
            FROM subject_matches
        ),
        subject_rollup AS (
            SELECT
                subject_id,
                COUNT(DISTINCT candidate_id) AS candidate_count,
                SUM(CASE WHEN review_status = 'pending' THEN 1 ELSE 0 END) AS pending_count,
                SUM(CASE WHEN review_status = 'approved' THEN 1 ELSE 0 END) AS approved_count,
                SUM(CASE WHEN review_status = 'merged' THEN 1 ELSE 0 END) AS merged_count,
                SUM(CASE WHEN review_status = 'rejected' THEN 1 ELSE 0 END) AS rejected_count,
                SUM(CASE WHEN assistant_suggestion = 1 THEN 1 ELSE 0 END) AS assistant_count,
                MAX(updated_at) AS latest_candidate_activity_at
            FROM distinct_subject_matches
            GROUP BY subject_id
        )
        SELECT
            s.id,
            s.slug,
            s.kind,
            s.name,
            s.description,
            s.created_at,
            s.updated_at,
            s.metadata_json,
            COALESCE(r.candidate_count, 0),
            COALESCE(r.pending_count, 0),
            COALESCE(r.approved_count, 0),
            COALESCE(r.merged_count, 0),
            COALESCE(r.rejected_count, 0),
            COALESCE(r.assistant_count, 0),
            r.latest_candidate_activity_at
        FROM subjects s
        LEFT JOIN subject_rollup r ON r.subject_id = s.id
        {subject_where_clause}
        ORDER BY
            COALESCE(r.candidate_count, 0) DESC,
            COALESCE(r.latest_candidate_activity_at, s.updated_at) DESC,
            s.name ASC
        {limit_clause}
        """,
        (*union_params, *subject_where_params, *([] if limit is None else [limit])),
    ).fetchall()

    return [
        {
            "id": row[0],
            "slug": row[1],
            "kind": row[2],
            "name": row[3],
            "description": row[4],
            "created_at": row[5],
            "updated_at": row[6],
            "metadata": json.loads(row[7] or "{}"),
            "candidate_count": int(row[8]),
            "pending_count": int(row[9]),
            "approved_count": int(row[10]),
            "merged_count": int(row[11]),
            "rejected_count": int(row[12]),
            "assistant_count": int(row[13]),
            "latest_candidate_activity_at": row[14],
        }
        for row in rows
    ]


def get_candidate_subject_summary(
    connection: sqlite3.Connection,
    subject_name: str,
    *,
    kind: str = "subject",
    review_status: str | None = "pending",
    source_role: str | None = None,
    assistant_suggestion: bool | None = None,
    quality_filter: str = "all",
) -> dict[str, Any]:
    subjects = list_candidate_subjects(
        connection,
        review_status=review_status,
        source_role=source_role,
        assistant_suggestion=assistant_suggestion,
        quality_filter=quality_filter,
        kind=kind,
        limit=None,
    )
    normalized_kind = _normalize_kind(kind)
    normalized_slug = normalize_subject_slug(subject_name)
    for subject in subjects:
        if subject["kind"] == normalized_kind and (subject["slug"] == normalized_slug or subject["name"].lower() == subject_name.strip().lower()):
            return subject
    raise KeyError(f"subject not found: {normalized_kind}/{normalized_slug}")


def get_candidate_memory(connection: sqlite3.Connection, candidate_id: str) -> CandidateMemory:
    init_candidate_memory_schema(connection)
    row = connection.execute(
        """
        SELECT
            id, content, memory_type, reason_type, domains_json, confidence,
            valid_from, valid_to, last_confirmed_at, source_kind, source_ref,
            source_links_json, review_status, review_notes, origin,
            assistant_suggestion, created_at, updated_at, metadata_json
        FROM candidate_memories
        WHERE id = ?
        """,
        (candidate_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"candidate memory not found: {candidate_id}")
    return _candidate_from_row(row)


def update_candidate_review(
    connection: sqlite3.Connection,
    candidate_id: str,
    *,
    review_status: str,
    review_notes: str | None = None,
    last_confirmed_at: str | None = None,
) -> CandidateMemory:
    init_candidate_memory_schema(connection)
    normalized_status = _validate_review_status(review_status)
    now = utc_now()
    fields = ["review_status = ?", "updated_at = ?"]
    params: list[Any] = [normalized_status, now]
    if review_notes is not None:
        fields.append("review_notes = ?")
        params.append(review_notes)
    if last_confirmed_at is not None:
        fields.append("last_confirmed_at = ?")
        params.append(last_confirmed_at)
    params.append(candidate_id)
    with connection:
        result = connection.execute(
            f"""
            UPDATE candidate_memories
            SET {", ".join(fields)}
            WHERE id = ?
            """,
            params,
        )
    if result.rowcount == 0:
        raise KeyError(f"candidate memory not found: {candidate_id}")
    return get_candidate_memory(connection, candidate_id)


def list_candidate_memories_for_subject(
    connection: sqlite3.Connection,
    subject_name: str,
    *,
    kind: str = "subject",
    review_status: str | None = "pending",
    source_role: str | None = None,
    assistant_suggestion: bool | None = None,
    quality_filter: str = "all",
    limit: int | None = None,
) -> list[CandidateMemory]:
    init_candidate_memory_schema(connection)
    init_subject_schema(connection)
    where: list[str] = []
    params: list[Any] = []
    if review_status is not None:
        where.append("review_status = ?")
        params.append(review_status)
    if source_role is not None:
        where.append("source_role = ?")
        params.append(source_role)
    if assistant_suggestion is not None:
        where.append("assistant_suggestion = ?")
        params.append(1 if assistant_suggestion else 0)
    quality_clause, quality_params = _candidate_quality_clause(quality_filter)
    if quality_clause:
        where.append(quality_clause)
        params.extend(quality_params)

    subject_clause, subject_params = _subject_filter_clause(connection, subject_name, subject_kind=kind)
    where.append(subject_clause)
    params.extend(subject_params)

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
            id, content, memory_type, reason_type, domains_json, confidence,
            valid_from, valid_to, last_confirmed_at, source_kind, source_ref,
            source_links_json, review_status, review_notes, origin,
            assistant_suggestion, created_at, updated_at, metadata_json
        FROM candidate_memories
        {where_clause}
        ORDER BY
            confidence DESC,
            updated_at DESC,
            id ASC
        {limit_clause}
        """,
        params,
    ).fetchall()
    return [_candidate_from_row(row) for row in rows]


def _candidate_from_chunk(chunk: dict[str, Any]) -> dict[str, Any] | None:
    content = str(chunk.get("text") or "").strip()
    if not content:
        return None
    role = str(chunk.get("role") or "unknown").strip().lower()
    created_at = _optional_str(chunk.get("created_at"))
    conversation_id = str(chunk["conversation_id"])
    message_id = str(chunk["message_id"])
    chunk_id = str(chunk["id"])
    domains = _guess_domains(chunk.get("title"), content)
    memory_type, reason_type, confidence = _classify_candidate(role=role, content=content)
    assistant_suggestion = role == "assistant"
    now = utc_now()
    return {
        "id": _candidate_id(conversation_id, message_id, chunk_id, content),
        "import_id": str(chunk["import_id"]),
        "conversation_id": conversation_id,
        "message_id": message_id,
        "chunk_id": chunk_id,
        "source_kind": "chatgpt_export",
        "source_ref": chunk_id,
        "source_role": role,
        "memory_type": memory_type,
        "reason_type": reason_type,
        "domain_primary": domains[0],
        "domains_json": json.dumps(domains, sort_keys=True),
        "confidence": confidence,
        "valid_from": created_at,
        "valid_to": None,
        "last_confirmed_at": None,
        "review_status": "pending",
        "review_notes": None,
        "origin": "chatgpt_export",
        "assistant_suggestion": 1 if assistant_suggestion else 0,
        "source_links_json": json.dumps(
            {
                "conversation_id": conversation_id,
                "message_id": message_id,
                "chunk_id": chunk_id,
                "source_kind": "chatgpt_export",
                "source_role": role,
            },
            sort_keys=True,
        ),
        "content": content,
        "metadata_json": json.dumps(
            {
                "message_turn_index": chunk.get("turn_index"),
                "chunk_index": chunk.get("chunk_index"),
                "conversation_title": chunk.get("title"),
            },
            sort_keys=True,
        ),
        "created_at": now,
        "updated_at": now,
    }


def _candidate_from_row(row: sqlite3.Row | tuple[Any, ...]) -> CandidateMemory:
    return CandidateMemory(
        id=row[0],
        content=row[1],
        memory_type=row[2],
        reason_type=row[3],
        domains=json.loads(row[4] or "[]"),
        confidence=float(row[5]),
        valid_from=row[6],
        valid_to=row[7],
        last_confirmed_at=row[8],
        source_kind=row[9],
        source_ref=row[10],
        source_links=json.loads(row[11] or "{}"),
        review_status=row[12],
        review_notes=row[13],
        origin=row[14],
        assistant_suggestion=bool(row[15]),
        created_at=row[16],
        updated_at=row[17],
        metadata=json.loads(row[18] or "{}"),
    )


def _candidate_id(conversation_id: str, message_id: str, chunk_id: str, content: str) -> str:
    import hashlib

    digest = hashlib.sha256(
        json.dumps(
            {
                "conversation_id": conversation_id,
                "message_id": message_id,
                "chunk_id": chunk_id,
                "content": content,
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:16]
    return f"cand_{digest}"


def _classify_candidate(*, role: str, content: str) -> tuple[str, str, float]:
    if role == "assistant":
        return "assistant_suggestion", "assistant_suggestion", 0.35

    lowered = content.lower()
    if _matches(lowered, r"\b(prefer|preferably|like|love|always use|usually use)\b"):
        return "preference", "preference_choice", 0.75
    if _matches(lowered, r"\b(decid|going with|settled on|we will use)\b"):
        return "decision", "preference_choice", 0.72
    if _matches(lowered, r"\b(fail|failed|error|broke|didn't work|doesn't work)\b"):
        return "failure", "user_reported_outcome", 0.68
    if _matches(lowered, r"\b(workaround|instead|because|blocked|couldn't|cannot|can't)\b"):
        return "workaround", "constraint_response", 0.66
    if _matches(lowered, r"\b(need to|should|must|use|run|do this|first|then)\b"):
        return "procedure", "ideal_procedure", 0.62
    if _matches(lowered, r"\b(todo|later|someday|not sure|question|open loop)\b"):
        return "open_loop", "unknown", 0.52
    if _matches(lowered, r"\b(worked|succeeded|fixed|resolved|confirmed)\b"):
        return "episodic", "user_reported_outcome", 0.64
    return "episodic", "unknown", 0.5


def _guess_domains(title: Any, content: str) -> list[str]:
    text = f"{title or ''} {content}".lower()
    domains: list[str] = []
    for domain, patterns in _DOMAIN_PATTERNS:
        if any(pattern in text for pattern in patterns):
            domains.append(domain)
    if not domains:
        return ["misc"]
    return domains


def _matches(text: str, pattern: str) -> bool:
    return re.search(pattern, text) is not None


_DOMAIN_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("cooking_baking", ("recipe", "bake", "baking", "cook", "oven", "sauce", "cake", "dough")),
    ("lab_automation", ("plate reader", "parser", "csv", "workflow", "pipette", "assay", "robot", "automation", "lab")),
    ("career_work", ("job", "interview", "resume", "manager", "career", "work", "project")),
    ("ai_memory_systems", ("memory", "rag", "embedding", "retrieval", "llm", "ollama", "vector", "subject")),
    ("finance_investing", ("portfolio", "stock", "market", "invest", "trade", "401k", "ira")),
    ("health_supplements", ("supplement", "vitamin", "health", "sleep", "dose")),
    ("relationships_life", ("partner", "friend", "family", "relationship", "dating")),
    ("law_lsat", ("lsat", "brief", "case", "legal", "law")),
    ("style_wardrobe", ("outfit", "wardrobe", "style", "shirt", "jacket")),
    ("creative_writing", ("story", "character", "plot", "novel", "poem", "write")),
    ("home_projects", ("home", "garage", "shelf", "repair", "wood", "tool")),
    ("fitness_training", ("workout", "run", "training", "lift", "gym")),
    ("pets", ("dog", "cat", "pet", "vet")),
]


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _subject_filter_clause(
    connection: sqlite3.Connection,
    subject_name: str,
    *,
    subject_kind: str | None = None,
) -> tuple[str, list[Any]]:
    normalized_kind = _normalize_kind(subject_kind) if subject_kind is not None else None
    clauses: list[str] = []
    params: list[Any] = []
    subject_slug = normalize_subject_slug(subject_name)
    subject_match = "(subjects.slug = ? OR subjects.name LIKE ?)"

    if _table_exists(connection, "chunk_subjects"):
        chunk_clause = [
            "EXISTS (",
            "    SELECT 1",
            "    FROM chunk_subjects",
            "    JOIN subjects ON subjects.id = chunk_subjects.subject_id",
            "    WHERE chunk_subjects.chunk_id = candidate_memories.chunk_id",
            f"      AND {subject_match}",
        ]
        chunk_params = [subject_slug, f"%{subject_name}%"]
        if normalized_kind is not None:
            chunk_clause.append("      AND subjects.kind = ?")
            chunk_params.append(normalized_kind)
        chunk_clause.append(")")
        clauses.append("\n".join(chunk_clause))
        params.extend(chunk_params)

    if _table_exists(connection, "conversation_subjects"):
        conversation_clause = [
            "EXISTS (",
            "    SELECT 1",
            "    FROM conversation_subjects",
            "    JOIN subjects ON subjects.id = conversation_subjects.subject_id",
            "    WHERE conversation_subjects.conversation_id = candidate_memories.conversation_id",
            f"      AND {subject_match}",
        ]
        conversation_params = [subject_slug, f"%{subject_name}%"]
        if normalized_kind is not None:
            conversation_clause.append("      AND subjects.kind = ?")
            conversation_params.append(normalized_kind)
        conversation_clause.append(")")
        clauses.append("\n".join(conversation_clause))
        params.extend(conversation_params)

    if not clauses:
        return "1 = 0", []
    if len(clauses) == 1:
        return clauses[0], params
    return f"({' OR '.join(clauses)})", params


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _candidate_quality_clause(quality_filter: str, *, table_alias: str | None = None) -> tuple[str, list[Any]]:
    normalized = _validate_quality_filter(quality_filter)
    if normalized == "all":
        return "", []

    prefix = f"{table_alias}." if table_alias else ""
    user_clause = f"{prefix}source_role = ? AND {prefix}assistant_suggestion = 0"
    params: list[Any] = ["user"]
    if normalized == "user_only":
        return user_clause, params

    content_expr = f"LOWER(TRIM({prefix}content))"
    high_signal_parts = [
        user_clause,
        f"{prefix}confidence >= ?",
        f"LENGTH(TRIM({prefix}content)) >= ?",
        f"{prefix}memory_type IN (?, ?, ?, ?, ?, ?)",
        f"{content_expr} NOT IN ({', '.join('?' for _ in _CONTEXTLESS_CANDIDATES)})",
    ]
    params.extend(
        [
            0.6,
            16,
            "preference",
            "decision",
            "failure",
            "workaround",
            "procedure",
            "episodic",
            *_CONTEXTLESS_CANDIDATES,
        ]
    )
    return " AND ".join(f"({part})" for part in high_signal_parts), params


def _validate_review_status(review_status: str) -> str:
    normalized = review_status.strip().lower().replace("-", "_").replace(" ", "_")
    if normalized not in VALID_REVIEW_STATUSES:
        raise ValueError(f"invalid candidate review status: {review_status}")
    return normalized


def _validate_quality_filter(quality_filter: str) -> str:
    normalized = quality_filter.strip().lower().replace("-", "_").replace(" ", "_")
    if normalized not in VALID_QUALITY_FILTERS:
        raise ValueError(f"invalid candidate quality filter: {quality_filter}")
    return normalized


def _normalize_kind(kind: str) -> str:
    return validate_subject_kind(kind)


_CONTEXTLESS_CANDIDATES = (
    "like this?",
    "like this",
    "yes",
    "no",
    "ok",
    "okay",
    "sure",
    "thanks",
    "thank you",
    "continue",
    "do it",
    "try again",
    "what next?",
    "what's next?",
)
