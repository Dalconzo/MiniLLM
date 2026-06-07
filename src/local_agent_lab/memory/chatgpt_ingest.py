from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..indexing.chunker import chunk_text
from .observability import MemoryObservationError, memory_db_path, utc_now


PARSER_VERSION = "chatgpt_export_v1"
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ParsedExport:
    import_record: dict[str, Any]
    conversations: list[dict[str, Any]]
    messages: list[dict[str, Any]]
    chunks: list[dict[str, Any]]
    attachments: list[dict[str, Any]]
    conversation_files: list[dict[str, Any]]


def import_chatgpt_export(*, input_path: Path, data_dir: Path, memory_dir: Path) -> dict[str, Any]:
    parsed = parse_chatgpt_export(input_path)
    parsed_dir = data_dir / "chatgpt_exports" / "parsed" / parsed.import_record["id"]
    parsed_dir.mkdir(parents=True, exist_ok=True)
    sqlite_path = memory_db_path(memory_dir)
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        _write_jsonl(parsed_dir / "conversations.jsonl", parsed.conversations)
        _write_jsonl(parsed_dir / "messages.jsonl", parsed.messages)
        _write_jsonl(parsed_dir / "chunks.jsonl", parsed.chunks)
        _write_jsonl(parsed_dir / "attachments.jsonl", parsed.attachments)
    except OSError as exc:
        raise MemoryObservationError(
            str(exc),
            stage="write_jsonl",
            error_code="jsonl_write_failed",
            source_ref=str(parsed_dir),
        ) from exc

    report = _build_report(
        status="ok",
        dry_run=False,
        input_path=input_path,
        parsed_dir=parsed_dir,
        sqlite_path=sqlite_path,
        parsed=parsed,
    )
    (parsed_dir / "import_report.json").write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")

    try:
        with sqlite3.connect(sqlite_path) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            init_chatgpt_memory_schema(connection)
            replace_import(connection, parsed)
    except sqlite3.Error as exc:
        raise MemoryObservationError(
            str(exc),
            stage="write_sqlite",
            error_code="sqlite_write_failed",
            source_ref=str(sqlite_path),
        ) from exc

    return report


