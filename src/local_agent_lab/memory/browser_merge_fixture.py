from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from .candidates import init_candidate_memory_schema
from .chatgpt_ingest import init_chatgpt_memory_schema
from .curated import init_curated_memory_schema
from .observability import MemoryObservationError
from .subjects import init_subject_schema


FIXTURE_SUITE = "browser_candidate_merge_v1"
FIXTURE_IMPORT_ID = "imp_browser_candidate_merge_v1"
FIXTURE_CONVERSATION_ID = "conv_browser_candidate_merge_v1"
FIXTURE_TITLE = "[DISPOSABLE TEST FIXTURE] Browser candidate merge v1"
FIXTURE_RECORD_TITLE = "Browser Candidate Merge Smoke Fixture"
FIXTURE_RECORD_BODY = (
    "The disposable browser merge fixture records that rosemary focaccia test batch A "
    "used extra olive oil and produced crisp edges."
)
FIXTURE_TIMESTAMP = "2000-01-01T00:00:00+00:00"
FIXTURE_SUBJECT_ID = "subject_recipes-and-baking"
FIXTURE_SUBJECT_SLUG = "recipes-and-baking"
FIXTURE_SUBJECT_NAME = "Recipes and Baking"

USER_CANDIDATE_ID = "cand_browser_merge_smoke_user_v1"
ASSISTANT_CANDIDATE_ID = "cand_browser_merge_smoke_assistant_v1"
UNSELECTED_CANDIDATE_ID = "cand_browser_merge_smoke_unselected_v1"
FIXTURE_CANDIDATE_IDS = (USER_CANDIDATE_ID, ASSISTANT_CANDIDATE_ID, UNSELECTED_CANDIDATE_ID)
SELECTED_CANDIDATE_IDS = (USER_CANDIDATE_ID, ASSISTANT_CANDIDATE_ID)

_FIXTURE_CANDIDATES = (
    {
        "id": USER_CANDIDATE_ID,
        "role": "user",
        "memory_type": "outcome",
        "reason_type": "explicit_user_report",
        "assistant_suggestion": 0,
        "content": (
            "Disposable fixture report: rosemary focaccia test batch A used extra olive oil and produced crisp edges."
        ),
    },
    {
        "id": ASSISTANT_CANDIDATE_ID,
        "role": "assistant",
        "memory_type": "assistant_suggestion",
        "reason_type": "assistant_suggestion",
        "assistant_suggestion": 1,
        "content": (
            "Disposable fixture suggestion: a future focaccia test could compare flaky salt "
            "added before and after baking."
        ),
    },
    {
        "id": UNSELECTED_CANDIDATE_ID,
        "role": "user",
        "memory_type": "preference",
        "reason_type": "explicit_user_report",
        "assistant_suggestion": 0,
        "content": "Disposable control fixture: roasted garlic is another focaccia topping to test.",
    },
)


