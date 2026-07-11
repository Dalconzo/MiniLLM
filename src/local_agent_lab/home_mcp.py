from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

import yaml

from .config import AppConfig
from .logging.run_logger import RunContext, RunLogger
from .memory.analysis import analyze_memory_corpus
from .memory.audit import init_audit_schema
from .memory.candidates import (
    CandidateMemory,
    get_candidate_memory,
    list_candidate_memories,
    list_candidate_memories_for_subject,
    list_candidate_subjects,
    update_candidate_review,
    init_candidate_memory_schema,
)
from .memory.chatgpt_ingest import init_chatgpt_memory_schema
from .memory.curated import MemoryRecord, create_memory_record, init_curated_memory_schema, list_memory_records
from .memory.feedback import init_feedback_schema, list_open_loops
from .memory.observability import MemoryObservationError, memory_db_path, read_memory_trace, summarize_memory_status
from .memory.search import search_chatgpt_memory
from .memory.subjects import init_subject_schema, list_subjects, normalize_subject_slug, upsert_subject
from .tools.file_tools import redact_text


DEFAULT_HOME_MCP_BASE_DIR = "data/home_mcp"
DEFAULT_HOME_MCP_ROOTS = {
    "recipe_book": ("recipes", True, "Recipe notes and attempts"),
    "household": ("household", True, "Household notes and checklists"),
    "projects": ("projects", True, "Project notes and planning"),
    "inbox": ("inbox", True, "Quick capture and scratch notes"),
    "archive": ("archive", False, "Read-only archive space"),
}
RECIPE_CARD_SCHEMA_VERSION = 1
TEXT_SUFFIXES = {".md", ".txt", ".json", ".yaml", ".yml", ".csv", ".log"}
MAX_FILE_BYTES = 128_000
MAX_SEARCH_FILES = 250
HOME_MCP_AUTH_MODES = {"none", "bearer", "oauth", "mixed"}
HOME_MCP_OAUTH_RESOURCE_PATHS = {"/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"}
HOME_MCP_OAUTH_AUTH_SERVER_PATHS = {"/.well-known/oauth-authorization-server", "/.well-known/openid-configuration"}


def _default_curated_record_type(memory_type: str) -> str:
    mapping = {
        "decision": "decision",
        "preference": "preference",
        "procedure": "workflow",
        "workaround": "lesson",
        "failure": "lesson",
        "open_loop": "open_loop",
        "episodic": "research_note",
        "skill": "workflow",
        "analogy": "lesson",
        "project": "project",
        "semantic_fact": "research_note",
        "source_note": "research_note",
        "constraint": "lesson",
        "risk": "research_note",
        "relationship": "contact_note",
        "health_note": "research_note",
        "financial_note": "research_note",
    }
    return mapping.get(memory_type, "research_note")


def _candidate_title(candidate: CandidateMemory) -> str:
    text = candidate.content.strip()
    if len(text) <= 80:
        return text
    return text[:77].rstrip() + "..."


def _recipe_standard() -> dict[str, Any]:
    template = (
        "# Title\n\n"
        "One short summary paragraph.\n\n"
        "## At a glance\n"
        "- Yield: ...\n"
        "- Prep time: ...\n"
        "- Cook time: ...\n"
        "- Total time: ...\n"
        "- Tags: ...\n\n"
        "## Ingredients\n"
        "- Ingredient 1\n"
        "- Ingredient 2\n\n"
        "## Method\n"
        "1. Step one.\n"
        "2. Step two.\n\n"
        "## Notes\n"
        "- substitutions, warnings, or brief context\n\n"
        "## Source\n"
        "- file id, query, or other provenance\n"
    )
    return {
        "schema_version": RECIPE_CARD_SCHEMA_VERSION,
        "title": "Recipe Card Standard",
        "purpose": "Minimal, AI-friendly recipe notes that are easy to read, create, and normalize.",
        "checklist": [
            "One title and one short summary paragraph.",
            "At a glance section with yield and timing if known.",
            "One ingredient per bullet; no nested ingredient subheadings.",
            "One method step per numbered item; no nested step headings.",
            "Notes stay brief and only capture exceptions or substitutions.",
            "Source is explicit so the recipe can be traced later.",
            "Use the same structure every time before creating a new recipe card.",
        ],
        "sections": [
            "Title",
            "At a glance",
            "Ingredients",
            "Method",
            "Notes",
            "Source",
        ],
        "template": template,
    }


def _extract_recipe_summary(text: str) -> str | None:
    lines = text.splitlines()
    summary_lines: list[str] = []
    started = False
    for line in lines:
        stripped = line.strip()
        if not started:
            if not stripped:
                continue
            if stripped.startswith("#"):
                started = True
                continue
            started = True
            summary_lines.append(stripped)
            continue
        if stripped.startswith("#"):
            break
        if stripped:
            summary_lines.append(stripped)
        elif summary_lines:
            break
    summary = " ".join(summary_lines).strip()
    return summary or None


def _strip_recipe_intro_block(text: str) -> str:
    lines = text.splitlines()
    index = 0
    while index < len(lines) and not lines[index].strip():
        index += 1
    if index < len(lines) and lines[index].strip().startswith("#"):
        index += 1
        while index < len(lines) and not lines[index].strip():
            index += 1
        while index < len(lines) and lines[index].strip() and not lines[index].strip().startswith("#"):
            index += 1
        while index < len(lines) and not lines[index].strip():
            index += 1
    return "\n".join(lines[index:])


def _promote_candidate_memory(
    connection: sqlite3.Connection,
    candidate: CandidateMemory,
    *,
    record_type: str | None,
    title: str | None,
    trust_level: str,
    note: str | None,
) -> MemoryRecord:
    curated_record_type = record_type or _default_curated_record_type(candidate.memory_type)
    provenance = {
        "source": candidate.source_links,
        "candidate_memory_id": candidate.id,
        "candidate_review_status": candidate.review_status,
        "candidate_reason_type": candidate.reason_type,
        "candidate_domains": candidate.domains,
        "assistant_suggestion": candidate.assistant_suggestion,
    }
    record_title = title or _candidate_title(candidate)
    return create_memory_record(
        connection,
        record_type=curated_record_type,
        title=record_title,
        body=candidate.content,
        trust_level=trust_level,
        source_kind="chatgpt_candidate",
        source_ref=candidate.id,
        provenance=provenance,
        metadata={
            "candidate_memory_id": candidate.id,
            "candidate_review_status": candidate.review_status,
            "review_note": note,
            "source_role": candidate.source_links.get("source_role"),
        },
        created_by="user",
    )


def _normalize_recipe_note_text(
    *,
    title: str,
    metadata: dict[str, Any],
    body: str,
    source_hint: str | None = None,
) -> tuple[str, dict[str, Any]]:
    parsed = _extract_recipe_structure(_strip_recipe_intro_block(body), title=title)
    recipe_card = metadata.get("recipe_card") if isinstance(metadata.get("recipe_card"), dict) else {}
    summary = str(metadata.get("summary") or recipe_card.get("summary") or "").strip() or _extract_recipe_summary(body)
    tags = metadata.get("tags") if isinstance(metadata.get("tags"), list) else []
    source_bits: list[str] = []
    if isinstance(recipe_card, dict):
        for key in ("source_file_id", "source_query"):
            value = recipe_card.get(key)
            if value:
                source_bits.append(f"{key.replace('_', ' ').title()}: {value}")
    for key in ("source", "created_from_chat", "created_for"):
        value = metadata.get(key)
        if value:
            source_bits.append(f"{key.replace('_', ' ').title()}: {value}")
    if source_hint:
        source_bits.append(source_hint)
    source = "\n".join(dict.fromkeys(source_bits)) or None
    body_text = _render_recipe_card_body(
        title,
        summary=summary,
        ingredients=parsed["ingredients"],
        steps=parsed["steps"],
        servings=parsed["servings"] or str(recipe_card.get("servings") or metadata.get("servings") or "").strip() or None,
        prep_time=parsed["prep_time"] or str(recipe_card.get("prep_time") or metadata.get("prep_time") or "").strip() or None,
        cook_time=parsed["cook_time"] or str(recipe_card.get("cook_time") or metadata.get("cook_time") or "").strip() or None,
        total_time=parsed["total_time"] or str(recipe_card.get("total_time") or metadata.get("total_time") or "").strip() or None,
        notes=parsed["notes"] or str(metadata.get("notes") or recipe_card.get("notes") or "").strip() or None,
        tags=[str(tag) for tag in tags if str(tag).strip()],
        source=source,
    )
    normalized_metadata = {
        **metadata,
        "kind": "recipe_card",
        "recipe_card": {
            **(recipe_card if isinstance(recipe_card, dict) else {}),
            "schema_version": RECIPE_CARD_SCHEMA_VERSION,
            "ingredients": parsed["ingredients"],
            "steps": parsed["steps"],
            "servings": parsed["servings"] or (recipe_card.get("servings") if isinstance(recipe_card, dict) else None),
            "prep_time": parsed["prep_time"] or (recipe_card.get("prep_time") if isinstance(recipe_card, dict) else None),
            "cook_time": parsed["cook_time"] or (recipe_card.get("cook_time") if isinstance(recipe_card, dict) else None),
            "total_time": parsed["total_time"] or (recipe_card.get("total_time") if isinstance(recipe_card, dict) else None),
            "notes": parsed["notes"] or (recipe_card.get("notes") if isinstance(recipe_card, dict) else None),
            "summary": summary,
            "source_file_id": recipe_card.get("source_file_id") if isinstance(recipe_card, dict) else None,
            "source_query": recipe_card.get("source_query") if isinstance(recipe_card, dict) else None,
        },
    }
    return body_text, normalized_metadata


class HomeMCPError(RuntimeError):
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


@dataclass(frozen=True)
class RootSpec:
    id: str
    path: Path
    writable: bool
    kind: str
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "path": str(self.path),
            "writable": self.writable,
            "kind": self.kind,
            "notes": self.notes,
        }


