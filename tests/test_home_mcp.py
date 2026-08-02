from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from local_agent_lab.config import load_config
from local_agent_lab.cli import _probe_home_mcp_health, _render_memory_search, _run_home_mcp_smoke_test
from local_agent_lab.home_mcp import HomeMCPError, _extract_recipe_structure, build_home_mcp_server, serve_home_mcp
from local_agent_lab.memory.audit import init_audit_schema
from local_agent_lab.memory.candidates import init_candidate_memory_schema
from local_agent_lab.memory.chatgpt_ingest import init_chatgpt_memory_schema
from local_agent_lab.memory.curated import init_curated_memory_schema
from local_agent_lab.memory.feedback import init_feedback_schema
from local_agent_lab.memory.subjects import assign_conversation_subject, init_subject_schema


def _write_config(tmp_path: Path, extra_home_mcp: dict | None = None) -> Path:
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "app": {"name": "local-agent-lab", "log_level": "info"},
        "paths": {
            "data_dir": "data",
            "logs_dir": "data/logs",
            "indexes_dir": "data/indexes",
            "memory_dir": "data/memory",
            "patches_dir": "data/patches",
        },
        "ollama": {"host": "http://127.0.0.1:11434", "request_timeout_seconds": 180},
        "runtime": {"default_task": "chat", "redact_before_model": True, "save_full_prompts": True},
        "models": {},
        "routing": {"task_map": {}},
    }
    if extra_home_mcp is not None:
        payload["home_mcp"] = extra_home_mcp
    config_path = config_dir / "agent.yaml"
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    return config_path


def _seed_memory_database(memory_dir: Path) -> dict[str, str]:
    data_dir = memory_dir.parent
    (data_dir / "chatgpt_exports" / "raw").mkdir(parents=True, exist_ok=True)
    (data_dir / "chatgpt_exports" / "parsed").mkdir(parents=True, exist_ok=True)
    memory_dir.mkdir(parents=True, exist_ok=True)

    db_path = memory_dir / "chatgpt_memory.sqlite3"
    import_id = "imp_test"
    conversation_id = "conv_test"
    message_id = "msg_test"
    chunk_id = "chunk_test"
    candidate_id = "cand_test"

    with sqlite3.connect(db_path) as connection:
        init_chatgpt_memory_schema(connection)
        init_candidate_memory_schema(connection)
        init_curated_memory_schema(connection)
        init_feedback_schema(connection)
        init_audit_schema(connection)
        init_subject_schema(connection)
        connection.execute(
            """
            INSERT INTO imports (
                id, source_root, raw_manifest_path, imported_at, status, parser_version,
                file_count, conversation_count, message_count, chunk_count, attachment_count,
                content_sha256, notes
            )
            VALUES (?, ?, ?, datetime('now'), ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                import_id,
                str(data_dir / "chatgpt_exports" / "raw"),
                "",
                "ok",
                "chatgpt_export_v1",
                1,
                1,
                1,
                1,
                0,
                "seeded",
                "",
            ),
        )
        connection.execute(
            """
            INSERT INTO conversations (
                id, import_id, source_conversation_id, title, created_at, updated_at,
                message_count, first_message_at, last_message_at, summary, content_sha256,
                is_deleted, metadata_json
            )
            VALUES (?, ?, ?, ?, datetime('now'), datetime('now'), ?, datetime('now'), datetime('now'), ?, ?, 0, '{}')
            """,
            (
                conversation_id,
                import_id,
                "source-conv",
                "Rosemary Focaccia",
                1,
                "A recipe discussion.",
                "seeded-conversation",
            ),
        )
        connection.execute(
            """
            INSERT INTO messages (
                id, conversation_id, import_id, source_message_id, parent_message_id, role,
                author_name, turn_index, created_at, content_text, content_sha256, token_estimate,
                attachment_count, is_deleted, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?, ?, ?, ?, 0, '{}')
            """,
            (
                message_id,
                conversation_id,
                import_id,
                "source-message",
                None,
                "user",
                None,
                1,
                "Please draft a rosemary focaccia recipe with notes.",
                "seeded-message",
                12,
                0,
            ),
        )
        connection.execute(
            """
            INSERT INTO message_chunks (
                id, message_id, conversation_id, import_id, chunk_index, text, text_sha256,
                token_estimate, start_char, end_char, source_kind, summary, is_deleted, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, '{}')
            """,
            (
                chunk_id,
                message_id,
                conversation_id,
                import_id,
                1,
                "Please draft a rosemary focaccia recipe with notes.",
                "seeded-chunk",
                12,
                0,
                49,
                "text",
                "Recipe request",
            ),
        )
        connection.execute(
            """
            INSERT INTO chatgpt_chunks_fts (
                title, role, text, import_id, conversation_id, message_id, chunk_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "Rosemary Focaccia",
                "user",
                "Please draft a rosemary focaccia recipe with notes.",
                import_id,
                conversation_id,
                message_id,
                chunk_id,
            ),
        )
        assign_conversation_subject(connection, conversation_id, "Cooking and Baking", include_chunks=True)
        connection.execute(
            """
            INSERT INTO candidate_memories (
                id, import_id, conversation_id, message_id, chunk_id, source_kind, source_ref,
                source_role, memory_type, reason_type, domain_primary, domains_json, confidence,
                valid_from, valid_to, last_confirmed_at, review_status, review_notes, origin,
                assistant_suggestion, source_links_json, content, metadata_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
            """,
            (
                candidate_id,
                import_id,
                conversation_id,
                message_id,
                chunk_id,
                "chatgpt_export",
                chunk_id,
                "user",
                "procedure",
                "recipe_instruction",
                "cooking_baking",
                json.dumps(["cooking_baking"], sort_keys=True),
                0.93,
                "2026-07-01T00:00:00+00:00",
                None,
                None,
                "pending",
                None,
                "chatgpt_export",
                0,
                json.dumps(
                    {
                        "conversation_id": conversation_id,
                        "message_id": message_id,
                        "chunk_id": chunk_id,
                        "source_kind": "chatgpt_export",
                        "source_role": "user",
                    },
                    sort_keys=True,
                ),
                "Please draft a rosemary focaccia recipe with notes.",
                json.dumps({"message_turn_index": 1, "chunk_index": 1}, sort_keys=True),
            ),
        )
    return {
        "db_path": str(db_path),
        "import_id": import_id,
        "conversation_id": conversation_id,
        "message_id": message_id,
        "chunk_id": chunk_id,
        "candidate_id": candidate_id,
    }


