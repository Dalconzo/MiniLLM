import json
import sqlite3
import textwrap

from typer.testing import CliRunner

from local_agent_lab.cli import app
from local_agent_lab.memory.eval_checks import _prepare_eval_memory_state, _write_eval_export
from local_agent_lab.memory.observability import read_memory_trace


def test_acceptance_chatgpt_import_pipeline_is_traceable_and_idempotent(tmp_path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)
    monkeypatch.setenv("LAGENT_CONFIG", str(config_path))
    input_path = _write_eval_export(tmp_path / "fixture")
    runner = CliRunner()

    dry_run = runner.invoke(app, ["ingest-chatgpt", "--input", str(input_path), "--dry-run", "--trace"])
    assert dry_run.exit_code == 0
    dry_payload = json.loads(dry_run.stdout)
    assert dry_payload["dry_run"] is True
    assert dry_payload["summary"]["conversations"] >= 10
    assert not (tmp_path / "data" / "memory" / "chatgpt_memory.sqlite3").exists()
    dry_trace = read_memory_trace(tmp_path / "data" / "logs", dry_payload["run_id"])
    assert {"discover_input", "parse_export", "write_audit"} <= {event["stage"] for event in dry_trace["trace_events"]}

    imported = runner.invoke(app, ["ingest-chatgpt", "--input", str(input_path), "--trace"])
    assert imported.exit_code == 0
    import_payload = json.loads(imported.stdout)
    assert import_payload["summary"]["conversations"] >= 10
    assert import_payload["summary"]["candidate_memories"] >= 10
    assert (tmp_path / "data" / "memory" / "chatgpt_memory.sqlite3").exists()
    import_trace = read_memory_trace(tmp_path / "data" / "logs", import_payload["run_id"])
    assert {"parse_export", "chunk_messages", "write_sqlite", "refresh_fts", "write_audit"} <= {
        event["stage"] for event in import_trace["trace_events"]
    }

    second_import = runner.invoke(app, ["ingest-chatgpt", "--input", str(input_path), "--trace"])
    assert second_import.exit_code == 0
    second_payload = json.loads(second_import.stdout)
    assert second_payload["summary"] == import_payload["summary"]
    with sqlite3.connect(tmp_path / "data" / "memory" / "chatgpt_memory.sqlite3") as connection:
        counts = dict(
            connection.execute(
                """
                SELECT 'conversations', COUNT(*) FROM conversations
                UNION ALL SELECT 'messages', COUNT(*) FROM messages
                UNION ALL SELECT 'chunks', COUNT(*) FROM message_chunks
                """
            ).fetchall()
        )
    assert counts["conversations"] == import_payload["summary"]["conversations"]
    assert counts["messages"] == import_payload["summary"]["messages"]
    assert counts["chunks"] == import_payload["summary"]["chunks"]

    check = runner.invoke(app, ["memory-check", "--json"])
    assert check.exit_code == 0
    check_payload = json.loads(check.stdout)
    assert check_payload["status"] in {"ok", "warn"}
    assert check_payload["summary"]["errors"] == 0


def test_acceptance_retrieval_context_and_audit_traces_are_explainable(tmp_path, monkeypatch) -> None:
    _seed_eval_memory(tmp_path, monkeypatch)
    runner = CliRunner()

    search = runner.invoke(
        app,
        [
            "memory-search",
            "what should an agent remember about my baking recipe format and miso butter",
            "--subject",
            "Recipes and Baking",
            "--depth",
            "full",
            "--explain",
            "--json",
        ],
    )
    assert search.exit_code == 0
    search_payload = json.loads(search.stdout)
    assert search_payload["count"] >= 1
    assert search_payload["domain_detection"]["primary_domain"] == "cooking_baking"
    assert search_payload["retrieval_event_id"].startswith("ret_")
    assert {"subject", "fts_strategy"} <= {item["field"] for item in search_payload["filters_applied"]}
    search_trace = read_memory_trace(tmp_path / "data" / "logs", search_payload["run_id"])
    assert {"search.json", "search_explain.json"} <= set(search_trace["artifacts"])
    assert {"retrieve_candidates", "rank_results", "apply_disclosure"} <= {event["stage"] for event in search_trace["trace_events"]}

    audit = runner.invoke(app, ["memory-audit", search_payload["run_id"], "--json"])
    assert audit.exit_code == 0
    audit_payload = json.loads(audit.stdout)
    assert audit_payload["count"] == search_payload["count"]
    assert audit_payload["exposures"][0]["source_id"]

    context = runner.invoke(
        app,
        [
            "memory-context",
            "recipe assistant miso butter confirmed recipe facts assistant draft separate",
            "--subject",
            "Recipes and Baking",
            "--depth",
            "full",
            "--effort",
            "4",
            "--explain",
            "--json",
        ],
    )
    assert context.exit_code == 0
    context_payload = json.loads(context.stdout)
    assert context_payload["context_items"]
    assert context_payload["retrieval_event_id"].startswith("ret_")
    assert context_payload["context_packet_id"].startswith("ctx_")
    assert context_payload["context_packet"]["schema_version"] == 2
    assert context_payload["context_packet"]["provenance"]["retrieval_event_id"] == context_payload["retrieval_event_id"]
    assert "inferred_patterns" in context_payload["context_packet"]
    assert context_payload["governance"]["policy"] == "standard_v1"
    context_trace = read_memory_trace(tmp_path / "data" / "logs", context_payload["run_id"])
    assert {"context_pack.json", "context_explain.json"} <= set(context_trace["artifacts"])