@dataclass
class HomeMCPTraceWriter:
    logger: RunLogger
    run: RunContext
    command: str
    argv: list[str]
    config_path: Path
    output_paths: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.trace_path = self.run.run_dir / "trace.jsonl"
        self.trace_path.touch(exist_ok=True)

    def trace(
        self,
        stage: str,
        message: str,
        *,
        level: str = "info",
        source_kind: str | None = None,
        source_ref: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        event = {
            "id": f"trc_{os.urandom(8).hex()}",
            "timestamp": utc_now(),
            "stage": stage,
            "level": level,
            "message": message,
            "source_kind": source_kind,
            "source_ref": source_ref,
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
            "output_paths": self.output_paths,
            "error": error,
        }
        self.logger.write_artifact(self.run, "command.json", json.dumps(command_payload, indent=2, sort_keys=True))
        self.logger.finish(self.run, status=status, result=result)


class HomeMCPServer:
    def __init__(
        self,
        *,
        config: AppConfig,
        base_dir: Path,
        root_specs: list[RootSpec],
        auth_mode: str = "none",
        auth_token: str | None = None,
    ) -> None:
        self.config = config
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.root_specs = root_specs
        for root in self.root_specs:
            root.path.mkdir(parents=True, exist_ok=True)
        self.roots_by_id = {root.id: root for root in self.root_specs}
        normalized_auth_mode = auth_mode.strip().lower()
        if normalized_auth_mode not in HOME_MCP_AUTH_MODES:
            raise HomeMCPError(
                f"unsupported auth mode: {auth_mode}",
                stage="config",
                error_code="unsupported_auth_mode",
                source_ref=auth_mode,
            )
        self.auth_mode = normalized_auth_mode
        self.auth_token = auth_token.strip() if auth_token else None
        self.resource_url = (
            (self.config.raw.get("home_mcp", {}) or {}).get("resource_url")
            if isinstance(self.config.raw.get("home_mcp", {}), dict)
            else None
        )
        self.resource_url = (
            self.resource_url
            or os.environ.get("LAGENT_HOME_MCP_RESOURCE_URL")
            or os.environ.get("HOME_MCP_RESOURCE_URL")
            or (f"https://api.openai.com/v1/tunnel/{os.environ.get('HOME_MCP_TUNNEL_ID')}" if os.environ.get("HOME_MCP_TUNNEL_ID") else None)
            or "http://127.0.0.1:8765"
        )
        self.logger = RunLogger(self.config.logs_dir / "home_mcp")

    def oauth_protected_resource_metadata(self, *, request_url: str | None = None) -> dict[str, Any]:
        return {
            "resource": self.resource_url.rstrip("/"),
            "authorization_servers": [],
            "scopes_supported": [],
            "resource_documentation": "https://developers.openai.com/api/docs/guides/secure-mcp-tunnels",
        }

    def oauth_authorization_server_metadata(self) -> dict[str, Any]:
        return {
            "issuer": "https://openai.invalid/no-auth",
            "authorization_endpoint": "",
            "token_endpoint": "",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": [],
        }

    @classmethod
    def from_config(
        cls,
        config: AppConfig,
        *,
        auth_mode: str | None = None,
        auth_token: str | None = None,
    ) -> "HomeMCPServer":
        raw = config.raw.get("home_mcp", {}) if isinstance(config.raw.get("home_mcp", {}), dict) else {}
        base_dir_value = raw.get("base_dir", DEFAULT_HOME_MCP_BASE_DIR)
        base_dir = _resolve_path(config.root_dir, base_dir_value)
        roots_payload = raw.get("allowed_roots")
        root_specs = _build_root_specs(base_dir, roots_payload if isinstance(roots_payload, dict) else None)
        auth_mode_value = auth_mode if auth_mode is not None else raw.get("auth_mode") or os.environ.get("LAGENT_HOME_MCP_AUTH_MODE") or "none"
        auth_mode_value = str(auth_mode_value)
        token = auth_token if auth_token is not None else raw.get("auth_token") or os.environ.get("LAGENT_HOME_MCP_TOKEN")
        return cls(config=config, base_dir=base_dir, root_specs=root_specs, auth_mode=auth_mode_value, auth_token=token)

    def list_allowed_roots(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "base_dir": str(self.base_dir),
            "roots": [root.to_dict() for root in self.root_specs],
        }

    def list_files(
        self,
        *,
        root_id: str,
        glob: str = "*",
        recursive: bool = True,
        limit: int = 100,
    ) -> dict[str, Any]:
        root = self._get_root(root_id)
        if limit < 1:
            return {"status": "ok", "count": 0, "files": [], "root_id": root_id}
        pattern = glob or "*"
        iterator = root.path.rglob(pattern) if recursive else root.path.glob(pattern)
        files: list[dict[str, Any]] = []
        for path in iterator:
            if len(files) >= limit:
                break
            if not path.is_file():
                continue
            if _is_hidden_path(path, root.path):
                continue
            resolved = path.resolve()
            if not _within_root(resolved, root.path):
                continue
            files.append(_file_metadata(root, resolved))
        return {"status": "ok", "count": len(files), "root_id": root_id, "files": files}

    def search_files(
        self,
        *,
        query: str,
        root_id: str | None = None,
        file_types: list[str] | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        normalized_query = query.strip().lower()
        if not normalized_query:
            raise HomeMCPError("query is required", stage="search_files", error_code="missing_query")
        roots = [self._get_root(root_id)] if root_id else list(self.root_specs)
        hits: list[dict[str, Any]] = []
        for root in roots:
            for path in _iter_text_files(root.path, file_types=file_types, limit=MAX_SEARCH_FILES):
                if len(hits) >= limit:
                    break
                try:
                    content = path.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue
                except OSError:
                    continue
                redacted = redact_text(content)
                path_text = str(path.relative_to(root.path)).lower()
                query_terms = [term for term in re.split(r"\s+", normalized_query) if term]
                matched_terms = [term for term in query_terms if term in redacted.lower() or term in path_text]
                if not matched_terms:
                    continue
                snippet = _make_snippet(redacted, matched_terms[0])
                hits.append(
                    {
                        **_file_metadata(root, path.resolve()),
                        "score": round(len(matched_terms) + (2 if normalized_query in path_text else 0), 3),
                        "match_reason": "path" if normalized_query in path_text else "content",
                        "matched_terms": matched_terms,
                        "snippet": snippet,
                    }
                )
            if len(hits) >= limit:
                break
        hits.sort(key=lambda item: (-float(item["score"]), item["relative_path"]))
        hits = hits[:limit]
        return {"status": "ok", "count": len(hits), "query": query, "results": hits}

    def search_notes(
        self,
        *,
        query: str,
        root_id: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        return {
            **self.search_files(query=query, root_id=root_id, file_types=[".md"], limit=limit),
            "view": "notes",
        }

    def list_recent_files(
        self,
        *,
        root_id: str,
        limit: int = 20,
        file_types: list[str] | None = None,
    ) -> dict[str, Any]:
        root = self._get_root(root_id)
        if limit < 1:
            return {"status": "ok", "root_id": root_id, "count": 0, "files": []}
        files: list[dict[str, Any]] = []
        for path in _iter_text_files(root.path, file_types=file_types, limit=MAX_SEARCH_FILES):
            resolved = path.resolve()
            if not _within_root(resolved, root.path):
                continue
            files.append(_file_metadata(root, resolved))
        files.sort(key=lambda item: str(item["modified_at"]), reverse=True)
        files = files[:limit]
        return {"status": "ok", "root_id": root_id, "count": len(files), "files": files}

    def read_file(
        self,
        *,
        file_id: str | None = None,
        root_id: str | None = None,
        relative_path: str | None = None,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> dict[str, Any]:
        path, root = self._resolve_file_reference(file_id=file_id, root_id=root_id, relative_path=relative_path)
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise HomeMCPError("file is not readable as UTF-8 text", stage="read_file", error_code="binary_file", source_ref=str(path)) from exc
        except OSError as exc:
            raise HomeMCPError(str(exc), stage="read_file", error_code="read_failed", source_ref=str(path)) from exc
        lines = content.splitlines()
        line_count = len(lines)
        start_index = max((start_line or 1) - 1, 0)
        end_index = line_count if end_line is None else min(max(end_line, 0), line_count)
        if start_index > end_index:
            raise HomeMCPError("start_line must be <= end_line", stage="read_file", error_code="invalid_range", source_ref=str(path))
        selected = lines[start_index:end_index]
        text = "\n".join(selected)
        truncated = len(text.encode("utf-8")) > MAX_FILE_BYTES
        if truncated:
            text = text.encode("utf-8")[:MAX_FILE_BYTES].decode("utf-8", errors="ignore")
        redacted = redact_text(text)
        return {
            "status": "ok",
            "file_id": _file_id(root, path),
            "root_id": root.id,
            "relative_path": str(path.relative_to(root.path)),
            "path": str(path),
            "line_count": line_count,
            "start_line": start_line or 1,
            "end_line": end_index,
            "truncated": truncated,
            "content": redacted,
        }

    def create_markdown_note(
        self,
        *,
        root_id: str,
        folder: str = "",
        title: str,
        body: str,
        tags: list[str] | None = None,
        filename: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        root = self._get_root(root_id)
        if not root.writable:
            raise HomeMCPError("root is read-only", stage="create_markdown_note", error_code="root_read_only", source_ref=root.id)
        folder_path = self._sanitized_folder(root, folder)
        folder_path.mkdir(parents=True, exist_ok=True)
        note_path = self._unique_markdown_path(folder_path, filename or title)
        now = utc_now()
        payload = {
            "title": title,
            "created_at": now,
            "updated_at": now,
            "root_id": root.id,
            "kind": "markdown_note",
            "tags": tags or [],
            "metadata": metadata or {},
        }
        content = _frontmatter(payload) + "\n" + body.strip() + "\n"
        note_path.write_text(content, encoding="utf-8")
        return self._write_result(root, note_path, "create_markdown_note", {"title": title, "tags": tags or []})

    def append_markdown_log(
        self,
        *,
        file_id: str | None = None,
        root_id: str | None = None,
        relative_path: str | None = None,
        entry: str,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        path, root = self._resolve_file_reference(file_id=file_id, root_id=root_id, relative_path=relative_path)
        if not root.writable:
            raise HomeMCPError("root is read-only", stage="append_markdown_log", error_code="root_read_only", source_ref=root.id)
        if path.suffix.lower() != ".md":
            raise HomeMCPError("append_markdown_log requires a markdown file", stage="append_markdown_log", error_code="invalid_file_type", source_ref=str(path))
        now = utc_now()
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
        block = [f"## {stamp}"]
        if tags:
            block.append(f"Tags: {', '.join(tags)}")
        block.append(entry.strip())
        append_text = "\n".join(block) + "\n\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        if existing and not existing.endswith("\n"):
            existing += "\n"
        path.write_text(existing + append_text, encoding="utf-8")
        return self._write_result(root, path, "append_markdown_log", {"entry": entry, "tags": tags or []}, extra={"appended_at": now})

    def create_recipe(
        self,
        *,
        title: str,
        body: str,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        result = self.create_markdown_note(
            root_id="recipe_book",
            folder="",
            title=title,
            body=body,
            tags=["recipe", *(tags or [])],
            metadata={"kind": "recipe", **(metadata or {})},
        )
        result["recipe_id"] = result["file_id"]
        return result

    def get_recipe(self, *, recipe_id: str) -> dict[str, Any]:
        read = self.read_file(file_id=recipe_id)
        if read["root_id"] != "recipe_book":
            raise HomeMCPError(
                "get_recipe only accepts recipe_book files",
                stage="get_recipe",
                error_code="invalid_recipe_root",
                source_ref=recipe_id,
            )
        metadata, body = _parse_markdown_document(str(read["content"]))
        title = str(metadata.get("title") or Path(str(read["relative_path"])).stem).strip()
        parsed = _extract_recipe_structure(body, title=title)
        nested_metadata = metadata.get("metadata") if isinstance(metadata.get("metadata"), dict) else {}
        return {
            "status": "ok",
            "recipe_id": recipe_id,
            "file": {key: read[key] for key in ("file_id", "root_id", "relative_path", "path", "line_count", "truncated")},
            "title": title,
            "tags": metadata.get("tags", []) if isinstance(metadata.get("tags"), list) else [],
            "metadata": metadata,
            "recipe_card": nested_metadata.get("recipe_card") if isinstance(nested_metadata.get("recipe_card"), dict) else metadata.get("recipe_card"),
            "structure": parsed,
            "content": body.strip(),
            "standard": _recipe_standard(),
        }

    def draft_recipe_card(
        self,
        *,
        source_text: str | None = None,
        file_id: str | None = None,
        title: str | None = None,
        query: str | None = None,
    ) -> dict[str, Any]:
        if not source_text and not file_id:
            raise HomeMCPError(
                "source_text or file_id is required",
                stage="draft_recipe_card",
                error_code="missing_source",
            )
        source_path: str | None = None
        source_root_id: str | None = None
        source_relative_path: str | None = None
        if file_id:
            read = self.read_file(file_id=file_id)
            source_text = read["content"]
            source_path = read["path"]
            source_root_id = read["root_id"]
            source_relative_path = read["relative_path"]
            title = title or str(Path(read["relative_path"]).stem).replace("-", " ").replace("_", " ").strip().title()
        source_text = source_text or ""
        parsed = _extract_recipe_structure(source_text, title=title, query=query)
        draft_title = parsed["title"]
        body = _render_recipe_card_body(
            draft_title,
            summary=parsed["summary"] or _extract_recipe_summary(source_text or ""),
            ingredients=parsed["ingredients"],
            steps=parsed["steps"],
            servings=parsed["servings"],
            prep_time=parsed["prep_time"],
            cook_time=parsed["cook_time"],
            total_time=parsed["total_time"],
            notes=parsed["notes"],
            tags=parsed["tags"],
            source=query or file_id,
        )
        return {
            "status": "ok",
            "source": {
                "file_id": file_id,
                "path": source_path,
                "root_id": source_root_id,
                "relative_path": source_relative_path,
                "query": query,
            },
            "draft": {
                "title": draft_title,
                "body": body,
                "ingredients": parsed["ingredients"],
                "steps": parsed["steps"],
                "servings": parsed["servings"],
                "prep_time": parsed["prep_time"],
                "cook_time": parsed["cook_time"],
                "total_time": parsed["total_time"],
                "notes": parsed["notes"],
                "summary": parsed["summary"],
                "confidence": parsed["confidence"],
                "tags": parsed["tags"],
                "standard": _recipe_standard(),
            },
        }

    def append_recipe_attempt(
        self,
        *,
        recipe_id: str,
        notes: str,
        outcome: str | None = None,
        next_time: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        lines = [notes.strip()]
        if outcome:
            lines.append(f"Outcome: {outcome}")
        if next_time:
            lines.append(f"Next time: {next_time}")
        return self.append_markdown_log(file_id=recipe_id, entry="\n".join(lines), tags=["recipe_attempt", *(tags or [])])

    def compare_recipe_attempts(self, *, recipe_id: str) -> dict[str, Any]:
        recipe = self.get_recipe(recipe_id=recipe_id)
        attempts = _extract_recipe_attempts(str(recipe["content"]))
        return {
            "status": "ok",
            "recipe_id": recipe_id,
            "title": recipe["title"],
            "attempt_count": len(attempts),
            "attempts": attempts,
            "comparison": _compare_recipe_attempts(attempts),
        }

    def create_project_note(
        self,
        *,
        project_id: str,
        title: str,
        body: str,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        cleaned_project_id = _slugify(project_id)
        result = self.create_markdown_note(
            root_id="projects",
            folder=cleaned_project_id,
            title=title,
            body=body,
            tags=["project", cleaned_project_id, *(tags or [])],
            metadata={"kind": "project_note", "project_id": cleaned_project_id},
        )
        result["project_id"] = cleaned_project_id
        return result

    def create_recipe_card(
        self,
        *,
        title: str,
        body: str | None = None,
        ingredients: list[str] | None = None,
        steps: list[str] | None = None,
        servings: str | None = None,
        prep_time: str | None = None,
        cook_time: str | None = None,
        total_time: str | None = None,
        notes: str | None = None,
        source_file_id: str | None = None,
        source_query: str | None = None,
        summary: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        structured_fields_present = any(
            value
            for value in [
                ingredients,
                steps,
                servings,
                prep_time,
                cook_time,
                total_time,
                notes,
                source_file_id,
                source_query,
                summary,
            ]
        )
        if body is None and not structured_fields_present:
            raise HomeMCPError(
                "body or structured recipe fields are required",
                stage="create_recipe_card",
                error_code="missing_recipe_content",
            )
        recipe_metadata = {
            "kind": "recipe_card",
            "schema_version": RECIPE_CARD_SCHEMA_VERSION,
            "recipe_card": {
                "ingredients": ingredients or [],
                "steps": steps or [],
                "servings": servings,
                "prep_time": prep_time,
                "cook_time": cook_time,
                "total_time": total_time,
                "notes": notes,
                "summary": summary,
                "source_file_id": source_file_id,
                "source_query": source_query,
                "schema_version": RECIPE_CARD_SCHEMA_VERSION,
            },
            **(metadata or {}),
        }
        recipe_body = body or _render_recipe_card_body(
            title,
            summary=summary,
            ingredients=ingredients or [],
            steps=steps or [],
            servings=servings,
            prep_time=prep_time,
            cook_time=cook_time,
            total_time=total_time,
            notes=notes,
            tags=tags or [],
            source=source_file_id or source_query,
        )
        result = self.create_markdown_note(
            root_id="recipe_book",
            folder="",
            title=title,
            body=recipe_body,
            tags=["recipe", "recipe_card", *(tags or [])],
            metadata=recipe_metadata,
        )
        result["recipe_id"] = result["file_id"]
        return result

    def recipe_standard(self) -> dict[str, Any]:
        return {"status": "ok", **_recipe_standard()}

    def normalize_recipe_book(self, *, apply: bool = False, limit: int = 500) -> dict[str, Any]:
        root = self._get_root("recipe_book")
        changed: list[dict[str, Any]] = []
        inspected = 0
        for path in _iter_text_files(root.path, file_types=[".md"], limit=limit):
            inspected += 1
            try:
                raw_text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            metadata, body = _parse_markdown_document(raw_text)
            title = str(metadata.get("title") or path.stem).strip()
            normalized_body, normalized_metadata = _normalize_recipe_note_text(
                title=title,
                metadata=metadata,
                body=body,
                source_hint=f"File: {path.relative_to(root.path)}",
            )
            normalized_text = _frontmatter(
                {
                    "title": title,
                    "created_at": metadata.get("created_at"),
                    "updated_at": utc_now() if apply else metadata.get("updated_at"),
                    "root_id": root.id,
                    "kind": "markdown_note",
                    "tags": metadata.get("tags", []),
                    "metadata": normalized_metadata,
                }
            )
            normalized_text += "\n" + normalized_body.strip() + "\n"
            if raw_text != normalized_text:
                changed.append(
                    {
                        "file_id": _file_id(root, path.resolve()),
                        "relative_path": str(path.relative_to(root.path)),
                        "title": title,
                    }
                )
                if apply:
                    path.write_text(normalized_text, encoding="utf-8")
        return {
            "status": "ok",
            "root_id": root.id,
            "inspected": inspected,
            "changed": len(changed),
            "dry_run": not apply,
            "changed_files": changed,
        }

    def search_recipes(
        self,
        *,
        query: str | None = None,
        tags: list[str] | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        root = self._get_root("recipe_book")
        normalized_query = (query or "").strip().lower()
        tag_filters = {str(item).strip().lower() for item in (tags or []) if str(item).strip()}
        hits: list[dict[str, Any]] = []
        for path in _iter_text_files(root.path, file_types=[".md"], limit=MAX_SEARCH_FILES):
            if len(hits) >= limit:
                break
            try:
                raw_text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            except OSError:
                continue
            metadata, body = _parse_markdown_document(raw_text)
            nested_metadata = metadata.get("metadata") if isinstance(metadata.get("metadata"), dict) else {}
            card_tags = [str(item) for item in metadata.get("tags", [])] if isinstance(metadata.get("tags"), list) else []
            card_tags_lower = {tag.lower() for tag in card_tags}
            if tag_filters and not tag_filters.issubset(card_tags_lower):
                continue
            title = str(metadata.get("title") or path.stem).strip()
            body_text = redact_text(body)
            recipe_structure = _extract_recipe_structure(body_text, title=title)
            card_summary = str(
                metadata.get("summary")
                or (metadata.get("recipe_card", {}) if isinstance(metadata.get("recipe_card"), dict) else {}).get("summary", "")
                or nested_metadata.get("recipe_card", {}).get("summary", "")
            )
            schema_version = None
            if isinstance(metadata.get("recipe_card"), dict):
                schema_version = metadata["recipe_card"].get("schema_version")
            elif isinstance(nested_metadata.get("recipe_card"), dict):
                schema_version = nested_metadata["recipe_card"].get("schema_version")
            combined_text = " ".join([title, " ".join(card_tags), body_text, str(metadata.get("kind", "")), card_summary]).lower()
            matched_terms = [term for term in re.split(r"\s+", normalized_query) if term] if normalized_query else []
            matched_terms = [term for term in matched_terms if term in combined_text]
            if normalized_query and not matched_terms and normalized_query not in combined_text:
                continue
            score = 0.0
            if normalized_query:
                score += len(matched_terms)
                if normalized_query in title.lower():
                    score += 4
                if normalized_query in " ".join(card_tags).lower():
                    score += 2
                if normalized_query in body_text.lower():
                    score += 1
                if normalized_query in str(path.relative_to(root.path)).lower():
                    score += 1
            else:
                try:
                    score += path.stat().st_mtime / 1_000_000_000
                except OSError:
                    score += 0
            snippet_source = body_text or title
            snippet_term = matched_terms[0] if matched_terms else (normalized_query or title.lower())
            hits.append(
                {
                    **_file_metadata(root, path.resolve()),
                    "title": title,
                    "kind": str(metadata.get("kind") or "recipe"),
                    "tags": card_tags,
                    "summary": str(metadata.get("summary") or "").strip() or None,
                    "created_at": metadata.get("created_at"),
                    "updated_at": metadata.get("updated_at"),
                    "ingredients_count": len(recipe_structure["ingredients"]),
                    "steps_count": len(recipe_structure["steps"]),
                    "servings": recipe_structure["servings"],
                    "prep_time": recipe_structure["prep_time"],
                    "cook_time": recipe_structure["cook_time"],
                    "total_time": recipe_structure["total_time"],
                    "recipe_summary": recipe_structure["summary"],
                    "schema_version": schema_version,
                    "score": round(score, 3),
                    "match_reason": "title" if normalized_query and normalized_query in title.lower() else "content" if normalized_query else "recent",
                    "matched_terms": matched_terms,
                    "snippet": _make_snippet(snippet_source, snippet_term),
                }
            )
        hits.sort(key=lambda item: (-float(item["score"]), item["title"].lower(), item["relative_path"]))
        hits = hits[:limit]
        return {
            "status": "ok",
            "root_id": "recipe_book",
            "query": query or "",
            "tags": sorted(tag_filters),
            "count": len(hits),
            "results": hits,
        }

    def browse_recipes(
        self,
        *,
        query: str | None = None,
        tags: list[str] | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        browse = self.search_recipes(query=query, tags=tags, limit=limit)
        browse["standard"] = _recipe_standard()
        browse["view"] = "standardized_recipe_browse"
        browse["status"] = "ok"
        return browse

    def memory_status(self, *, recent_limit: int = 5) -> dict[str, Any]:
        return summarize_memory_status(
            data_dir=self.config.paths["data_dir"],
            memory_dir=self.config.paths["memory_dir"],
            logs_dir=self.config.logs_dir,
            recent_limit=recent_limit,
        )

    def memory_analyze(self, *, subject_limit: int = 10, recent_limit: int = 5) -> dict[str, Any]:
        return analyze_memory_corpus(
            data_dir=self.config.paths["data_dir"],
            memory_dir=self.config.paths["memory_dir"],
            logs_dir=self.config.logs_dir,
            subject_limit=subject_limit,
            recent_limit=recent_limit,
        )

    def memory_subjects(self, *, kind: str | None = None, limit: int | None = None) -> dict[str, Any]:
        db_path = memory_db_path(self.config.paths["memory_dir"])
        if not db_path.exists():
            raise HomeMCPError(
                f"ChatGPT memory database does not exist: {db_path}",
                stage="memory_subjects",
                error_code="memory_database_not_found",
                source_ref=str(db_path),
            )
        with sqlite3.connect(db_path) as connection:
            connection.row_factory = sqlite3.Row
            init_subject_schema(connection)
            subjects = [summary.to_dict() for summary in list_subjects(connection, kind=kind, limit=limit)]
        return {"status": "ok", "count": len(subjects), "subjects": subjects}

    def memory_candidates(
        self,
        *,
        review_status: str | None = "pending",
        domain: str | None = None,
        source_role: str | None = None,
        assistant_suggestion: bool | None = None,
        subject: str | None = None,
        subject_kind: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        db_path = memory_db_path(self.config.paths["memory_dir"])
        if not db_path.exists():
            raise HomeMCPError(
                f"ChatGPT memory database does not exist: {db_path}",
                stage="memory_candidates",
                error_code="memory_database_not_found",
                source_ref=str(db_path),
            )
        with sqlite3.connect(db_path) as connection:
            connection.row_factory = sqlite3.Row
            candidates = [
                candidate.to_dict()
                for candidate in list_candidate_memories(
                    connection,
                    review_status=review_status,
                    domain=domain,
                    source_role=source_role,
                    assistant_suggestion=assistant_suggestion,
                    subject=subject,
                    subject_kind=subject_kind,
                    limit=limit,
                )
            ]
        return {"status": "ok", "count": len(candidates), "candidate_memories": candidates}

    def memory_review_subjects(
        self,
        *,
        subject: str | None = None,
        kind: str = "subject",
        review_status: str | None = "pending",
        source_role: str | None = None,
        assistant_only: bool = False,
        subject_limit: int = 20,
        candidate_limit: int = 20,
    ) -> dict[str, Any]:
        db_path = memory_db_path(self.config.paths["memory_dir"])
        if not db_path.exists():
            raise HomeMCPError(
                f"ChatGPT memory database does not exist: {db_path}",
                stage="memory_review_subjects",
                error_code="memory_database_not_found",
                source_ref=str(db_path),
            )
        with sqlite3.connect(db_path) as connection:
            connection.row_factory = sqlite3.Row
            init_subject_schema(connection)
            init_candidate_memory_schema(connection)
            subject_summaries = list_candidate_subjects(
                connection,
                review_status=review_status,
                source_role=source_role,
                assistant_suggestion=True if assistant_only else None,
                kind=kind,
                limit=subject_limit,
            )
            candidate_memories: list[dict[str, Any]] = []
            selected_subject: dict[str, Any] | None = None
            if subject is not None:
                candidate_memories = [
                    candidate.to_dict()
                    for candidate in list_candidate_memories_for_subject(
                        connection,
                        subject,
                        kind=kind,
                        review_status=review_status,
                        source_role=source_role,
                        assistant_suggestion=True if assistant_only else None,
                        limit=candidate_limit,
                    )
                ]
                selected_subject = next(
                    (item for item in subject_summaries if item["kind"] == kind and item["slug"] == normalize_subject_slug(subject)),
                    None,
                )
                if selected_subject is None:
                    selected_subject = {
                        "kind": kind,
                        "slug": normalize_subject_slug(subject),
                        "name": subject,
                        "candidate_count": len(candidate_memories),
                        "pending_count": len(candidate_memories) if review_status == "pending" else 0,
                        "approved_count": 0,
                        "merged_count": 0,
                        "rejected_count": 0,
                        "assistant_count": sum(1 for item in candidate_memories if item["assistant_suggestion"]),
                        "latest_candidate_activity_at": None,
                    }
        return {
            "status": "ok",
            "count": len(subject_summaries),
            "subject_summaries": subject_summaries,
            "selected_subject": selected_subject,
            "candidate_memories": candidate_memories,
            "filters": {
                "subject": subject,
                "kind": kind,
                "review_status": review_status,
                "source_role": source_role,
                "assistant_only": assistant_only,
                "subject_limit": subject_limit,
                "candidate_limit": candidate_limit,
            },
        }

    def memory_review(
        self,
        *,
        candidate_id: str | None = None,
        action: str | None = None,
        review_status: str | None = "pending",
        domain: str | None = None,
        subject: str | None = None,
        subject_kind: str = "subject",
        source_role: str | None = None,
        assistant_only: bool = False,
        limit: int | None = 20,
        note: str | None = None,
        record_type: str | None = None,
        title: str | None = None,
        trust_level: str = "high",
        allow_assistant: bool = False,
    ) -> dict[str, Any]:
        db_path = memory_db_path(self.config.paths["memory_dir"])
        if not db_path.exists():
            raise HomeMCPError(
                f"ChatGPT memory database does not exist: {db_path}",
                stage="memory_review",
                error_code="memory_database_not_found",
                source_ref=str(db_path),
            )
        with sqlite3.connect(db_path) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            init_candidate_memory_schema(connection)
            init_curated_memory_schema(connection)
            init_subject_schema(connection)
            if candidate_id is not None:
                candidate = get_candidate_memory(connection, candidate_id)
                selected = [candidate]
            else:
                selected = list_candidate_memories(
                    connection,
                    review_status=review_status,
                    domain=domain,
                    source_role=source_role,
                    assistant_suggestion=True if assistant_only else None,
                    subject=subject,
                    subject_kind=subject_kind,
                    limit=limit,
                )

            if action is None:
                candidates = [candidate.to_dict() for candidate in selected]
                return {
                    "status": "ok",
                    "count": len(candidates),
                    "candidate_memories": candidates,
                    "filters": {
                        "review_status": review_status,
                        "domain": domain,
                        "subject": subject,
                        "subject_kind": subject_kind,
                        "source_role": source_role,
                        "assistant_only": assistant_only,
                        "limit": limit,
                    },
                }

            if candidate_id is None:
                raise HomeMCPError(
                    "--candidate-id is required when using an action",
                    stage="memory_review",
                    error_code="missing_candidate_id",
                )
            candidate = selected[0]
            if action == "approve":
                updated = update_candidate_review(
                    connection,
                    candidate.id,
                    review_status="approved",
                    review_notes=note,
                    last_confirmed_at=utc_now(),
                )
                return {"status": "ok", "candidate_memory": updated.to_dict()}
            if action == "reject":
                updated = update_candidate_review(
                    connection,
                    candidate.id,
                    review_status="rejected",
                    review_notes=note,
                )
                return {"status": "ok", "candidate_memory": updated.to_dict()}
            if action == "promote":
                if candidate.assistant_suggestion and not allow_assistant:
                    raise HomeMCPError(
                        "assistant suggestions stay separate until explicit confirmation",
                        stage="memory_review",
                        error_code="assistant_promotion_blocked",
                        source_ref=candidate.id,
                    )
                promoted = _promote_candidate_memory(
                    connection,
                    candidate,
                    record_type=record_type,
                    title=title,
                    trust_level=trust_level,
                    note=note,
                )
                updated = update_candidate_review(
                    connection,
                    candidate.id,
                    review_status="merged",
                    review_notes=note,
                    last_confirmed_at=utc_now(),
                )
                return {"status": "ok", "candidate_memory": updated.to_dict(), "memory_record": promoted.to_dict()}
            raise HomeMCPError(
                f"invalid review action: {action}",
                stage="memory_review",
                error_code="invalid_review_action",
                source_ref=action,
            )

    def memory_search(
        self,
        *,
        query: str,
        limit: int = 8,
        subject: str | None = None,
        title: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        exclude_source_ids: list[str] | None = None,
        exclude_subjects: list[str] | None = None,
        depth: str = "medium",
        effort: int = 2,
        allow_cross_domain: bool = False,
    ) -> dict[str, Any]:
        return search_chatgpt_memory(
            memory_dir=self.config.paths["memory_dir"],
            query=query,
            limit=limit,
            subject=subject,
            title=title,
            date_from=date_from,
            date_to=date_to,
            exclude_source_ids=exclude_source_ids,
            exclude_subjects=exclude_subjects,
            depth=depth,
            effort=effort,
            allow_cross_domain=allow_cross_domain,
        )

    def memory_list(self, *, record_type: str | None = None, limit: int | None = None) -> dict[str, Any]:
        db_path = memory_db_path(self.config.paths["memory_dir"])
        if not db_path.exists():
            raise HomeMCPError(
                f"ChatGPT memory database does not exist: {db_path}",
                stage="memory_list",
                error_code="memory_database_not_found",
                source_ref=str(db_path),
            )
        with sqlite3.connect(db_path) as connection:
            connection.row_factory = sqlite3.Row
            init_curated_memory_schema(connection)
            records = [record.to_dict() for record in list_memory_records(connection, record_type=record_type, limit=limit)]
        return {"status": "ok", "count": len(records), "memory_records": records}

    def memory_open_loops(self, *, limit: int | None = None) -> dict[str, Any]:
        db_path = memory_db_path(self.config.paths["memory_dir"])
        if not db_path.exists():
            raise HomeMCPError(
                f"ChatGPT memory database does not exist: {db_path}",
                stage="memory_open_loops",
                error_code="memory_database_not_found",
                source_ref=str(db_path),
            )
        with sqlite3.connect(db_path) as connection:
            connection.row_factory = sqlite3.Row
            init_feedback_schema(connection)
            loops = list_open_loops(connection, limit=limit)
        return {"status": "ok", "count": len(loops), "open_loops": loops}

    def memory_trace(self, *, run_id: str) -> dict[str, Any]:
        return read_memory_trace(self.config.logs_dir, run_id)

    def bridge_recipe_note_to_memory(
        self,
        *,
        file_id: str,
        record_type: str = "research_note",
        title: str | None = None,
        trust_level: str = "high",
        subject: str | None = None,
        subject_kind: str = "subject",
    ) -> dict[str, Any]:
        read = self.read_file(file_id=file_id)
        if read["root_id"] != "recipe_book":
            raise HomeMCPError(
                "recipe note bridge only accepts recipe_book files",
                stage="bridge_recipe_note_to_memory",
                error_code="invalid_recipe_root",
                source_ref=file_id,
            )
        metadata, body = _parse_markdown_document(str(read["content"]))
        note_title = title or str(metadata.get("title") or Path(read["relative_path"]).stem).strip()
        db_path = memory_db_path(self.config.paths["memory_dir"])
        with sqlite3.connect(db_path) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            init_subject_schema(connection)
            init_curated_memory_schema(connection)
            subject_id = None
            if subject:
                subject_record = upsert_subject(connection, subject, kind=subject_kind)
                subject_id = subject_record.id
            memory_record = create_memory_record(
                connection,
                record_type=record_type,
                title=note_title,
                body=body.strip() or str(read["content"]).strip(),
                subject_id=subject_id,
                trust_level=trust_level,
                source_kind="recipe_book",
                source_ref=file_id,
                provenance={
                    "recipe_file_id": file_id,
                    "recipe_root_id": read["root_id"],
                    "recipe_relative_path": read["relative_path"],
                    "recipe_note_metadata": metadata,
                },
                metadata={
                    "recipe_file_id": file_id,
                    "recipe_root_id": read["root_id"],
                    "recipe_relative_path": read["relative_path"],
                    "recipe_note_kind": metadata.get("kind"),
                    "recipe_note_tags": metadata.get("tags", []),
                },
                created_by="user",
            )
        return {
            "status": "ok",
            "source_file": {
                "file_id": file_id,
                "root_id": read["root_id"],
                "relative_path": read["relative_path"],
            },
            "memory_record": memory_record.to_dict(),
        }

    def tools(self) -> list[dict[str, Any]]:
        return [
            _tool_definition(
                "recipe_standard",
                "List the canonical recipe card standard before creating or normalizing recipes.",
                {"type": "object", "properties": {}, "additionalProperties": False},
            ),
            _tool_definition("list_allowed_roots", "List allowed roots exposed by the home-mcp server.", {"type": "object", "properties": {}, "additionalProperties": False}),
            _tool_definition(
                "list_files",
                "List files inside an allowlisted root.",
                {
                    "type": "object",
                    "properties": {
                        "root_id": {"type": "string"},
                        "glob": {"type": "string"},
                        "recursive": {"type": "boolean"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 500},
                    },
                    "required": ["root_id"],
                    "additionalProperties": False,
                },
            ),
            _tool_definition(
                "search_files",
                "Search files inside the allowlisted roots for text matches.",
                {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "root_id": {"type": "string"},
                        "file_types": {"type": "array", "items": {"type": "string"}},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            ),
            _tool_definition(
                "search_notes",
                "Search Markdown notes inside the allowlisted roots.",
                {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "root_id": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            ),
            _tool_definition(
                "list_recent_files",
                "List recently modified files inside an allowlisted root.",
                {
                    "type": "object",
                    "properties": {
                        "root_id": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                        "file_types": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["root_id"],
                    "additionalProperties": False,
                },
            ),
            _tool_definition(
                "search_recipes",
                "Search the recipe book for recipe notes and attempts.",
                {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                    },
                    "additionalProperties": False,
                },
            ),
            _tool_definition(
                "get_recipe",
                "Read a recipe card by recipe_id with parsed structure and provenance.",
                {
                    "type": "object",
                    "properties": {
                        "recipe_id": {"type": "string"},
                    },
                    "required": ["recipe_id"],
                    "additionalProperties": False,
                },
            ),
            _tool_definition(
                "browse_recipes",
                "Browse standardized recipe cards in the recipe book.",
                {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                    },
                    "additionalProperties": False,
                },
            ),
            _tool_definition(
                "draft_recipe_card",
                "Draft a structured recipe card from source text or a file.",
                {
                    "type": "object",
                    "properties": {
                        "source_text": {"type": "string"},
                        "file_id": {"type": "string"},
                        "title": {"type": "string"},
                        "query": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            ),
            _tool_definition(
                "read_file",
                "Read a text file by file_id or path inside an allowlisted root.",
                {
                    "type": "object",
                    "properties": {
                        "file_id": {"type": "string"},
                        "root_id": {"type": "string"},
                        "relative_path": {"type": "string"},
                        "start_line": {"type": "integer", "minimum": 1},
                        "end_line": {"type": "integer", "minimum": 1},
                    },
                    "additionalProperties": False,
                },
            ),
            _tool_definition(
                "create_markdown_note",
                "Create a new Markdown note in a writable root.",
                {
                    "type": "object",
                    "properties": {
                        "root_id": {"type": "string"},
                        "folder": {"type": "string"},
                        "title": {"type": "string"},
                        "body": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "filename": {"type": "string"},
                        "metadata": {"type": "object"},
                    },
                    "required": ["root_id", "title", "body"],
                    "additionalProperties": False,
                },
            ),
            _tool_definition(
                "append_markdown_log",
                "Append a timestamped entry to an existing Markdown note.",
                {
                    "type": "object",
                    "properties": {
                        "file_id": {"type": "string"},
                        "root_id": {"type": "string"},
                        "relative_path": {"type": "string"},
                        "entry": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["entry"],
                    "additionalProperties": False,
                },
            ),
            _tool_definition(
                "create_recipe",
                "Create a recipe note in the recipe book.",
                {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "body": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "metadata": {"type": "object"},
                    },
                    "required": ["title", "body"],
                    "additionalProperties": False,
                },
            ),
            _tool_definition(
                "create_recipe_card",
                "Create a recipe card note in the recipe book.",
                {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "body": {"type": "string"},
                        "ingredients": {"type": "array", "items": {"type": "string"}},
                        "steps": {"type": "array", "items": {"type": "string"}},
                        "servings": {"type": "string"},
                        "prep_time": {"type": "string"},
                        "cook_time": {"type": "string"},
                        "total_time": {"type": "string"},
                        "notes": {"type": "string"},
                        "source_file_id": {"type": "string"},
                        "source_query": {"type": "string"},
                        "summary": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "metadata": {"type": "object"},
                    },
                    "required": ["title"],
                    "additionalProperties": False,
                },
            ),
            _tool_definition(
                "append_recipe_attempt",
                "Append a recipe attempt to a recipe note.",
                {
                    "type": "object",
                    "properties": {
                        "recipe_id": {"type": "string"},
                        "notes": {"type": "string"},
                        "outcome": {"type": "string"},
                        "next_time": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["recipe_id", "notes"],
                    "additionalProperties": False,
                },
            ),
            _tool_definition(
                "compare_recipe_attempts",
                "Compare logged attempts for a recipe note without modifying it.",
                {
                    "type": "object",
                    "properties": {
                        "recipe_id": {"type": "string"},
                    },
                    "required": ["recipe_id"],
                    "additionalProperties": False,
                },
            ),
            _tool_definition(
                "memory_status",
                "Summarize ChatGPT memory health and recent activity.",
                {
                    "type": "object",
                    "properties": {
                        "recent_limit": {"type": "integer", "minimum": 1, "maximum": 20},
                    },
                    "additionalProperties": False,
                },
            ),
            _tool_definition(
                "create_project_note",
                "Create a Markdown project note under the projects root.",
                {
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "title": {"type": "string"},
                        "body": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["project_id", "title", "body"],
                    "additionalProperties": False,
                },
            ),
            _tool_definition(
                "memory_analyze",
                "Summarize corpus shape and write analysis artifacts.",
                {
                    "type": "object",
                    "properties": {
                        "subject_limit": {"type": "integer", "minimum": 1, "maximum": 50},
                        "recent_limit": {"type": "integer", "minimum": 1, "maximum": 20},
                    },
                    "additionalProperties": False,
                },
            ),
            _tool_definition(
                "memory_subjects",
                "List ChatGPT memory subjects with counts and recency.",
                {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1},
                    },
                    "additionalProperties": False,
                },
            ),
            _tool_definition(
                "memory_candidates",
                "List candidate memories with review filters.",
                {
                    "type": "object",
                    "properties": {
                        "review_status": {"type": "string"},
                        "domain": {"type": "string"},
                        "source_role": {"type": "string"},
                        "assistant_suggestion": {"type": "boolean"},
                        "subject": {"type": "string"},
                        "subject_kind": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1},
                    },
                    "additionalProperties": False,
                },
            ),
            _tool_definition(
                "memory_review_subjects",
                "Browse candidate memories by subject with traceable drill-down.",
                {
                    "type": "object",
                    "properties": {
                        "subject": {"type": "string"},
                        "kind": {"type": "string"},
                        "review_status": {"type": "string"},
                        "source_role": {"type": "string"},
                        "assistant_only": {"type": "boolean"},
                        "subject_limit": {"type": "integer", "minimum": 1},
                        "candidate_limit": {"type": "integer", "minimum": 1},
                    },
                    "additionalProperties": False,
                },
            ),
            _tool_definition(
                "memory_review",
                "Inspect candidate memories and optionally approve, reject, or promote them.",
                {
                    "type": "object",
                    "properties": {
                        "candidate_id": {"type": "string"},
                        "action": {"type": "string"},
                        "review_status": {"type": "string"},
                        "domain": {"type": "string"},
                        "subject": {"type": "string"},
                        "subject_kind": {"type": "string"},
                        "source_role": {"type": "string"},
                        "assistant_only": {"type": "boolean"},
                        "limit": {"type": "integer", "minimum": 1},
                        "note": {"type": "string"},
                        "record_type": {"type": "string"},
                        "title": {"type": "string"},
                        "trust_level": {"type": "string"},
                        "allow_assistant": {"type": "boolean"},
                    },
                    "additionalProperties": False,
                },
            ),
            _tool_definition(
                "memory_search",
                "Search the ChatGPT memory corpus.",
                {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                        "subject": {"type": "string"},
                        "title": {"type": "string"},
                        "date_from": {"type": "string"},
                        "date_to": {"type": "string"},
                        "exclude_source_ids": {"type": "array", "items": {"type": "string"}},
                        "exclude_subjects": {"type": "array", "items": {"type": "string"}},
                        "depth": {"type": "string"},
                        "effort": {"type": "integer", "minimum": 1, "maximum": 5},
                        "allow_cross_domain": {"type": "boolean"},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            ),
            _tool_definition(
                "memory_list",
                "List curated memory records.",
                {
                    "type": "object",
                    "properties": {
                        "record_type": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1},
                    },
                    "additionalProperties": False,
                },
            ),
            _tool_definition(
                "memory_open_loops",
                "List open loops recorded in memory.",
                {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "minimum": 1},
                    },
                    "additionalProperties": False,
                },
            ),
            _tool_definition(
                "memory_trace",
                "Read a run trace by run_id.",
                {
                    "type": "object",
                    "properties": {
                        "run_id": {"type": "string"},
                    },
                    "required": ["run_id"],
                    "additionalProperties": False,
                },
            ),
            _tool_definition(
                "bridge_recipe_note_to_memory",
                "Promote a recipe note into the curated memory layer.",
                {
                    "type": "object",
                    "properties": {
                        "file_id": {"type": "string"},
                        "record_type": {"type": "string"},
                        "title": {"type": "string"},
                        "trust_level": {"type": "string"},
                        "subject": {"type": "string"},
                        "subject_kind": {"type": "string"},
                    },
                    "required": ["file_id"],
                    "additionalProperties": False,
                },
            ),
        ]

    def dispatch_jsonrpc(self, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = payload.get("id")
        method = payload.get("method")
        params = payload.get("params") or {}
        run = self.logger.start(f"home-mcp:{method or 'unknown'}", {"method": method, "params": params})
        trace = HomeMCPTraceWriter(logger=self.logger, run=run, command=f"home-mcp:{method or 'unknown'}", argv=[], config_path=self.config.path)
        trace.trace("receive_request", "Received JSON-RPC request.", details={"method": method})
        if payload.get("jsonrpc") != "2.0":
            response = _jsonrpc_error(request_id, -32600, "Invalid Request", {"message": "jsonrpc must be '2.0'"})
            trace.trace("render_response", "Returning JSON-RPC error.", level="error", details={"response": response})
            trace.write_json("result.json", response)
            trace.finish(status="error", result=response, error=response.get("error"))
            return response
        try:
            if method in {"initialize", "mcp/initialize"}:
                result = {
                    "protocolVersion": "2024-11-05",
                    "serverInfo": {"name": "home-mcp", "version": "0.1.0"},
                    "capabilities": {"tools": {}, "logging": {}},
                    "instructions": "Use the allowlisted roots only. Read, search, and create notes via tools.",
                    "authentication": {
                        "mode": self.auth_mode,
                        "tokenRequired": self.auth_mode in {"bearer"},
                        "proxyHandled": self.auth_mode == "oauth",
                    },
                    "roots": self.list_allowed_roots(),
                }
                response = _jsonrpc_ok(request_id, result)
                trace.trace("render_response", "Returning initialize response.", details={"roots": len(self.root_specs)})
                trace.write_json("result.json", response)
                trace.finish(status="ok", result=response)
                return response
            if method in {"tools/list", "mcp/tools/list"}:
                response = _jsonrpc_ok(request_id, {"tools": self.tools()})
                trace.trace("render_response", "Returning tool list.", details={"tools": len(self.tools())})
                trace.write_json("result.json", response)
                trace.finish(status="ok", result=response)
                return response
            if method in {"tools/call", "mcp/tools/call"}:
                tool_name = params.get("name")
                arguments = params.get("arguments") or {}
                if not tool_name:
                    raise HomeMCPError("tool name is required", stage="tools/call", error_code="missing_tool_name")
                trace.trace("call_tool", "Dispatching tool call.", details={"tool": tool_name})
                result = self.call_tool(str(tool_name), dict(arguments))
                response = _jsonrpc_ok(request_id, _format_tool_result(result))
                trace.trace("render_response", "Returning tool result.", details={"tool": tool_name})
                trace.write_json("result.json", response)
                trace.finish(status="ok", result=response)
                return response
            if method == "roots/list":
                response = _jsonrpc_ok(request_id, self.list_allowed_roots())
                trace.trace("render_response", "Returning roots list.", details={"roots": len(self.root_specs)})
                trace.write_json("result.json", response)
                trace.finish(status="ok", result=response)
                return response
            raise HomeMCPError(f"unsupported method: {method}", stage="dispatch", error_code="unsupported_method")
        except HomeMCPError as exc:
            response = _jsonrpc_error(request_id, -32000, str(exc), exc.to_dict())
            trace.trace("dispatch_error", str(exc), level="error", details={"error": exc.to_dict()})
            trace.write_json("result.json", response)
            trace.finish(status="error", result=response, error=exc.to_dict())
            return response
        except Exception as exc:
            error = {
                "message": str(exc),
                "stage": "dispatch",
                "error_code": "unexpected_error",
                "source_ref": None,
            }
            response = _jsonrpc_error(request_id, -32000, str(exc), error)
            trace.trace("dispatch_error", str(exc), level="error", details={"error": error})
            trace.write_json("result.json", response)
            trace.finish(status="error", result=response, error=error)
            return response

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "list_allowed_roots":
            return self.list_allowed_roots()
        if name == "list_files":
            return self.list_files(
                root_id=str(arguments["root_id"]),
                glob=str(arguments.get("glob") or "*"),
                recursive=bool(arguments.get("recursive", True)),
                limit=int(arguments.get("limit", 100)),
            )
        if name == "search_files":
            file_types = arguments.get("file_types")
            return self.search_files(
                query=str(arguments["query"]),
                root_id=arguments.get("root_id"),
                file_types=[str(item) for item in file_types] if isinstance(file_types, list) else None,
                limit=int(arguments.get("limit", 10)),
            )
        if name == "search_notes":
            return self.search_notes(
                query=str(arguments["query"]),
                root_id=str(arguments["root_id"]) if arguments.get("root_id") else None,
                limit=int(arguments.get("limit", 10)),
            )
        if name == "list_recent_files":
            file_types = arguments.get("file_types")
            return self.list_recent_files(
                root_id=str(arguments["root_id"]),
                limit=int(arguments.get("limit", 20)),
                file_types=[str(item) for item in file_types] if isinstance(file_types, list) else None,
            )
        if name == "search_recipes":
            return self.search_recipes(
                query=str(arguments["query"]) if arguments.get("query") is not None else None,
                tags=[str(item) for item in arguments.get("tags", [])] if isinstance(arguments.get("tags"), list) else None,
                limit=int(arguments.get("limit", 10)),
            )
        if name == "get_recipe":
            return self.get_recipe(recipe_id=str(arguments["recipe_id"]))
        if name == "browse_recipes":
            return self.browse_recipes(
                query=str(arguments["query"]) if arguments.get("query") is not None else None,
                tags=[str(item) for item in arguments.get("tags", [])] if isinstance(arguments.get("tags"), list) else None,
                limit=int(arguments.get("limit", 10)),
            )
        if name == "draft_recipe_card":
            return self.draft_recipe_card(
                source_text=str(arguments["source_text"]) if arguments.get("source_text") is not None else None,
                file_id=str(arguments["file_id"]) if arguments.get("file_id") else None,
                title=str(arguments["title"]) if arguments.get("title") else None,
                query=str(arguments["query"]) if arguments.get("query") else None,
            )
        if name == "read_file":
            return self.read_file(
                file_id=arguments.get("file_id"),
                root_id=arguments.get("root_id"),
                relative_path=arguments.get("relative_path"),
                start_line=_optional_int(arguments.get("start_line")),
                end_line=_optional_int(arguments.get("end_line")),
            )
        if name == "create_markdown_note":
            return self.create_markdown_note(
                root_id=str(arguments["root_id"]),
                folder=str(arguments.get("folder") or ""),
                title=str(arguments["title"]),
                body=str(arguments["body"]),
                tags=[str(item) for item in arguments.get("tags", [])] if isinstance(arguments.get("tags"), list) else None,
                filename=str(arguments["filename"]) if arguments.get("filename") else None,
                metadata=arguments.get("metadata") if isinstance(arguments.get("metadata"), dict) else None,
            )
        if name == "append_markdown_log":
            return self.append_markdown_log(
                file_id=arguments.get("file_id"),
                root_id=arguments.get("root_id"),
                relative_path=arguments.get("relative_path"),
                entry=str(arguments["entry"]),
                tags=[str(item) for item in arguments.get("tags", [])] if isinstance(arguments.get("tags"), list) else None,
            )
        if name == "create_recipe":
            return self.create_recipe(
                title=str(arguments["title"]),
                body=str(arguments["body"]),
                tags=[str(item) for item in arguments.get("tags", [])] if isinstance(arguments.get("tags"), list) else None,
                metadata=arguments.get("metadata") if isinstance(arguments.get("metadata"), dict) else None,
            )
        if name == "recipe_standard":
            return self.recipe_standard()
        if name == "create_recipe_card":
            return self.create_recipe_card(
                title=str(arguments["title"]),
                body=str(arguments["body"]) if arguments.get("body") is not None else None,
                ingredients=[str(item) for item in arguments.get("ingredients", [])] if isinstance(arguments.get("ingredients"), list) else None,
                steps=[str(item) for item in arguments.get("steps", [])] if isinstance(arguments.get("steps"), list) else None,
                servings=str(arguments["servings"]) if arguments.get("servings") else None,
                prep_time=str(arguments["prep_time"]) if arguments.get("prep_time") else None,
                cook_time=str(arguments["cook_time"]) if arguments.get("cook_time") else None,
                total_time=str(arguments["total_time"]) if arguments.get("total_time") else None,
                notes=str(arguments["notes"]) if arguments.get("notes") else None,
                source_file_id=str(arguments["source_file_id"]) if arguments.get("source_file_id") else None,
                source_query=str(arguments["source_query"]) if arguments.get("source_query") else None,
                summary=str(arguments["summary"]) if arguments.get("summary") else None,
                tags=[str(item) for item in arguments.get("tags", [])] if isinstance(arguments.get("tags"), list) else None,
                metadata=arguments.get("metadata") if isinstance(arguments.get("metadata"), dict) else None,
            )
        if name == "append_recipe_attempt":
            return self.append_recipe_attempt(
                recipe_id=str(arguments["recipe_id"]),
                notes=str(arguments["notes"]),
                outcome=str(arguments["outcome"]) if arguments.get("outcome") else None,
                next_time=str(arguments["next_time"]) if arguments.get("next_time") else None,
                tags=[str(item) for item in arguments.get("tags", [])] if isinstance(arguments.get("tags"), list) else None,
            )
        if name == "compare_recipe_attempts":
            return self.compare_recipe_attempts(recipe_id=str(arguments["recipe_id"]))
        if name == "create_project_note":
            return self.create_project_note(
                project_id=str(arguments["project_id"]),
                title=str(arguments["title"]),
                body=str(arguments["body"]),
                tags=[str(item) for item in arguments.get("tags", [])] if isinstance(arguments.get("tags"), list) else None,
            )
        if name == "memory_status":
            return self.memory_status(recent_limit=int(arguments.get("recent_limit", 5)))
        if name == "memory_analyze":
            return self.memory_analyze(
                subject_limit=int(arguments.get("subject_limit", 10)),
                recent_limit=int(arguments.get("recent_limit", 5)),
            )
        if name == "memory_subjects":
            return self.memory_subjects(
                kind=str(arguments["kind"]) if arguments.get("kind") else None,
                limit=_optional_int(arguments.get("limit")),
            )
        if name == "memory_candidates":
            assistant_suggestion = arguments.get("assistant_suggestion")
            return self.memory_candidates(
                review_status=str(arguments["review_status"]) if arguments.get("review_status") is not None else "pending",
                domain=str(arguments["domain"]) if arguments.get("domain") else None,
                source_role=str(arguments["source_role"]) if arguments.get("source_role") else None,
                assistant_suggestion=bool(assistant_suggestion) if assistant_suggestion is not None else None,
                subject=str(arguments["subject"]) if arguments.get("subject") else None,
                subject_kind=str(arguments["subject_kind"]) if arguments.get("subject_kind") else None,
                limit=_optional_int(arguments.get("limit")),
            )
        if name == "memory_review_subjects":
            return self.memory_review_subjects(
                subject=str(arguments["subject"]) if arguments.get("subject") else None,
                kind=str(arguments["kind"]) if arguments.get("kind") else "subject",
                review_status=str(arguments["review_status"]) if arguments.get("review_status") is not None else "pending",
                source_role=str(arguments["source_role"]) if arguments.get("source_role") else None,
                assistant_only=bool(arguments.get("assistant_only", False)),
                subject_limit=int(arguments.get("subject_limit", 20)),
                candidate_limit=int(arguments.get("candidate_limit", 20)),
            )
        if name == "memory_review":
            return self.memory_review(
                candidate_id=str(arguments["candidate_id"]) if arguments.get("candidate_id") else None,
                action=str(arguments["action"]) if arguments.get("action") else None,
                review_status=str(arguments["review_status"]) if arguments.get("review_status") is not None else "pending",
                domain=str(arguments["domain"]) if arguments.get("domain") else None,
                subject=str(arguments["subject"]) if arguments.get("subject") else None,
                subject_kind=str(arguments["subject_kind"]) if arguments.get("subject_kind") else "subject",
                source_role=str(arguments["source_role"]) if arguments.get("source_role") else None,
                assistant_only=bool(arguments.get("assistant_only", False)),
                limit=_optional_int(arguments.get("limit")),
                note=str(arguments["note"]) if arguments.get("note") else None,
                record_type=str(arguments["record_type"]) if arguments.get("record_type") else None,
                title=str(arguments["title"]) if arguments.get("title") else None,
                trust_level=str(arguments["trust_level"]) if arguments.get("trust_level") else "high",
                allow_assistant=bool(arguments.get("allow_assistant", False)),
            )
        if name == "memory_search":
            exclude_source_ids = arguments.get("exclude_source_ids")
            exclude_subjects = arguments.get("exclude_subjects")
            return self.memory_search(
                query=str(arguments["query"]),
                limit=int(arguments.get("limit", 8)),
                subject=str(arguments["subject"]) if arguments.get("subject") else None,
                title=str(arguments["title"]) if arguments.get("title") else None,
                date_from=str(arguments["date_from"]) if arguments.get("date_from") else None,
                date_to=str(arguments["date_to"]) if arguments.get("date_to") else None,
                exclude_source_ids=[str(item) for item in exclude_source_ids] if isinstance(exclude_source_ids, list) else None,
                exclude_subjects=[str(item) for item in exclude_subjects] if isinstance(exclude_subjects, list) else None,
                depth=str(arguments.get("depth", "medium")),
                effort=int(arguments.get("effort", 2)),
                allow_cross_domain=bool(arguments.get("allow_cross_domain", False)),
            )
        if name == "memory_list":
            return self.memory_list(
                record_type=str(arguments["record_type"]) if arguments.get("record_type") else None,
                limit=_optional_int(arguments.get("limit")),
            )
        if name == "memory_open_loops":
            return self.memory_open_loops(limit=_optional_int(arguments.get("limit")))
        if name == "memory_trace":
            return self.memory_trace(run_id=str(arguments["run_id"]))
        if name == "bridge_recipe_note_to_memory":
            return self.bridge_recipe_note_to_memory(
                file_id=str(arguments["file_id"]),
                record_type=str(arguments["record_type"]) if arguments.get("record_type") else "research_note",
                title=str(arguments["title"]) if arguments.get("title") else None,
                trust_level=str(arguments["trust_level"]) if arguments.get("trust_level") else "high",
                subject=str(arguments["subject"]) if arguments.get("subject") else None,
                subject_kind=str(arguments["subject_kind"]) if arguments.get("subject_kind") else "subject",
            )
        raise HomeMCPError(f"unsupported tool: {name}", stage="tools/call", error_code="unsupported_tool", source_ref=name)

    def _resolve_file_reference(
        self,
        *,
        file_id: str | None = None,
        root_id: str | None = None,
        relative_path: str | None = None,
    ) -> tuple[Path, RootSpec]:
        if file_id:
            root_name, sep, rel = file_id.partition(":")
            if not sep:
                raise HomeMCPError("file_id must be of the form root_id:relative/path.md", stage="resolve_file", error_code="invalid_file_id", source_ref=file_id)
            root_id = root_name
            relative_path = rel
        if not root_id or relative_path is None:
            raise HomeMCPError("file reference requires file_id or root_id + relative_path", stage="resolve_file", error_code="missing_file_reference")
        root = self._get_root(str(root_id))
        resolved = _resolve_safe_path(root.path, relative_path, allow_hidden=False)
        if not resolved.exists():
            raise HomeMCPError("file does not exist", stage="resolve_file", error_code="file_not_found", source_ref=str(resolved))
        return resolved, root

    def _get_root(self, root_id: str) -> RootSpec:
        if root_id not in self.roots_by_id:
            raise HomeMCPError(f"unknown root: {root_id}", stage="resolve_root", error_code="unknown_root", source_ref=root_id)
        return self.roots_by_id[root_id]

    def _sanitized_folder(self, root: RootSpec, folder: str) -> Path:
        folder_path = root.path
        if folder.strip():
            folder_path = _resolve_safe_path(root.path, folder, allow_hidden=False, create_ok=True)
        return folder_path

    def _unique_markdown_path(self, folder_path: Path, title_or_filename: str) -> Path:
        base = _slugify(Path(title_or_filename).stem if title_or_filename.endswith(".md") else title_or_filename)
        if not base:
            base = "note"
        candidate = folder_path / f"{base}.md"
        suffix = 2
        while candidate.exists():
            candidate = folder_path / f"{base}-{suffix}.md"
            suffix += 1
        return candidate

    def _write_result(
        self,
        root: RootSpec,
        path: Path,
        action: str,
        details: dict[str, Any],
        *,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "status": "ok",
            "file_id": _file_id(root, path),
            "root_id": root.id,
            "relative_path": str(path.relative_to(root.path)),
            "path": str(path),
            "action": action,
            "details": details,
            **(extra or {}),
        }


def serve_home_mcp(server: HomeMCPServer, *, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def _metadata_request_url(self) -> str:
            scheme = self.headers.get("X-Forwarded-Proto", "http")
            host_header = self.headers.get("Host") or f"{host}:{port}"
            return f"{scheme}://{host_header}"

        def _json_public(self, payload: dict[str, Any], *, status_code: int = 200) -> None:
            body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path in {"/", "/health", "/mcp"}:
                self._json_public(
                    {
                        "status": "ok",
                        "server": "home-mcp",
                        "endpoint": "/mcp",
                        "authentication": {
                            "mode": server.auth_mode,
                            "tokenRequired": server.auth_mode == "bearer",
                            "proxyHandled": server.auth_mode == "oauth",
                        },
                        "roots": server.list_allowed_roots(),
                    }
                )
                return
            if parsed.path in HOME_MCP_OAUTH_RESOURCE_PATHS:
                self._json_public(server.oauth_protected_resource_metadata(request_url=self._metadata_request_url()))
                return
            if parsed.path in HOME_MCP_OAUTH_AUTH_SERVER_PATHS:
                self._json_public(server.oauth_authorization_server_metadata())
                return
            self._json_public({"error": "not_found"}, status_code=404)

        def do_HEAD(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path in {"/", "/health", "/mcp"} | HOME_MCP_OAUTH_RESOURCE_PATHS | HOME_MCP_OAUTH_AUTH_SERVER_PATHS:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                return
            self.send_response(404)
            self.end_headers()

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path != "/mcp":
                self._json_response(404, {"error": "not_found"})
                return
            if server.auth_mode == "bearer":
                if not _matches_bearer_auth(self.headers.get("Authorization", ""), server.auth_token):
                    self._json_response(401, {"error": "unauthorized"})
                    return
            elif server.auth_mode == "mixed":
                if server.auth_token and not _matches_bearer_auth(self.headers.get("Authorization", ""), server.auth_token):
                    # Mixed mode allows unauthenticated access for the connector path but still
                    # accepts a bearer token when an admin client presents one.
                    pass
            content_length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(content_length)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except Exception:
                self._json_response(400, {"error": "invalid_json"})
                return
            response = server.dispatch_jsonrpc(payload)
            self._json_response(200, response)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            return

        def _json_response(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = ThreadingHTTPServer((host, port), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


def build_home_mcp_server(
    config: AppConfig,
    *,
    auth_mode: str | None = None,
    auth_token: str | None = None,
) -> HomeMCPServer:
    return HomeMCPServer.from_config(config, auth_mode=auth_mode, auth_token=auth_token)


def _build_root_specs(base_dir: Path, roots_payload: dict[str, Any] | None = None) -> list[RootSpec]:
    specs: list[RootSpec] = []
    if roots_payload:
        for root_id, payload in roots_payload.items():
            if isinstance(payload, dict):
                rel_path = payload.get("path", "")
                writable = bool(payload.get("writable", True))
                kind = str(payload.get("kind", "notes"))
                notes = str(payload.get("notes", ""))
            else:
                rel_path = payload
                writable = True
                kind = "notes"
                notes = ""
            path = _resolve_path(base_dir, rel_path)
            specs.append(RootSpec(id=str(root_id), path=path, writable=writable, kind=kind, notes=notes))
        return specs
    for root_id, (relative_path, writable, notes) in DEFAULT_HOME_MCP_ROOTS.items():
        specs.append(RootSpec(id=root_id, path=_resolve_path(base_dir, relative_path), writable=writable, kind="notes", notes=notes))
    return specs


def _iter_text_files(root: Path, *, file_types: list[str] | None = None, limit: int = MAX_SEARCH_FILES):
    allowed_suffixes = {suffix.lower() for suffix in file_types} if file_types else TEXT_SUFFIXES
    count = 0
    for path in root.rglob("*"):
        if count >= limit:
            break
        if not path.is_file():
            continue
        if _is_hidden_path(path, root):
            continue
        if path.suffix.lower() not in allowed_suffixes:
            continue
        count += 1
        yield path


def _file_metadata(root: RootSpec, path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "file_id": _file_id(root, path),
        "root_id": root.id,
        "relative_path": str(path.relative_to(root.path)),
        "path": str(path),
        "size_bytes": stat.st_size,
        "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        "writable": root.writable,
        "kind": root.kind,
    }


def _file_id(root: RootSpec, path: Path) -> str:
    return f"{root.id}:{path.relative_to(root.path).as_posix()}"


def _resolve_safe_path(root: Path, relative_path: str, *, allow_hidden: bool = False, create_ok: bool = False) -> Path:
    fragment = PurePosixPath(relative_path)
    if fragment.is_absolute() or any(part == ".." for part in fragment.parts):
        raise HomeMCPError("path escapes allowlisted root", stage="path_safety", error_code="path_escape", source_ref=relative_path)
    if not allow_hidden and any(part.startswith(".") for part in fragment.parts if part not in {"", "."}):
        raise HomeMCPError("hidden paths are blocked by policy", stage="path_safety", error_code="hidden_path_blocked", source_ref=relative_path)
    resolved = (root / Path(fragment.as_posix())).resolve(strict=False)
    if not _within_root(resolved, root):
        raise HomeMCPError("path escapes allowlisted root", stage="path_safety", error_code="path_escape", source_ref=str(resolved))
    if create_ok:
        resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def _within_root(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _is_hidden_path(path: Path, root: Path) -> bool:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return True
    return any(part.startswith(".") for part in rel.parts)


def _resolve_path(base_dir: Path, value: Any) -> Path:
    path = Path(str(value))
    if not path.is_absolute():
        path = (base_dir / path).resolve()
    return path


def _slugify(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return normalized or "note"


def _frontmatter(payload: dict[str, Any]) -> str:
    return "---\n" + json.dumps(payload, indent=2, sort_keys=True) + "\n---"


def _parse_markdown_document(text: str) -> tuple[dict[str, Any], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    end_index = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end_index = index
            break
    if end_index is None:
        return {}, text
    raw_metadata = "\n".join(lines[1:end_index]).strip()
    try:
        metadata = yaml.safe_load(raw_metadata) if raw_metadata else {}
    except Exception:
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    body = "\n".join(lines[end_index + 1 :])
    return metadata, body


def _extract_recipe_structure(text: str, *, title: str | None = None, query: str | None = None) -> dict[str, Any]:
    lines = [line.rstrip() for line in text.splitlines()]
    if not any(line.strip() for line in lines):
        inferred_title = title or query or "Recipe"
        return {
            "title": inferred_title,
            "ingredients": [],
            "steps": [],
            "servings": None,
            "prep_time": None,
            "cook_time": None,
            "total_time": None,
            "notes": "",
            "summary": "",
            "confidence": 0.0,
            "tags": [],
        }
    section_map = {
        "ingredients": "ingredients",
        "ingredient": "ingredients",
        "steps": "steps",
        "step": "steps",
        "step by step instructions": "steps",
        "step by step": "steps",
        "directions": "steps",
        "direction": "steps",
        "instructions": "steps",
        "method": "steps",
        "notes": "notes",
        "note": "notes",
        "servings": "servings",
        "yield": "servings",
        "makes": "servings",
        "prep time": "prep_time",
        "preparation time": "prep_time",
        "cook time": "cook_time",
        "cooking time": "cook_time",
        "total time": "total_time",
    }
    current_section = "notes"
    sections: dict[str, list[str]] = {key: [] for key in {"ingredients", "steps", "notes"}}
    scalar_fields: dict[str, str] = {}
    inferred_title = title
    step_heading_mode = False
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            if current_section == "notes":
                sections[current_section].append("")
            continue
        heading_match = re.match(r"^(#{1,6})\s+(.*)$", line)
        if heading_match:
            heading = heading_match.group(2).strip()
            normalized = re.sub(r"[^a-z0-9]+", " ", heading.lower()).strip()
            if normalized in section_map:
                current_section = section_map[normalized]
                if current_section == "steps":
                    step_heading_mode = False
                if current_section not in sections and current_section not in {"servings", "prep_time", "cook_time", "total_time"}:
                    sections[current_section] = []
                continue
            if inferred_title is None:
                inferred_title = heading
                continue
            if current_section == "ingredients":
                continue
            if current_section == "steps":
                step_heading_mode = True
                step_title = heading
                if re.match(r"^\d+[\.\)]\s+", step_title):
                    step_title = re.sub(r"^\d+[\.\)]\s+", "", step_title).strip()
                if step_title:
                    sections["steps"].append(step_title)
                continue
        if inferred_title is None and raw_line == lines[0] and len(raw_line.split()) <= 16 and not raw_line.startswith(("-", "*", "1.")):
            inferred_title = raw_line.strip("# ").strip()
            continue
        if line.endswith(":"):
            normalized = re.sub(r"[^a-z0-9]+", " ", line[:-1].lower()).strip()
            if normalized in section_map:
                mapped = section_map[normalized]
                if mapped in {"ingredients", "steps", "notes"}:
                    current_section = mapped
                    if current_section == "steps":
                        step_heading_mode = False
                    if current_section not in sections:
                        sections[current_section] = []
                    continue
        if ":" in line:
            key, value = [item.strip() for item in line.split(":", 1)]
            normalized = re.sub(r"[^a-z0-9]+", " ", key.lower()).strip()
            if normalized in section_map and section_map[normalized] not in {"ingredients", "steps", "notes"}:
                scalar_fields[section_map[normalized]] = value
                continue
        if current_section == "ingredients" and (line.startswith("###") or line.startswith("##")):
            continue
        if current_section == "steps" and step_heading_mode:
            continue
        if current_section in {"ingredients", "steps", "notes"}:
            sections[current_section].append(raw_line.rstrip())
        else:
            sections.setdefault("notes", []).append(raw_line.rstrip())
    inferred_title = inferred_title or title or query or "Recipe"
    ingredients = _normalize_recipe_lines(sections.get("ingredients", []), bullet_prefixes=("-", "*"))
    steps = _normalize_recipe_steps(sections.get("steps", []))
    notes = "\n".join(line for line in sections.get("notes", []) if line.strip()).strip()
    summary_bits: list[str] = []
    if ingredients:
        summary_bits.append(f"{len(ingredients)} ingredients")
    if steps:
        summary_bits.append(f"{len(steps)} steps")
    if scalar_fields.get("servings"):
        summary_bits.append(f"servings {scalar_fields['servings']}")
    elif any(re.search(r"\b(serves|servings|yield|makes)\b", line.lower()) for line in lines):
        summary_bits.append("servings noted")
    summary = ", ".join(summary_bits)
    tags: list[str] = []
    if ingredients:
        tags.append("ingredients")
    if steps:
        tags.append("instructions")
    if summary:
        tags.append("structured")
    confidence = 0.2
    if ingredients:
        confidence += 0.3
    if steps:
        confidence += 0.3
    if summary_bits:
        confidence += 0.1
    return {
        "title": inferred_title,
        "ingredients": ingredients,
        "steps": steps,
        "servings": scalar_fields.get("servings"),
        "prep_time": scalar_fields.get("prep_time"),
        "cook_time": scalar_fields.get("cook_time"),
        "total_time": scalar_fields.get("total_time"),
        "notes": notes,
        "summary": summary,
        "confidence": round(min(confidence, 0.98), 2),
        "tags": tags,
    }


def _normalize_recipe_lines(lines: list[str], *, bullet_prefixes: tuple[str, ...]) -> list[str]:
    items: list[str] = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line.startswith(bullet_prefixes):
            line = line.lstrip("-*").strip()
        if re.match(r"^\d+[\.\)]\s+", line):
            line = re.sub(r"^\d+[\.\)]\s+", "", line).strip()
        if line:
            items.append(line)
    return items


def _normalize_recipe_steps(lines: list[str]) -> list[str]:
    steps = _normalize_recipe_lines(lines, bullet_prefixes=("-", "*"))
    if steps:
        return steps
    fallback = [line.strip() for line in lines if line.strip()]
    return fallback


def _render_recipe_card_body(
    title: str,
    *,
    summary: str | None = None,
    ingredients: list[str] | None = None,
    steps: list[str] | None = None,
    servings: str | None = None,
    prep_time: str | None = None,
    cook_time: str | None = None,
    total_time: str | None = None,
    notes: str | None = None,
    tags: list[str] | None = None,
    source: str | None = None,
) -> str:
    lines: list[str] = [f"# {title}"]
    if summary:
        lines.append("")
        lines.append(summary.strip())
    if servings or prep_time or cook_time or total_time or tags:
        lines.append("")
        lines.append("## At a glance")
        if servings:
            lines.append(f"- Yield: {servings}")
        if prep_time:
            lines.append(f"- Prep time: {prep_time}")
        if cook_time:
            lines.append(f"- Cook time: {cook_time}")
        if total_time:
            lines.append(f"- Total time: {total_time}")
        if tags:
            joined_tags = ", ".join(tag for tag in tags if str(tag).strip())
            if joined_tags:
                lines.append(f"- Tags: {joined_tags}")
    if ingredients:
        lines.append("")
        lines.append("## Ingredients")
        lines.extend(f"- {item}" for item in ingredients if item.strip())
    if steps:
        lines.append("")
        lines.append("## Method")
        lines.extend(f"{index + 1}. {item}" for index, item in enumerate(steps) if item.strip())
    if notes:
        lines.append("")
        lines.append("## Notes")
        lines.append(notes.strip())
    if source:
        lines.append("")
        lines.append("## Source")
        source_lines = [line.strip() for line in str(source).splitlines() if line.strip()]
        lines.extend(f"- {line}" if not line.startswith("-") else line for line in source_lines)
    return "\n".join(line for line in lines if line is not None).strip() + "\n"


def _make_snippet(text: str, term: str, width: int = 220) -> str:
    lowered = text.lower()
    index = lowered.find(term.lower())
    if index < 0:
        return text[:width]
    start = max(index - width // 3, 0)
    end = min(start + width, len(text))
    snippet = text[start:end]
    if start > 0:
        snippet = "..." + snippet
    if end < len(text):
        snippet = snippet + "..."
    return snippet


def _extract_recipe_attempts(body: str) -> list[dict[str, Any]]:
    attempts: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw_line in body.splitlines():
        line = raw_line.strip()
        heading = re.match(r"^##\s+(.+)$", line)
        if heading:
            if current and _is_recipe_attempt(current):
                attempts.append(_finalize_recipe_attempt(current))
            current = {"heading": heading.group(1).strip(), "tags": [], "notes": [], "outcome": None, "next_time": None}
            continue
        if current is None:
            continue
        if line.lower().startswith("tags:"):
            tags = [tag.strip() for tag in line.split(":", 1)[1].split(",") if tag.strip()]
            current["tags"] = tags
            continue
        if line.lower().startswith("outcome:"):
            current["outcome"] = line.split(":", 1)[1].strip()
            continue
        if line.lower().startswith("next time:"):
            current["next_time"] = line.split(":", 1)[1].strip()
            continue
        if line:
            current["notes"].append(line)
    if current and _is_recipe_attempt(current):
        attempts.append(_finalize_recipe_attempt(current))
    return attempts


def _is_recipe_attempt(attempt: dict[str, Any]) -> bool:
    tags = {str(tag).lower() for tag in attempt.get("tags", [])}
    return "recipe_attempt" in tags or bool(attempt.get("outcome")) or bool(attempt.get("next_time"))


def _finalize_recipe_attempt(attempt: dict[str, Any]) -> dict[str, Any]:
    return {
        "heading": attempt.get("heading"),
        "tags": attempt.get("tags", []),
        "notes": "\n".join(str(line) for line in attempt.get("notes", [])).strip(),
        "outcome": attempt.get("outcome"),
        "next_time": attempt.get("next_time"),
    }


def _compare_recipe_attempts(attempts: list[dict[str, Any]]) -> dict[str, Any]:
    if not attempts:
        return {"summary": "No recipe attempts recorded.", "latest_outcome": None, "latest_next_time": None, "changes": []}
    changes: list[str] = []
    previous_notes = ""
    for index, attempt in enumerate(attempts, start=1):
        notes = str(attempt.get("notes") or "").strip()
        if index == 1:
            changes.append(f"Attempt {index}: initial logged attempt.")
        elif notes and notes != previous_notes:
            changes.append(f"Attempt {index}: notes changed from previous attempt.")
        elif notes:
            changes.append(f"Attempt {index}: notes repeated the previous attempt.")
        if attempt.get("outcome"):
            changes.append(f"Attempt {index}: outcome - {attempt['outcome']}")
        if attempt.get("next_time"):
            changes.append(f"Attempt {index}: next time - {attempt['next_time']}")
        previous_notes = notes
    latest = attempts[-1]
    return {
        "summary": f"{len(attempts)} recipe attempt{'s' if len(attempts) != 1 else ''} recorded.",
        "latest_outcome": latest.get("outcome"),
        "latest_next_time": latest.get("next_time"),
        "changes": changes,
    }


def _tool_definition(name: str, description: str, input_schema: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "description": description, "inputSchema": input_schema}


def _format_tool_result(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(result, indent=2, sort_keys=True),
            }
        ],
        "isError": result.get("status") == "error",
        "structuredContent": result,
    }


def _matches_bearer_auth(header_value: str, token: str | None) -> bool:
    if not token:
        return False
    return header_value == f"Bearer {token}"


def _jsonrpc_ok(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _jsonrpc_error(request_id: Any, code: int, message: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
    if data is not None:
        payload["error"]["data"] = data
    return payload


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
