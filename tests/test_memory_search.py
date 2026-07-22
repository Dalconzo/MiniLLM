import json
import sqlite3

import pytest

from local_agent_lab.memory.chatgpt_ingest import import_chatgpt_export
from local_agent_lab.memory.curated import create_memory_record
from local_agent_lab.memory.observability import MemoryObservationError
from local_agent_lab.memory.search import search_chatgpt_memory
from local_agent_lab.memory.subjects import assign_conversation_subject, upsert_subject


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
    assert result["results"][0]["score_breakdown"]["profile"] == "hybrid_memory_v1"
    assert "keyword_relevance" in result["results"][0]["score_breakdown"]["components"]
    assert result["results"][0]["disclosure_tier"] in {"far", "medium", "close", "full"}
    assert result["domain_detection"]["primary_domain"] == "lab_automation"
    assert "lab_automation" in result["domain_detection"]["domains"]
    assert all("domains" in item and "domain_primary" in item for item in result["results"])
    assert "operational" in result["lenses"] or "procedural" in result["lenses"]
    assert result["results"][0]["validation_checks"]["temporal"]["status"] == "not_run"


def test_search_chatgpt_memory_falls_back_for_natural_language_queries(tmp_path) -> None:
    data_dir = tmp_path / "data"
    import_chatgpt_export(input_path=_write_export(tmp_path), data_dir=data_dir, memory_dir=data_dir / "memory")

    result = search_chatgpt_memory(
        memory_dir=data_dir / "memory",
        query="where exactly should we look when debugging the barcode workflow",
    )

    assert result["status"] == "ok"
    assert result["count"] >= 1
    assert any(item["field"] == "fts_strategy" and item["value"] == "broad_any_terms" for item in result["filters_applied"])
    assert result["results"][0]["title"] == "Barcode parser debugging"


def test_search_chatgpt_memory_effort_caps_disclosure_and_adds_validation(tmp_path) -> None:
    data_dir = tmp_path / "data"
    import_chatgpt_export(input_path=_write_export(tmp_path), data_dir=data_dir, memory_dir=data_dir / "memory")

    result = search_chatgpt_memory(memory_dir=data_dir / "memory", query="barcode parser", depth="full", effort=1)

    assert result["results"][0]["disclosure_tier"] == "far"
    assert result["lenses"] == ["operational", "procedural", "planning"]


def test_search_chatgpt_memory_high_effort_exposes_validation_checks(tmp_path) -> None:
    data_dir = tmp_path / "data"
    import_chatgpt_export(input_path=_write_export(tmp_path), data_dir=data_dir, memory_dir=data_dir / "memory")

    result = search_chatgpt_memory(memory_dir=data_dir / "memory", query="barcode parser", depth="full", effort=4)

    assert "temporal" in result["lenses"]
    assert "contradiction" in result["lenses"]
    assert result["results"][0]["validation_checks"]["temporal"]["status"] in {"current", "stale"}
    assert result["results"][0]["validation_checks"]["contradiction"]["status"] in {"not_detected", "review", "possible"}


def test_search_chatgpt_memory_marks_high_risk_queries_and_blocks_stale_records(tmp_path) -> None:
    data_dir = tmp_path / "data"
    memory_dir = data_dir / "memory"
    import_chatgpt_export(input_path=_write_export(tmp_path), data_dir=data_dir, memory_dir=memory_dir)
    with sqlite3.connect(memory_dir / "chatgpt_memory.sqlite3") as connection:
        create_memory_record(
            connection,
            record_type="decision",
            title="Portfolio rebalancing notes",
            body="Rebalance index funds monthly and document the rationale.",
            trust_level="canonical",
        )
        create_memory_record(
            connection,
            record_type="decision",
            title="Old portfolio note",
            body="This should no longer surface in high-risk search.",
            trust_level="low",
            status="stale",
        )

    result = search_chatgpt_memory(memory_dir=memory_dir, query="portfolio", depth="full", effort=4)

    assert result["governance"]["high_risk"] is True
    assert "financial_caution" in result["governance"]["labels"]
    assert all(item.get("governance_reason") != "stale_high_risk_memory" for item in result["results"])
    assert all(item.get("governance_labels") == ["financial_caution"] for item in result["results"])


