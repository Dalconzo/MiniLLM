import json
import sqlite3

import pytest

from local_agent_lab.memory.chatgpt_ingest import import_chatgpt_export
from local_agent_lab.memory.subjects import (
    assign_chunk_subject,
    assign_conversation_subject,
    get_subject,
    init_subject_schema,
    list_conversation_subjects,
    list_subject_conversations,
    list_subjects,
    normalize_subject_slug,
    remove_conversation_subject,
    resolve_subject,
    upsert_subject,
)


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
        },
        {
            "id": "conversation-beta",
            "title": "Code review habit",
            "create_time": 1_700_001_000,
            "update_time": 1_700_001_100,
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
                    "children": [],
                    "message": {
                        "id": "msg-user",
                        "author": {"role": "user", "name": None},
                        "create_time": 1_700_001_001,
                        "content": {"content_type": "text", "parts": ["Review this diff before merge."]},
                    },
                },
            },
        },
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
        yield connection


def test_normalize_subject_slug_is_stable_ascii() -> None:
    assert normalize_subject_slug(" Lab Automation / Plate Readers! ") == "lab-automation-plate-readers"
    assert normalize_subject_slug("Café workflow") == "cafe-workflow"
    assert normalize_subject_slug("!!!") == "untitled"


def test_upsert_subject_supports_subject_project_and_workflow(memory_connection) -> None:
    subject = upsert_subject(
        memory_connection,
        "Lab Automation",
        kind="subject",
        description="Wet-lab automation work",
        metadata={"owner": "local"},
    )
    project = upsert_subject(memory_connection, "MiniLLM", kind="project")
    workflow = upsert_subject(memory_connection, "Code Review", kind="workflow")

    assert subject.id == "subject_lab-automation"
    assert subject.metadata == {"owner": "local"}
    assert project.id == "project_minillm"
    assert workflow.id == "workflow_code-review"
    assert get_subject(memory_connection, "lab automation").description == "Wet-lab automation work"


def test_resolve_subject_supports_known_aliases_without_creating_subjects(memory_connection) -> None:
    upsert_subject(memory_connection, "AI Memory and Local LLMs")

    resolved, alias_target = resolve_subject(memory_connection, "Memory System")

    assert resolved.name == "AI Memory and Local LLMs"
    assert alias_target == "AI Memory and Local LLMs"
    assert len(list_subjects(memory_connection)) == 1


def test_assign_conversation_subject_lists_counts_and_recency(memory_connection) -> None:
    conversation_id = memory_connection.execute(
        "SELECT id FROM conversations WHERE title = 'Plate reader workflow'"
    ).fetchone()[0]

    assign_conversation_subject(
        memory_connection,
        conversation_id,
        "Lab Automation",
        confidence=0.95,
        source="manual",
        include_chunks=True,
    )

    summaries = list_subjects(memory_connection)
    assert len(summaries) == 1
    assert summaries[0].subject.slug == "lab-automation"
    assert summaries[0].conversation_count == 1
    assert summaries[0].chunk_count == 2
    assert summaries[0].latest_activity_at is not None

    assignments = list_conversation_subjects(memory_connection, conversation_id)
    assert assignments[0]["subject"]["slug"] == "lab-automation"
    assert assignments[0]["confidence"] == 0.95
    assert assignments[0]["source"] == "manual"


def test_list_subject_conversations_returns_recent_conversations(memory_connection) -> None:
    plate_reader_id = memory_connection.execute(
        "SELECT id FROM conversations WHERE title = 'Plate reader workflow'"
    ).fetchone()[0]
    code_review_id = memory_connection.execute(
        "SELECT id FROM conversations WHERE title = 'Code review habit'"
    ).fetchone()[0]

    assign_conversation_subject(memory_connection, plate_reader_id, "Workflows", kind="subject")
    assign_conversation_subject(memory_connection, code_review_id, "Workflows", kind="subject")

    conversations = list_subject_conversations(memory_connection, "workflows")

    assert [conversation["title"] for conversation in conversations] == [
        "Code review habit",
        "Plate reader workflow",
    ]


def test_assign_chunk_subject_counts_chunk_without_conversation_assignment(memory_connection) -> None:
    chunk_id = memory_connection.execute(
        """
        SELECT id
        FROM message_chunks
        WHERE text LIKE '%parser module%'
        """
    ).fetchone()[0]

    assign_chunk_subject(memory_connection, chunk_id, "Parser Implementation", kind="project", confidence=0.8)

    summaries = list_subjects(memory_connection, kind="project")
    assert summaries[0].subject.slug == "parser-implementation"
    assert summaries[0].conversation_count == 0
    assert summaries[0].chunk_count == 1


def test_remove_conversation_subject_can_remove_chunk_assignments(memory_connection) -> None:
    conversation_id = memory_connection.execute(
        "SELECT id FROM conversations WHERE title = 'Plate reader workflow'"
    ).fetchone()[0]
    assign_conversation_subject(memory_connection, conversation_id, "Lab Automation", include_chunks=True)

    deleted = remove_conversation_subject(
        memory_connection,
        conversation_id,
        "Lab Automation",
        remove_chunk_assignments=True,
    )

    assert deleted == 1
    assert list_conversation_subjects(memory_connection, conversation_id) == []
    summary = list_subjects(memory_connection)[0]
    assert summary.conversation_count == 0
    assert summary.chunk_count == 0
