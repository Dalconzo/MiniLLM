import json
import sqlite3

from local_agent_lab.memory.chatgpt_ingest import import_chatgpt_export, parse_chatgpt_export


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


def test_parse_chatgpt_export_normalizes_conversations_messages_and_chunks(tmp_path) -> None:
    input_path = _write_export(tmp_path)

    parsed = parse_chatgpt_export(input_path)

    assert parsed.import_record["conversation_count"] == 1
    assert parsed.import_record["message_count"] == 2
    assert parsed.import_record["chunk_count"] == 2
    assert parsed.import_record["candidate_memory_count"] == 2
    assert parsed.conversations[0]["title"] == "Plate reader workflow"
    assert parsed.messages[0]["role"] == "user"
    assert parsed.messages[1]["role"] == "assistant"
    assert parsed.chunks[0]["source_kind"] == "chatgpt_export"
    assert parsed.candidate_memories[1]["assistant_suggestion"] == 1
    assert parsed.candidate_memories[1]["review_status"] == "pending"
    assert parsed.candidate_memories[1]["source_ref"].startswith("chk_")


def test_import_chatgpt_export_writes_jsonl_sqlite_and_fts(tmp_path) -> None:
    input_path = _write_export(tmp_path)
    data_dir = tmp_path / "data"
    memory_dir = data_dir / "memory"

    report = import_chatgpt_export(input_path=input_path, data_dir=data_dir, memory_dir=memory_dir)

    assert report["status"] == "ok"
    assert report["summary"]["conversations"] == 1
    parsed_dir = data_dir / "chatgpt_exports" / "parsed" / report["import_id"]
    assert (parsed_dir / "conversations.jsonl").exists()
    assert (parsed_dir / "messages.jsonl").exists()
    assert (parsed_dir / "chunks.jsonl").exists()
    assert (parsed_dir / "candidate_memories.jsonl").exists()
    assert (parsed_dir / "import_report.json").exists()

    with sqlite3.connect(memory_dir / "chatgpt_memory.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM imports").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM message_chunks").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM candidate_memories").fetchone()[0] == 2
        hits = connection.execute(
            """
            SELECT COUNT(*)
            FROM chatgpt_chunks_fts
            WHERE chatgpt_chunks_fts MATCH 'parser'
            """
        ).fetchone()[0]
        assert hits == 1


def test_import_chatgpt_export_is_idempotent_for_same_export(tmp_path) -> None:
    input_path = _write_export(tmp_path)
    data_dir = tmp_path / "data"
    memory_dir = data_dir / "memory"

    first = import_chatgpt_export(input_path=input_path, data_dir=data_dir, memory_dir=memory_dir)
    second = import_chatgpt_export(input_path=input_path, data_dir=data_dir, memory_dir=memory_dir)

    assert first["import_id"] == second["import_id"]
    with sqlite3.connect(memory_dir / "chatgpt_memory.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM imports").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM chatgpt_chunks_fts").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM candidate_memories").fetchone()[0] == 2
