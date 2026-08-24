from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from local_agent_lab.memory.browser_merge_fixture import (
    ASSISTANT_CANDIDATE_ID,
    FIXTURE_CANDIDATE_IDS,
    FIXTURE_IMPORT_ID,
    FIXTURE_RECORD_BODY,
    FIXTURE_RECORD_TITLE,
    UNSELECTED_CANDIDATE_ID,
    USER_CANDIDATE_ID,
    browser_merge_fixture_status,
    cleanup_browser_merge_fixture,
    seed_browser_merge_fixture,
)
from local_agent_lab.memory.candidate_merge import apply_candidate_merge, preview_candidate_merge
from local_agent_lab.memory.observability import MemoryObservationError


@pytest.fixture
def connection(tmp_path: Path):
    db_path = tmp_path / "chatgpt_memory.sqlite3"
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        yield db


def test_browser_merge_fixture_seed_is_exact_and_idempotent(connection: sqlite3.Connection) -> None:
    assert browser_merge_fixture_status(connection)["state"] == "absent"

    seeded = seed_browser_merge_fixture(connection)

    assert seeded["state"] == "ready"
    assert seeded["database_changed"] is True
    assert seeded["idempotent"] is False
    assert {candidate["id"] for candidate in seeded["candidates"]} == set(FIXTURE_CANDIDATE_IDS)
    assert all(candidate["review_status"] == "pending" for candidate in seeded["candidates"])
    assistant = next(candidate for candidate in seeded["candidates"] if candidate["id"] == ASSISTANT_CANDIDATE_ID)
    assert assistant["source_role"] == "assistant"
    assert assistant["assistant_suggestion"] is True
    assert assistant["metadata"]["disposable_test_fixture"] is True
    assert assistant["metadata"]["source_title"].startswith("[DISPOSABLE TEST FIXTURE]")
    assert seeded["subject"]["name"] == "Recipes and Baking"
    assert seeded["subject"]["fixture_owned"] is True

    repeated = seed_browser_merge_fixture(connection)

    assert repeated["state"] == "ready"
    assert repeated["database_changed"] is False
    assert repeated["idempotent"] is True
    assert connection.execute("SELECT COUNT(*) FROM imports WHERE id = ?", (FIXTURE_IMPORT_ID,)).fetchone()[0] == 1


def test_browser_merge_fixture_preview_is_clean_and_non_mutating(connection: sqlite3.Connection) -> None:
    seed_browser_merge_fixture(connection)
    before = browser_merge_fixture_status(connection)

    preview = preview_candidate_merge(
        connection,
        [USER_CANDIDATE_ID, ASSISTANT_CANDIDATE_ID],
        title=FIXTURE_RECORD_TITLE,
        body=FIXTURE_RECORD_BODY,
        record_type="lesson",
        trust_level="low",
        subject="Recipes and Baking",
        created_by="browser_chatgpt",
    )

    assert preview["mutates_memory_truth"] is False
    assert [warning["code"] for warning in preview["conflict_warnings"]] == ["assistant_evidence_present"]
    assert preview["user_candidate_ids"] == [USER_CANDIDATE_ID]
    assert preview["assistant_candidate_ids"] == [ASSISTANT_CANDIDATE_ID]
    assert preview["proposed_record"]["body"] == FIXTURE_RECORD_BODY
    assert browser_merge_fixture_status(connection) == before


def test_browser_merge_fixture_cleanup_requires_confirmation(connection: sqlite3.Connection) -> None:
    seed_browser_merge_fixture(connection)

    with pytest.raises(MemoryObservationError) as exc_info:
        cleanup_browser_merge_fixture(connection, confirmed=False)

    assert exc_info.value.error_code == "browser_merge_fixture_cleanup_confirmation_required"
    assert browser_merge_fixture_status(connection)["state"] == "ready"

    cleaned = cleanup_browser_merge_fixture(connection, confirmed=True)
    assert cleaned["state"] == "absent"
    assert cleaned["database_changed"] is True
    assert cleaned["subject"] is None