def browser_merge_fixture_status(connection: sqlite3.Connection) -> dict[str, Any]:
    init_chatgpt_memory_schema(connection)
    init_candidate_memory_schema(connection)
    init_subject_schema(connection)
    init_curated_memory_schema(connection)
    rows = connection.execute(
        f"SELECT * FROM candidate_memories WHERE id IN ({','.join('?' for _ in FIXTURE_CANDIDATE_IDS)}) ORDER BY id",
        FIXTURE_CANDIDATE_IDS,
    ).fetchall()
    candidates = [_candidate_status(row) for row in rows]
    records = connection.execute(
        "SELECT id, title, body, source_kind, source_ref, provenance_json, status FROM memory_records WHERE title = ?",
        (FIXTURE_RECORD_TITLE,),
    ).fetchall()
    subject_row = connection.execute(
        "SELECT id, slug, kind, name, metadata_json FROM subjects WHERE kind = 'subject' AND slug = ?",
        (FIXTURE_SUBJECT_SLUG,),
    ).fetchone()
    subject = _subject_status(subject_row) if subject_row is not None else None
    events: list[sqlite3.Row] = []
    if _table_exists(connection, "candidate_merge_events"):
        event_where = ["candidate_ids_json LIKE ?"]
        event_params: list[Any] = [f"%{USER_CANDIDATE_ID}%"]
        if records:
            event_where.append(f"record_id IN ({','.join('?' for _ in records)})")
            event_params.extend(str(row[0]) for row in records)
        events = connection.execute(
            "SELECT operation_id, record_id, candidate_ids_json, created_by, created_at FROM candidate_merge_events WHERE "
            + " OR ".join(event_where),
            event_params,
        ).fetchall()
    exact_candidates = len(candidates) == len(FIXTURE_CANDIDATE_IDS) and all(
        _is_exact_fixture_candidate(candidate) for candidate in candidates
    )
    subject_valid = subject is None or (subject["id"] == FIXTURE_SUBJECT_ID and subject["name"] == FIXTURE_SUBJECT_NAME)
    if not candidates and not records and not events and subject_valid:
        state = "absent"
    elif (
        exact_candidates
        and all(candidate["review_status"] == "pending" for candidate in candidates)
        and not records
        and subject is not None
        and subject_valid
    ):
        state = "ready"
    elif exact_candidates and records and subject is not None and subject_valid:
        state = "applied_or_partial"
    else:
        state = "invalid_or_partial"
    return {
        "status": "ok",
        "fixture_suite": FIXTURE_SUITE,
        "state": state,
        "mutates_memory_truth": False,
        "fixture_candidate_ids": list(FIXTURE_CANDIDATE_IDS),
        "selected_candidate_ids": list(SELECTED_CANDIDATE_IDS),
        "candidates": candidates,
        "records": [_record_status(row) for row in records],
        "events": [_event_status(row) for row in events],
        "subject": subject,
    }


