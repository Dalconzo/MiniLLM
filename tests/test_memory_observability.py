import json
import os
import sqlite3
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

from typer.testing import CliRunner

from local_agent_lab.cli import app
from local_agent_lab.logging.run_logger import RunLogger
from local_agent_lab.llm.ollama_client import OllamaClient
from local_agent_lab.memory.analysis import (
    analyze_memory_corpus,
    analyze_memory_patterns,
    render_memory_analysis,
    render_memory_analysis_html,
    render_memory_patterns,
    render_memory_patterns_html,
)
from local_agent_lab.memory.candidates import get_candidate_memory
from local_agent_lab.memory.subjects import assign_conversation_subject
from local_agent_lab.memory.observability import (
    MemoryTraceWriter,
    build_unimplemented_search_explain,
    dry_run_chatgpt_ingest,
    memory_db_path,
    read_memory_trace,
    render_memory_trace,
    summarize_memory_status,
    validate_memory_state,
)
from local_agent_lab.memory.chatgpt_ingest import import_chatgpt_export


def _write_config(tmp_path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "agent.yaml").write_text(
        textwrap.dedent(
            """
            app:
              name: local-agent-lab
              log_level: info
            paths:
              data_dir: data
              logs_dir: data/logs
              indexes_dir: data/indexes
              memory_dir: data/memory
              patches_dir: data/patches
            ollama:
              host: http://127.0.0.1:11434
              request_timeout_seconds: 180
            runtime:
              default_task: chat
              redact_before_model: true
              save_full_prompts: true
            models: {}
            routing:
              task_map: {}
            """
        ).strip()
        + "\n",
        encoding="utf-8",
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


def test_memory_check_exits_nonzero_for_invalid_sqlite(tmp_path, monkeypatch) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "agent.yaml"
    config_path.write_text(
        textwrap.dedent(
            """
            app:
              name: local-agent-lab
              log_level: info
            paths:
              data_dir: data
              logs_dir: data/logs
              indexes_dir: data/indexes
              memory_dir: data/memory
              patches_dir: data/patches
            ollama:
              host: http://127.0.0.1:11434
              request_timeout_seconds: 180
            runtime:
              default_task: chat
              redact_before_model: true
              save_full_prompts: true
            models: {}
            routing:
              task_map: {}
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    memory_dir = tmp_path / "data" / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "chatgpt_memory.sqlite3").write_text("not sqlite", encoding="utf-8")
    monkeypatch.setenv("LAGENT_CONFIG", str(config_path))

    runner = CliRunner()
    result = runner.invoke(app, ["memory-check", "--json"])

    assert result.exit_code == 1
    assert "sqlite_schema" in result.stdout or "sqlite_open" in result.stdout or "sqlite_schema" in result.stderr or "sqlite_open" in result.stderr


def test_memory_audit_exits_nonzero_for_unknown_run_id(tmp_path, monkeypatch) -> None:
    data_dir = tmp_path / "data"
    memory_dir = data_dir / "memory"
    import_path = tmp_path / "raw"
    export_dir = import_path / "export-1"
    export_dir.mkdir(parents=True)
    (export_dir / "conversations.json").write_text(
        json.dumps(
            [
                {
                    "id": "conversation-audit",
                    "title": "Audit run",
                    "mapping": {
                        "u": {
                            "id": "u",
                            "message": {
                                "id": "u",
                                "author": {"role": "user"},
                                "content": {"parts": ["Find the barcode parser."]},
                            },
                        }
                    },
                }
            ]
        ),
        encoding="utf-8",
    )
    import_chatgpt_export(input_path=import_path, data_dir=data_dir, memory_dir=memory_dir)

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "agent.yaml"
    config_path.write_text(
        textwrap.dedent(
            """
            app:
              name: local-agent-lab
              log_level: info
            paths:
              data_dir: data
              logs_dir: data/logs
              indexes_dir: data/indexes
              memory_dir: data/memory
              patches_dir: data/patches
            ollama:
              host: http://127.0.0.1:11434
              request_timeout_seconds: 180
            runtime:
              default_task: chat
              redact_before_model: true
              save_full_prompts: true
            models: {}
            routing:
              task_map: {}
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LAGENT_CONFIG", str(config_path))

    runner = CliRunner()
    result = runner.invoke(app, ["memory-audit", "not-a-run", "--json"])

    assert result.exit_code == 1
    assert "retrieval_event_not_found" in result.stdout or "retrieval_event_not_found" in result.stderr


def test_memory_status_reports_counts_and_recent_runs(tmp_path, monkeypatch) -> None:
    data_dir = tmp_path / "data"
    memory_dir = data_dir / "memory"
    import_path = tmp_path / "raw"
    export_dir = import_path / "export-1"
    export_dir.mkdir(parents=True)
    (export_dir / "conversations.json").write_text(
        json.dumps(
            [
                {
                    "id": "conversation-status",
                    "title": "Status run",
                    "mapping": {
                        "u": {
                            "id": "u",
                            "message": {
                                "id": "u",
                                "author": {"role": "user"},
                                "content": {"parts": ["Check the status dashboard."]},
                            },
                        }
                    },
                }
            ]
        ),
        encoding="utf-8",
    )
    import_chatgpt_export(input_path=import_path, data_dir=data_dir, memory_dir=memory_dir)

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "agent.yaml").write_text(
        textwrap.dedent(
            """
            app:
              name: local-agent-lab
              log_level: info
            paths:
              data_dir: data
              logs_dir: data/logs
              indexes_dir: data/indexes
              memory_dir: data/memory
              patches_dir: data/patches
            ollama:
              host: http://127.0.0.1:11434
              request_timeout_seconds: 180
            runtime:
              default_task: chat
              redact_before_model: true
              save_full_prompts: true
            models:
              chat_small:
                model: fake
                task: chat
                temperature: 0.1
                max_tokens: 4096
                routing_label: local
            routing:
              task_map:
                chat: chat_small
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LAGENT_CONFIG", str(config_dir / "agent.yaml"))

    runner = CliRunner()
    result = runner.invoke(app, ["memory-status"])

    assert result.exit_code == 0
    assert "Status:" in result.stdout
    assert "Counts:" in result.stdout
    assert "Corpus Freshness:" in result.stdout
    assert "Latest Import:" in result.stdout
    assert "Recent Runs:" in result.stdout


def test_memory_status_reports_corpus_freshness_separately_from_embedding_health(tmp_path) -> None:
    data_dir = tmp_path / "data"
    memory_dir = data_dir / "memory"
    import_path = tmp_path / "raw"
    export_dir = import_path / "export-1"
    export_dir.mkdir(parents=True)
    recent_time = int((datetime.now(timezone.utc) - timedelta(days=1)).timestamp())
    (export_dir / "conversations.json").write_text(
        json.dumps(
            [
                {
                    "id": "conversation-freshness",
                    "title": "Fresh starter note",
                    "create_time": recent_time,
                    "update_time": recent_time,
                    "mapping": {
                        "u": {
                            "id": "u",
                            "message": {
                                "id": "u",
                                "author": {"role": "user"},
                                "create_time": recent_time,
                                "content": {"parts": ["The starter doubled after feeding yesterday."]},
                            },
                        }
                    },
                }
            ]
        ),
        encoding="utf-8",
    )
    import_chatgpt_export(input_path=import_path, data_dir=data_dir, memory_dir=memory_dir)

    status = summarize_memory_status(data_dir=data_dir, memory_dir=memory_dir, logs_dir=data_dir / "logs")

    sqlite_status = status["sqlite"]
    assert sqlite_status["embedding_health"]["status"] in {"missing_schema", "empty", "no_model", "partial", "ok"}
    assert sqlite_status["corpus_freshness"]["status"] == "current"
    assert sqlite_status["corpus_freshness"]["latest_source_message"]["conversation_title"] == "Fresh starter note"
    assert sqlite_status["corpus_freshness"]["latest_imported_at"] == sqlite_status["latest_import"]["imported_at"]
    assert sqlite_status["corpus_freshness"]["latest_import_id"] == sqlite_status["latest_import"]["id"]
    assert sqlite_status["corpus_freshness"]["import_lag_days"] is not None
    assert sqlite_status["corpus_freshness"]["import_lag_days"] < 2


def test_memory_runs_lists_recent_commands(tmp_path, monkeypatch) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "agent.yaml").write_text(
        textwrap.dedent(
            """
            app:
              name: local-agent-lab
              log_level: info
            paths:
              data_dir: data
              logs_dir: data/logs
              indexes_dir: data/indexes
              memory_dir: data/memory
              patches_dir: data/patches
            ollama:
              host: http://127.0.0.1:11434
              request_timeout_seconds: 180
            runtime:
              default_task: chat
              redact_before_model: true
              save_full_prompts: true
            models:
              chat_small:
                model: fake
                task: chat
                temperature: 0.1
                max_tokens: 4096
                routing_label: local
            routing:
              task_map:
                chat: chat_small
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LAGENT_CONFIG", str(config_dir / "agent.yaml"))

    runner = CliRunner()
    result = runner.invoke(app, ["memory-runs", "--limit", "3"])

    assert result.exit_code == 0
    assert "Recent runs:" in result.stdout


def test_memory_analyze_reports_corpus_shape_and_writes_html(tmp_path, monkeypatch) -> None:
    data_dir = tmp_path / "data"
    memory_dir = data_dir / "memory"
    import_path = tmp_path / "raw"
    export_dir = import_path / "export-1"
    export_dir.mkdir(parents=True)
    (export_dir / "conversations.json").write_text(
        json.dumps(
            [
                {
                    "id": "conversation-analysis",
                    "title": "Codex usage review",
                    "mapping": {
                        "u": {
                            "id": "u",
                            "message": {
                                "id": "u",
                                "author": {"role": "user"},
                                "content": {"parts": ["How should we analyze Codex chats?"]},
                            },
                        },
                        "a": {
                            "id": "a",
                            "message": {
                                "id": "a",
                                "author": {"role": "assistant"},
                                "content": {"parts": ["Use a local dashboard and review by subject."]},
                            },
                        },
                    },
                }
            ]
        ),
        encoding="utf-8",
    )
    import_chatgpt_export(input_path=import_path, data_dir=data_dir, memory_dir=memory_dir)

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "agent.yaml").write_text(
        textwrap.dedent(
            """
            app:
              name: local-agent-lab
              log_level: info
            paths:
              data_dir: data
              logs_dir: data/logs
              indexes_dir: data/indexes
              memory_dir: data/memory
              patches_dir: data/patches
            ollama:
              host: http://127.0.0.1:11434
              request_timeout_seconds: 180
            runtime:
              default_task: chat
              redact_before_model: true
              save_full_prompts: true
            models:
              chat_small:
                model: fake
                task: chat
                temperature: 0.1
                max_tokens: 4096
                routing_label: local
            routing:
              task_map:
                chat: chat_small
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LAGENT_CONFIG", str(config_dir / "agent.yaml"))

    runner = CliRunner()
    result = runner.invoke(app, ["memory-analyze"])

    assert result.exit_code == 0
    assert "Corpus Shape:" in result.stdout
    assert "Token Stats:" in result.stdout
    assert "Codex Coverage:" in result.stdout
    assert "HTML report:" in result.stdout

    html_line = next(line for line in result.stdout.splitlines() if line.startswith("HTML report:"))
    html_path = html_line.split("HTML report:", 1)[1].strip()
    html = Path(html_path).read_text(encoding="utf-8")
    assert "<title>Memory Corpus Analysis</title>" in html

    report = analyze_memory_corpus(data_dir=data_dir, memory_dir=memory_dir, logs_dir=data_dir / "logs")
    rendered = render_memory_analysis(report)
    html_rendered = render_memory_analysis_html(report)
    assert report["status"] == "ok"
    assert report["codex"]["title_conversations"] >= 1
    assert report["candidate_stats"]["per_1000_chunks"] > 0
    assert "Candidate Memory:" in rendered
    assert "Memory Corpus Analysis" in html_rendered


def test_memory_patterns_discovers_recipe_and_project_categories(tmp_path, monkeypatch) -> None:
    data_dir = tmp_path / "data"
    memory_dir = data_dir / "memory"
    import_path = tmp_path / "raw"
    export_dir = import_path / "export-1"
    export_dir.mkdir(parents=True)
    (export_dir / "conversations.json").write_text(
        json.dumps(
            [
                {
                    "id": "conversation-recipes",
                    "title": "Sunday Bake Prep Ideas",
                    "mapping": {
                        "u1": {
                            "id": "u1",
                            "message": {
                                "id": "u1",
                                "author": {"role": "user"},
                                "content": {"parts": ["I need a crème brûlée recipe and some pie ideas."]},
                            },
                        }
                    },
                },
                {
                    "id": "conversation-projects",
                    "title": "Resume Optimization for Automation Role",
                    "mapping": {
                        "u2": {
                            "id": "u2",
                            "message": {
                                "id": "u2",
                                "author": {"role": "user"},
                                "content": {"parts": ["I need a job search strategy and a VBA macro."]},
                            },
                        }
                    },
                },
            ]
        ),
        encoding="utf-8",
    )
    import_chatgpt_export(input_path=import_path, data_dir=data_dir, memory_dir=memory_dir)

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "agent.yaml").write_text(
        textwrap.dedent(
            """
            app:
              name: local-agent-lab
              log_level: info
            paths:
              data_dir: data
              logs_dir: data/logs
              indexes_dir: data/indexes
              memory_dir: data/memory
              patches_dir: data/patches
            ollama:
              host: http://127.0.0.1:11434
              request_timeout_seconds: 180
            runtime:
              default_task: chat
              redact_before_model: true
              save_full_prompts: true
            models:
              chat_small:
                model: fake
                task: chat
                temperature: 0.1
                max_tokens: 4096
                routing_label: local
            routing:
              task_map:
                chat: chat_small
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LAGENT_CONFIG", str(config_dir / "agent.yaml"))

    report = analyze_memory_patterns(
        data_dir=data_dir,
        memory_dir=memory_dir,
        logs_dir=data_dir / "logs",
        focus="all",
        source_role="user",
        limit=100,
    )
    rendered = render_memory_patterns(report)
    html_rendered = render_memory_patterns_html(report)

    assert report["status"] == "ok"
    assert len(report["pattern_sets"]) == 2
    recipe = next(item for item in report["pattern_sets"] if item["focus"] == "recipes")
    project = next(item for item in report["pattern_sets"] if item["focus"] == "projects")
    assert any(category["name"] == "baking_and_desserts" for category in recipe["suggested_categories"])
    assert any(category["name"] == "career_and_search" for category in project["suggested_categories"])
    assert "natural_categories" in rendered
    assert "Memory Pattern Analysis" in html_rendered

    runner = CliRunner()
    result = runner.invoke(app, ["memory-patterns", "--focus", "all", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    assert len(payload["pattern_sets"]) == 2
    trace = read_memory_trace(data_dir / "logs", payload["run_id"])
    stages = [event["stage"] for event in trace["trace_events"]]
    assert "analyze_patterns" in stages
    assert "render_output" in stages
    assert trace["command"]["command"] == "memory-patterns"


def test_memory_review_promotes_user_candidate_but_blocks_assistant_suggestion(tmp_path, monkeypatch) -> None:
    data_dir = tmp_path / "data"
    memory_dir = data_dir / "memory"
    import_path = tmp_path / "raw"
    export_dir = import_path / "export-1"
    export_dir.mkdir(parents=True)
    (export_dir / "conversations.json").write_text(
        json.dumps(
            [
                {
                    "id": "conversation-review",
                    "title": "Review loop",
                    "mapping": {
                        "u": {
                            "id": "u",
                            "message": {
                                "id": "u",
                                "author": {"role": "user"},
                                "content": {"parts": ["I prefer deterministic local tools."]},
                            },
                        },
                        "a": {
                            "id": "a",
                            "message": {
                                "id": "a",
                                "author": {"role": "assistant"},
                                "content": {"parts": ["You could promote this into curated memory."]},
                            },
                        },
                    },
                }
            ]
        ),
        encoding="utf-8",
    )
    import_chatgpt_export(input_path=import_path, data_dir=data_dir, memory_dir=memory_dir)

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "agent.yaml").write_text(
        textwrap.dedent(
            """
            app:
              name: local-agent-lab
              log_level: info
            paths:
              data_dir: data
              logs_dir: data/logs
              indexes_dir: data/indexes
              memory_dir: data/memory
              patches_dir: data/patches
            ollama:
              host: http://127.0.0.1:11434
              request_timeout_seconds: 180
            runtime:
              default_task: chat
              redact_before_model: true
              save_full_prompts: true
            models:
              chat_small:
                model: fake
                task: chat
                temperature: 0.1
                max_tokens: 4096
                routing_label: local
            routing:
              task_map:
                chat: chat_small
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LAGENT_CONFIG", str(config_dir / "agent.yaml"))

    runner = CliRunner()
    queue = runner.invoke(app, ["memory-review", "--json"])
    assert queue.exit_code == 0
    payload = json.loads(queue.stdout)
    user_candidate = next(item for item in payload["candidate_memories"] if not item["assistant_suggestion"])
    assistant_candidate = next(item for item in payload["candidate_memories"] if item["assistant_suggestion"])

    promote = runner.invoke(
        app,
        [
            "memory-review",
            "--candidate-id",
            user_candidate["id"],
            "--action",
            "promote",
            "--record-type",
            "preference",
            "--title",
            "Prefer deterministic local tools",
        ],
    )
    assert promote.exit_code == 0
    assert "Promoted:" in promote.stdout

    with sqlite3.connect(memory_dir / "chatgpt_memory.sqlite3") as connection:
        candidate = get_candidate_memory(connection, user_candidate["id"])
        assert candidate.review_status == "merged"
        record = connection.execute(
            "SELECT id, record_type, title, source_kind FROM memory_records WHERE source_ref = ?",
            (user_candidate["id"],),
        ).fetchone()
        assert record[1] == "preference"
        assert record[2] == "Prefer deterministic local tools"
        assert record[3] == "chatgpt_candidate"

    blocked = runner.invoke(
        app,
        [
            "memory-review",
            "--candidate-id",
            assistant_candidate["id"],
            "--action",
            "promote",
        ],
    )
    assert blocked.exit_code == 1
    assert "assistant suggestions stay separate" in blocked.stdout or "assistant suggestions stay separate" in blocked.stderr


def test_memory_context_explain_writes_trace_artifact(tmp_path, monkeypatch) -> None:
    data_dir = tmp_path / "data"
    memory_dir = data_dir / "memory"
    import_path = tmp_path / "raw"
    export_dir = import_path / "export-1"
    export_dir.mkdir(parents=True)
    (export_dir / "conversations.json").write_text(
        json.dumps(
            [
                {
                    "id": "conversation-context",
                    "title": "Recipe context",
                    "mapping": {
                        "u": {
                            "id": "u",
                            "message": {
                                "id": "u",
                                "author": {"role": "user"},
                                "content": {"parts": ["I want a sourdough recipe workflow with clear baking notes."]},
                            },
                        }
                    },
                }
            ]
        ),
        encoding="utf-8",
    )
    import_chatgpt_export(input_path=import_path, data_dir=data_dir, memory_dir=memory_dir)

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "agent.yaml").write_text(
        textwrap.dedent(
            """
            app:
              name: local-agent-lab
              log_level: info
            paths:
              data_dir: data
              logs_dir: data/logs
              indexes_dir: data/indexes
              memory_dir: data/memory
              patches_dir: data/patches
            ollama:
              host: http://127.0.0.1:11434
              request_timeout_seconds: 180
            runtime:
              default_task: chat
              redact_before_model: true
              save_full_prompts: true
            models:
              chat_small:
                model: fake
                task: chat
                temperature: 0.1
                max_tokens: 4096
                routing_label: local
            routing:
              task_map:
                chat: chat_small
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LAGENT_CONFIG", str(config_dir / "agent.yaml"))

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["memory-context", "sourdough recipe baking notes", "--depth", "medium", "--explain", "--json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    assert payload["context_items"]
    assert payload["domain_detection"]["primary_domain"] == "cooking_baking"
    assert payload["candidate_counts"]["after_filters"] == 1

    trace = read_memory_trace(data_dir / "logs", payload["run_id"])
    assert "context_pack.json" in trace["artifacts"]
    assert "context_explain.json" in trace["artifacts"]
    explain = json.loads((data_dir / "logs" / payload["run_id"] / "context_explain.json").read_text(encoding="utf-8"))
    assert explain["ranking_profile"] == "hybrid_memory_v1"
    assert explain["context_items"][0]["source_id"].startswith("chk_")


def test_memory_review_subjects_browses_candidate_queue_by_subject_and_traces(tmp_path, monkeypatch) -> None:
    data_dir = tmp_path / "data"
    memory_dir = data_dir / "memory"
    import_path = tmp_path / "raw"
    export_dir = import_path / "export-1"
    export_dir.mkdir(parents=True)
    (export_dir / "conversations.json").write_text(
        json.dumps(
            [
                {
                    "id": "conversation-subject-review",
                    "title": "Subject review",
                    "mapping": {
                        "u": {
                            "id": "u",
                            "message": {
                                "id": "u",
                                "author": {"role": "user"},
                                "content": {"parts": ["We should use a local dashboard for memory review."]},
                            },
                        },
                        "u2": {
                            "id": "u2",
                            "message": {
                                "id": "u2",
                                "author": {"role": "user"},
                                "content": {"parts": ["do it"]},
                            },
                        },
                        "a": {
                            "id": "a",
                            "message": {
                                "id": "a",
                                "author": {"role": "assistant"},
                                "content": {"parts": ["You could make a dashboard for memory review."]},
                            },
                        },
                    },
                }
            ]
        ),
        encoding="utf-8",
    )
    import_chatgpt_export(input_path=import_path, data_dir=data_dir, memory_dir=memory_dir)

    with sqlite3.connect(memory_dir / "chatgpt_memory.sqlite3") as connection:
        conversation_id = connection.execute(
            "SELECT id FROM conversations WHERE title = ?",
            ("Subject review",),
        ).fetchone()[0]
        assign_conversation_subject(connection, conversation_id, "Memory Ops", include_chunks=True)

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "agent.yaml").write_text(
        textwrap.dedent(
            """
            app:
              name: local-agent-lab
              log_level: info
            paths:
              data_dir: data
              logs_dir: data/logs
              indexes_dir: data/indexes
              memory_dir: data/memory
              patches_dir: data/patches
            ollama:
              host: http://127.0.0.1:11434
              request_timeout_seconds: 180
            runtime:
              default_task: chat
              redact_before_model: true
              save_full_prompts: true
            models:
              chat_small:
                model: fake
                task: chat
                temperature: 0.1
                max_tokens: 4096
                routing_label: local
            routing:
              task_map:
                chat: chat_small
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LAGENT_CONFIG", str(config_dir / "agent.yaml"))

    runner = CliRunner()
    result = runner.invoke(app, ["memory-review-subjects", "--subject", "Memory Ops", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["selected_subject"]["name"] == "Memory Ops"
    assert payload["filters"]["quality_filter"] == "high_signal"
    assert [item["content"] for item in payload["candidate_memories"]] == ["We should use a local dashboard for memory review."]

    full_queue = runner.invoke(app, ["memory-review-subjects", "--subject", "Memory Ops", "--quality-filter", "all", "--json"])
    assert full_queue.exit_code == 0
    full_payload = json.loads(full_queue.stdout)
    assert full_payload["filters"]["quality_filter"] == "all"
    assert len(full_payload["candidate_memories"]) == 3

    trace = read_memory_trace(data_dir / "logs", payload["run_id"])
    stages = [event["stage"] for event in trace["trace_events"]]
    assert "load_config" in stages
    assert "retrieve_subjects" in stages
    assert "retrieve_candidates" in stages
    assert "render_output" in stages
    assert trace["command"]["command"] == "memory-review-subjects"


def test_memory_assist_plans_and_executes_analyze_for_broad_dataset_question(tmp_path, monkeypatch) -> None:
    data_dir = tmp_path / "data"
    memory_dir = data_dir / "memory"
    import_path = tmp_path / "raw"
    export_dir = import_path / "export-1"
    export_dir.mkdir(parents=True)
    (export_dir / "conversations.json").write_text(
        json.dumps(
            [
                {
                    "id": "conversation-assist",
                    "title": "Assist frontdoor",
                    "mapping": {
                        "u": {
                            "id": "u",
                            "message": {
                                "id": "u",
                                "author": {"role": "user"},
                                "content": {"parts": ["Tell me what this dataset looks like."]},
                            },
                        }
                    },
                }
            ]
        ),
        encoding="utf-8",
    )
    import_chatgpt_export(input_path=import_path, data_dir=data_dir, memory_dir=memory_dir)

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "agent.yaml").write_text(
        textwrap.dedent(
            """
            app:
              name: local-agent-lab
              log_level: info
            paths:
              data_dir: data
              logs_dir: data/logs
              indexes_dir: data/indexes
              memory_dir: data/memory
              patches_dir: data/patches
            ollama:
              host: http://127.0.0.1:11434
              request_timeout_seconds: 180
            runtime:
              default_task: chat
              redact_before_model: true
              save_full_prompts: true
            models:
              chat_small:
                model: fake
                task: chat
                temperature: 0.1
                max_tokens: 4096
                routing_label: local
            routing:
              task_map:
                chat: chat_small
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LAGENT_CONFIG", str(config_dir / "agent.yaml"))

    def fake_generate(self, *, model, prompt, system, temperature):
        return {
            "response": json.dumps(
                {
                    "summary": "The user wants a broad understanding of the dataset.",
                    "action": "memory-analyze",
                    "arguments": {"subject_limit": 5, "recent_limit": 3},
                    "rationale": "Broad dataset questions should start with corpus analysis.",
                    "confidence": 0.98,
                    "needs_confirmation": False,
                    "result_style": "summary",
                }
            )
        }

    monkeypatch.setattr(OllamaClient, "generate", fake_generate)

    runner = CliRunner()
    result = runner.invoke(app, ["memory-assist", "tell me what this dataset looks like", "--execute", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["plan"]["action"] == "memory-analyze"
    assert payload["executed"] is True
    assert payload["execution"]["command"] == "memory-analyze"
    assert payload["execution"]["result"]["status"] == "ok"

    trace = read_memory_trace(data_dir / "logs", payload["run_id"])
    stages = [event["stage"] for event in trace["trace_events"]]
    assert "collect_state" in stages
    assert "plan_request" in stages
    assert "execute_action" in stages
    assert "render_output" in stages
    assert trace["command"]["command"] == "memory-assist"