def parse_chatgpt_export(input_path: Path) -> ParsedExport:
    if not input_path.exists():
        raise MemoryObservationError(
            f"input path does not exist: {input_path}",
            stage="discover_input",
            error_code="input_not_found",
            source_ref=str(input_path),
        )

    conversation_files = _find_conversation_files(input_path)
    if not conversation_files:
        raise MemoryObservationError(
            "No conversations.json files found.",
            stage="discover_input",
            error_code="unsupported_export_shape",
            source_ref=str(input_path),
        )

    file_hashes = [_file_sha256(path) for path in conversation_files]
    import_id = "imp_" + _short_hash("|".join(f"{path}:{digest}" for path, digest in zip(conversation_files, file_hashes)))

    conversations: list[dict[str, Any]] = []
    messages: list[dict[str, Any]] = []
    chunks: list[dict[str, Any]] = []
    attachments: list[dict[str, Any]] = []
    file_reports: list[dict[str, Any]] = []

    for source_path, file_hash in zip(conversation_files, file_hashes):
        try:
            payload = json.loads(source_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise MemoryObservationError(
                str(exc),
                stage="parse_export",
                error_code="invalid_json",
                source_ref=str(source_path),
            ) from exc
        except (OSError, UnicodeDecodeError) as exc:
            raise MemoryObservationError(
                str(exc),
                stage="parse_export",
                error_code="parse_error",
                source_ref=str(source_path),
            ) from exc

        raw_conversations = payload if isinstance(payload, list) else [payload] if isinstance(payload, dict) else None
        if raw_conversations is None:
            raise MemoryObservationError(
                f"unsupported top-level JSON type: {type(payload).__name__}",
                stage="parse_export",
                error_code="unsupported_export_shape",
                source_ref=str(source_path),
            )

        file_reports.append(
            {
                "path": str(source_path),
                "content_sha256": file_hash,
                "conversation_count": len(raw_conversations),
            }
        )

        for raw_index, raw_conversation in enumerate(raw_conversations):
            if not isinstance(raw_conversation, dict):
                continue
            conversation_id = _conversation_id(raw_conversation, source_path, raw_index)
            message_rows = _message_rows(raw_conversation, import_id, conversation_id)
            title = str(raw_conversation.get("title") or "Untitled conversation")
            conversation = {
                "id": conversation_id,
                "import_id": import_id,
                "source_conversation_id": _optional_str(raw_conversation.get("id") or raw_conversation.get("conversation_id")),
                "title": title,
                "created_at": _timestamp(raw_conversation.get("create_time") or raw_conversation.get("created_at")),
                "updated_at": _timestamp(raw_conversation.get("update_time") or raw_conversation.get("updated_at")),
                "message_count": len(message_rows),
                "mapping_shape": "mapping" if isinstance(raw_conversation.get("mapping"), dict) else "list",
                "source_path": str(source_path),
                "content_sha256": _short_hash(json.dumps(raw_conversation, sort_keys=True, default=str), length=64),
                "metadata_json": json.dumps(
                    {
                        "raw_index": raw_index,
                        "source_file_sha256": file_hash,
                    },
                    sort_keys=True,
                ),
            }
            conversations.append(conversation)
            messages.extend(message_rows)
            chunks.extend(_chunk_rows(message_rows, import_id, conversation_id, title))

    import_record = {
        "id": import_id,
        "source_root": str(input_path),
        "raw_manifest_path": "",
        "imported_at": utc_now(),
        "status": "ok",
        "parser_version": PARSER_VERSION,
        "file_count": len(conversation_files),
        "conversation_count": len(conversations),
        "message_count": len(messages),
        "chunk_count": len(chunks),
        "attachment_count": len(attachments),
        "content_sha256": _short_hash("|".join(file_hashes), length=64),
        "notes": "",
    }
    return ParsedExport(
        import_record=import_record,
        conversations=conversations,
        messages=messages,
        chunks=chunks,
        attachments=attachments,
        conversation_files=file_reports,
    )


def init_chatgpt_memory_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL,
            checksum TEXT NOT NULL
        );

        INSERT OR IGNORE INTO schema_migrations (version, name, applied_at, checksum)
        VALUES (1, 'chatgpt_memory_initial', datetime('now'), 'v1');

        CREATE TABLE IF NOT EXISTS imports (
            id TEXT PRIMARY KEY,
            source_root TEXT NOT NULL,
            raw_manifest_path TEXT NOT NULL,
            imported_at TEXT NOT NULL,
            status TEXT NOT NULL,
            parser_version TEXT NOT NULL,
            file_count INTEGER NOT NULL,
            conversation_count INTEGER NOT NULL,
            message_count INTEGER NOT NULL,
            chunk_count INTEGER NOT NULL,
            attachment_count INTEGER NOT NULL DEFAULT 0,
            content_sha256 TEXT NOT NULL,
            notes TEXT
        );

        CREATE TABLE IF NOT EXISTS conversations (
            id TEXT PRIMARY KEY,
            import_id TEXT NOT NULL REFERENCES imports(id) ON DELETE CASCADE,
            source_conversation_id TEXT,
            title TEXT NOT NULL,
            created_at TEXT,
            updated_at TEXT,
            message_count INTEGER NOT NULL,
            first_message_at TEXT,
            last_message_at TEXT,
            summary TEXT,
            content_sha256 TEXT NOT NULL,
            is_deleted INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            import_id TEXT NOT NULL REFERENCES imports(id) ON DELETE CASCADE,
            source_message_id TEXT,
            parent_message_id TEXT,
            role TEXT NOT NULL,
            author_name TEXT,
            turn_index INTEGER NOT NULL,
            created_at TEXT,
            content_text TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            token_estimate INTEGER NOT NULL,
            attachment_count INTEGER NOT NULL DEFAULT 0,
            is_deleted INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS message_chunks (
            id TEXT PRIMARY KEY,
            message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
            conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            import_id TEXT NOT NULL REFERENCES imports(id) ON DELETE CASCADE,
            chunk_index INTEGER NOT NULL,
            text TEXT NOT NULL,
            text_sha256 TEXT NOT NULL,
            token_estimate INTEGER NOT NULL,
            start_char INTEGER NOT NULL,
            end_char INTEGER NOT NULL,
            source_kind TEXT NOT NULL,
            summary TEXT,
            is_deleted INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS attachments (
            id TEXT PRIMARY KEY,
            message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
            conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            import_id TEXT NOT NULL REFERENCES imports(id) ON DELETE CASCADE,
            source_path TEXT NOT NULL,
            filename TEXT,
            mime_type TEXT,
            size_bytes INTEGER,
            content_sha256 TEXT,
            extracted_text TEXT,
            extracted_text_sha256 TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS chatgpt_chunks_fts USING fts5(
            title,
            role,
            text,
            import_id UNINDEXED,
            conversation_id UNINDEXED,
            message_id UNINDEXED,
            chunk_id UNINDEXED,
            tokenize = 'unicode61'
        );

        CREATE INDEX IF NOT EXISTS idx_imports_imported_at ON imports(imported_at);
        CREATE INDEX IF NOT EXISTS idx_imports_status ON imports(status);
        CREATE INDEX IF NOT EXISTS idx_conversations_import_id ON conversations(import_id);
        CREATE INDEX IF NOT EXISTS idx_conversations_title ON conversations(title);
        CREATE INDEX IF NOT EXISTS idx_messages_conversation_turn ON messages(conversation_id, turn_index);
        CREATE INDEX IF NOT EXISTS idx_messages_role ON messages(role);
        CREATE INDEX IF NOT EXISTS idx_chunks_message_index ON message_chunks(message_id, chunk_index);
        CREATE INDEX IF NOT EXISTS idx_chunks_conversation ON message_chunks(conversation_id);
        CREATE INDEX IF NOT EXISTS idx_chunks_import ON message_chunks(import_id);
        """
    )
    connection.commit()


def replace_import(connection: sqlite3.Connection, parsed: ParsedExport) -> None:
    import_id = parsed.import_record["id"]
    with connection:
        connection.execute("DELETE FROM chatgpt_chunks_fts WHERE import_id = ?", (import_id,))
        connection.execute("DELETE FROM imports WHERE id = ?", (import_id,))
        connection.execute(
            """
            INSERT INTO imports (
                id, source_root, raw_manifest_path, imported_at, status, parser_version,
                file_count, conversation_count, message_count, chunk_count, attachment_count,
                content_sha256, notes
            )
            VALUES (
                :id, :source_root, :raw_manifest_path, :imported_at, :status, :parser_version,
                :file_count, :conversation_count, :message_count, :chunk_count, :attachment_count,
                :content_sha256, :notes
            )
            """,
            parsed.import_record,
        )
        connection.executemany(
            """
            INSERT INTO conversations (
                id, import_id, source_conversation_id, title, created_at, updated_at,
                message_count, first_message_at, last_message_at, summary, content_sha256,
                metadata_json
            )
            VALUES (
                :id, :import_id, :source_conversation_id, :title, :created_at, :updated_at,
                :message_count, NULL, NULL, NULL, :content_sha256, :metadata_json
            )
            """,
            parsed.conversations,
        )
        connection.executemany(
            """
            INSERT INTO messages (
                id, conversation_id, import_id, source_message_id, parent_message_id, role,
                author_name, turn_index, created_at, content_text, content_sha256,
                token_estimate, attachment_count, metadata_json
            )
            VALUES (
                :id, :conversation_id, :import_id, :source_message_id, :parent_message_id, :role,
                :author_name, :turn_index, :created_at, :content_text, :content_sha256,
                :token_estimate, :attachment_count, :metadata_json
            )
            """,
            parsed.messages,
        )
        connection.executemany(
            """
            INSERT INTO message_chunks (
                id, message_id, conversation_id, import_id, chunk_index, text, text_sha256,
                token_estimate, start_char, end_char, source_kind, summary, metadata_json
            )
            VALUES (
                :id, :message_id, :conversation_id, :import_id, :chunk_index, :text, :text_sha256,
                :token_estimate, :start_char, :end_char, :source_kind, NULL, :metadata_json
            )
            """,
            parsed.chunks,
        )
        connection.executemany(
            """
            INSERT INTO chatgpt_chunks_fts (
                title, role, text, import_id, conversation_id, message_id, chunk_id
            )
            VALUES (:title, :role, :text, :import_id, :conversation_id, :message_id, :id)
            """,
            parsed.chunks,
        )


def _message_rows(raw_conversation: dict[str, Any], import_id: str, conversation_id: str) -> list[dict[str, Any]]:
    mapping = raw_conversation.get("mapping")
    nodes = list(mapping.values()) if isinstance(mapping, dict) else []
    node_messages: list[tuple[float, int, dict[str, Any], dict[str, Any]]] = []
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            continue
        message = node.get("message")
        if not isinstance(message, dict):
            continue
        content_text = _content_text(message)
        if not content_text.strip():
            continue
        created_at = message.get("create_time") or message.get("update_time") or node.get("created_at")
        node_messages.append((_sort_time(created_at), index, node, message))

    rows: list[dict[str, Any]] = []
    for turn_index, (_sort_key, node_index, node, message) in enumerate(sorted(node_messages)):
        source_message_id = _optional_str(message.get("id") or node.get("id"))
        role = _role(message)
        content_text = _content_text(message)
        row_id = "msg_" + _short_hash(f"{conversation_id}:{source_message_id or node_index}:{content_text}")
        rows.append(
            {
                "id": row_id,
                "conversation_id": conversation_id,
                "import_id": import_id,
                "source_message_id": source_message_id,
                "parent_message_id": _optional_str(node.get("parent")),
                "role": role,
                "author_name": _author_name(message),
                "turn_index": turn_index,
                "created_at": _timestamp(message.get("create_time") or message.get("update_time")),
                "content_text": content_text,
                "content_sha256": _short_hash(content_text, length=64),
                "token_estimate": _token_estimate(content_text),
                "attachment_count": 0,
                "metadata_json": json.dumps({"node_index": node_index}, sort_keys=True),
            }
        )
    return rows


def _chunk_rows(messages: list[dict[str, Any]], import_id: str, conversation_id: str, title: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for message in messages:
        start = 0
        for chunk_index, text in enumerate(chunk_text(message["content_text"], chunk_size=1200)):
            end = start + len(text)
            chunk_id = "chk_" + _short_hash(f"{message['id']}:{chunk_index}:{text}")
            rows.append(
                {
                    "id": chunk_id,
                    "message_id": message["id"],
                    "conversation_id": conversation_id,
                    "import_id": import_id,
                    "chunk_index": chunk_index,
                    "text": text,
                    "title": title,
                    "role": message["role"],
                    "text_sha256": _short_hash(text, length=64),
                    "token_estimate": _token_estimate(text),
                    "start_char": start,
                    "end_char": end,
                    "source_kind": "chatgpt_export",
                    "metadata_json": "{}",
                }
            )
            start = end
    return rows


def _build_report(
    *,
    status: str,
    dry_run: bool,
    input_path: Path,
    parsed_dir: Path,
    sqlite_path: Path,
    parsed: ParsedExport,
) -> dict[str, Any]:
    return {
        "status": status,
        "dry_run": dry_run,
        "import_id": parsed.import_record["id"],
        "input_path": str(input_path),
        "parsed_dir": str(parsed_dir),
        "sqlite_path": str(sqlite_path),
        "parser_version": PARSER_VERSION,
        "schema_version": SCHEMA_VERSION,
        "conversation_files": parsed.conversation_files,
        "summary": {
            "conversation_files": len(parsed.conversation_files),
            "conversations": len(parsed.conversations),
            "messages": len(parsed.messages),
            "chunks": len(parsed.chunks),
            "attachments": len(parsed.attachments),
            "errors": 0,
        },
        "written_files": [
            str(parsed_dir / "conversations.jsonl"),
            str(parsed_dir / "messages.jsonl"),
            str(parsed_dir / "chunks.jsonl"),
            str(parsed_dir / "attachments.jsonl"),
            str(parsed_dir / "import_report.json"),
            str(sqlite_path),
        ],
    }


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def _find_conversation_files(input_path: Path) -> list[Path]:
    if input_path.is_file() and input_path.name == "conversations.json":
        return [input_path]
    if input_path.is_dir():
        direct = input_path / "conversations.json"
        if direct.exists():
            return [direct]
        return sorted(input_path.rglob("conversations.json"))
    return []


def _conversation_id(raw_conversation: dict[str, Any], source_path: Path, raw_index: int) -> str:
    source_id = raw_conversation.get("id") or raw_conversation.get("conversation_id")
    if source_id:
        return "conv_" + _short_hash(str(source_id))
    title = raw_conversation.get("title") or ""
    created = raw_conversation.get("create_time") or raw_conversation.get("created_at") or ""
    return "conv_" + _short_hash(f"{source_path}:{raw_index}:{title}:{created}")


def _content_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, dict):
        parts = content.get("parts")
        if isinstance(parts, list):
            return "\n".join(_part_text(part) for part in parts if _part_text(part)).strip()
        text = content.get("text")
        if isinstance(text, str):
            return text.strip()
    if isinstance(content, str):
        return content.strip()
    return ""


def _part_text(part: Any) -> str:
    if isinstance(part, str):
        return part
    if isinstance(part, dict):
        if isinstance(part.get("text"), str):
            return part["text"]
        if isinstance(part.get("content"), str):
            return part["content"]
    return ""


def _role(message: dict[str, Any]) -> str:
    author = message.get("author")
    if isinstance(author, dict):
        role = author.get("role")
        if isinstance(role, str) and role:
            return role
    return "unknown"


def _author_name(message: dict[str, Any]) -> str | None:
    author = message.get("author")
    if isinstance(author, dict):
        name = author.get("name")
        if isinstance(name, str) and name:
            return name
    return None


def _timestamp(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        from datetime import datetime, timezone

        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    if isinstance(value, str):
        return value
    return None


def _sort_time(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


def _optional_str(value: Any) -> str | None:
    return str(value) if value is not None else None


def _token_estimate(text: str) -> int:
    return max(1, len(text) // 4) if text else 0


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _short_hash(value: str, *, length: int = 16) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]