def test_home_mcp_lists_roots_and_blocks_escape(tmp_path) -> None:
    config_path = _write_config(tmp_path)
    config = load_config(config_path)
    server = build_home_mcp_server(config)

    roots = server.list_allowed_roots()
    assert roots["status"] == "ok"
    assert {root["id"] for root in roots["roots"]} >= {"recipe_book", "household", "projects"}

    with pytest.raises(HomeMCPError) as exc_info:
        server.create_markdown_note(root_id="recipe_book", folder="../escape", title="Bad", body="nope")
    assert exc_info.value.error_code == "path_escape"


def test_home_mcp_security_acceptance_rejects_boundary_violations_and_audits(tmp_path) -> None:
    config_path = _write_config(tmp_path)
    config = load_config(config_path)
    server = build_home_mcp_server(config)
    recipe_root = server.roots_by_id["recipe_book"].path
    recipe_root.mkdir(parents=True, exist_ok=True)
    (recipe_root / ".secret.md").write_text("do not expose", encoding="utf-8")
    outside = tmp_path / "outside-secret.md"
    outside.write_text("outside", encoding="utf-8")
    (recipe_root / "outside-link.md").symlink_to(outside)

    hidden = server.dispatch_jsonrpc(
        {
            "jsonrpc": "2.0",
            "id": "hidden",
            "method": "tools/call",
            "params": {"name": "read_file", "arguments": {"file_id": "recipe_book:.secret.md"}},
        }
    )
    assert hidden["error"]["data"]["error_code"] == "hidden_path_blocked"

    symlink_escape = server.dispatch_jsonrpc(
        {
            "jsonrpc": "2.0",
            "id": "symlink",
            "method": "tools/call",
            "params": {"name": "read_file", "arguments": {"file_id": "recipe_book:outside-link.md"}},
        }
    )
    assert symlink_escape["error"]["data"]["error_code"] == "path_escape"

    unknown_root = server.dispatch_jsonrpc(
        {
            "jsonrpc": "2.0",
            "id": "unknown-root",
            "method": "tools/call",
            "params": {"name": "list_files", "arguments": {"root_id": "production_secrets"}},
        }
    )
    assert unknown_root["error"]["data"]["error_code"] == "unknown_root"

    arbitrary_shell = server.dispatch_jsonrpc(
        {
            "jsonrpc": "2.0",
            "id": "shell",
            "method": "tools/call",
            "params": {"name": "shell", "arguments": {"command": "id"}},
        }
    )
    assert arbitrary_shell["error"]["data"]["error_code"] == "unsupported_tool"

    unsupported_method = server.dispatch_jsonrpc({"jsonrpc": "2.0", "id": "method", "method": "shell/exec", "params": {}})
    assert unsupported_method["error"]["data"]["error_code"] == "unsupported_method"

    assert not any(tool["name"] in {"shell", "exec", "run_command"} for tool in server.tools())
    error_run_dirs = [path for path in server.logger.logs_dir.iterdir() if path.is_dir()]
    assert error_run_dirs
    assert any((path / "trace.jsonl").exists() and (path / "result.json").exists() for path in error_run_dirs)


def test_home_mcp_creates_recipe_notes_and_appends_attempts(tmp_path) -> None:
    config_path = _write_config(tmp_path)
    config = load_config(config_path)
    server = build_home_mcp_server(config)

    created = server.create_recipe(title="Chocolate Cake", body="Ingredients:\n- cocoa\nSteps:\n- mix\n")
    recipe_path = Path(created["path"])
    assert recipe_path.exists()
    assert '"kind": "recipe"' in recipe_path.read_text(encoding="utf-8")
    assert "Chocolate Cake" in recipe_path.read_text(encoding="utf-8")

    rpc = server.dispatch_jsonrpc(
        {
            "jsonrpc": "2.0",
            "id": 42,
            "method": "tools/call",
            "params": {
                "name": "append_recipe_attempt",
                "arguments": {
                    "recipe_id": created["file_id"],
                    "notes": "Baked at 350F for 30 minutes.",
                    "outcome": "good",
                    "next_time": "Use a deeper pan.",
                },
            },
        }
    )
    assert rpc["result"]["structuredContent"]["status"] == "ok"
    content = recipe_path.read_text(encoding="utf-8")
    assert "Baked at 350F for 30 minutes." in content
    assert "Outcome: good" in content
    assert "Next time: Use a deeper pan." in content

    run_dirs = sorted(path for path in server.logger.logs_dir.iterdir() if path.is_dir())
    assert run_dirs
    trace_path = run_dirs[-1] / "trace.jsonl"
    assert trace_path.exists()
    trace_events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert any(event["stage"] == "receive_request" for event in trace_events)
    assert any(event["stage"] == "call_tool" for event in trace_events)