def test_search_chatgpt_memory_supports_title_filter(tmp_path) -> None:
    data_dir = tmp_path / "data"
    import_chatgpt_export(input_path=_write_export(tmp_path), data_dir=data_dir, memory_dir=data_dir / "memory")

    result = search_chatgpt_memory(memory_dir=data_dir / "memory", query="plan", title="Finance")

    assert result["count"] == 1
    assert result["results"][0]["title"] == "Finance notes"
    assert result["filters_applied"] == [{"field": "title", "value": "Finance"}]


def test_search_chatgpt_memory_depth_caps_disclosure_tier(tmp_path) -> None:
    data_dir = tmp_path / "data"
    import_chatgpt_export(input_path=_write_export(tmp_path), data_dir=data_dir, memory_dir=data_dir / "memory")

    result = search_chatgpt_memory(memory_dir=data_dir / "memory", query="barcode", depth="far")

    assert result["results"][0]["disclosure_tier"] == "far"
    assert "snippet" not in result["results"][0]["exposed_fields"]
    assert "snippet" not in result["results"][0]


def test_search_chatgpt_memory_rejects_invalid_depth_before_effort_cap(tmp_path) -> None:
    data_dir = tmp_path / "data"
    import_chatgpt_export(input_path=_write_export(tmp_path), data_dir=data_dir, memory_dir=data_dir / "memory")

    with pytest.raises(ValueError, match="depth must be one of: far, medium, close, full"):
        search_chatgpt_memory(memory_dir=data_dir / "memory", query="barcode", depth="deep")


def test_search_chatgpt_memory_includes_curated_memory_records(tmp_path) -> None:
    data_dir = tmp_path / "data"
    memory_dir = data_dir / "memory"
    import_chatgpt_export(input_path=_write_export(tmp_path), data_dir=data_dir, memory_dir=memory_dir)
    with sqlite3.connect(memory_dir / "chatgpt_memory.sqlite3") as connection:
        create_memory_record(
            connection,
            record_type="decision",
            title="Barcode parser decision",
            body="Use the curated barcode parser workflow.",
            trust_level="canonical",
        )

    result = search_chatgpt_memory(memory_dir=memory_dir, query="curated barcode")

    assert any(item["source_kind"] == "curated_memory" for item in result["results"])
    assert result["candidate_counts"]["curated"] == 1


def test_search_chatgpt_memory_subject_filter_is_explicit_without_subject_tables(tmp_path) -> None:
    data_dir = tmp_path / "data"
    import_chatgpt_export(input_path=_write_export(tmp_path), data_dir=data_dir, memory_dir=data_dir / "memory")

    result = search_chatgpt_memory(memory_dir=data_dir / "memory", query="barcode", subject="lab automation")

    assert result["count"] == 0
    assert result["filters_applied"][0]["field"] == "subject"
    assert result["filters_applied"][0]["warning"] == "subject tables do not exist yet"


def test_search_chatgpt_memory_supports_conversation_level_subjects(tmp_path) -> None:
    data_dir = tmp_path / "data"
    report = import_chatgpt_export(input_path=_write_export(tmp_path), data_dir=data_dir, memory_dir=data_dir / "memory")
    db_path = data_dir / "memory" / "chatgpt_memory.sqlite3"
    with sqlite3.connect(db_path) as connection:
        conversation_id = connection.execute(
            "SELECT id FROM conversations WHERE title = 'Barcode parser debugging'"
        ).fetchone()[0]
        assign_conversation_subject(connection, conversation_id, "Lab Automation", include_chunks=False)

    result = search_chatgpt_memory(memory_dir=data_dir / "memory", query="barcode", subject="Lab Automation")

    assert result["count"] == 2
    assert result["filters_applied"] == [{"field": "subject", "value": "Lab Automation"}]


