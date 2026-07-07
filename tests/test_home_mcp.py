from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from local_agent_lab.config import load_config
from local_agent_lab.home_mcp import HomeMCPError, build_home_mcp_server, serve_home_mcp


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
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health") as response:
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