def test_home_mcp_searches_recipe_cards_and_exposes_aliases(tmp_path) -> None:
    config_path = _write_config(tmp_path)
    config = load_config(config_path)
    server = build_home_mcp_server(config)

    created = server.create_recipe_card(
        title="Focaccia with Rosemary",
        body="Ingredients:\n- flour\n- rosemary\n\nSteps:\n- mix\n- bake\n",
        tags=["bread", "rosemary"],
    )

    search = server.search_recipes(query="rosemary", tags=["bread"], limit=5)
    assert search["count"] == 1
    result = search["results"][0]
    assert result["file_id"] == created["file_id"]
    assert result["title"] == "Focaccia with Rosemary"
    assert "bread" in result["tags"]

    rpc = server.dispatch_jsonrpc({"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}})
    tools = rpc["result"]["tools"]
    assert any(tool["name"] == "recipe_standard" for tool in tools)
    assert any(tool["name"] == "browse_recipes" for tool in tools)
    assert any(tool["name"] == "search_recipes" for tool in tools)
    assert any(tool["name"] == "search_notes" for tool in tools)
    assert any(tool["name"] == "list_recent_files" for tool in tools)
    assert any(tool["name"] == "get_recipe" for tool in tools)
    assert any(tool["name"] == "compare_recipe_attempts" for tool in tools)
    assert any(tool["name"] == "create_project_note" for tool in tools)
    assert any(tool["name"] == "create_recipe_card" for tool in tools)
    assert any(tool["name"] == "draft_recipe_card" for tool in tools)

    rpc_call = server.dispatch_jsonrpc(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "search_recipes", "arguments": {"query": "rosemary", "tags": ["bread"], "limit": 5}},
        }
    )
    assert rpc_call["result"]["structuredContent"]["count"] == 1


def test_home_mcp_recipe_search_boosts_exact_title_phrase_over_weak_cooccurrence(tmp_path) -> None:
    config_path = _write_config(tmp_path)
    config = load_config(config_path)
    server = build_home_mcp_server(config)

    weak = server.create_recipe_card(
        title="Peach Crumble Cobbler",
        ingredients=["peaches", "butter", "miso caramel"],
        steps=["Bake until bubbling."],
        tags=["dessert"],
    )
    exact = server.create_recipe_card(
        title="Miso-Butter Roast Bowl",
        ingredients=["miso", "butter", "rice"],
        steps=["Roast vegetables.", "Serve with miso butter."],
        tags=["bowl"],
    )

    result = server.search_recipes(query="miso butter", limit=1)

    assert result["results"][0]["file_id"] == exact["file_id"]
    assert result["results"][0]["match_reason"] == "title_phrase"
    comparison = server.search_recipes(query="miso butter", limit=10)
    weak_result = next(item for item in comparison["results"] if item["file_id"] == weak["file_id"])
    exact_result = next(item for item in comparison["results"] if item["file_id"] == exact["file_id"])
    assert exact_result["score"] > weak_result["score"]


def test_home_mcp_flags_heading_only_recipe_methods_as_non_actionable(tmp_path) -> None:
    config_path = _write_config(tmp_path)
    config = load_config(config_path)
    server = build_home_mcp_server(config)

    created = server.create_recipe_card(
        title="Miso-Butter Roast Bowl with Jammy Eggs",
        body=(
            "## Ingredients\n"
            "- 1 cup jasmine rice\n"
            "- 1 1/4 cups water\n"
            "- 2 tbsp butter\n"
            "- 2 tbsp white or yellow miso\n\n"
            "## Method\n"
            "1. Start the rice\n"
            "2. Heat the oven and prep the first roast\n"
            "3. Cook the jammy eggs\n"
            "4. Make the miso-butter sauce\n"
        ),
        tags=["bowl"],
    )

    assert created["validation"]["actionable"] is False
    assert "non_executable_method" in created["validation_warnings"]
    assert "heading_only_method_steps" in created["validation_warnings"]
    assert created["validation"]["steps_count"] == 4
    assert created["validation"]["executable_steps_count"] == 0

    fetched = server.get_recipe(recipe_id=created["file_id"])
    assert fetched["structure"]["steps"] == [
        "Start the rice",
        "Heat the oven and prep the first roast",
        "Cook the jammy eggs",
        "Make the miso-butter sauce",
    ]
    assert fetched["validation"]["actionable"] is False
    assert fetched["validation"]["heading_only_steps_count"] == 4

    search = server.search_recipes(query="miso butter", limit=1)
    result = search["results"][0]
    assert result["file_id"] == created["file_id"]
    assert result["actionable"] is False
    assert "heading_only_method_steps" in result["validation_warnings"]


def test_home_mcp_recipe_validation_accepts_detailed_steps_and_optional_ingredients(tmp_path) -> None:
    config_path = _write_config(tmp_path)
    config = load_config(config_path)
    server = build_home_mcp_server(config)

    created = server.create_recipe_card(
        title="Detailed Roast Bowl",
        ingredients=[
            "1 cup jasmine rice",
            "1 1/4 cups water",
            "2 tbsp butter",
            "2 tbsp white miso",
            "Optional: sesame seeds",
            "Optional: sliced scallions",
        ],
        steps=[
            "Rinse 1 cup jasmine rice, then cook it with 1 1/4 cups water until tender.",
            "Roast vegetables at 425F for 25 minutes until browned at the edges.",
            "Whisk 2 tbsp butter with 2 tbsp miso in a bowl until smooth.",
        ],
        tags=["bowl"],
    )

    assert created["validation"]["actionable"] is True
    assert created["validation"]["severity"] == "ok"
    assert created["validation_warnings"] == []

    fetched = server.get_recipe(recipe_id=created["file_id"])
    assert fetched["validation"]["actionable"] is True
    assert fetched["validation"]["ingredients_count"] == 6
    assert fetched["validation"]["executable_steps_count"] == 3

    search = server.search_recipes(query="roast bowl", limit=1)
    assert search["results"][0]["actionable"] is True
    assert search["results"][0]["validation_warnings"] == []


