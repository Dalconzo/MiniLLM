from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from local_agent_lab.config import load_config
from local_agent_lab.home_mcp import build_home_mcp_server
from local_agent_lab.memory.candidates import init_candidate_memory_schema
from local_agent_lab.memory.chatgpt_ingest import init_chatgpt_memory_schema
from local_agent_lab.memory.curated import init_curated_memory_schema
from local_agent_lab.memory.subjects import init_subject_schema


APPLY_TOOL = "memory_merge_apply"
SELECTED_CANDIDATES = ["cand_merge_user", "cand_merge_assistant"]
UNSELECTED_CANDIDATE = "cand_merge_unselected"
CANONICAL_FIELDS = {
    "title": "Rosemary focaccia preferences and outcome",
    "body": (
        "The user prefers rosemary focaccia with crisp edges and reported that using "
        "generous olive oil produced a successful bake."
    ),
    "record_type": "lesson",
    "trust_level": "high",
}


def _write_config(tmp_path: Path) -> Path:
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "agent.yaml"
    config_path.write_text(
        json.dumps(
            {
                "app": {"name": "local-agent-lab", "log_level": "info"},
                "paths": {
                    "data_dir": "data",
                    "logs_dir": "data/logs",
                    "indexes_dir": "data/indexes",
                    "memory_dir": "data/memory",
                    "patches_dir": "data/patches",
                },
                "ollama": {
                    "host": "http://127.0.0.1:11434",
                    "request_timeout_seconds": 180,
                },
                "runtime": {
                    "default_task": "chat",
                    "redact_before_model": True,
                    "save_full_prompts": True,
                },
                "models": {},
                "routing": {"task_map": {}},
            }
        ),
        encoding="utf-8",
    )
    return config_path


def _seed_candidates(memory_dir: Path) -> Path:
    memory_dir.mkdir(parents=True, exist_ok=True)
    db_path = memory_dir / "chatgpt_memory.sqlite3"
    candidates = [
        (
            SELECTED_CANDIDATES[0],
            "user",
            "outcome",
            0,
            "The rosemary focaccia was a success after I used more olive oil and baked it until crisp.",
        ),
        (
            SELECTED_CANDIDATES[1],
            "assistant",
            "assistant_suggestion",
            1,
            "Rosemary focaccia may benefit from generous olive oil and a deeply golden bake.",
        ),
        (
            UNSELECTED_CANDIDATE,
            "user",
            "preference",
            0,
            "I also like focaccia topped with roasted garlic.",
        ),
    ]

    with sqlite3.connect(db_path) as connection:
        init_chatgpt_memory_schema(connection)
        init_candidate_memory_schema(connection)
        init_subject_schema(connection)
        init_curated_memory_schema(connection)
        connection.execute(
            """
            INSERT INTO imports (
                id, source_root, raw_manifest_path, imported_at, status, parser_version,
                file_count, conversation_count, message_count, chunk_count, attachment_count,
                content_sha256, notes
            ) VALUES ('imp_disposable', '/fixture', '', datetime('now'), 'ok', 'test', 1, 1, 3, 3, 0, 'fixture', '')
            """
        )
        connection.execute(
            """
            INSERT INTO conversations (
                id, import_id, source_conversation_id, title, created_at, updated_at,
                message_count, first_message_at, last_message_at, summary, content_sha256,
                is_deleted, metadata_json
            ) VALUES ('conv_disposable', 'imp_disposable', 'conv_disposable', 'Disposable merge fixture',
                      datetime('now'), datetime('now'), 3, datetime('now'), datetime('now'),
                      NULL, 'fixture-conversation', 0, '{}')
            """
        )
        for index, (_candidate_id, source_role, _memory_type, _assistant_suggestion, content) in enumerate(
            candidates, start=1
        ):
            connection.execute(
                """
                INSERT INTO messages (
                    id, conversation_id, import_id, source_message_id, parent_message_id, role,
                    author_name, turn_index, created_at, content_text, content_sha256,
                    token_estimate, attachment_count, is_deleted, metadata_json
                ) VALUES (?, 'conv_disposable', 'imp_disposable', ?, NULL, ?, NULL, ?, datetime('now'),
                          ?, ?, 20, 0, 0, '{}')
                """,
                (f"msg_{index}", f"msg_{index}", source_role, index, content, f"sha-msg-{index}"),
            )
            connection.execute(
                """
                INSERT INTO message_chunks (
                    id, message_id, conversation_id, import_id, chunk_index, text, text_sha256,
                    token_estimate, start_char, end_char, source_kind, summary, is_deleted, metadata_json
                ) VALUES (?, ?, 'conv_disposable', 'imp_disposable', 0, ?, ?, 20, 0, ?,
                          'test_fixture', NULL, 0, '{}')
                """,
                (f"chunk_{index}", f"msg_{index}", content, f"sha-chunk-{index}", len(content)),
            )
        connection.executemany(
            """
            INSERT INTO candidate_memories (
                id, import_id, conversation_id, message_id, chunk_id, source_kind, source_ref,
                source_role, memory_type, reason_type, domain_primary, domains_json, confidence,
                valid_from, valid_to, last_confirmed_at, review_status, review_notes, origin,
                assistant_suggestion, source_links_json, content, metadata_json, created_at, updated_at
            )
            VALUES (
                ?, 'imp_disposable', 'conv_disposable', ?, ?, 'test_fixture', ?,
                ?, ?, 'explicit_user_report', 'cooking_baking', '["cooking_baking"]', 0.9,
                '2026-08-19T00:00:00+00:00', NULL, NULL, 'pending', NULL, 'test_fixture',
                ?, ?, ?, '{}', datetime('now'), datetime('now')
            )
            """,
            [
                (
                    candidate_id,
                    f"msg_{index}",
                    f"chunk_{index}",
                    f"chunk_{index}",
                    source_role,
                    memory_type,
                    assistant_suggestion,
                    json.dumps(
                        {
                            "conversation_id": "conv_disposable",
                            "message_id": f"msg_{index}",
                            "chunk_id": f"chunk_{index}",
                            "source_kind": "test_fixture",
                            "source_role": source_role,
                        },
                        sort_keys=True,
                    ),
                    content,
                )
                for index, (candidate_id, source_role, memory_type, assistant_suggestion, content) in enumerate(
                    candidates, start=1
                )
            ],
        )
    return db_path


