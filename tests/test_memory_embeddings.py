import json
import sqlite3

import pytest

from local_agent_lab.memory.chatgpt_ingest import import_chatgpt_export
from local_agent_lab.memory.embeddings import (
    cosine_similarity,
    deterministic_fallback_embedding,
    embed_missing_chunks,
    fallback_model_spec,
    init_embedding_schema,
    list_chunks_needing_embeddings,
    load_local_vector,
    register_embedding_model,
)


def _write_export(root):
    export_dir = root / "raw" / "export-1"
    export_dir.mkdir(parents=True)
    export = [
        {
            "id": "conversation-alpha",
            "title": "Memory architecture",
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
                        "content": {"content_type": "text", "parts": ["Build semantic memory for lab automation notes."]},
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
                        "content": {"content_type": "text", "parts": ["Use chunk embeddings plus keyword search."]},
                    },
                },
            },
        }
    ]
    (export_dir / "conversations.json").write_text(json.dumps(export), encoding="utf-8")
    return root / "raw"


def _import_memory(tmp_path):
    data_dir = tmp_path / "data"
    memory_dir = data_dir / "memory"
    import_chatgpt_export(input_path=_write_export(tmp_path), data_dir=data_dir, memory_dir=memory_dir)
    return memory_dir / "chatgpt_memory.sqlite3"


def test_deterministic_fallback_embedding_is_stable_normalized_and_token_based() -> None:
    first = deterministic_fallback_embedding("semantic lab memory", dimension=16)
    second = deterministic_fallback_embedding("semantic lab memory", dimension=16)
    different = deterministic_fallback_embedding("frontend design system", dimension=16)

    assert first == second
    assert len(first) == 16
    assert cosine_similarity(first, first) == pytest.approx(1.0)
    assert cosine_similarity(first, different) < 1.0


def test_embedding_schema_registers_model_idempotently(tmp_path) -> None:
    db_path = _import_memory(tmp_path)
    spec = fallback_model_spec(dimension=12)

    with sqlite3.connect(db_path) as connection:
        init_embedding_schema(connection)
        first_id = register_embedding_model(connection, spec)
        second_id = register_embedding_model(connection, spec)

        assert first_id == second_id
        assert connection.execute("SELECT COUNT(*) FROM embedding_models").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE version = 2").fetchone()[0] == 1


def test_lists_chunks_needing_embeddings_then_skips_unchanged_chunks(tmp_path) -> None:
    db_path = _import_memory(tmp_path)
    spec = fallback_model_spec(dimension=10)

    with sqlite3.connect(db_path) as connection:
        model_id = register_embedding_model(connection, spec)
        chunks = list_chunks_needing_embeddings(connection, embedding_model_id=model_id)

        assert len(chunks) == 2

        report = embed_missing_chunks(connection, spec=spec)

        assert report["embeddings_written"] == 2
        assert list_chunks_needing_embeddings(connection, embedding_model_id=model_id) == []
        assert connection.execute("SELECT COUNT(*) FROM chunk_embeddings").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM local_embedding_vectors").fetchone()[0] == 2

        vector = load_local_vector(connection, report["vector_refs"][0])
        assert len(vector) == spec.dimension


def test_stale_chunk_hash_is_detected_and_reembedded(tmp_path) -> None:
    db_path = _import_memory(tmp_path)
    spec = fallback_model_spec(dimension=8)

    with sqlite3.connect(db_path) as connection:
        model_id = register_embedding_model(connection, spec)
        first_report = embed_missing_chunks(connection, spec=spec)
        chunk_id = connection.execute("SELECT id FROM message_chunks ORDER BY id LIMIT 1").fetchone()[0]

        connection.execute(
            "UPDATE message_chunks SET text = ?, text_sha256 = ? WHERE id = ?",
            ("Updated semantic memory text.", "updated-hash", chunk_id),
        )
        connection.commit()

        stale = list_chunks_needing_embeddings(connection, embedding_model_id=model_id)

        assert [chunk.chunk_id for chunk in stale] == [chunk_id]

        second_report = embed_missing_chunks(connection, spec=spec)

        assert first_report["embeddings_written"] == 2
        assert second_report["embeddings_written"] == 1
        stored_hash = connection.execute(
            """
            SELECT text_sha256
            FROM chunk_embeddings
            WHERE chunk_id = ? AND embedding_model_id = ?
            """,
            (chunk_id, model_id),
        ).fetchone()[0]
        assert stored_hash == "updated-hash"