def test_home_mcp_note_discovery_recipe_lookup_and_attempt_comparison(tmp_path) -> None:
    config_path = _write_config(tmp_path)
    config = load_config(config_path)
    server = build_home_mcp_server(config)

    recipe = server.create_recipe_card(
        title="Attempted Focaccia",
        ingredients=["500g flour", "1 tbsp rosemary"],
        steps=["Mix.", "Bake."],
        tags=["bread"],
    )
    server.append_recipe_attempt(
        recipe_id=recipe["file_id"],
        notes="Baked at 425F for 22 minutes.",
        outcome="good crust",
        next_time="Add more olive oil.",
    )
    server.append_recipe_attempt(
        recipe_id=recipe["file_id"],
        notes="Baked at 425F for 20 minutes with more oil.",
        outcome="better crumb",
        next_time="Try longer proof.",
    )

    notes = server.search_notes(query="rosemary", root_id="recipe_book", limit=5)
    assert notes["status"] == "ok"
    assert notes["view"] == "notes"
    assert notes["count"] == 1
    assert notes["results"][0]["file_id"] == recipe["file_id"]

    recent = server.list_recent_files(root_id="recipe_book", limit=5, file_types=[".md"])
    assert recent["count"] == 1
    assert recent["files"][0]["file_id"] == recipe["file_id"]

    fetched = server.get_recipe(recipe_id=recipe["file_id"])
    assert fetched["title"] == "Attempted Focaccia"
    assert fetched["structure"]["ingredients"] == ["500g flour", "1 tbsp rosemary"]
    assert fetched["structure"]["steps"][:2] == ["Mix.", "Bake."]
    assert fetched["standard"]["schema_version"] == 1

    compared = server.compare_recipe_attempts(recipe_id=recipe["file_id"])
    assert compared["attempt_count"] == 2
    assert compared["comparison"]["latest_outcome"] == "better crumb"
    assert compared["comparison"]["latest_next_time"] == "Try longer proof."

    rpc_call = server.dispatch_jsonrpc(
        {
            "jsonrpc": "2.0",
            "id": 40,
            "method": "tools/call",
            "params": {"name": "compare_recipe_attempts", "arguments": {"recipe_id": recipe["file_id"]}},
        }
    )
    assert rpc_call["result"]["structuredContent"]["attempt_count"] == 2


def test_home_mcp_creates_project_notes_inside_project_root(tmp_path) -> None:
    config_path = _write_config(tmp_path)
    config = load_config(config_path)
    server = build_home_mcp_server(config)

    created = server.create_project_note(
        project_id="../Craft iPad",
        title="Station plan",
        body="Mount, charger, and note workflow.",
        tags=["craft"],
    )
    path = Path(created["path"])
    assert created["root_id"] == "projects"
    assert created["project_id"] == "craft-ipad"
    assert created["relative_path"].startswith("craft-ipad/")
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert '"kind": "project_note"' in content
    assert "Mount, charger, and note workflow." in content

    rpc_call = server.dispatch_jsonrpc(
        {
            "jsonrpc": "2.0",
            "id": 41,
            "method": "tools/call",
            "params": {
                "name": "create_project_note",
                "arguments": {"project_id": "bread monitor", "title": "MVP", "body": "Sensor checklist."},
            },
        }
    )
    assert rpc_call["result"]["structuredContent"]["root_id"] == "projects"


def test_home_mcp_get_recipe_rejects_non_recipe_root(tmp_path) -> None:
    config_path = _write_config(tmp_path)
    config = load_config(config_path)
    server = build_home_mcp_server(config)

    note = server.create_markdown_note(root_id="projects", title="Not a Recipe", body="Project body.")
    with pytest.raises(HomeMCPError) as exc_info:
        server.get_recipe(recipe_id=note["file_id"])
    assert exc_info.value.error_code == "invalid_recipe_root"


def test_home_mcp_drafts_structured_recipe_cards(tmp_path) -> None:
    config_path = _write_config(tmp_path)
    config = load_config(config_path)
    server = build_home_mcp_server(config)

    draft = server.draft_recipe_card(
        source_text=(
            "Focaccia with Rosemary\n"
            "Ingredients:\n"
            "- 500g flour\n"
            "- 1 tbsp rosemary\n"
            "\n"
            "Instructions:\n"
            "1. Mix the dough.\n"
            "2. Bake until golden.\n"
            "\n"
            "Servings: 4\n"
            "Prep time: 20 minutes\n"
            "Cook time: 25 minutes\n"
        )
    )
    structured = draft["draft"]
    assert structured["title"] == "Focaccia with Rosemary"
    assert structured["ingredients"] == ["500g flour", "1 tbsp rosemary"]
    assert structured["steps"] == ["Mix the dough.", "Bake until golden."]
    assert structured["servings"] == "4"
    assert structured["prep_time"] == "20 minutes"
    assert structured["cook_time"] == "25 minutes"
    assert structured["confidence"] > 0.5
    assert "## At a glance" in structured["body"]
    assert "## Ingredients" in structured["body"]
    assert "## Method" in structured["body"]

    rpc = server.dispatch_jsonrpc(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": "draft_recipe_card",
                "arguments": {"source_text": "Toast\nIngredients:\n- bread\nSteps:\n- toast\n"},
            },
        }
    )
    assert rpc["result"]["structuredContent"]["draft"]["ingredients"] == ["bread"]


def test_home_mcp_reports_recipe_standard(tmp_path) -> None:
    config_path = _write_config(tmp_path)
    config = load_config(config_path)
    server = build_home_mcp_server(config)

    standard = server.recipe_standard()
    assert standard["status"] == "ok"
    assert standard["schema_version"] == 1
    assert "## Ingredients" in standard["template"]
    assert "Use the same structure every time before creating a new recipe card." in standard["checklist"]