def test_acceptance_status_review_and_lifecycle_surfaces_are_visible(tmp_path, monkeypatch) -> None:
    _seed_eval_memory(tmp_path, monkeypatch)
    runner = CliRunner()

    status = runner.invoke(app, ["memory-status", "--json"])
    assert status.exit_code == 0
    status_payload = json.loads(status.stdout)
    assert status_payload["status"] in {"ok", "warn"}
    assert status_payload["validation"]["summary"]["errors"] == 0
    assert status_payload["sqlite"]["counts"]["candidate_memories"] >= 10
    assert status_payload["sqlite"]["counts"]["memory_records"] >= 8
    assert status_payload["recent_runs"]

    subjects = runner.invoke(app, ["memory-subjects", "--json"])
    assert subjects.exit_code == 0
    subject_payload = json.loads(subjects.stdout)
    assert any(item["name"] == "Recipes and Baking" for item in subject_payload["subjects"])

    review_subjects = runner.invoke(app, ["memory-review-subjects", "--subject", "Recipes and Baking", "--json"])
    assert review_subjects.exit_code == 0
    review_payload = json.loads(review_subjects.stdout)
    assert review_payload["count"] >= 1
    assert review_payload["candidate_memories"]

    open_loops = runner.invoke(app, ["memory-open-loops", "--json"])
    assert open_loops.exit_code == 0
    loop_payload = json.loads(open_loops.stdout)
    assert loop_payload["count"] >= 1
    assert any("Baking camera open loop" == item["title"] for item in loop_payload["open_loops"])

    records = runner.invoke(app, ["memory-list", "--json"])
    assert records.exit_code == 0
    record_payload = json.loads(records.stdout)
    target_record = next(item for item in record_payload["memory_records"] if item["title"] == "Home MCP memory tools")
    assigned = runner.invoke(app, ["memory-curated-assign-subject", target_record["id"], "Home MCP", "--json"])
    assert assigned.exit_code == 0
    assigned_payload = json.loads(assigned.stdout)
    assert assigned_payload["memory_record"]["subject_id"] == assigned_payload["subject"]["id"]

    eval_run = runner.invoke(app, ["memory-eval", "--json"])
    assert eval_run.exit_code == 0
    eval_payload = json.loads(eval_run.stdout)
    assert eval_payload["summary"]["usage_cases"] == 14
    assert eval_payload["summary"]["usage_score"] == eval_payload["summary"]["usage_max_score"] == 70
    assert eval_payload["ab_report"]["winner"] in {"local_structured_object_memory", "combined_memory"}
    assert {variant["variant"] for variant in eval_payload["ab_report"]["variants"]} == {
        "no_memory",
        "platform_memory",
        "raw_history_rag",
        "local_structured_object_memory",
        "combined_memory",
    }


def _seed_eval_memory(tmp_path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)
    monkeypatch.setenv("LAGENT_CONFIG", str(config_path))
    input_path = _write_eval_export(tmp_path / "fixture")
    runner = CliRunner()
    result = runner.invoke(app, ["ingest-chatgpt", "--input", str(input_path)])
    assert result.exit_code == 0
    _prepare_eval_memory_state(tmp_path / "data" / "memory" / "chatgpt_memory.sqlite3")


def _write_config(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
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
    return config_path
