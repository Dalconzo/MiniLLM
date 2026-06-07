from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from .observability import utc_now


def init_audit_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL,
            checksum TEXT NOT NULL
        );

        INSERT OR IGNORE INTO schema_migrations (version, name, applied_at, checksum)
        VALUES (3, 'chatgpt_memory_audit', datetime('now'), 'audit_tables_v1');

        CREATE TABLE IF NOT EXISTS retrieval_events (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            query TEXT NOT NULL,
            query_sha256 TEXT NOT NULL,
            caller TEXT NOT NULL,
            command TEXT NOT NULL,
            filters_json TEXT NOT NULL DEFAULT '{}',
            ranking_profile TEXT NOT NULL,
            disclosure_depth TEXT NOT NULL,
            result_count INTEGER NOT NULL,
            redaction_applied INTEGER NOT NULL DEFAULT 1,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS retrieval_exposures (
            id TEXT PRIMARY KEY,
            retrieval_event_id TEXT NOT NULL REFERENCES retrieval_events(id) ON DELETE CASCADE,
            source_kind TEXT NOT NULL,
            source_id TEXT NOT NULL,
            rank INTEGER NOT NULL,
            score REAL NOT NULL,
            score_breakdown_json TEXT NOT NULL DEFAULT '{}',
            disclosure_tier TEXT NOT NULL,
            exposed_fields_json TEXT NOT NULL,
            redacted INTEGER NOT NULL DEFAULT 1,
            redacted_secret_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS deletions (
            id TEXT PRIMARY KEY,
            source_kind TEXT NOT NULL,
            source_id TEXT NOT NULL,
            reason TEXT NOT NULL,
            deleted_at TEXT NOT NULL,
            deleted_by TEXT NOT NULL,
            tombstone_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(source_kind, source_id)
        );

        CREATE INDEX IF NOT EXISTS idx_retrieval_events_run ON retrieval_events(run_id);
        CREATE INDEX IF NOT EXISTS idx_retrieval_events_query_hash ON retrieval_events(query_sha256);
        CREATE INDEX IF NOT EXISTS idx_retrieval_exposures_event ON retrieval_exposures(retrieval_event_id);
        CREATE INDEX IF NOT EXISTS idx_retrieval_exposures_source ON retrieval_exposures(source_kind, source_id);
        CREATE INDEX IF NOT EXISTS idx_deletions_source ON deletions(source_kind, source_id);
        """
    )
    connection.commit()


def tombstone_source(
    connection: sqlite3.Connection,
    *,
    source_kind: str,
    source_id: str,
    reason: str,
    deleted_by: str = "user",
) -> dict[str, Any]:
    init_audit_schema(connection)
    tombstone_id = "del_" + _short_hash(f"{source_kind}:{source_id}")
    payload = {
        "id": tombstone_id,
        "source_kind": source_kind,
        "source_id": source_id,
        "reason": reason,
        "deleted_at": utc_now(),
        "deleted_by": deleted_by,
    }
    with connection:
        connection.execute(
            """
            INSERT INTO deletions (
                id, source_kind, source_id, reason, deleted_at, deleted_by, tombstone_json
            )
            VALUES (:id, :source_kind, :source_id, :reason, :deleted_at, :deleted_by, '{}')
            ON CONFLICT(source_kind, source_id) DO UPDATE SET
                reason = excluded.reason,
                deleted_at = excluded.deleted_at,
                deleted_by = excluded.deleted_by
            """,
            payload,
        )
    return payload


def blocked_source_ids(connection: sqlite3.Connection, source_kind: str | None = None) -> set[str]:
    init_audit_schema(connection)
    if source_kind is None:
        rows = connection.execute("SELECT source_id FROM deletions").fetchall()
    else:
        rows = connection.execute("SELECT source_id FROM deletions WHERE source_kind = ?", (source_kind,)).fetchall()
    return {str(row[0]) for row in rows}


def record_retrieval_event(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    query: str,
    command: str,
    filters: list[dict[str, Any]],
    ranking_profile: str,
    disclosure_depth: str,
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    init_audit_schema(connection)
    event_id = "ret_" + _short_hash(run_id)
    now = utc_now()
    event = {
        "id": event_id,
        "run_id": run_id,
        "created_at": now,
        "query": query,
        "query_sha256": _hash(query),
        "caller": "cli",
        "command": command,
        "filters_json": json.dumps(filters, sort_keys=True),
        "ranking_profile": ranking_profile,
        "disclosure_depth": disclosure_depth,
        "result_count": len(results),
        "redaction_applied": 1,
        "metadata_json": "{}",
    }
    with connection:
        connection.execute(
            """
            INSERT OR REPLACE INTO retrieval_events (
                id, run_id, created_at, query, query_sha256, caller, command,
                filters_json, ranking_profile, disclosure_depth, result_count,
                redaction_applied, metadata_json
            )
            VALUES (
                :id, :run_id, :created_at, :query, :query_sha256, :caller, :command,
                :filters_json, :ranking_profile, :disclosure_depth, :result_count,
                :redaction_applied, :metadata_json
            )
            """,
            event,
        )
        connection.execute("DELETE FROM retrieval_exposures WHERE retrieval_event_id = ?", (event_id,))
        connection.executemany(
            """
            INSERT INTO retrieval_exposures (
                id, retrieval_event_id, source_kind, source_id, rank, score,
                score_breakdown_json, disclosure_tier, exposed_fields_json,
                redacted, redacted_secret_count, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "exp_" + _short_hash(f"{event_id}:{item['rank']}:{item['chunk_id']}"),
                    event_id,
                    item.get("source_kind", "unknown"),
                    item["chunk_id"],
                    item["rank"],
                    item["score"],
                    json.dumps(item.get("score_breakdown", {}), sort_keys=True),
                    item.get("disclosure_tier", "medium"),
                    json.dumps(item.get("exposed_fields", []), sort_keys=True),
                    1,
                    int(item.get("redacted_secret_count", 0)),
                    now,
                )
                for item in results
            ],
        )
    return {"retrieval_event_id": event_id, "exposures": len(results)}


def retrieval_exposures_for_run(connection: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    init_audit_schema(connection)
    rows = connection.execute(
        """
        SELECT
            e.run_id, x.source_kind, x.source_id, x.rank, x.score,
            x.disclosure_tier, x.exposed_fields_json, x.redacted_secret_count
        FROM retrieval_events e
        JOIN retrieval_exposures x ON x.retrieval_event_id = e.id
        WHERE e.run_id = ?
        ORDER BY x.rank ASC
        """,
        (run_id,),
    ).fetchall()
    return [
        {
            "run_id": row[0],
            "source_kind": row[1],
            "source_id": row[2],
            "rank": row[3],
            "score": row[4],
            "disclosure_tier": row[5],
            "exposed_fields": json.loads(row[6] or "[]"),
            "redacted_secret_count": row[7],
        }
        for row in rows
    ]


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _short_hash(value: str) -> str:
    return _hash(value)[:16]