def test_home_mcp_browses_standardized_recipes(tmp_path) -> None:
    config_path = _write_config(tmp_path)
    config = load_config(config_path)
    server = build_home_mcp_server(config)

    created = server.create_recipe_card(
        title="Browse Me",
        ingredients=["1 cup flour"],
        steps=["Mix."],
        summary="Browseable.",
        tags=["bread"],
    )
    browse = server.browse_recipes(query="Browse", tags=["bread"], limit=5)
    assert browse["status"] == "ok"
    assert browse["view"] == "standardized_recipe_browse"
    assert browse["standard"]["schema_version"] == 1
    assert browse["count"] == 1
    assert browse["results"][0]["file_id"] == created["file_id"]
    assert browse["results"][0]["schema_version"] == 1


def test_home_mcp_extracts_sectioned_recipe_cards(tmp_path) -> None:
    text = (
        "Miso-Butter Roast Bowl with Jammy Eggs\n"
        "\n"
        "## Ingredients\n"
        "### Rice\n"
        "- 1 cup jasmine rice\n"
        "- 1 1/4 cups water\n"
        "\n"
        "### Miso-butter sauce\n"
        "- 2 tbsp butter\n"
        "- 2 tbsp white or yellow miso\n"
        "\n"
        "## Step-by-step instructions\n"
        "### 1. Start the rice\n"
        "Use:\n"
        "- 1 cup jasmine rice\n"
        "- 1 1/4 cups water\n"
        "\n"
        "### 2. Heat the oven and prep the first roast\n"
        "Use:\n"
        "- 1 large sweet potato\n"
    )
    parsed = _extract_recipe_structure(text, title="Miso-Butter Roast Bowl with Jammy Eggs")
    assert parsed["title"] == "Miso-Butter Roast Bowl with Jammy Eggs"
    assert parsed["ingredients"] == ["1 cup jasmine rice", "1 1/4 cups water", "2 tbsp butter", "2 tbsp white or yellow miso"]
    assert parsed["steps"] == ["Start the rice", "Heat the oven and prep the first roast"]
    assert parsed["summary"] == "4 ingredients, 2 steps"


def test_home_mcp_normalizes_recipe_cards(tmp_path) -> None:
    config_path = _write_config(tmp_path)
    config = load_config(config_path)
    server = build_home_mcp_server(config)

    recipe = server.create_recipe_card(
        title="Normalize Me",
        ingredients=["1 cup flour"],
        steps=["Mix."],
        summary="Needs normalization.",
        tags=["bread"],
    )
    path = Path(recipe["path"])
    raw_before = path.read_text(encoding="utf-8")
    assert "## Method" in raw_before

    normalized = server.normalize_recipe_book(apply=True, limit=10)
    assert normalized["status"] == "ok"
    assert normalized["changed"] >= 1
    raw_after = path.read_text(encoding="utf-8")
    assert '"schema_version": 1' in raw_after
    assert "## At a glance" in raw_after
    assert "## Method" in raw_after
    assert "# Normalize Me" not in raw_after.split("## Notes", 1)[1]


def test_home_mcp_health_probe_reports_json_status(tmp_path) -> None:
    config_path = _write_config(tmp_path)
    config = load_config(config_path)
    server = build_home_mcp_server(config)
    httpd = serve_home_mcp(server, host="127.0.0.1", port=0)
    try:
        port = httpd.server_address[1]
        probe = _probe_home_mcp_health(f"http://127.0.0.1:{port}/health")
        assert probe["ok"] is True
        assert probe["status"] == "ok"
        assert probe["response"]["status"] == "ok"
    finally:
        httpd.shutdown()


def test_home_mcp_creates_structured_recipe_cards(tmp_path) -> None:
    config_path = _write_config(tmp_path)
    config = load_config(config_path)
    server = build_home_mcp_server(config)

    created = server.create_recipe_card(
        title="Focaccia with Rosemary",
        ingredients=["500g flour", "1 tbsp rosemary"],
        steps=["Mix the dough.", "Bake until golden."],
        servings="4",
        prep_time="20 minutes",
        cook_time="25 minutes",
        summary="A simple rosemary focaccia.",
        notes="Use a generous amount of olive oil.",
        tags=["bread", "rosemary"],
    )
    recipe_path = Path(created["path"])
    content = recipe_path.read_text(encoding="utf-8")
    assert "## Ingredients" in content
    assert "- 500g flour" in content
    assert "## Method" in content
    assert "1. Mix the dough." in content
    assert '"kind": "recipe_card"' in content

    search = server.search_recipes(query="golden", tags=["bread"], limit=5)
    assert search["count"] == 1
    result = search["results"][0]
    assert result["ingredients_count"] == 2
    assert result["steps_count"] == 2
    assert result["servings"] == "4"
    assert result["prep_time"] == "20 minutes"
    assert result["cook_time"] == "25 minutes"
    assert result["recipe_summary"] == "2 ingredients, 2 steps, servings 4"


