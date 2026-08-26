from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..security.paths import safe_append_text, safe_write_text
from ..tools.file_tools import redact_text


_DETAILED_SUFFIXES = {".diff", ".html", ".log", ".md", ".patch", ".txt"}
DIAGNOSTIC_SCHEMA_VERSION = "minillm_diagnostics_v1"
_PRIVATE_CONTENT_FIELDS = {
    "access_token",
    "authorization",
    "body",
    "candidate_text",
    "claim",
    "content",
    "credentials",
    "diff",
    "diff_text",
    "downstream_effect",
    "entry",
    "expected_behavior",
    "file_content",
    "headers",
    "full_text",
    "ingredients",
    "log_text",
    "message",
    "notes",
    "observed_behavior",
    "outcome",
    "password",
    "private_key",
    "prompt",
    "question",
    "query",
    "raw_response",
    "refresh_token",
    "request",
    "response",
    "retrieved_context",
    "review_notes",
    "secret",
    "snippet",
    "spec_text",
    "steps",
    "suggested_direction",
    "summary",
    "text",
    "title",
    "token",
}

_STRUCTURAL_CONTAINER_FIELDS = {
    "authentication",
    "counts",
    "details",
    "diagnostics",
    "error",
    "filters",
    "governance",
    "immutability",
    "params",
    "payload",
    "ranking",
    "score_components",
}

_SAFE_STRING_FIELDS = {
    "action",
    "artifact_dir",
    "auth_mode",
    "category",
    "command",
    "component",
    "config_path",
    "disclosure_tier",
    "domain",
    "effective_depth",
    "error_code",
    "event",
    "finished_at",
    "id",
    "level",
    "method",
    "mode",
    "model",
    "operation",
    "packet_detail",
    "primary_domain",
    "profile",
    "protocol_version",
    "provider",
    "ranking_profile",
    "record_id",
    "request_id",
    "requested_depth",
    "retrieval_depth",
    "run_id",
    "schema_version",
    "severity",
    "source_id",
    "source_kind",
    "source_type",
    "stage",
    "started_at",
    "status",
    "timestamp",
    "tool",
    "tool_name",
    "trace_id",
    "version",
}

_SAFE_STRING_LIST_FIELDS = {
    "fields_exposed",
    "input_paths",
    "output_paths",
    "source_ids",
    "stages",
}


@dataclass(frozen=True)
class RunContext:
    run_id: str
    run_dir: Path
    started_at: str


class RunLogger:
    def __init__(self, logs_dir: Path, *, allow_detailed_content: bool = False) -> None:
        if logs_dir.is_symlink():
            raise ValueError("logs directory must not be a symlink")
        logs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.logs_dir = logs_dir.resolve(strict=True)
        if not self.logs_dir.is_dir():
            raise ValueError("logs path must be a directory")
        self.allow_detailed_content = allow_detailed_content
        os.chmod(self.logs_dir, 0o700)
        self.events_file = self.logs_dir / "events.jsonl"
        safe_append_text(self.logs_dir, self.events_file.name, "", mode=0o600)

    def start(self, command: str, payload: dict[str, Any]) -> RunContext:
        started_at = datetime.now(timezone.utc).isoformat()
        run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"
        run_dir = self.logs_dir / run_id
        run_dir.mkdir(mode=0o700)
        os.chmod(run_dir, 0o700)
        self._append(
            {
                "event": "start",
                "run_id": run_id,
                "command": command,
                "payload": payload,
                "started_at": started_at,
            }
        )
        return RunContext(run_id=run_id, run_dir=run_dir, started_at=started_at)

    def finish(self, run: RunContext, *, status: str, result: dict[str, Any]) -> None:
        self._append(
            {
                "event": "finish",
                "run_id": run.run_id,
                "status": status,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "result": result,
            }
        )

    def write_artifact(self, run: RunContext, name: str, content: str) -> None:
        sanitized = sanitize_artifact_content(
            name,
            content,
            allow_detailed_content=self.allow_detailed_content,
        )
        artifact_path = safe_write_text(run.run_dir, name, sanitized, create_parents=False, mode=0o600)
        os.chmod(artifact_path, 0o600)

    def _append(self, payload: dict[str, Any]) -> None:
        persisted_payload = {"schema_version": DIAGNOSTIC_SCHEMA_VERSION, **payload}
        safe_append_text(
            self.logs_dir,
            self.events_file.name,
            json.dumps(sanitize_payload_for_persistence(persisted_payload)) + "\n",
        )


def sanitize_payload_for_persistence(value: Any, *, allow_detailed_content: bool = False) -> Any:
    """Redact secrets and persist only structural metadata by default."""
    return _sanitize_value(value, allow_detailed_content=allow_detailed_content, field_name=None)


def _sanitize_value(value: Any, *, allow_detailed_content: bool, field_name: str | None) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = key.lower()
            if normalized_key in _PRIVATE_CONTENT_FIELDS and not allow_detailed_content:
                redacted[key] = _content_metadata(item)
            elif not allow_detailed_content and not _is_structural_field(normalized_key, item):
                redacted[key] = _content_metadata(item)
            else:
                redacted[key] = _sanitize_value(
                    item,
                    allow_detailed_content=allow_detailed_content,
                    field_name=normalized_key,
                )
        return redacted
    if isinstance(value, list):
        if not allow_detailed_content and field_name not in _SAFE_STRING_LIST_FIELDS:
            if any(isinstance(item, str) for item in value):
                return _content_metadata(value)
        return [
            _sanitize_value(item, allow_detailed_content=allow_detailed_content, field_name=field_name)
            for item in value
        ]
    if isinstance(value, str):
        if not allow_detailed_content and not _is_safe_string_field(field_name):
            return _content_metadata(value)
        return redact_text(value)
    return value


def _is_structural_field(field_name: str, value: Any) -> bool:
    if value is None or isinstance(value, (bool, int, float)):
        return True
    if field_name in _STRUCTURAL_CONTAINER_FIELDS:
        return True
    if isinstance(value, str):
        return _is_safe_string_field(field_name)
    if isinstance(value, list):
        return field_name in _SAFE_STRING_LIST_FIELDS or not any(isinstance(item, str) for item in value)
    return False


def _is_safe_string_field(field_name: str | None) -> bool:
    if not field_name:
        return False
    return (
        field_name in _SAFE_STRING_FIELDS
        or field_name.endswith("_id")
        or field_name.endswith("_at")
        or field_name.endswith("_path")
        or field_name.endswith("_dir")
        or field_name.endswith("_profile")
    )


def sanitize_artifact_content(name: str, content: str, *, allow_detailed_content: bool = False) -> str:
    suffix = Path(name).suffix.lower()
    if suffix in _DETAILED_SUFFIXES and not allow_detailed_content:
        return json.dumps(_content_metadata(content), indent=2, sort_keys=True) + "\n"
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return redact_text(content)
    sanitized = sanitize_payload_for_persistence(payload, allow_detailed_content=allow_detailed_content)
    return json.dumps(sanitized, indent=2, sort_keys=True) + "\n"


def _content_metadata(value: Any) -> dict[str, object]:
    serialized = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)
    redacted = redact_text(serialized)
    return {
        "detailed_content_omitted": True,
        "character_count": len(serialized),
        "redaction_applied": redacted != serialized,
    }
