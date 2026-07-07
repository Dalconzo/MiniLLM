from __future__ import annotations

import json
import sqlite3

from local_agent_lab.memory.chatgpt_ingest import import_chatgpt_export
from local_agent_lab.memory.curated import create_memory_record, get_memory_record
from local_agent_lab.memory.feedback import list_open_loops, record_memory_feedback


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