def seed_browser_merge_fixture(connection: sqlite3.Connection) -> dict[str, Any]:
    before = browser_merge_fixture_status(connection)
    if before["state"] == "ready":
        return {**before, "database_changed": False, "idempotent": True}
    if before["state"] != "absent":
        raise _fixture_error(
            f"browser merge fixture cannot be seeded from state: {before['state']}",
            error_code="browser_merge_fixture_state_conflict",
        )

    metadata = {
        "disposable_test_fixture": True,
        "fixture_suite": FIXTURE_SUITE,
        "source_title": FIXTURE_TITLE,
        "cleanup_required": True,
    }
    try:
        connection.execute("BEGIN IMMEDIATE")
        subject_row = connection.execute(
            "SELECT id, name FROM subjects WHERE kind = 'subject' AND slug = ?",
            (FIXTURE_SUBJECT_SLUG,),
        ).fetchone()
        if subject_row is None:
            connection.execute(
                """
                INSERT INTO subjects (id, slug, kind, name, description, created_at, updated_at, metadata_json)
                VALUES (?, ?, 'subject', ?, ?, ?, ?, ?)
                """,
                (
                    FIXTURE_SUBJECT_ID,
                    FIXTURE_SUBJECT_SLUG,
                    FIXTURE_SUBJECT_NAME,
                    "Disposable subject row for the browser candidate merge acceptance fixture.",
                    FIXTURE_TIMESTAMP,
                    FIXTURE_TIMESTAMP,
                    json.dumps(metadata, sort_keys=True),
                ),
            )
        elif str(subject_row[0]) != FIXTURE_SUBJECT_ID or str(subject_row[1]) != FIXTURE_SUBJECT_NAME:
            raise _fixture_error(
                "Recipes and Baking subject exists with an incompatible identity",
                error_code="browser_merge_fixture_subject_conflict",
            )
        connection.execute(
            """
            INSERT INTO imports (
                id, source_root, raw_manifest_path, imported_at, status, parser_version,
                file_count, conversation_count, message_count, chunk_count, attachment_count,
                content_sha256, notes
            ) VALUES (?, ?, '', ?, 'ok', 'disposable_test_fixture_v1', 0, 1, 3, 3, 0, ?, ?)
            """,
            (
                FIXTURE_IMPORT_ID,
                "[disposable-test-fixture]",
                FIXTURE_TIMESTAMP,
                _sha256(FIXTURE_SUITE),
                "Disposable browser candidate merge acceptance fixture.",
            ),
        )
        connection.execute(
            """
            INSERT INTO conversations (
                id, import_id, source_conversation_id, title, created_at, updated_at,
                message_count, first_message_at, last_message_at, summary, content_sha256,
                is_deleted, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, 3, ?, ?, NULL, ?, 0, ?)
            """,
            (
                FIXTURE_CONVERSATION_ID,
                FIXTURE_IMPORT_ID,
                FIXTURE_CONVERSATION_ID,
                FIXTURE_TITLE,
                FIXTURE_TIMESTAMP,
                FIXTURE_TIMESTAMP,
                FIXTURE_TIMESTAMP,
                FIXTURE_TIMESTAMP,
                _sha256(FIXTURE_TITLE),
                json.dumps(metadata, sort_keys=True),
            ),
        )
        for index, fixture in enumerate(_FIXTURE_CANDIDATES, start=1):
            message_id = f"msg_browser_candidate_merge_v1_{index}"
            chunk_id = f"chk_browser_candidate_merge_v1_{index}"
            content = str(fixture["content"])
            connection.execute(
                """
                INSERT INTO messages (
                    id, conversation_id, import_id, source_message_id, parent_message_id, role,
                    author_name, turn_index, created_at, content_text, content_sha256,
                    token_estimate, attachment_count, is_deleted, metadata_json
                ) VALUES (?, ?, ?, ?, NULL, ?, NULL, ?, ?, ?, ?, 24, 0, 0, ?)
                """,
                (
                    message_id,
                    FIXTURE_CONVERSATION_ID,
                    FIXTURE_IMPORT_ID,
                    message_id,
                    fixture["role"],
                    index,
                    FIXTURE_TIMESTAMP,
                    content,
                    _sha256(content),
                    json.dumps(metadata, sort_keys=True),
                ),
            )
            connection.execute(
                """
                INSERT INTO message_chunks (
                    id, message_id, conversation_id, import_id, chunk_index, text, text_sha256,
                    token_estimate, start_char, end_char, source_kind, summary, is_deleted, metadata_json
                ) VALUES (?, ?, ?, ?, 0, ?, ?, 24, 0, ?, ?, NULL, 0, ?)
                """,
                (
                    chunk_id,
                    message_id,
                    FIXTURE_CONVERSATION_ID,
                    FIXTURE_IMPORT_ID,
                    content,
                    _sha256(content),
                    len(content),
                    f"test_fixture:{FIXTURE_SUITE}",
                    json.dumps(metadata, sort_keys=True),
                ),
            )
            source_links = {
                "conversation_id": FIXTURE_CONVERSATION_ID,
                "message_id": message_id,
                "chunk_id": chunk_id,
                "source_kind": f"test_fixture:{FIXTURE_SUITE}",
                "source_role": fixture["role"],
            }
            connection.execute(
                """
                INSERT INTO candidate_memories (
                    id, import_id, conversation_id, message_id, chunk_id, source_kind, source_ref,
                    source_role, memory_type, reason_type, domain_primary, domains_json, confidence,
                    valid_from, valid_to, last_confirmed_at, review_status, review_notes, origin,
                    assistant_suggestion, source_links_json, content, metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'cooking_baking', '["cooking_baking"]', 0.9,
                          ?, NULL, NULL, 'pending', NULL, 'test_fixture', ?, ?, ?, ?, ?, ?)
                """,
                (
                    fixture["id"],
                    FIXTURE_IMPORT_ID,
                    FIXTURE_CONVERSATION_ID,
                    message_id,
                    chunk_id,
                    f"test_fixture:{FIXTURE_SUITE}",
                    chunk_id,
                    fixture["role"],
                    fixture["memory_type"],
                    fixture["reason_type"],
                    FIXTURE_TIMESTAMP,
                    fixture["assistant_suggestion"],
                    json.dumps(source_links, sort_keys=True),
                    content,
                    json.dumps(metadata, sort_keys=True),
                    FIXTURE_TIMESTAMP,
                    FIXTURE_TIMESTAMP,
                ),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise

    after = browser_merge_fixture_status(connection)
    if after["state"] != "ready":
        raise _fixture_error(
            f"browser merge fixture failed post-seed validation: {after['state']}",
            error_code="browser_merge_fixture_validation_failed",
        )
    return {**after, "database_changed": True, "idempotent": False}


def cleanup_browser_merge_fixture(
    connection: sqlite3.Connection,
    *,
    confirmed: bool,
    expected_record_id: str | None = None,
    expected_operation_id: str | None = None,
) -> dict[str, Any]:
    before = browser_merge_fixture_status(connection)
    if not confirmed:
        raise _fixture_error(
            "browser merge fixture cleanup requires explicit confirmation",
            error_code="browser_merge_fixture_cleanup_confirmation_required",
        )
    if before["state"] == "absent":
        return {**before, "database_changed": False, "idempotent": True}
    if before["state"] not in {"ready", "applied_or_partial"}:
        raise _fixture_error(
            f"browser merge fixture cleanup refused invalid state: {before['state']}",
            error_code="browser_merge_fixture_cleanup_refused",
        )

    records = before["records"]
    events = before["events"]
    if records:
        if len(records) != 1 or not expected_record_id or records[0]["id"] != expected_record_id:
            raise _fixture_error(
                "fixture record exists; cleanup requires its exact record ID",
                error_code="browser_merge_fixture_record_mismatch",
            )
        if not _is_exact_fixture_record(records[0]):
            raise _fixture_error(
                "fixture record provenance or canonical fields do not match",
                error_code="browser_merge_fixture_record_mismatch",
            )
        if len(events) != 1 or not expected_operation_id or events[0]["operation_id"] != expected_operation_id:
            raise _fixture_error(
                "fixture merge event exists; cleanup requires its exact operation ID",
                error_code="browser_merge_fixture_operation_mismatch",
            )
    elif expected_record_id or expected_operation_id:
        raise _fixture_error(
            "cleanup IDs were supplied but no fixture record exists",
            error_code="browser_merge_fixture_cleanup_refused",
        )

    try:
        connection.execute("BEGIN IMMEDIATE")
        fixture_vector_refs: list[str] = []
        if _table_exists(connection, "chunk_embeddings") and _table_exists(connection, "local_embedding_vectors"):
            fixture_vector_refs = [
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT ce.vector_ref
                    FROM chunk_embeddings ce
                    JOIN message_chunks mc ON mc.id = ce.chunk_id
                    WHERE mc.import_id = ?
                    """,
                    (FIXTURE_IMPORT_ID,),
                ).fetchall()
            ]
            connection.execute(
                "DELETE FROM chunk_embeddings WHERE chunk_id IN (SELECT id FROM message_chunks WHERE import_id = ?)",
                (FIXTURE_IMPORT_ID,),
            )
            if fixture_vector_refs:
                connection.execute(
                    f"DELETE FROM local_embedding_vectors WHERE vector_ref IN ({','.join('?' for _ in fixture_vector_refs)})",
                    fixture_vector_refs,
                )
        if records:
            record_id = records[0]["id"]
            connection.execute(
                "DELETE FROM memory_links WHERE from_kind = 'memory_record' AND from_id = ?", (record_id,)
            )
            connection.execute(
                "DELETE FROM candidate_merge_events WHERE operation_id = ?", (events[0]["operation_id"],)
            )
            connection.execute("DELETE FROM memory_records WHERE id = ?", (record_id,))
        connection.execute("DELETE FROM chatgpt_chunks_fts WHERE import_id = ?", (FIXTURE_IMPORT_ID,))
        connection.execute("DELETE FROM imports WHERE id = ?", (FIXTURE_IMPORT_ID,))
        subject = before.get("subject")
        if subject and subject.get("fixture_owned"):
            reference_count = sum(
                connection.execute(query, (FIXTURE_SUBJECT_ID,)).fetchone()[0]
                for query in (
                    "SELECT COUNT(*) FROM memory_records WHERE subject_id = ?",
                    "SELECT COUNT(*) FROM conversation_subjects WHERE subject_id = ?",
                    "SELECT COUNT(*) FROM chunk_subjects WHERE subject_id = ?",
                )
            )
            if reference_count:
                raise _fixture_error(
                    "fixture-owned subject still has references after scoped cleanup",
                    error_code="browser_merge_fixture_cleanup_refused",
                )
            connection.execute(
                "DELETE FROM subjects WHERE id = ? AND json_extract(metadata_json, '$.fixture_suite') = ?",
                (FIXTURE_SUBJECT_ID, FIXTURE_SUITE),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise

    after = browser_merge_fixture_status(connection)
    if after["state"] != "absent":
        raise _fixture_error(
            f"browser merge fixture cleanup failed validation: {after['state']}",
            error_code="browser_merge_fixture_cleanup_failed",
        )
    return {
        **after,
        "database_changed": True,
        "idempotent": False,
        "removed_candidate_ids": list(FIXTURE_CANDIDATE_IDS),
        "removed_record_id": records[0]["id"] if records else None,
        "removed_operation_id": events[0]["operation_id"] if events else None,
    }


def _candidate_status(row: sqlite3.Row) -> dict[str, Any]:
    metadata = json.loads(row["metadata_json"] or "{}")
    return {
        "id": str(row["id"]),
        "source_role": str(row["source_role"]),
        "assistant_suggestion": bool(row["assistant_suggestion"]),
        "review_status": str(row["review_status"]),
        "domain_primary": str(row["domain_primary"]),
        "source_kind": str(row["source_kind"]),
        "source_ref": str(row["source_ref"]),
        "merge_target_id": metadata.get("merge_target_id"),
        "metadata": metadata,
    }


def _record_status(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "title": str(row["title"]),
        "body": str(row["body"]),
        "source_kind": str(row["source_kind"]),
        "source_ref": row["source_ref"],
        "provenance": json.loads(row["provenance_json"] or "{}"),
        "status": str(row["status"]),
    }


def _event_status(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "operation_id": str(row["operation_id"]),
        "record_id": str(row["record_id"]),
        "candidate_ids": json.loads(row["candidate_ids_json"] or "[]"),
        "created_by": str(row["created_by"]),
        "created_at": str(row["created_at"]),
    }


def _subject_status(row: sqlite3.Row) -> dict[str, Any]:
    metadata = json.loads(row["metadata_json"] or "{}")
    return {
        "id": str(row["id"]),
        "slug": str(row["slug"]),
        "kind": str(row["kind"]),
        "name": str(row["name"]),
        "fixture_owned": bool(
            metadata.get("disposable_test_fixture") is True and metadata.get("fixture_suite") == FIXTURE_SUITE
        ),
        "metadata": metadata,
    }


def _is_exact_fixture_candidate(candidate: dict[str, Any]) -> bool:
    metadata = candidate["metadata"]
    expected_role = "assistant" if candidate["id"] == ASSISTANT_CANDIDATE_ID else "user"
    return bool(
        candidate["id"] in FIXTURE_CANDIDATE_IDS
        and candidate["source_role"] == expected_role
        and candidate["assistant_suggestion"] == (expected_role == "assistant")
        and candidate["domain_primary"] == "cooking_baking"
        and candidate["source_kind"] == f"test_fixture:{FIXTURE_SUITE}"
        and metadata.get("disposable_test_fixture") is True
        and metadata.get("fixture_suite") == FIXTURE_SUITE
        and str(metadata.get("source_title") or "").startswith("[DISPOSABLE TEST FIXTURE]")
    )


def _is_exact_fixture_record(record: dict[str, Any]) -> bool:
    provenance = record["provenance"]
    return bool(
        record["title"] == FIXTURE_RECORD_TITLE
        and record["body"] == FIXTURE_RECORD_BODY
        and record["source_kind"] == "chatgpt_candidate_merge"
        and provenance.get("source_candidate_ids") == list(SELECTED_CANDIDATE_IDS)
        and provenance.get("user_candidate_ids") == [USER_CANDIDATE_ID]
        and provenance.get("assistant_candidate_ids") == [ASSISTANT_CANDIDATE_ID]
        and provenance.get("assistant_suggestions_separate") is True
    )


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _fixture_error(message: str, *, error_code: str) -> MemoryObservationError:
    return MemoryObservationError(
        message,
        stage="browser_merge_fixture",
        error_code=error_code,
        source_ref=FIXTURE_SUITE,
    )
