from __future__ import annotations

import json
import sqlite3

from local_agent_lab.memory.chatgpt_ingest import import_chatgpt_export
from local_agent_lab.memory.curated import create_memory_record, get_memory_record
from local_agent_lab.memory.feedback import (
    create_feedback_ranking_control,
    init_feedback_schema,
    list_feedback_ranking_controls,
    list_open_loops,
    record_agent_feedback,
    record_memory_feedback,
)


def _write_export(root):
    export_dir = root / "raw" / "export-1"
    export_dir.mkdir(parents=True)
    export = [
        {
            "id": "conversation-feedback",
            "title": "Feedback workflow",
            "mapping": {
                "u": {
                    "id": "u",
                    "message": {
                        "id": "u",
                        "author": {"role": "user"},
                        "content": {"parts": ["We should keep the memory loop open."]},
                    },
                }
            },
        }
    ]
    (export_dir / "conversations.json").write_text(json.dumps(export), encoding="utf-8")
    return root / "raw"


def test_feedback_can_stale_a_memory_record_after_repeated_negative_signals(tmp_path) -> None:
    data_dir = tmp_path / "data"
    memory_dir = data_dir / "memory"
    import_chatgpt_export(input_path=_write_export(tmp_path), data_dir=data_dir, memory_dir=memory_dir)

    with sqlite3.connect(memory_dir / "chatgpt_memory.sqlite3") as connection:
        record = create_memory_record(
            connection,
            record_type="decision",
            title="Keep memory loop open",
            body="Use the feedback path conservatively.",
            trust_level="high",
        )
        record_memory_feedback(
            connection,
            source_kind="curated_memory",
            source_id=record.id,
            memory_record_id=record.id,
            rating="down",
            run_id="run-1",
            query="feedback",
        )
        record_memory_feedback(
            connection,
            source_kind="curated_memory",
            source_id=record.id,
            memory_record_id=record.id,
            rating="down",
            run_id="run-2",
            query="feedback",
        )

        updated = get_memory_record(connection, record.id)

    assert updated.status == "stale"
    assert updated.metadata["feedback_summary"]["down"] == 2
    assert updated.metadata["last_feedback_rating"] == "down"


def test_open_loops_are_listable_as_active_memory_records(tmp_path) -> None:
    data_dir = tmp_path / "data"
    memory_dir = data_dir / "memory"
    import_chatgpt_export(input_path=_write_export(tmp_path), data_dir=data_dir, memory_dir=memory_dir)

    with sqlite3.connect(memory_dir / "chatgpt_memory.sqlite3") as connection:
        create_memory_record(
            connection,
            record_type="open_loop",
            title="Review the feedback loop",
            body="This is intentionally left open.",
            trust_level="medium",
        )
        loops = list_open_loops(connection)

    assert len(loops) == 1
    assert loops[0]["record_type"] == "open_loop"
    assert loops[0]["status"] == "active"


def test_agent_feedback_submission_creates_submitted_review_without_ranking_control() -> None:
    with sqlite3.connect(":memory:") as connection:
        init_feedback_schema(connection)

        feedback = record_agent_feedback(
            connection,
            run_id="run_noisy",
            trace_id="run_noisy",
            component="memory_search",
            category="retrieval_noise",
            severity="medium",
            observed_behavior="A wrong-domain source ranked highly.",
            expected_behavior="Wrong-domain sources should be filtered or penalized.",
            confidence=0.9,
            relevant_source_ids=["chunk_bad"],
        )

        review = connection.execute(
            "SELECT status FROM agent_feedback_reviews WHERE feedback_id = ?",
            (feedback.id,),
        ).fetchone()
        controls = list_feedback_ranking_controls(connection)

    assert review[0] == "submitted"
    assert controls == []


def test_feedback_ranking_control_is_explicit_reviewed_hook() -> None:
    with sqlite3.connect(":memory:") as connection:
        init_feedback_schema(connection)
        feedback = record_agent_feedback(
            connection,
            run_id="run_noisy",
            component="memory_search",
            category="retrieval_noise",
            severity="medium",
            observed_behavior="A source was irrelevant for this query.",
            expected_behavior="The source should be penalized for this query pattern.",
            confidence=0.8,
            relevant_source_ids=["chunk_bad"],
        )

        control = create_feedback_ranking_control(
            connection,
            feedback_id=feedback.id,
            control_type="query_source_penalty",
            query_pattern="starter rise fermentation",
            source_id="chunk_bad",
            subject="Recipes and Baking",
            weight=-0.4,
            rationale="Browser acceptance test marked this source as irrelevant retrieval noise.",
            rollback_note="Delete or supersede this control if later judged relevant.",
            created_by="test",
        )
        controls = list_feedback_ranking_controls(connection, status="reviewed")
        review = connection.execute(
            "SELECT status FROM agent_feedback_reviews WHERE feedback_id = ?",
            (feedback.id,),
        ).fetchone()

    assert control.id.startswith("frc_")
    assert control.status == "reviewed"
    assert controls[0].source_id == "chunk_bad"
    assert controls[0].weight == -0.4
    assert review[0] == "reviewed"
