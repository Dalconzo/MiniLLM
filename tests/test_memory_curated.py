import json
import sqlite3

import pytest

from local_agent_lab.memory.chatgpt_ingest import import_chatgpt_export
from local_agent_lab.memory.curated import (
    create_memory_link,
    create_memory_record,
    get_memory_record,
    init_curated_memory_schema,
    list_memory_links,
    list_memory_records,
    promote_chunk_to_memory_record,
    record_type_options,
    status_options,
    trust_level_options,
    update_memory_record_status,
)
from local_agent_lab.memory.subjects import init_subject_schema, upsert_subject


def _write_export(root):
    export_dir = root / "raw" / "export-1"
    export_dir.mkdir(parents=True)
    export = [
        {
            "id": "conversation-alpha",
            "title": "Plate reader workflow",
            "create_time": 1_700_000_000,
            "update_time": 1_700_000_100,
            "mapping": {
                "root": {
                    "id": "root",
                    "message": None,
                    "parent": None,
                    "children": ["msg-user"],
                },
                "msg-user": {
                    "id": "msg-user",
                    "parent": "root",
                    "children": ["msg-assistant"],
                    "message": {
                        "id": "msg-user",
                        "author": {"role": "user", "name": None},
                        "create_time": 1_700_000_001,
                        "content": {"content_type": "text", "parts": ["How do we parse plate reader CSV files?"]},
                    },
                },
                "msg-assistant": {
                    "id": "msg-assistant",
                    "parent": "msg-user",
                    "children": [],
                    "message": {
                        "id": "msg-assistant",
                        "author": {"role": "assistant", "name": None},
                        "create_time": 1_700_000_002,
                        "content": {"content_type": "text", "parts": ["Use the lab automation parser module."]},
                    },
                },
            },
        }
    ]
    (export_dir / "conversations.json").write_text(json.dumps(export), encoding="utf-8")
    return root / "raw"


@pytest.fixture()
def memory_connection(tmp_path):
    input_path = _write_export(tmp_path)
    data_dir = tmp_path / "data"
    memory_dir = data_dir / "memory"
    import_chatgpt_export(input_path=input_path, data_dir=data_dir, memory_dir=memory_dir)
    with sqlite3.connect(memory_dir / "chatgpt_memory.sqlite3") as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        init_subject_schema(connection)
        init_curated_memory_schema(connection)
        yield connection


def test_curated_schema_creates_contract_tables(memory_connection) -> None:
    tables = {
        row[0]
        for row in memory_connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual table')"
        ).fetchall()
    }

    assert "memory_records" in tables
    assert "memory_links" in tables
    assert memory_connection.execute(
        "SELECT COUNT(*) FROM schema_migrations WHERE name = 'chatgpt_memory_curated_records'"
    ).fetchone()[0] == 1


def test_create_get_and_list_manual_memory_records(memory_connection) -> None:
    subject = upsert_subject(memory_connection, "Lab Automation", kind="subject")

    record = create_memory_record(
        memory_connection,
        record_type="preference",
        title="Prefer traceable memory",
        body="Memory features should expose run IDs, source IDs, and score explanations.",
        subject_id=subject.id,
        trust_level="canonical",
        provenance={"captured_from": "planning"},
        metadata={"owner": "local-agent"},
    )

    fetched = get_memory_record(memory_connection, record.id)
    assert fetched.to_dict()["title"] == "Prefer traceable memory"
    assert fetched.subject_id == subject.id
    assert fetched.trust_level == "canonical"
    assert fetched.provenance == {"captured_from": "planning"}
    assert fetched.metadata == {"owner": "local-agent"}

    assert list_memory_records(memory_connection, record_type="preference")[0].id == record.id
    assert list_memory_records(memory_connection, trust_level="canonical")[0].id == record.id
    assert list_memory_records(memory_connection, subject_id=subject.id)[0].id == record.id


def test_promote_chunk_to_record_captures_provenance_and_links(memory_connection) -> None:
    chunk_id = memory_connection.execute(
        """
        SELECT id
        FROM message_chunks
        WHERE text LIKE '%parser module%'
        """
    ).fetchone()[0]

    record = promote_chunk_to_memory_record(
        memory_connection,
        chunk_id,
        record_type="workflow",
        title="Plate reader parser workflow",
        trust_level="high",
        created_by="agent",
        metadata={"reviewed": True},
    )

    assert record.source_kind == "chatgpt_chunk"
    assert record.source_ref == chunk_id
    assert record.body == "Use the lab automation parser module."
    assert record.provenance["source"]["chunk_id"] == chunk_id
    assert record.provenance["source"]["conversation_title"] == "Plate reader workflow"
    assert record.metadata == {"reviewed": True}

    links = list_memory_links(memory_connection, from_kind="memory_record", from_id=record.id)
    assert {(link.to_kind, link.to_id, link.link_type) for link in links} == {
        ("conversation", record.provenance["source"]["conversation_id"], "derived_from"),
        ("message_chunk", chunk_id, "derived_from"),
    }


def test_memory_links_are_idempotent_and_listable(memory_connection) -> None:
    first = create_memory_link(
        memory_connection,
        from_kind="memory_record",
        from_id="mem_a",
        to_kind="memory_record",
        to_id="mem_b",
        link_type="supersedes",
        confidence=0.5,
        notes="initial",
    )
    second = create_memory_link(
        memory_connection,
        from_kind="memory_record",
        from_id="mem_a",
        to_kind="memory_record",
        to_id="mem_b",
        link_type="supersedes",
        confidence=0.9,
        notes="updated",
    )

    assert first.link_type == second.link_type
    links = list_memory_links(memory_connection, to_kind="memory_record", to_id="mem_b")
    assert len(links) == 1
    assert links[0].confidence == 0.9
    assert links[0].notes == "updated"


def test_update_status_hides_non_active_records_by_default(memory_connection) -> None:
    record = create_memory_record(
        memory_connection,
        record_type="decision",
        title="Use SQLite first",
        body="Keep the first memory implementation boring and local.",
        trust_level="high",
    )

    updated = update_memory_record_status(
        memory_connection,
        record.id,
        "stale",
        metadata_patch={"reason": "replaced"},
    )

    assert updated.status == "stale"
    assert updated.metadata["reason"] == "replaced"
    assert list_memory_records(memory_connection) == []
    assert list_memory_records(memory_connection, status="stale")[0].id == record.id
    assert list_memory_records(memory_connection, status=None)[0].id == record.id


def test_curated_helpers_validate_enums_and_missing_chunks(memory_connection) -> None:
    assert "decision" in record_type_options()
    assert trust_level_options() == ("low", "medium", "high", "canonical")
    assert status_options() == ("active", "stale", "superseded", "archived", "deleted")

    with pytest.raises(ValueError, match="invalid memory record type"):
        create_memory_record(
            memory_connection,
            record_type="random",
            title="Nope",
            body="Nope",
        )

    with pytest.raises(ValueError, match="invalid memory trust level"):
        create_memory_record(
            memory_connection,
            record_type="decision",
            title="Nope",
            body="Nope",
            trust_level="absolute",
        )

    with pytest.raises(KeyError, match="message chunk not found"):
        promote_chunk_to_memory_record(
            memory_connection,
            "missing-chunk",
            record_type="lesson",
        )
