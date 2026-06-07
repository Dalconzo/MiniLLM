from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..logging.run_logger import RunContext, RunLogger


CHATGPT_MEMORY_DB = "chatgpt_memory.sqlite3"
TRACE_FILE = "trace.jsonl"


class MemoryObservationError(RuntimeError):
    def __init__(self, message: str, *, stage: str, error_code: str, source_ref: str | None = None) -> None:
        super().__init__(message)
        self.stage = stage
        self.error_code = error_code
        self.source_ref = source_ref

    def to_dict(self) -> dict[str, str | None]:
        return {
            "message": str(self),
            "stage": self.stage,
            "error_code": self.error_code,
            "source_ref": self.source_ref,
        }


@dataclass
class MemoryTraceWriter:
    logger: RunLogger
    run: RunContext
    command: str
    argv: list[str]
    config_path: Path
    sqlite_path: Path
    input_paths: list[str] = field(default_factory=list)
    output_paths: list[str] = field(default_factory=list)
    schema_version: int | None = None

    def __post_init__(self) -> None:
        self.trace_path = self.run.run_dir / TRACE_FILE
        self.trace_path.touch(exist_ok=True)

    def trace(
        self,
        stage: str,
        message: str,
        *,
        level: str = "info",
        source_kind: str | None = None,
        source_ref: str | None = None,
        record_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        event = {
            "id": f"trc_{uuid4().hex}",
            "timestamp": utc_now(),
            "stage": stage,
            "level": level,
            "message": message,
            "source_kind": source_kind,
            "source_ref": source_ref,
            "record_id": record_id,
            "details": details or {},
        }
        with self.trace_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")
        return event

    def write_json(self, name: str, payload: dict[str, Any]) -> Path:
        path = self.run.run_dir / name
        self.logger.write_artifact(self.run, name, json.dumps(payload, indent=2, sort_keys=True))
        if str(path) not in self.output_paths:
            self.output_paths.append(str(path))
        return path

    def finish(self, *, status: str, result: dict[str, Any], error: dict[str, Any] | None = None) -> None:
        command_payload = {
            "run_id": self.run.run_id,
            "command": self.command,
            "argv": self.argv,
            "started_at": self.run.started_at,
            "finished_at": utc_now(),
            "status": status,
            "config_path": str(self.config_path),
            "artifact_dir": str(self.run.run_dir),
            "input_paths": self.input_paths,
            "output_paths": self.output_paths,
            "sqlite_path": str(self.sqlite_path),
            "schema_version": self.schema_version,
            "error": error,
        }
        self.logger.write_artifact(self.run, "command.json", json.dumps(command_payload, indent=2, sort_keys=True))
        self.logger.finish(self.run, status=status, result=result)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def memory_db_path(memory_dir: Path) -> Path:
    return memory_dir / CHATGPT_MEMORY_DB


def validate_memory_state(*, data_dir: Path, memory_dir: Path) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    exports_dir = data_dir / "chatgpt_exports"
    raw_dir = exports_dir / "raw"
    parsed_dir = exports_dir / "parsed"
    sqlite_path = memory_db_path(memory_dir)

    _check_path(checks, "chatgpt_exports_dir", exports_dir, required=False)
    _check_path(checks, "raw_exports_dir", raw_dir, required=False)
    _check_path(checks, "parsed_exports_dir", parsed_dir, required=False)
    _check_path(checks, "memory_dir", memory_dir, required=True)

    if sqlite_path.exists():
        checks.extend(_validate_sqlite(sqlite_path))
    else:
        checks.append(
            {
                "name": "sqlite_database",
                "status": "warn",
                "message": "ChatGPT memory database does not exist yet.",
                "path": str(sqlite_path),
            }
        )

    if parsed_dir.exists():
        checks.extend(_validate_jsonl_files(parsed_dir))

    error_count = sum(1 for check in checks if check["status"] == "error")
    warning_count = sum(1 for check in checks if check["status"] == "warn")
    status = "error" if error_count else "warn" if warning_count else "ok"
    return {
        "status": status,
        "checked_at": utc_now(),
        "data_dir": str(data_dir),
        "memory_dir": str(memory_dir),
        "sqlite_path": str(sqlite_path),
        "summary": {
            "checks": len(checks),
            "errors": error_count,
            "warnings": warning_count,
        },
        "checks": checks,
    }


def dry_run_chatgpt_ingest(input_path: Path) -> dict[str, Any]:
    if not input_path.exists():
        raise MemoryObservationError(
            f"input path does not exist: {input_path}",
            stage="discover_input",
            error_code="input_not_found",
            source_ref=str(input_path),
        )

    conversation_files = _find_conversation_files(input_path)
    files: list[dict[str, Any]] = []
    total_conversations = 0
    total_errors = 0

    for path in conversation_files:
        item: dict[str, Any] = {
            "path": str(path),
            "recognized": True,
            "conversation_count": 0,
            "error": None,
        }
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, list):
                item["conversation_count"] = len(payload)
            elif isinstance(payload, dict):
                item["conversation_count"] = 1
            else:
                raise ValueError(f"unsupported top-level JSON type: {type(payload).__name__}")
            total_conversations += int(item["conversation_count"])
        except (json.JSONDecodeError, UnicodeDecodeError, OSError, ValueError) as exc:
            item["error"] = {
                "stage": "parse_export",
                "error_code": "invalid_json" if isinstance(exc, json.JSONDecodeError) else "parse_error",
                "message": str(exc),
            }
            total_errors += 1
        files.append(item)

    if not conversation_files:
        files.append(
            {
                "path": str(input_path),
                "recognized": False,
                "conversation_count": 0,
                "error": {
                    "stage": "discover_input",
                    "error_code": "unsupported_export_shape",
                    "message": "No conversations.json files found.",
                },
            }
        )
        total_errors += 1

    return {
        "status": "error" if total_errors else "ok",
        "dry_run": True,
        "input_path": str(input_path),
        "conversation_files": files,
        "summary": {
            "conversation_files": len(conversation_files),
            "conversations": total_conversations,
            "errors": total_errors,
        },
        "planned_writes": [
            "data/chatgpt_exports/parsed/<import_id>/conversations.jsonl",
            "data/chatgpt_exports/parsed/<import_id>/messages.jsonl",
            "data/chatgpt_exports/parsed/<import_id>/chunks.jsonl",
            "data/chatgpt_exports/parsed/<import_id>/import_report.json",
            "data/memory/chatgpt_memory.sqlite3",
        ],
    }