def test_browser_merge_fixture_cleanup_removes_fixture_embedding_vectors(connection: sqlite3.Connection) -> None:
    seed_browser_merge_fixture(connection)
    connection.executescript(
        """
        CREATE TABLE embedding_models (
            id TEXT PRIMARY KEY, provider TEXT, model TEXT, dimension INTEGER,
            normalize INTEGER, created_at TEXT, metadata_json TEXT
        );
        CREATE TABLE chunk_embeddings (
            chunk_id TEXT, embedding_model_id TEXT, vector_backend TEXT, vector_ref TEXT,
            text_sha256 TEXT, embedded_at TEXT, is_stale INTEGER, metadata_json TEXT,
            PRIMARY KEY(chunk_id, embedding_model_id)
        );
        CREATE TABLE local_embedding_vectors (
            vector_ref TEXT PRIMARY KEY, embedding_model_id TEXT, vector_json TEXT,
            dimension INTEGER, created_at TEXT, metadata_json TEXT
        );
        INSERT INTO embedding_models VALUES ('emb_fixture', 'ollama', 'fixture', 2, 1, 'now', '{}');
        INSERT INTO local_embedding_vectors VALUES ('vec_fixture', 'emb_fixture', '[0,1]', 2, 'now', '{}');
        INSERT INTO chunk_embeddings
        VALUES ('chk_browser_candidate_merge_v1_1', 'emb_fixture', 'sqlite', 'vec_fixture', 'sha', 'now', 0, '{}');
        """
    )

    cleanup_browser_merge_fixture(connection, confirmed=True)

    assert connection.execute("SELECT COUNT(*) FROM chunk_embeddings").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM local_embedding_vectors").fetchone()[0] == 0


def test_browser_merge_fixture_cleanup_verifies_applied_record_and_event(connection: sqlite3.Connection) -> None:
    seed_browser_merge_fixture(connection)
    preview = preview_candidate_merge(
        connection,
        [USER_CANDIDATE_ID, ASSISTANT_CANDIDATE_ID],
        title=FIXTURE_RECORD_TITLE,
        body=FIXTURE_RECORD_BODY,
        record_type="lesson",
        trust_level="low",
        subject="Recipes and Baking",
        created_by="browser_chatgpt",
    )
    applied = apply_candidate_merge(
        connection,
        [USER_CANDIDATE_ID, ASSISTANT_CANDIDATE_ID],
        preview_token=preview["preview_token"],
        confirmed=True,
        title=FIXTURE_RECORD_TITLE,
        body=FIXTURE_RECORD_BODY,
        record_type="lesson",
        trust_level="low",
        subject="Recipes and Baking",
        created_by="browser_chatgpt",
        caller_run_id="run_fixture_test",
    )
    record_id = applied["memory_record"]["id"]
    operation_id = applied["audit"]["operation_id"]
    status = browser_merge_fixture_status(connection)
    assert status["state"] == "applied_or_partial"
    selected = {candidate["id"]: candidate for candidate in status["candidates"]}
    assert selected[USER_CANDIDATE_ID]["review_status"] == "merged"
    assert selected[ASSISTANT_CANDIDATE_ID]["review_status"] == "merged"
    assert selected[UNSELECTED_CANDIDATE_ID]["review_status"] == "pending"

    with pytest.raises(MemoryObservationError) as exc_info:
        cleanup_browser_merge_fixture(
            connection,
            confirmed=True,
            expected_record_id="mem_wrong",
            expected_operation_id=operation_id,
        )
    assert exc_info.value.error_code == "browser_merge_fixture_record_mismatch"
    assert browser_merge_fixture_status(connection)["state"] == "applied_or_partial"

    cleaned = cleanup_browser_merge_fixture(
        connection,
        confirmed=True,
        expected_record_id=record_id,
        expected_operation_id=operation_id,
    )

    assert cleaned["state"] == "absent"
    assert cleaned["removed_record_id"] == record_id
    assert cleaned["removed_operation_id"] == operation_id
    assert connection.execute("SELECT COUNT(*) FROM memory_records WHERE id = ?", (record_id,)).fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM candidate_merge_events WHERE operation_id = ?", (operation_id,)).fetchone()[0] == 0
