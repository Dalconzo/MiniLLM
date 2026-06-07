import json

from local_agent_lab.logging.run_logger import RunLogger
from local_agent_lab.memory.observability import (
    MemoryTraceWriter,
    build_unimplemented_search_explain,
    dry_run_chatgpt_ingest,
    memory_db_path,
    read_memory_trace,
    render_memory_trace,
    validate_memory_state,
)


def test_dry_run_chatgpt_ingest_counts_conversations(tmp_path) -> None:
    export_dir = tmp_path / "raw" / "export-1"
    export_dir.mkdir(parents=True)
    (export_dir / "conversations.json").write_text(
        json.dumps([{"id": "a", "title": "Alpha"}, {"id": "b", "title": "Beta"}]),
        encoding="utf-8",
    )

    report = dry_run_chatgpt_ingest(tmp_path / "raw")

    assert report["status"] == "ok"
    assert report["summary"]["conversation_files"] == 1
    assert report["summary"]["conversations"] == 2
    assert "data/memory/chatgpt_memory.sqlite3" in report["planned_writes"]


def test_dry_run_chatgpt_ingest_reports_invalid_json(tmp_path) -> None:
    export_dir = tmp_path / "raw"
    export_dir.mkdir()
    (export_dir / "conversations.json").write_text("{not json", encoding="utf-8")

    report = dry_run_chatgpt_ingest(export_dir)

    assert report["status"] == "error"
    assert report["summary"]["errors"] == 1
    assert report["conversation_files"][0]["error"]["stage"] == "parse_export"


def test_validate_memory_state_reports_missing_database_as_warning(tmp_path) -> None:
    data_dir = tmp_path / "data"
    memory_dir = data_dir / "memory"
    memory_dir.mkdir(parents=True)

    report = validate_memory_state(data_dir=data_dir, memory_dir=memory_dir)

    assert report["status"] == "warn"
    assert report["sqlite_path"] == str(memory_db_path(memory_dir))
    assert any(check["name"] == "sqlite_database" and check["status"] == "warn" for check in report["checks"])


def test_memory_trace_writer_creates_artifacts_and_trace_can_be_rendered(tmp_path) -> None:
    logger = RunLogger(tmp_path / "logs")
    run = logger.start("memory-check", {})
    writer = MemoryTraceWriter(
        logger=logger,
        run=run,
        command="memory-check",
        argv=["memory-check"],
        config_path=tmp_path / "config" / "agent.yaml",
        sqlite_path=tmp_path / "data" / "memory" / "chatgpt_memory.sqlite3",
    )

    writer.trace("load_config", "Loaded config.")
    writer.write_json("validation_report.json", {"status": "ok"})
    writer.finish(status="ok", result={"status": "ok"})

    trace = read_memory_trace(tmp_path / "logs", run.run_id)
    rendered = render_memory_trace(trace)

    assert trace["command"]["command"] == "memory-check"
    assert trace["trace_events"][0]["stage"] == "load_config"
    assert "validation_report.json" in trace["artifacts"]
    assert "Loaded config." in rendered


def test_unimplemented_search_explain_is_explicit() -> None:
    explain = build_unimplemented_search_explain("plate reader workflows")

    assert explain["status"] == "not_implemented"
    assert explain["error"]["stage"] == "retrieve_candidates"
    assert explain["candidate_counts"]["fts"] == 0