@pytest.fixture
def merge_server(tmp_path: Path):
    config = load_config(_write_config(tmp_path))
    db_path = _seed_candidates(config.paths["memory_dir"])
    return build_home_mcp_server(config), db_path


def _call_apply(server, *, request_id: str, arguments: dict) -> dict:
    return server.dispatch_jsonrpc(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": APPLY_TOOL, "arguments": arguments},
        }
    )


def _tool_payload(response: dict) -> dict:
    assert "error" not in response, response.get("error")
    return json.loads(response["result"]["content"][0]["text"])


def _valid_arguments(server) -> dict:
    preview = server.memory_merge_preview(
        candidate_ids=list(SELECTED_CANDIDATES),
        title=CANONICAL_FIELDS["title"],
        body=CANONICAL_FIELDS["body"],
        record_type=CANONICAL_FIELDS["record_type"],
        trust_level=CANONICAL_FIELDS["trust_level"],
    )
    return {
        "candidate_ids": list(SELECTED_CANDIDATES),
        "preview_token": preview["preview_token"],
        "confirmed": True,
        **CANONICAL_FIELDS,
    }


def _truth_counts(db_path: Path) -> dict[str, int]:
    with sqlite3.connect(db_path) as connection:
        return {
            "records": connection.execute("SELECT COUNT(*) FROM memory_records").fetchone()[0],
            "links": connection.execute("SELECT COUNT(*) FROM memory_links").fetchone()[0],
            "merged": connection.execute(
                "SELECT COUNT(*) FROM candidate_memories WHERE review_status = 'merged'"
            ).fetchone()[0],
        }


def test_merge_apply_is_discoverable_with_an_explicit_confirmation_schema(merge_server) -> None:
    server, _ = merge_server
    response = server.dispatch_jsonrpc(
        {"jsonrpc": "2.0", "id": "list", "method": "tools/list", "params": {}}
    )
    tools = {tool["name"]: tool for tool in response["result"]["tools"]}

    assert APPLY_TOOL in tools
    tool = tools[APPLY_TOOL]
    schema = tool["inputSchema"]
    properties = schema["properties"]
    assert "mutat" in tool["description"].lower()
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) >= {
        "candidate_ids",
        "preview_token",
        "confirmed",
        "title",
        "body",
        "record_type",
        "trust_level",
    }
    assert properties["candidate_ids"]["type"] == "array"
    assert properties["candidate_ids"]["minItems"] == 2
    assert properties["confirmed"]["type"] == "boolean"
    assert properties["confirmed"].get("const", True) is True
    assert properties["title"]["type"] == "string"
    assert properties["body"]["type"] == "string"
    assert "lesson" in properties["record_type"]["enum"]
    assert "high" in properties["trust_level"]["enum"]