def test_home_mcp_exposes_memory_tools_and_recipe_bridge(tmp_path) -> None:
    config_path = _write_config(tmp_path)
    config = load_config(config_path)
    seed = _seed_memory_database(config.paths["memory_dir"])
    server = build_home_mcp_server(config)

    rpc = server.dispatch_jsonrpc({"jsonrpc": "2.0", "id": 7, "method": "tools/list", "params": {}})
    tools = rpc["result"]["tools"]
    tool_names = {tool["name"] for tool in tools}
    assert {
        "memory_status",
        "memory_search",
        "memory_context",
        "memory_review",
        "memory_subjects",
        "bridge_recipe_note_to_memory",
        "submit_agent_feedback",
    } <= tool_names
    schemas = {tool["name"]: tool["inputSchema"]["properties"] for tool in tools}
    assert schemas["memory_context"]["depth"]["enum"] == ["far", "medium", "close", "full"]
    assert schemas["memory_context"]["retrieval_depth"]["enum"] == ["close", "broad"]
    assert schemas["memory_context"]["packet_detail"]["enum"] == ["summary", "standard", "complete"]
    assert schemas["memory_context"]["disclosure_tier"]["enum"] == ["far", "medium", "close", "full"]
    assert schemas["memory_search"]["depth"]["enum"] == ["far", "medium", "close", "full"]
    assert schemas["memory_context"]["debug_min_disclosure_tier"]["enum"] == ["far", "medium", "close", "full"]
    assert schemas["memory_search"]["debug_min_disclosure_tier"]["enum"] == ["far", "medium", "close", "full"]
    assert schemas["memory_review_subjects"]["quality_filter"]["enum"] == ["all", "user_only", "high_signal"]
    assert schemas["memory_review_subjects"]["review_status"]["enum"] == ["pending", "approved", "rejected", "merged"]
    assert schemas["memory_review"]["action"]["enum"] == ["list", "show", "approve", "reject", "promote"]
    assert "high" in schemas["memory_review"]["trust_level"]["enum"]
    assert "retrieval_noise" in schemas["submit_agent_feedback"]["category"]["enum"]
    assert schemas["submit_agent_feedback"]["severity"]["enum"] == ["low", "medium", "high", "critical"]

    status = server.memory_status(recent_limit=1)
    assert status["status"] == "ok"
    assert status["sqlite"]["exists"] is True

    subjects = server.memory_subjects(kind="subject", limit=5)
    assert subjects["count"] >= 1

    candidates = server.memory_candidates(review_status="pending", domain="cooking_baking", limit=5)
    assert candidates["count"] == 1

    review = server.memory_review(
        candidate_id=seed["candidate_id"],
        action="promote",
        record_type="research_note",
        title="Rosemary Focaccia",
        trust_level="high",
    )
    assert review["memory_record"]["source_kind"] == "chatgpt_candidate"
    assert review["candidate_memory"]["review_status"] == "merged"

    search = server.memory_search(query="rosemary", limit=5)
    assert search["count"] >= 1
    assert any(result["source_kind"] in {"curated_memory", "fts"} for result in search["results"])

    context = server.dispatch_jsonrpc(
        {
            "jsonrpc": "2.0",
            "id": "context",
            "method": "tools/call",
            "params": {"name": "memory_context", "arguments": {"query": "rosemary", "limit": 5, "depth": "medium"}},
        }
    )
    structured = context["result"]["structuredContent"]
    assert structured["status"] == "ok"
    assert structured["run_id"]
    assert structured["trace_id"] == structured["run_id"]
    assert structured["tool_name"] == "memory_context"
    assert structured["retrieval_event_id"].startswith("ret_")
    assert structured["context_packet_id"].startswith("ctx_")
    assert structured["context_packet"]["schema_version"] == 2
    assert structured["context_packet"]["task"]["depth"] == "medium"
    assert structured["context_controls"]["retrieval_depth"] == "close"
    assert structured["context_controls"]["packet_detail"] == "standard"
    assert structured["context_controls"]["disclosure_tier"] == "medium"
    assert structured["context_packet"]["provenance"]["retrieval_event_id"] == structured["retrieval_event_id"]
    assert structured["context_packet"]["relevant_preferences"] or structured["context_packet"]["current_state"]
    assert structured["context_items"]
    trace = server.dispatch_jsonrpc(
        {
            "jsonrpc": "2.0",
            "id": "trace",
            "method": "tools/call",
            "params": {"name": "memory_trace", "arguments": {"run_id": structured["run_id"]}},
        }
    )
    trace_content = trace["result"]["structuredContent"]
    assert trace_content["status"] == "ok"
    assert trace_content["run_id"] == structured["run_id"]
    assert trace_content["tool_name"] == "memory_trace"
    trace_stages = {event["stage"] for event in trace_content["trace_events"]}
    assert {
        "subject_resolution",
        "domain_detection",
        "retrieval_sources",
        "apply_filters",
        "rank_results",
        "apply_disclosure",
        "record_retrieval_event",
        "compile_context_packet",
    } <= trace_stages
    ranking_event = next(event for event in trace_content["trace_events"] if event["stage"] == "rank_results")
    assert ranking_event["details"]["ranking_profile"] == "hybrid_memory_v1"
    assert ranking_event["details"]["returned"] >= 1
    assert "snippet" not in json.dumps(ranking_event["details"]).lower()

    feedback_response = server.dispatch_jsonrpc(
        {
            "jsonrpc": "2.0",
            "id": "feedback",
            "method": "tools/call",
            "params": {
                "name": "submit_agent_feedback",
                "arguments": {
                    "run_id": structured["run_id"],
                    "trace_id": structured["trace_id"],
                    "component": "memory_context",
                    "category": "retrieval_noise",
                    "severity": "medium",
                    "observed_behavior": "Returned weak baking-adjacent history.",
                    "expected_behavior": "Prefer canonical cooking constraints and outcomes.",
                    "relevant_source_ids": ["chk_noise"],
                    "confidence": 0.9,
                },
            },
        }
    )
    feedback = feedback_response["result"]["structuredContent"]
    assert feedback["status"] == "ok"
    assert feedback["feedback"]["id"].startswith("afbk_")
    assert feedback["feedback"]["run_id"] == structured["run_id"]
    assert feedback["feedback"]["category"] == "retrieval_noise"
    assert feedback["feedback"]["build_sha"] is not None
    assert feedback["immutability"]["append_only"] is True
    assert feedback["immutability"]["mutates_memory_truth"] is False
    with sqlite3.connect(seed["db_path"]) as connection:
        row = connection.execute(
            "SELECT category, severity, relevant_source_ids_json FROM agent_feedback WHERE id = ?",
            (feedback["feedback"]["id"],),
        ).fetchone()
    assert row[0] == "retrieval_noise"
    assert row[1] == "medium"
    assert json.loads(row[2]) == ["chk_noise"]

    invalid_feedback = server.dispatch_jsonrpc(
        {
            "jsonrpc": "2.0",
            "id": "feedback-invalid",
            "method": "tools/call",
            "params": {
                "name": "submit_agent_feedback",
                "arguments": {
                    "run_id": structured["run_id"],
                    "component": "memory_context",
                    "category": "changes_code",
                    "severity": "medium",
                    "observed_behavior": "bad",
                    "expected_behavior": "good",
                    "confidence": 0.5,
                },
            },
        }
    )
    assert invalid_feedback["error"]["code"] == -32602
    assert invalid_feedback["error"]["data"]["error_code"] == "invalid_argument"

    invalid_depth = server.dispatch_jsonrpc(
        {
            "jsonrpc": "2.0",
            "id": "context-invalid-depth",
            "method": "tools/call",
            "params": {"name": "memory_context", "arguments": {"query": "rosemary", "depth": "deep"}},
        }
    )
    assert invalid_depth["error"]["code"] == -32602
    assert invalid_depth["error"]["data"]["stage"] == "tools/call"
    assert invalid_depth["error"]["data"]["error_code"] == "invalid_argument"
    assert "depth must be one of: far, medium, close, full" in invalid_depth["error"]["message"]

    recipe = server.create_recipe_card(
        title="Bridge Test Focaccia",
        body="Ingredients:\n- flour\n- rosemary\n\nSteps:\n- mix\n- bake\n",
        tags=["bread", "rosemary"],
    )
    bridged = server.bridge_recipe_note_to_memory(file_id=recipe["file_id"], subject="Cooking and Baking")
    assert bridged["memory_record"]["source_kind"] == "recipe_book"
    assert bridged["memory_record"]["source_ref"] == recipe["file_id"]