def build_unimplemented_search_explain(query: str) -> dict[str, Any]:
    return {
        "status": "not_implemented",
        "query": query,
        "ranking_profile": "fts_first_v0",
        "candidate_counts": {
            "fts": 0,
            "vector": 0,
            "curated": 0,
            "after_filters": 0,
        },
        "filters_applied": [],
        "results": [],
        "error": {
            "stage": "retrieve_candidates",
            "error_code": "memory_search_not_implemented",
            "message": "ChatGPT memory search is tracked by lagent-103 and has not been implemented yet.",
        },
    }


def read_memory_trace(logs_dir: Path, run_id: str) -> dict[str, Any]:
    run_dir = logs_dir / run_id
    if not run_dir.exists() or not run_dir.is_dir():
        raise FileNotFoundError(f"run_id not found: {run_id}")

    command_path = run_dir / "command.json"
    command = _read_json(command_path) if command_path.exists() else None
    events = _read_jsonl(run_dir / TRACE_FILE)
    artifacts = sorted(path.name for path in run_dir.iterdir() if path.is_file())
    return {
        "run_id": run_id,
        "run_dir": str(run_dir),
        "command": command,
        "trace_events": events,
        "artifacts": artifacts,
    }


def render_memory_trace(trace: dict[str, Any]) -> str:
    lines = [
        f"Run: {trace['run_id']}",
        f"Directory: {trace['run_dir']}",
    ]
    command = trace.get("command")
    if command:
        lines.extend(
            [
                f"Command: {command.get('command')}",
                f"Status: {command.get('status')}",
            ]
        )
        error = command.get("error")
        if error:
            lines.append(f"Error: {error.get('stage')} / {error.get('error_code')} - {error.get('message')}")

    lines.append("Artifacts:")
    for artifact in trace["artifacts"]:
        lines.append(f"- {artifact}")

    lines.append("Trace:")
    if not trace["trace_events"]:
        lines.append("- No trace events recorded.")
    for event in trace["trace_events"]:
        source = f" [{event.get('source_ref')}]" if event.get("source_ref") else ""
        lines.append(f"- {event.get('timestamp')} {event.get('level')} {event.get('stage')}{source}: {event.get('message')}")
    return "\n".join(lines)


def _find_conversation_files(input_path: Path) -> list[Path]:
    if input_path.is_file() and input_path.name == "conversations.json":
        return [input_path]
    if input_path.is_dir():
        direct = input_path / "conversations.json"
        if direct.exists():
            return [direct]
        return sorted(input_path.rglob("conversations.json"))
    return []


def _check_path(checks: list[dict[str, Any]], name: str, path: Path, *, required: bool) -> None:
    if path.exists():
        checks.append({"name": name, "status": "ok", "message": "Path exists.", "path": str(path)})
    elif required:
        checks.append({"name": name, "status": "error", "message": "Required path is missing.", "path": str(path)})
    else:
        checks.append({"name": name, "status": "warn", "message": "Path does not exist yet.", "path": str(path)})


def _validate_sqlite(sqlite_path: Path) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    try:
        with sqlite3.connect(sqlite_path) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual table')"
                ).fetchall()
            }
            required = {"imports", "conversations", "messages", "message_chunks"}
            missing = sorted(required - tables)
            checks.append(
                {
                    "name": "sqlite_schema",
                    "status": "error" if missing else "ok",
                    "message": "Missing required tables." if missing else "Required tables exist.",
                    "path": str(sqlite_path),
                    "details": {"missing_tables": missing, "tables": sorted(tables)},
                }
            )
    except sqlite3.Error as exc:
        checks.append(
            {
                "name": "sqlite_open",
                "status": "error",
                "message": str(exc),
                "path": str(sqlite_path),
            }
        )
    return checks


def _validate_jsonl_files(parsed_dir: Path) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for path in sorted(parsed_dir.rglob("*.jsonl")):
        errors = 0
        lines = 0
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                lines += 1
                try:
                    json.loads(line)
                except json.JSONDecodeError:
                    errors += 1
                    checks.append(
                        {
                            "name": "jsonl_valid",
                            "status": "error",
                            "message": f"Invalid JSON on line {line_number}.",
                            "path": str(path),
                        }
                    )
        if errors == 0:
            checks.append(
                {
                    "name": "jsonl_valid",
                    "status": "ok",
                    "message": f"Validated {lines} JSONL records.",
                    "path": str(path),
                }
            )
    return checks


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                events.append(json.loads(line))
    return events
