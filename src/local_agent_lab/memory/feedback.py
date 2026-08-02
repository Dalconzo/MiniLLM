from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from .curated import get_memory_record, init_curated_memory_schema, list_memory_records, update_memory_record_status
from .observability import utc_now


FEEDBACK_SCHEMA_VERSION = 6
VALID_FEEDBACK_RATINGS = ("up", "down", "saved", "ignored", "resolved")
VALID_AGENT_FEEDBACK_CATEGORIES = (
    "retrieval_miss",
    "retrieval_noise",
    "constraint_miss",
    "stale_memory",
    "misattribution",
    "duplicate_memory",
    "bad_canonicalization",
    "bad_context_compilation",
    "cross_domain_leak",
    "tool_contract_error",
    "unsupported_inference",
    "provenance_gap",
    "latency",
    "security",
    "other",
)
VALID_AGENT_FEEDBACK_SEVERITIES = ("low", "medium", "high", "critical")
VALID_FEEDBACK_REVIEW_STATUSES = ("submitted", "reviewed", "applied", "superseded", "rejected")
VALID_FEEDBACK_CONTROL_TYPES = (
    "query_source_penalty",
    "wrong_subject_penalty",
    "global_source_penalty",
    "eval_example",
)


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


@dataclass(frozen=True)
class AgentFeedback:
    id: str
    run_id: str
    trace_id: str | None
    component: str
    category: str
    severity: str
    observed_behavior: str
    expected_behavior: str
    relevant_source_ids: list[str]
    confidence: float
    downstream_effect: str | None
    suggested_direction: str | None
    build_sha: str | None
    schema_version: int
    tool_version: str
    environment: str
    retrieval_profile: str | None
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "run_id": self.run_id,
            "trace_id": self.trace_id,
            "component": self.component,
            "category": self.category,
            "severity": self.severity,
            "observed_behavior": self.observed_behavior,
            "expected_behavior": self.expected_behavior,
            "relevant_source_ids": self.relevant_source_ids,
            "confidence": self.confidence,
            "downstream_effect": self.downstream_effect,
            "suggested_direction": self.suggested_direction,
            "build_sha": self.build_sha,
            "schema_version": self.schema_version,
            "tool_version": self.tool_version,
            "environment": self.environment,
            "retrieval_profile": self.retrieval_profile,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class FeedbackRankingControl:
    id: str
    feedback_id: str
    control_type: str
    status: str
    query_pattern: str | None
    source_id: str | None
    subject: str | None
    weight: float
    rationale: str
    rollback_note: str | None
    created_by: str
    created_at: str
    applied_at: str | None
    superseded_by: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "feedback_id": self.feedback_id,
            "control_type": self.control_type,
            "status": self.status,
            "query_pattern": self.query_pattern,
            "source_id": self.source_id,
            "subject": self.subject,
            "weight": self.weight,
            "rationale": self.rationale,
            "rollback_note": self.rollback_note,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "applied_at": self.applied_at,
            "superseded_by": self.superseded_by,
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

        CREATE TABLE IF NOT EXISTS agent_feedback (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            trace_id TEXT,
            component TEXT NOT NULL,
            category TEXT NOT NULL,
            severity TEXT NOT NULL,
            observed_behavior TEXT NOT NULL,
            expected_behavior TEXT NOT NULL,
            relevant_source_ids_json TEXT NOT NULL DEFAULT '[]',
            confidence REAL NOT NULL,
            downstream_effect TEXT,
            suggested_direction TEXT,
            build_sha TEXT,
            schema_version INTEGER NOT NULL,
            tool_version TEXT NOT NULL,
            environment TEXT NOT NULL,
            retrieval_profile TEXT,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_agent_feedback_run
            ON agent_feedback(run_id);
        CREATE INDEX IF NOT EXISTS idx_agent_feedback_category
            ON agent_feedback(category);
        CREATE INDEX IF NOT EXISTS idx_agent_feedback_created
            ON agent_feedback(created_at);

        CREATE TABLE IF NOT EXISTS agent_feedback_reviews (
            feedback_id TEXT PRIMARY KEY REFERENCES agent_feedback(id) ON DELETE CASCADE,
            status TEXT NOT NULL CHECK (status IN ('submitted', 'reviewed', 'applied', 'superseded', 'rejected')),
            reviewed_by TEXT,
            reviewed_at TEXT,
            review_notes TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS feedback_ranking_controls (
            id TEXT PRIMARY KEY,
            feedback_id TEXT NOT NULL REFERENCES agent_feedback(id) ON DELETE CASCADE,
            control_type TEXT NOT NULL CHECK (
                control_type IN ('query_source_penalty', 'wrong_subject_penalty', 'global_source_penalty', 'eval_example')
            ),
            status TEXT NOT NULL CHECK (status IN ('submitted', 'reviewed', 'applied', 'superseded', 'rejected')),
            query_pattern TEXT,
            source_id TEXT,
            subject TEXT,
            weight REAL NOT NULL,
            rationale TEXT NOT NULL,
            rollback_note TEXT,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            applied_at TEXT,
            superseded_by TEXT REFERENCES feedback_ranking_controls(id) ON DELETE SET NULL
        );

        CREATE INDEX IF NOT EXISTS idx_feedback_controls_feedback
            ON feedback_ranking_controls(feedback_id);
        CREATE INDEX IF NOT EXISTS idx_feedback_controls_status
            ON feedback_ranking_controls(status);
        CREATE INDEX IF NOT EXISTS idx_feedback_controls_source
            ON feedback_ranking_controls(source_id);
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


def record_agent_feedback(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    component: str,
    category: str,
    severity: str,
    observed_behavior: str,
    expected_behavior: str,
    confidence: float,
    relevant_source_ids: list[str] | None = None,
    trace_id: str | None = None,
    downstream_effect: str | None = None,
    suggested_direction: str | None = None,
    build_sha: str | None = None,
    tool_version: str = "agent_feedback_v1",
    environment: str = "local",
    retrieval_profile: str | None = None,
) -> AgentFeedback:
    init_feedback_schema(connection)
    normalized_category = _validate_agent_feedback_category(category)
    normalized_severity = _validate_agent_feedback_severity(severity)
    normalized_confidence = _validate_confidence(confidence)
    source_ids = [str(source_id) for source_id in (relevant_source_ids or []) if str(source_id).strip()]
    created_at = utc_now()
    feedback_id = "afbk_" + _short_hash(
        json.dumps(
            {
                "run_id": run_id,
                "trace_id": trace_id,
                "component": component,
                "category": normalized_category,
                "severity": normalized_severity,
                "observed_behavior": observed_behavior,
                "expected_behavior": expected_behavior,
                "source_ids": source_ids,
                "confidence": normalized_confidence,
                "created_at": created_at,
            },
            sort_keys=True,
        )
    )
    feedback = AgentFeedback(
        id=feedback_id,
        run_id=_require_nonempty(run_id, "run_id"),
        trace_id=trace_id,
        component=_require_nonempty(component, "component"),
        category=normalized_category,
        severity=normalized_severity,
        observed_behavior=_require_nonempty(observed_behavior, "observed_behavior"),
        expected_behavior=_require_nonempty(expected_behavior, "expected_behavior"),
        relevant_source_ids=source_ids,
        confidence=normalized_confidence,
        downstream_effect=downstream_effect,
        suggested_direction=suggested_direction,
        build_sha=build_sha,
        schema_version=1,
        tool_version=tool_version,
        environment=_require_nonempty(environment, "environment"),
        retrieval_profile=retrieval_profile,
        created_at=created_at,
    )

    with connection:
        connection.execute(
            """
            INSERT INTO agent_feedback (
                id, run_id, trace_id, component, category, severity,
                observed_behavior, expected_behavior, relevant_source_ids_json,
                confidence, downstream_effect, suggested_direction, build_sha,
                schema_version, tool_version, environment, retrieval_profile, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                feedback.id,
                feedback.run_id,
                feedback.trace_id,
                feedback.component,
                feedback.category,
                feedback.severity,
                feedback.observed_behavior,
                feedback.expected_behavior,
                json.dumps(feedback.relevant_source_ids, sort_keys=True),
                feedback.confidence,
                feedback.downstream_effect,
                feedback.suggested_direction,
                feedback.build_sha,
                feedback.schema_version,
                feedback.tool_version,
                feedback.environment,
                feedback.retrieval_profile,
                feedback.created_at,
            ),
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO agent_feedback_reviews (feedback_id, status, reviewed_by, reviewed_at, review_notes, updated_at)
            VALUES (?, 'submitted', NULL, NULL, NULL, ?)
            """,
            (feedback.id, feedback.created_at),
        )
    return feedback


def create_feedback_ranking_control(
    connection: sqlite3.Connection,
    *,
    feedback_id: str,
    control_type: str,
    query_pattern: str | None = None,
    source_id: str | None = None,
    subject: str | None = None,
    weight: float = 0.0,
    rationale: str,
    rollback_note: str | None = None,
    created_by: str = "local",
    status: str = "reviewed",
) -> FeedbackRankingControl:
    init_feedback_schema(connection)
    normalized_type = _validate_feedback_control_type(control_type)
    normalized_status = _validate_feedback_review_status(status)
    now = utc_now()
    control_id = "frc_" + _short_hash(
        json.dumps(
            {
                "feedback_id": feedback_id,
                "control_type": normalized_type,
                "query_pattern": query_pattern,
                "source_id": source_id,
                "subject": subject,
                "weight": weight,
                "rationale": rationale,
                "created_at": now,
            },
            sort_keys=True,
        )
    )
    control = FeedbackRankingControl(
        id=control_id,
        feedback_id=_require_nonempty(feedback_id, "feedback_id"),
        control_type=normalized_type,
        status=normalized_status,
        query_pattern=query_pattern.strip() if query_pattern else None,
        source_id=source_id.strip() if source_id else None,
        subject=subject.strip() if subject else None,
        weight=float(weight),
        rationale=_require_nonempty(rationale, "rationale"),
        rollback_note=rollback_note,
        created_by=_require_nonempty(created_by, "created_by"),
        created_at=now,
        applied_at=now if normalized_status == "applied" else None,
        superseded_by=None,
    )
    with connection:
        connection.execute(
            """
            INSERT INTO feedback_ranking_controls (
                id, feedback_id, control_type, status, query_pattern, source_id, subject, weight,
                rationale, rollback_note, created_by, created_at, applied_at, superseded_by
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                control.id,
                control.feedback_id,
                control.control_type,
                control.status,
                control.query_pattern,
                control.source_id,
                control.subject,
                control.weight,
                control.rationale,
                control.rollback_note,
                control.created_by,
                control.created_at,
                control.applied_at,
                control.superseded_by,
            ),
        )
        connection.execute(
            """
            INSERT INTO agent_feedback_reviews (feedback_id, status, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(feedback_id) DO UPDATE SET
                status = excluded.status,
                updated_at = excluded.updated_at
            """,
            (control.feedback_id, control.status, now),
        )
    return control


def list_feedback_ranking_controls(
    connection: sqlite3.Connection,
    *,
    status: str | None = None,
    limit: int | None = None,
) -> list[FeedbackRankingControl]:
    init_feedback_schema(connection)
    where = ""
    params: list[Any] = []
    if status is not None:
        where = "WHERE status = ?"
        params.append(_validate_feedback_review_status(status))
    limit_clause = ""
    if limit is not None:
        if limit < 1:
            return []
        limit_clause = "LIMIT ?"
        params.append(limit)
    rows = connection.execute(
        f"""
        SELECT id, feedback_id, control_type, status, query_pattern, source_id, subject, weight,
               rationale, rollback_note, created_by, created_at, applied_at, superseded_by
        FROM feedback_ranking_controls
        {where}
        ORDER BY created_at DESC, id DESC
        {limit_clause}
        """,
        params,
    ).fetchall()
    return [_feedback_ranking_control_from_row(row) for row in rows]


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


def _validate_agent_feedback_category(category: str) -> str:
    normalized = category.strip().lower()
    if normalized not in VALID_AGENT_FEEDBACK_CATEGORIES:
        raise ValueError(f"invalid agent feedback category: {category}")
    return normalized


def _validate_agent_feedback_severity(severity: str) -> str:
    normalized = severity.strip().lower()
    if normalized not in VALID_AGENT_FEEDBACK_SEVERITIES:
        raise ValueError(f"invalid agent feedback severity: {severity}")
    return normalized


def _validate_feedback_review_status(status: str) -> str:
    normalized = status.strip().lower()
    if normalized not in VALID_FEEDBACK_REVIEW_STATUSES:
        raise ValueError(f"invalid feedback review status: {status}")
    return normalized


def _validate_feedback_control_type(control_type: str) -> str:
    normalized = control_type.strip().lower()
    if normalized not in VALID_FEEDBACK_CONTROL_TYPES:
        raise ValueError(f"invalid feedback control type: {control_type}")
    return normalized


def _validate_confidence(confidence: float) -> float:
    value = float(confidence)
    if value < 0.0 or value > 1.0:
        raise ValueError("agent feedback confidence must be between 0.0 and 1.0")
    return value


def _require_nonempty(value: str, field_name: str) -> str:
    text = str(value).strip()
    if not text:
        raise ValueError(f"{field_name} is required")
    return text


def _short_hash(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _feedback_ranking_control_from_row(row: sqlite3.Row | tuple[Any, ...]) -> FeedbackRankingControl:
    return FeedbackRankingControl(
        id=row[0],
        feedback_id=row[1],
        control_type=row[2],
        status=row[3],
        query_pattern=row[4],
        source_id=row[5],
        subject=row[6],
        weight=float(row[7]),
        rationale=row[8],
        rollback_note=row[9],
        created_by=row[10],
        created_at=row[11],
        applied_at=row[12],
        superseded_by=row[13],
    )