def test_home_mcp_subject_review_defaults_to_high_signal_candidates(tmp_path) -> None:
    config_path = _write_config(tmp_path)
    config = load_config(config_path)
    seed = _seed_memory_database(config.paths["memory_dir"])

    with sqlite3.connect(seed["db_path"]) as connection:
        for candidate_id, source_role, memory_type, confidence, assistant_suggestion, domain, domains, content in [
            ("cand_contextless", "user", "procedure", 0.9, 0, "cooking_baking", ["cooking_baking"], "do it"),
            ("cand_question", "user", "procedure", 0.91, 0, "cooking_baking", ["cooking_baking"], "Is this enough filling for the cake?"),
            ("cand_image", "user", "procedure", 0.91, 0, "cooking_baking", ["cooking_baking"], "Does this look proofed enough in the image?"),
            ("cand_pepper", "user", "procedure", 0.91, 0, "cooking_baking", ["cooking_baking"], "Should I cut the peppers into strips or dice?"),
            (
                "cand_duplicate",
                "user",
                "procedure",
                0.89,
                0,
                "cooking_baking",
                ["cooking_baking"],
                "Please draft a rosemary focaccia recipe with notes.",
            ),
            (
                "cand_incidental",
                "user",
                "preference",
                0.92,
                0,
                "career_work",
                ["career_work"],
                "I want a more stable career path and sometimes baking is part of the life rhythm I imagine.",
            ),
            (
                "cand_assistant",
                "assistant",
                "assistant_suggestion",
                0.35,
                1,
                "cooking_baking",
                ["cooking_baking"],
                "You could use this as a recipe memory.",
            ),
        ]:
            connection.execute(
                """
                INSERT INTO candidate_memories (
                    id, import_id, conversation_id, message_id, chunk_id, source_kind, source_ref,
                    source_role, memory_type, reason_type, domain_primary, domains_json, confidence,
                    valid_from, valid_to, last_confirmed_at, review_status, review_notes, origin,
                    assistant_suggestion, source_links_json, content, metadata_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
                """,
                (
                    candidate_id,
                    seed["import_id"],
                    seed["conversation_id"],
                    seed["message_id"],
                    seed["chunk_id"],
                    "chatgpt_export",
                    seed["chunk_id"],
                    source_role,
                    memory_type,
                    "unknown",
                    domain,
                    json.dumps(domains, sort_keys=True),
                    confidence,
                    "2026-07-01T00:00:00+00:00",
                    None,
                    None,
                    "pending",
                    None,
                    "chatgpt_export",
                    assistant_suggestion,
                    json.dumps(
                        {
                            "conversation_id": seed["conversation_id"],
                            "message_id": seed["message_id"],
                            "chunk_id": seed["chunk_id"],
                            "source_kind": "chatgpt_export",
                            "source_role": source_role,
                        },
                        sort_keys=True,
                    ),
                    content,
                    json.dumps({"message_turn_index": 1, "chunk_index": 1}, sort_keys=True),
                ),
            )

    server = build_home_mcp_server(config)
    filtered = server.memory_review_subjects(subject="Cooking and Baking", candidate_limit=10)
    assert filtered["filters"]["quality_filter"] == "high_signal"
    assert filtered["filters"]["effective_quality_filter"] == "high_signal"
    assert filtered["subject_count"] == filtered["count"]
    assert filtered["candidate_count"] == len(filtered["candidate_memories"])
    assert [item["id"] for item in filtered["candidate_memories"]] == [seed["candidate_id"]]

    full = server.memory_review_subjects(subject="Cooking and Baking", quality_filter="all", candidate_limit=10)
    assert full["filters"]["quality_filter"] == "all"
    assert {item["id"] for item in full["candidate_memories"]} == {
        seed["candidate_id"],
        "cand_contextless",
        "cand_question",
        "cand_image",
        "cand_pepper",
        "cand_duplicate",
        "cand_incidental",
        "cand_assistant",
    }


