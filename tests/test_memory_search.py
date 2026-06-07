import json
import sqlite3

import pytest

from local_agent_lab.memory.chatgpt_ingest import import_chatgpt_export
from local_agent_lab.memory.observability import MemoryObservationError
from local_agent_lab.memory.search import search_chatgpt_memory


def _write_export(root):
    export_dir = root / "raw" / "export-1"
    export_dir.mkdir(parents=True)
    export = [
        {
            "id": "conversation-search",
            "title": "Barcode parser debugging",
            "mapping": {
                "u": {
                    "id": "u",
                    "parent": None,
                    "children": ["a"],
                    "message": {
                        "id": "u",
                        "author": {"role": "user"},
                        "create_time": 1_700_000_000,
                        "content": {"parts": ["Where is the barcode parser configured?"]},
                    },
                },
                "a": {
                    "id": "a",
                    "parent": "u",
                    "children": [],
                    "message": {
                        "id": "a",
                        "author": {"role": "assistant"},
                        "create_time": 1_700_000_001,
                        "content": {"parts": ["The barcode parser lives in lab automation config."]},
                    },
                },
            },
        },
        {
            "id": "conversation-other",
            "title": "Finance notes",
            "mapping": {
                "u": {
                    "id": "u2",
                    "message": {
                        "id": "u2",
                        "author": {"role": "user"},
                        "content": {"parts": ["What is the savings plan?"]},
                    },
                }
            },
        },
    ]
    (export_dir / "conversations.json").write_text(json.dumps(export), encoding="utf-8")
    return root / "raw"


def test_search_chatgpt_memory_returns_citations_and_score_breakdown(tmp_path) -> None:
    data_dir = tmp_path / "data"
    import_chatgpt_export(input_path=_write_export(tmp_path), data_dir=data_dir, memory_dir=data_dir / "memory")

    result = search_chatgpt_memory(memory_dir=data_dir / "memory", query="barcode parser")

    assert result["status"] == "ok"
    assert result["count"] == 2
    assert result["candidate_counts"]["fts"] == 2
    assert result["results"][0]["conversation_id"].startswith("conv_")
    assert result["results"][0]["message_id"].startswith("msg_")
    assert result["results"][0]["chunk_id"].startswith("chk_")
    assert result["results"][0]["score_breakdown"]["fts_bm25"] == result["results"][0]["score"]
    assert result["results"][0]["disclosure_tier"] == "medium"


def test_search_chatgpt_memory_supports_title_filter(tmp_path) -> None:
    data_dir = tmp_path / "data"
    import_chatgpt_export(input_path=_write_export(tmp_path), data_dir=data_dir, memory_dir=data_dir / "memory")

    result = search_chatgpt_memory(memory_dir=data_dir / "memory", query="plan", title="Finance")

    assert result["count"] == 1
    assert result["results"][0]["title"] == "Finance notes"
    assert result["filters_applied"] == [{"field": "title", "value": "Finance"}]


def test_search_chatgpt_memory_subject_filter_is_explicit_without_subject_tables(tmp_path) -> None:
    data_dir = tmp_path / "data"
    import_chatgpt_export(input_path=_write_export(tmp_path), data_dir=data_dir, memory_dir=data_dir / "memory")

    result = search_chatgpt_memory(memory_dir=data_dir / "memory", query="barcode", subject="lab automation")

    assert result["count"] == 0
    assert result["filters_applied"][0]["field"] == "subject"
    assert result["filters_applied"][0]["warning"] == "subject tables do not exist yet"


def test_search_chatgpt_memory_supports_subject_tables_when_present(tmp_path) -> None:
    data_dir = tmp_path / "data"
    report = import_chatgpt_export(input_path=_write_export(tmp_path), data_dir=data_dir, memory_dir=data_dir / "memory")
    db_path = data_dir / "memory" / "chatgpt_memory.sqlite3"
    with sqlite3.connect(db_path) as connection:
        chunk_id = connection.execute(
            "SELECT id FROM message_chunks WHERE import_id = ? LIMIT 1",
            (report["import_id"],),
        ).fetchone()[0]
        connection.execute(
            "CREATE TABLE subjects (id TEXT PRIMARY KEY, slug TEXT NOT NULL, name TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE chunk_subjects (chunk_id TEXT NOT NULL, subject_id TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO subjects (id, slug, name) VALUES ('sub_lab', 'lab-automation', 'Lab Automation')")
        connection.execute(
            "INSERT INTO chunk_subjects (chunk_id, subject_id) VALUES (?, 'sub_lab')",
            (chunk_id,),
        )

    result = search_chatgpt_memory(memory_dir=data_dir / "memory", query="barcode", subject="Lab Automation")

    assert result["count"] == 1
    assert result["filters_applied"] == [{"field": "subject", "value": "Lab Automation"}]


def test_search_chatgpt_memory_errors_when_database_missing(tmp_path) -> None:
    with pytest.raises(MemoryObservationError) as exc_info:
        search_chatgpt_memory(memory_dir=tmp_path / "memory", query="barcode")

    assert exc_info.value.stage == "retrieve_candidates"
    assert exc_info.value.error_code == "memory_database_not_found"
