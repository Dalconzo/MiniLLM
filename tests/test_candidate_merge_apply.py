from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from local_agent_lab.memory.candidates import init_candidate_memory_schema
from local_agent_lab.memory.audit import tombstone_source
from local_agent_lab.memory.chatgpt_ingest import init_chatgpt_memory_schema
from local_agent_lab.memory.curated import get_memory_record, init_curated_memory_schema, list_memory_links
from local_agent_lab.memory.observability import MemoryObservationError
from local_agent_lab.memory.subjects import init_subject_schema, upsert_subject


@pytest.fixture()
def merge_connection(tmp_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(tmp_path / "candidate-merge.sqlite3")
    connection.execute("PRAGMA foreign_keys = ON")
    init_chatgpt_memory_schema(connection)
    init_candidate_memory_schema(connection)
    init_subject_schema(connection)
    init_curated_memory_schema(connection)
    upsert_subject(connection, "Recipes and Baking", kind="subject")
    _insert_import(connection)

    _insert_candidate(
        connection,
        candidate_id="cand_user_state",
        source_role="user",
        content="My starter rose 75 to 100 percent after feeding and then fell about 25 percent.",
        memory_type="episodic",
        reason_type="reported_outcome",
    )
    _insert_candidate(
        connection,
        candidate_id="cand_user_preference",
        source_role="user",
        content="I want starter tracking to preserve feed times, peak rise, and fall observations.",
        memory_type="preference",
        reason_type="preference_choice",
    )
    _insert_candidate(
        connection,
        candidate_id="cand_assistant_tip",
        source_role="assistant",
        content="The assistant suggested recording ambient temperature at every feeding.",
        memory_type="assistant_suggestion",
        reason_type="assistant_suggestion",
        assistant_suggestion=True,
    )
    _insert_candidate(
        connection,
        candidate_id="cand_unselected",
        source_role="user",
        content="I prefer crisp edges on rosemary focaccia.",
        memory_type="preference",
        reason_type="preference_choice",
    )
    _insert_candidate(
        connection,
        candidate_id="cand_conflict_old",
        source_role="user",
        content="I prefer maintaining the starter at a low hydration.",
        memory_type="preference",
        reason_type="preference_choice",
    )
    _insert_candidate(
        connection,
        candidate_id="cand_conflict_new",
        source_role="user",
        content="That was wrong; I do not prefer low hydration and changed to equal flour and water.",
        memory_type="preference",
        reason_type="correction",
    )
    connection.commit()

    try:
        yield connection
    finally:
        connection.close()


def test_apply_candidate_merge_creates_one_record_and_preserves_all_evidence(
    merge_connection: sqlite3.Connection,
) -> None:
    selected = ["cand_user_state", "cand_user_preference"]
    before = _truth_counts(merge_connection)

    payload = _apply(
        merge_connection,
        selected,
        title="Sourdough starter tracking state",
        body=(
            "The starter rose 75 to 100 percent after feeding and then fell about 25 percent. "
            "Track feed time, peak rise, and subsequent fall."
        ),
    )

    assert payload["mode"] == "apply"
    assert payload["mutates_memory_truth"] is True
    assert payload["mutation_policy"]["mutates_memory_truth"] is True
    assert payload["candidate_ids"] == selected

    after = _truth_counts(merge_connection)
    assert after["memory_records"] == before["memory_records"] + 1
    record = get_memory_record(merge_connection, payload["memory_record"]["id"])
    assert record.title == "Sourdough starter tracking state"
    assert record.source_kind == "chatgpt_candidate_merge"
    assert record.provenance["merge"]["source_candidate_ids"] == selected
    assert record.provenance["merge"]["user_candidate_ids"] == selected
    assert record.provenance["merge"]["assistant_candidate_ids"] == []

    links = list_memory_links(merge_connection, from_kind="memory_record", from_id=record.id)
    assert {link.to_id for link in links if link.to_kind == "candidate_memory"} == set(selected)
    assert {link.to_id for link in links if link.to_kind == "message_chunk"} == {
        _chunk_id(candidate_id) for candidate_id in selected
    }

    statuses = _candidate_statuses(merge_connection)
    assert {statuses[candidate_id] for candidate_id in selected} == {"merged"}
    assert statuses["cand_unselected"] == "pending"
    assert statuses["cand_assistant_tip"] == "pending"
    event = merge_connection.execute(
        "SELECT record_id, candidate_ids_json, candidate_states_before_json FROM candidate_merge_events"
    ).fetchone()
    assert event is not None
    assert event[0] == record.id
    assert json.loads(event[1]) == selected
    assert set(json.loads(event[2])) == set(selected)


def test_apply_candidate_merge_preserves_user_and_assistant_evidence_separately(
    merge_connection: sqlite3.Connection,
) -> None:
    payload = _apply(
        merge_connection,
        ["cand_user_state", "cand_assistant_tip"],
        title="Starter observation with unconfirmed guidance",
        body="The user reported a 75 to 100 percent rise followed by a 25 percent fall.",
        confirm_conflicts=True,
    )

    record = get_memory_record(merge_connection, payload["memory_record"]["id"])
    assert record.provenance["merge"]["user_candidate_ids"] == ["cand_user_state"]
    assert record.provenance["merge"]["assistant_candidate_ids"] == ["cand_assistant_tip"]
    assert record.provenance["merge"]["assistant_suggestions_separate"] is True
    assert payload["mutation_policy"]["assistant_suggestions_promoted_to_fact"] is False
    assert "ambient temperature" not in record.body

    evidence = {item["candidate_id"]: item for item in record.provenance["evidence"]}
    assert evidence["cand_user_state"]["source_role"] == "user"
    assert evidence["cand_user_state"]["assistant_suggestion"] is False
    assert evidence["cand_assistant_tip"]["source_role"] == "assistant"
    assert evidence["cand_assistant_tip"]["assistant_suggestion"] is True


def test_apply_candidate_merge_rejects_invalid_ids_atomically(merge_connection: sqlite3.Connection) -> None:
    before = _truth_counts(merge_connection)
    statuses_before = _candidate_statuses(merge_connection)

    with pytest.raises((KeyError, MemoryObservationError)) as exc_info:
        _apply(
            merge_connection,
            ["cand_user_state", "cand_missing"],
            title="Must not be created",
            body="This write must be atomic.",
        )

    if isinstance(exc_info.value, MemoryObservationError):
        assert exc_info.value.error_code == "candidate_merge_candidate_not_found"
    assert _truth_counts(merge_connection) == before
    assert _candidate_statuses(merge_connection) == statuses_before


def test_apply_candidate_merge_repeated_apply_is_idempotent_or_a_typed_conflict(
    merge_connection: sqlite3.Connection,
) -> None:
    kwargs = {
        "title": "Sourdough starter tracking state",
        "body": "Track feed time, peak rise, and fall after peak.",
    }
    first = _apply(merge_connection, ["cand_user_state", "cand_user_preference"], **kwargs)
    record_id = first["memory_record"]["id"]
    counts_after_first = _truth_counts(merge_connection)

    try:
        second = _apply(
            merge_connection,
            ["cand_user_state", "cand_user_preference"],
            preview_token=first["preview_token"],
            **kwargs,
        )
    except MemoryObservationError as exc:
        assert exc.error_code in {"candidate_merge_already_applied", "candidate_merge_status_conflict"}
    else:
        assert second["memory_record"]["id"] == record_id
        assert second["idempotent"] is True

    assert _truth_counts(merge_connection) == counts_after_first
    assert _candidate_statuses(merge_connection)["cand_user_state"] == "merged"
    assert _candidate_statuses(merge_connection)["cand_user_preference"] == "merged"


def test_apply_candidate_merge_refuses_unconfirmed_conflicts_without_writes(
    merge_connection: sqlite3.Connection,
) -> None:
    selected = ["cand_conflict_old", "cand_conflict_new"]
    before = _truth_counts(merge_connection)
    statuses_before = _candidate_statuses(merge_connection)

    with pytest.raises(MemoryObservationError) as exc_info:
        _apply(
            merge_connection,
            selected,
            title="Starter hydration preference",
            body="The current preference is equal weights of flour and water.",
        )

    assert exc_info.value.error_code == "candidate_merge_unresolved_conflict"
    assert _truth_counts(merge_connection) == before
    assert _candidate_statuses(merge_connection) == statuses_before


def test_apply_candidate_merge_rejects_tombstoned_source_without_writes(
    merge_connection: sqlite3.Connection,
) -> None:
    selected = ["cand_user_state", "cand_user_preference"]
    preview = _preview(
        merge_connection,
        selected,
        title="Blocked source merge",
        body="This must not be written.",
    )
    tombstone_source(
        merge_connection,
        source_kind="message_chunk",
        source_id=_chunk_id("cand_user_state"),
        reason="disposable blocked-source test",
    )
    before = _truth_counts(merge_connection)

    with pytest.raises(MemoryObservationError) as exc_info:
        _apply(
            merge_connection,
            selected,
            title="Blocked source merge",
            body="This must not be written.",
            preview_token=preview["preview_token"],
        )

    assert exc_info.value.error_code == "candidate_merge_source_blocked"
    assert _truth_counts(merge_connection) == before


def test_apply_candidate_merge_rolls_back_record_links_and_audit_on_update_failure(
    merge_connection: sqlite3.Connection,
) -> None:
    selected = ["cand_user_state", "cand_user_preference"]
    preview = _preview(
        merge_connection,
        selected,
        title="Atomic rollback fixture",
        body="No partial state may survive.",
    )
    merge_connection.execute(
        """
        CREATE TRIGGER fail_candidate_merge_update
        BEFORE UPDATE ON candidate_memories
        WHEN OLD.id = 'cand_user_preference'
        BEGIN
            SELECT RAISE(ABORT, 'injected candidate update failure');
        END;
        """
    )
    merge_connection.commit()
    before = _truth_counts(merge_connection)
    statuses_before = _candidate_statuses(merge_connection)

    with pytest.raises(sqlite3.IntegrityError, match="injected candidate update failure"):
        _apply(
            merge_connection,
            selected,
            title="Atomic rollback fixture",
            body="No partial state may survive.",
            preview_token=preview["preview_token"],
        )

    assert _truth_counts(merge_connection) == before
    assert _candidate_statuses(merge_connection) == statuses_before
    assert merge_connection.execute("SELECT COUNT(*) FROM candidate_merge_events").fetchone()[0] == 0


def _apply(
    connection: sqlite3.Connection,
    candidate_ids: list[str],
    *,
    title: str,
    body: str,
    confirm_conflicts: bool = False,
    preview_token: str | None = None,
) -> dict[str, Any]:
    # Import lazily so this acceptance sidecar can be collected independently.
    from local_agent_lab.memory import candidate_merge

    apply_candidate_merge = getattr(candidate_merge, "apply_candidate_merge", None)
    assert callable(apply_candidate_merge), "candidate_merge.apply_candidate_merge must be exposed"
    if preview_token is None:
        preview = candidate_merge.preview_candidate_merge(
            connection,
            candidate_ids,
            title=title,
            body=body,
            record_type="lesson",
            trust_level="high",
            subject="Recipes and Baking",
            created_by="user",
        )
        preview_token = preview["preview_token"]
    return apply_candidate_merge(
        connection,
        candidate_ids,
        preview_token=preview_token,
        confirmed=True,
        title=title,
        body=body,
        record_type="lesson",
        trust_level="high",
        subject="Recipes and Baking",
        created_by="user",
        allow_conflicts=confirm_conflicts,
    )


def _preview(
    connection: sqlite3.Connection,
    candidate_ids: list[str],
    *,
    title: str,
    body: str,
) -> dict[str, Any]:
    from local_agent_lab.memory.candidate_merge import preview_candidate_merge

    return preview_candidate_merge(
        connection,
        candidate_ids,
        title=title,
        body=body,
        record_type="lesson",
        trust_level="high",
        subject="Recipes and Baking",
        created_by="user",
    )


def _insert_import(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        INSERT INTO imports (
            id, source_root, raw_manifest_path, imported_at, status, parser_version,
            file_count, conversation_count, message_count, chunk_count, attachment_count,
            content_sha256, notes
        )
        VALUES ('imp_merge', '/fixture', '', datetime('now'), 'ok', 'test', 1, 6, 6, 6, 0, 'fixture', '')
        """
    )


def _insert_candidate(
    connection: sqlite3.Connection,
    *,
    candidate_id: str,
    source_role: str,
    content: str,
    memory_type: str,
    reason_type: str,
    assistant_suggestion: bool = False,
) -> None:
    conversation_id = f"conv_{candidate_id}"
    message_id = f"msg_{candidate_id}"
    chunk_id = _chunk_id(candidate_id)
    connection.execute(
        """
        INSERT INTO conversations (
            id, import_id, source_conversation_id, title, created_at, updated_at,
            message_count, first_message_at, last_message_at, summary, content_sha256,
            is_deleted, metadata_json
        ) VALUES (?, 'imp_merge', ?, 'Sourdough tracking fixture', datetime('now'), datetime('now'),
                  1, datetime('now'), datetime('now'), NULL, ?, 0, '{}')
        """,
        (conversation_id, conversation_id, f"sha-{conversation_id}"),
    )
    connection.execute(
        """
        INSERT INTO messages (
            id, conversation_id, import_id, source_message_id, parent_message_id, role,
            author_name, turn_index, created_at, content_text, content_sha256, token_estimate,
            attachment_count, is_deleted, metadata_json
        ) VALUES (?, ?, 'imp_merge', ?, NULL, ?, NULL, 0, datetime('now'), ?, ?, 20, 0, 0, '{}')
        """,
        (message_id, conversation_id, message_id, source_role, content, f"sha-{message_id}"),
    )
    connection.execute(
        """
        INSERT INTO message_chunks (
            id, message_id, conversation_id, import_id, chunk_index, text, text_sha256,
            token_estimate, start_char, end_char, source_kind, summary, is_deleted, metadata_json
        ) VALUES (?, ?, ?, 'imp_merge', 0, ?, ?, 20, 0, ?, 'chatgpt_export', NULL, 0, '{}')
        """,
        (chunk_id, message_id, conversation_id, content, f"sha-{chunk_id}", len(content)),
    )
    connection.execute(
        """
        INSERT INTO candidate_memories (
            id, import_id, conversation_id, message_id, chunk_id, source_kind, source_ref,
            source_role, memory_type, reason_type, domain_primary, domains_json, confidence,
            valid_from, valid_to, last_confirmed_at, review_status, review_notes, origin,
            assistant_suggestion, source_links_json, content, metadata_json, created_at, updated_at
        ) VALUES (?, 'imp_merge', ?, ?, ?, 'chatgpt_export', ?, ?, ?, ?, 'cooking_baking', ?, 0.9,
                  datetime('now'), NULL, NULL, 'pending', NULL, 'chatgpt_export', ?, ?, ?, '{}',
                  datetime('now'), datetime('now'))
        """,
        (
            candidate_id,
            conversation_id,
            message_id,
            chunk_id,
            chunk_id,
            source_role,
            memory_type,
            reason_type,
            json.dumps(["cooking_baking"]),
            int(assistant_suggestion),
            json.dumps(
                {
                    "conversation_id": conversation_id,
                    "message_id": message_id,
                    "chunk_id": chunk_id,
                    "source_kind": "chatgpt_export",
                    "source_role": source_role,
                },
                sort_keys=True,
            ),
            content,
        ),
    )


def _chunk_id(candidate_id: str) -> str:
    return f"chk_{candidate_id}"


def _truth_counts(connection: sqlite3.Connection) -> dict[str, int]:
    return {
        table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in ("candidate_memories", "memory_records", "memory_links")
    }


def _candidate_statuses(connection: sqlite3.Connection) -> dict[str, str]:
    return dict(connection.execute("SELECT id, review_status FROM candidate_memories").fetchall())