@pytest.mark.parametrize(
    ("arguments", "expected_error_code"),
    [
        (
            {"candidate_ids": list(SELECTED_CANDIDATES), **CANONICAL_FIELDS},
            "candidate_merge_confirmation_required",
        ),
        (
            {"candidate_ids": list(SELECTED_CANDIDATES), "confirmed": False, **CANONICAL_FIELDS},
            "candidate_merge_confirmation_required",
        ),
        (
            {
                "candidate_ids": list(SELECTED_CANDIDATES),
                "confirmed": True,
                "record_type": "lesson",
                "trust_level": "high",
            },
            "candidate_merge_missing_canonical_fields",
        ),
    ],
)
def test_merge_apply_refuses_unconfirmed_or_incomplete_requests(
    merge_server, arguments: dict, expected_error_code: str
) -> None:
    server, db_path = merge_server
    before = _truth_counts(db_path)

    response = _call_apply(server, request_id="refusal", arguments=arguments)

    assert response["error"]["data"]["stage"] == APPLY_TOOL
    assert response["error"]["data"]["error_code"] == expected_error_code
    assert _truth_counts(db_path) == before


def test_merge_apply_jsonrpc_mutates_only_selected_disposable_candidates(merge_server) -> None:
    server, db_path = merge_server

    payload = _tool_payload(_call_apply(server, request_id="apply", arguments=_valid_arguments(server)))

    assert payload["status"] == "ok"
    assert payload["tool_name"] == APPLY_TOOL
    assert payload["mode"] == "apply"
    assert payload["mutates_memory_truth"] is True
    assert payload["mutation_policy"]["mutates_memory_truth"] is True
    assert payload["mutation_policy"]["explicitly_confirmed"] is True
    assert payload["run_id"]
    assert payload["trace_id"]
    assert payload["trace_artifact_dir"]
    assert Path(payload["trace_artifact_dir"]).is_dir()
    assert payload["source_candidate_ids"] == SELECTED_CANDIDATES

    record = payload["memory_record"]
    assert record["title"] == CANONICAL_FIELDS["title"]
    assert record["body"] == CANONICAL_FIELDS["body"]
    assert record["record_type"] == CANONICAL_FIELDS["record_type"]
    assert record["trust_level"] == CANONICAL_FIELDS["trust_level"]
    assert record["provenance"]["source_candidate_ids"] == SELECTED_CANDIDATES
    assert record["provenance"]["user_candidate_ids"] == [SELECTED_CANDIDATES[0]]
    assert record["provenance"]["assistant_candidate_ids"] == [SELECTED_CANDIDATES[1]]
    assert record["provenance"]["assistant_suggestions_separate"] is True

    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        records = connection.execute("SELECT * FROM memory_records").fetchall()
        links = connection.execute(
            "SELECT from_kind, from_id, to_kind, to_id, link_type FROM memory_links ORDER BY from_id"
        ).fetchall()
        statuses = dict(
            connection.execute("SELECT id, review_status FROM candidate_memories ORDER BY id").fetchall()
        )

    assert len(records) == 1
    assert records[0]["id"] == record["id"]
    candidate_links = [link for link in links if link["to_kind"] == "candidate_memory"]
    assert len(candidate_links) == len(SELECTED_CANDIDATES)
    assert {link["to_id"] for link in candidate_links} == set(SELECTED_CANDIDATES)
    assert {link["from_id"] for link in candidate_links} == {record["id"]}
    assert all(link["from_kind"] == "memory_record" for link in candidate_links)
    assert statuses[SELECTED_CANDIDATES[0]] == "merged"
    assert statuses[SELECTED_CANDIDATES[1]] == "merged"
    assert statuses[UNSELECTED_CANDIDATE] == "pending"


def test_merge_apply_repeat_is_idempotent_or_returns_a_typed_conflict(merge_server) -> None:
    server, db_path = merge_server
    arguments = _valid_arguments(server)
    first = _tool_payload(_call_apply(server, request_id="first", arguments=arguments))
    after_first = _truth_counts(db_path)

    repeated = _call_apply(server, request_id="repeat", arguments=arguments)

    if "error" in repeated:
        assert repeated["error"]["data"]["stage"] == APPLY_TOOL
        assert repeated["error"]["data"]["error_code"] in {
            "candidate_merge_already_applied",
            "merge_already_applied",
        }
    else:
        payload = _tool_payload(repeated)
        assert payload["status"] == "ok"
        assert payload["mutates_memory_truth"] is True
        assert payload["idempotent_replay"] is True
        assert payload["memory_record"]["id"] == first["memory_record"]["id"]

    assert _truth_counts(db_path) == after_first
