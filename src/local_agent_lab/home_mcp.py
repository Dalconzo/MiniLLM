from __future__ import annotations

import json
import os
import re
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
from .tools.file_tools import redact_text


DEFAULT_HOME_MCP_BASE_DIR = "data/home_mcp"
DEFAULT_HOME_MCP_ROOTS = {
    "recipe_book": ("recipes", True, "Recipe notes and attempts"),
    "household": ("household", True, "Household notes and checklists"),
    "projects": ("projects", True, "Project notes and planning"),
    "inbox": ("inbox", True, "Quick capture and scratch notes"),
    "archive": ("archive", False, "Read-only archive space"),
}
TEXT_SUFFIXES = {".md", ".txt", ".json", ".yaml", ".yml", ".csv", ".log"}
MAX_FILE_BYTES = 128_000
MAX_SEARCH_FILES = 250
HOME_MCP_AUTH_MODES = {"none", "bearer", "oauth", "mixed"}
HOME_MCP_OAUTH_RESOURCE_PATHS = {"/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"}
HOME_MCP_OAUTH_AUTH_SERVER_PATHS = {"/.well-known/oauth-authorization-server", "/.well-known/openid-configuration"}


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

    def create_recipe_card(
        self,
        *,
        title: str,
        body: str,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.create_recipe(title=title, body=body, tags=tags, metadata=metadata)

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
            card_tags = [str(item) for item in metadata.get("tags", [])] if isinstance(metadata.get("tags"), list) else []
            card_tags_lower = {tag.lower() for tag in card_tags}
            if tag_filters and not tag_filters.issubset(card_tags_lower):
                continue
            title = str(metadata.get("title") or path.stem).strip()
            body_text = redact_text(body)
            combined_text = " ".join([title, " ".join(card_tags), body_text, str(metadata.get("kind", "")), str(metadata.get("summary", ""))]).lower()
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

    def tools(self) -> list[dict[str, Any]]:
        return [
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
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "metadata": {"type": "object"},
                    },
                    "required": ["title", "body"],
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
        if name == "search_recipes":
            return self.search_recipes(
                query=str(arguments["query"]) if arguments.get("query") is not None else None,
                tags=[str(item) for item in arguments.get("tags", [])] if isinstance(arguments.get("tags"), list) else None,
                limit=int(arguments.get("limit", 10)),
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
        if name == "create_recipe_card":
            return self.create_recipe_card(
                title=str(arguments["title"]),
                body=str(arguments["body"]),
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