def test_search_chatgpt_memory_normalizes_unicode_subjects(tmp_path) -> None:
    data_dir = tmp_path / "data"
    import_chatgpt_export(input_path=_write_export(tmp_path), data_dir=data_dir, memory_dir=data_dir / "memory")
    db_path = data_dir / "memory" / "chatgpt_memory.sqlite3"
    with sqlite3.connect(db_path) as connection:
        conversation_id = connection.execute(
            "SELECT id FROM conversations WHERE title = 'Barcode parser debugging'"
        ).fetchone()[0]
        assign_conversation_subject(connection, conversation_id, "Café workflow", include_chunks=False)

    result = search_chatgpt_memory(memory_dir=data_dir / "memory", query="barcode", subject="Cafe workflow")

    assert result["count"] == 2
    assert result["filters_applied"] == [{"field": "subject", "value": "Cafe workflow"}]


def test_search_chatgpt_memory_excludes_curated_records_by_subject(tmp_path) -> None:
    data_dir = tmp_path / "data"
    memory_dir = data_dir / "memory"
    import_chatgpt_export(input_path=_write_export(tmp_path), data_dir=data_dir, memory_dir=memory_dir)
    with sqlite3.connect(memory_dir / "chatgpt_memory.sqlite3") as connection:
        conversation_id = connection.execute(
            "SELECT id FROM conversations WHERE title = 'Barcode parser debugging'"
        ).fetchone()[0]
        subject = assign_conversation_subject(connection, conversation_id, "Lab Automation", include_chunks=False)
        create_memory_record(
            connection,
            record_type="decision",
            title="Curated barcode decision",
            body="Use the curated barcode parser workflow.",
            subject_id=subject.id,
            trust_level="canonical",
        )

    result = search_chatgpt_memory(
        memory_dir=memory_dir,
        query="curated barcode",
        exclude_subjects=["Lab Automation"],
    )

    assert result["count"] == 0
    assert {"field": "exclude_subject", "value": ["Lab Automation"]} in result["filters_applied"]


def test_search_chatgpt_memory_blocks_curated_records_from_tombstoned_sources(tmp_path) -> None:
    data_dir = tmp_path / "data"
    memory_dir = data_dir / "memory"
    import_chatgpt_export(input_path=_write_export(tmp_path), data_dir=data_dir, memory_dir=memory_dir)
    db_path = memory_dir / "chatgpt_memory.sqlite3"
    with sqlite3.connect(db_path) as connection:
        chunk_id = connection.execute(
            "SELECT id FROM message_chunks WHERE text LIKE '%barcode parser%' LIMIT 1"
        ).fetchone()[0]
        create_memory_record(
            connection,
            record_type="decision",
            title="Curated barcode decision",
            body="Use the curated barcode parser workflow.",
            trust_level="canonical",
            source_ref=chunk_id,
        )

    first = search_chatgpt_memory(memory_dir=memory_dir, query="curated barcode")
    assert any(item["source_kind"] == "curated_memory" for item in first["results"])

    with sqlite3.connect(db_path) as connection:
        from local_agent_lab.memory.audit import tombstone_source

        tombstone_source(connection, source_kind="chatgpt_export", source_id=chunk_id, reason="private")

    second = search_chatgpt_memory(memory_dir=memory_dir, query="curated barcode")
    assert all(item["chunk_id"] != chunk_id for item in second["results"])
    assert all(item["source_kind"] != "curated_memory" for item in second["results"])


def test_search_chatgpt_memory_errors_when_database_missing(tmp_path) -> None:
    with pytest.raises(MemoryObservationError) as exc_info:
        search_chatgpt_memory(memory_dir=tmp_path / "memory", query="barcode")

    assert exc_info.value.stage == "retrieve_candidates"
    assert exc_info.value.error_code == "memory_database_not_found"
