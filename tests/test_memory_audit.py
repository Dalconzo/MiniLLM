import json
import sqlite3

from local_agent_lab.memory.audit import (
    blocked_source_ids,
    record_retrieval_event,
    retrieval_exposures_for_run,
    tombstone_source,
)
from local_agent_lab.memory.chatgpt_ingest import import_chatgpt_export
from local_agent_lab.memory.curated import create_memory_record
from local_agent_lab.memory.search import search_chatgpt_memory


def _write_export(root):
    export_dir = root / "raw" / "export-1"
    export_dir.mkdir(parents=True)
    export = [
        {
            "id": "conversation-audit",
            "title": "Credentials and parser notes",
            "mapping": {
                "u": {
                    "id": "u",
                    "message": {
                        "id": "u",
                        "author": {"role": "user"},
                        "content": {
                            "parts": [
                                "The credential sk-abcdefghijklmnopqrstuvwxyz123456 belongs near the barcode parser note."
                            ]
                        },
                    },
                }
            },
        }
    ]
    (export_dir / "conversations.json").write_text(json.dumps(export), encoding="utf-8")
    return root / "raw"


def _import_memory(tmp_path):
    data_dir = tmp_path / "data"
    memory_dir = data_dir / "memory"
    import_chatgpt_export(input_path=_write_export(tmp_path), data_dir=data_dir, memory_dir=memory_dir)
    return memory_dir


def test_search_redacts_obvious_secrets_from_snippets(tmp_path) -> None:
    memory_dir = _import_memory(tmp_path)

    result = search_chatgpt_memory(memory_dir=memory_dir, query="credential", depth="full")

    assert result["count"] == 1
    snippet = result["results"][0].get("snippet", "")
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in snippet
    assert result["results"][0]["redacted_secret_count"] == 1


def test_tombstoned_source_is_excluded_from_search(tmp_path) -> None:
    memory_dir = _import_memory(tmp_path)
    db_path = memory_dir / "chatgpt_memory.sqlite3"
    first = search_chatgpt_memory(memory_dir=memory_dir, query="barcode")
    chunk_id = first["results"][0]["chunk_id"]

    with sqlite3.connect(db_path) as connection:
        tombstone_source(connection, source_kind="chatgpt_export", source_id=chunk_id, reason="private")
        assert chunk_id in blocked_source_ids(connection)

    second = search_chatgpt_memory(memory_dir=memory_dir, query="barcode")

    assert second["count"] == 0
    assert second["filters_applied"][0]["field"] == "exclude_source"


def test_tombstoned_curated_memory_id_is_excluded_from_search(tmp_path) -> None:
    memory_dir = _import_memory(tmp_path)
    db_path = memory_dir / "chatgpt_memory.sqlite3"
    with sqlite3.connect(db_path) as connection:
        record = create_memory_record(
            connection,
            record_type="decision",
            title="Use barcode parser workflow",
            body="The barcode parser workflow should be used for recipe-adjacent lab notes.",
            trust_level="high",
            record_id="mem_direct_block_test",
        )
        tombstone_source(connection, source_kind="curated_memory", source_id=record.id, reason="private")

    result = search_chatgpt_memory(memory_dir=memory_dir, query="barcode parser workflow")

    assert all(item["chunk_id"] != record.id for item in result["results"])
    assert result["filters_applied"][0]["field"] == "exclude_source"


def test_record_retrieval_event_stores_exposed_sources(tmp_path) -> None:
    memory_dir = _import_memory(tmp_path)
    db_path = memory_dir / "chatgpt_memory.sqlite3"
    result = search_chatgpt_memory(memory_dir=memory_dir, query="barcode")

    with sqlite3.connect(db_path) as connection:
        audit = record_retrieval_event(
            connection,
            run_id="run_test",
            query="barcode",
            command="memory-search",
            filters=result["filters_applied"],
            ranking_profile=result["ranking_profile"],
            disclosure_depth="medium",
            results=result["results"],
        )
        exposures = retrieval_exposures_for_run(connection, "run_test")

    assert audit["exposures"] == 1
    assert exposures[0]["source_id"] == result["results"][0]["chunk_id"]
    assert exposures[0]["redacted_secret_count"] == result["results"][0]["redacted_secret_count"]