def test_memory_search_renderer_handles_curated_results() -> None:
    rendered = _render_memory_search(
        {
            "run_id": "run_test",
            "count": 1,
            "results": [
                {
                    "rank": 1,
                    "title": "Smoke Test Focaccia",
                    "role": None,
                    "source_role": None,
                    "record_type": "research_note",
                    "chunk_id": "mem_123",
                    "score": 1.25,
                    "snippet": "Rosemary focaccia.",
                }
            ],
        }
    )
    assert "Smoke Test Focaccia" in rendered
    assert "[research_note]" in rendered


def test_home_mcp_searches_reads_and_dispatches_jsonrpc(tmp_path) -> None:
    config_path = _write_config(tmp_path)
    config = load_config(config_path)
    server = build_home_mcp_server(config)

    created = server.create_markdown_note(
        root_id="recipe_book",
        title="Focaccia Notes",
        body="Rosemary focaccia with olive oil and sea salt.",
    )
    search = server.search_files(query="focaccia", root_id="recipe_book", limit=5)
    assert search["count"] == 1
    assert search["results"][0]["file_id"] == created["file_id"]

    read = server.read_file(file_id=created["file_id"])
    assert "Rosemary focaccia" in read["content"]

    rpc = server.dispatch_jsonrpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    tools = rpc["result"]["tools"]
    assert any(tool["name"] == "create_recipe" for tool in tools)

    rpc_call = server.dispatch_jsonrpc(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "search_files", "arguments": {"query": "focaccia", "root_id": "recipe_book"}},
        }
    )
    assert rpc_call["result"]["structuredContent"]["count"] == 1


def test_home_mcp_http_health_and_rpc_round_trip(tmp_path) -> None:
    config_path = _write_config(tmp_path)
    config = load_config(config_path)
    server = build_home_mcp_server(config)
    httpd = serve_home_mcp(server, host="127.0.0.1", port=0)
    try:
        port = httpd.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/") as response:
            payload = json.loads(response.read().decode("utf-8"))
        assert payload["status"] == "ok"
        assert payload["endpoint"] == "/mcp"

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health") as response:
            payload = json.loads(response.read().decode("utf-8"))
        assert payload["status"] == "ok"

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/mcp") as response:
            payload = json.loads(response.read().decode("utf-8"))
        assert payload["status"] == "ok"

        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/mcp",
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "roots/list", "params": {}}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request) as response:
            payload = json.loads(response.read().decode("utf-8"))
        assert payload["result"]["roots"]
        initialize = server.dispatch_jsonrpc({"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {}})
        assert initialize["result"]["authentication"]["mode"] == "none"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_home_mcp_smoke_test_exercises_http_jsonrpc(tmp_path) -> None:
    config_path = _write_config(tmp_path)
    config = load_config(config_path)
    server = build_home_mcp_server(config)
    httpd = serve_home_mcp(server, host="127.0.0.1", port=0)
    try:
        port = httpd.server_address[1]
        payload = _run_home_mcp_smoke_test(url=f"http://127.0.0.1:{port}/mcp")
        assert payload["ok"] is True
        assert payload["tool_count"] > 0
        assert payload["required_tools_present"]["list_allowed_roots"] is True
        assert payload["required_tools_present"]["recipe_standard"] is True
        assert payload["required_tools_present"]["search_recipes"] is True
        assert payload["required_tools_present"]["memory_status"] is True
        assert payload["required_tools_present"]["memory_context"] is True
        assert payload["write_result"] is None
        assert payload["memory_status_ok"] is True
        assert {stage["name"] for stage in payload["stages"]} >= {
            "health",
            "initialize",
            "tools/list",
            "tool:list_allowed_roots",
            "tool:recipe_standard",
            "tool:search_recipes",
            "tool:memory_status",
        }
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_home_mcp_smoke_test_write_probe_is_explicit(tmp_path) -> None:
    config_path = _write_config(tmp_path)
    config = load_config(config_path)
    server = build_home_mcp_server(config)
    httpd = serve_home_mcp(server, host="127.0.0.1", port=0)
    try:
        port = httpd.server_address[1]
        payload = _run_home_mcp_smoke_test(url=f"http://127.0.0.1:{port}/mcp", write_probe=True)
        assert payload["ok"] is True
        assert payload["write_probe"] is True
        assert payload["write_result"] is not None
        created = sorted((tmp_path / "data" / "home_mcp" / "projects" / "_smoke_tests").glob("*.md"))
        assert len(created) == 1
        assert "Automated write probe" in created[0].read_text(encoding="utf-8")
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_home_mcp_serves_oauth_metadata_endpoints(tmp_path) -> None:
    config_path = _write_config(tmp_path)
    config = load_config(config_path)
    server = build_home_mcp_server(config)
    httpd = serve_home_mcp(server, host="127.0.0.1", port=0)
    try:
        port = httpd.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/.well-known/oauth-protected-resource/mcp") as response:
            payload = json.loads(response.read().decode("utf-8"))
        assert payload["resource"].startswith("http://127.0.0.1:")
        assert payload["authorization_servers"] == []

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/.well-known/oauth-authorization-server") as response:
            payload = json.loads(response.read().decode("utf-8"))
        assert payload["issuer"] == "https://openai.invalid/no-auth"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_home_mcp_bearer_auth_blocks_and_allows(tmp_path) -> None:
    config_path = _write_config(tmp_path, extra_home_mcp={"auth_mode": "bearer", "auth_token": "secret"})
    config = load_config(config_path)
    server = build_home_mcp_server(config)
    httpd = serve_home_mcp(server, host="127.0.0.1", port=0)
    try:
        port = httpd.server_address[1]
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/mcp",
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "roots/list", "params": {}}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(request)
        assert exc_info.value.code == 401

        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/mcp",
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "roots/list", "params": {}}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer secret"},
        )
        with urllib.request.urlopen(request) as response:
            payload = json.loads(response.read().decode("utf-8"))
        assert payload["result"]["roots"]
    finally:
        httpd.shutdown()
        httpd.server_close()
