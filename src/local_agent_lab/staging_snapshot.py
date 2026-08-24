from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote


SNAPSHOT_SCHEMA_VERSION = "staging_snapshot_v1"
SNAPSHOT_EXPORTER_VERSION = "1"
DEFAULT_MAX_ROWS_PER_DATASET = 500
DEFAULT_MAX_OUTPUT_BYTES = 2_000_000
ABSOLUTE_MAX_ROWS_PER_DATASET = 5_000
ABSOLUTE_MAX_OUTPUT_BYTES = 20_000_000

_REQUIRED_TABLES = (
    "schema_migrations",
    "subjects",
    "conversation_subjects",
    "chunk_subjects",
    "candidate_memories",
    "memory_records",
    "memory_links",
)

FIELD_ALLOWLIST: dict[str, tuple[str, ...]] = {
    "subjects": ("id", "kind", "synthetic_name"),
    "conversation_subjects": (
        "conversation_id",
        "subject_id",
        "confidence",
        "assignment_source_class",
    ),
    "chunk_subjects": (
        "chunk_id",
        "subject_id",
        "confidence",
        "assignment_source_class",
    ),
    "candidate_memories": (
        "id",
        "import_id",
        "conversation_id",
        "message_id",
        "chunk_id",
        "source_role",
        "memory_type",
        "reason_type",
        "domain_primary",
        "domains",
        "confidence",
        "review_status",
        "origin_class",
        "assistant_suggestion",
        "source_class",
        "synthetic_text",
    ),
    "memory_records": (
        "id",
        "record_type",
        "subject_id",
        "trust_level",
        "source_class",
        "source_ref_id",
        "status",
        "created_by_class",
        "has_valid_from",
        "has_valid_to",
        "has_verification_time",
        "synthetic_title",
        "synthetic_body",
    ),
    "memory_links": (
        "from_kind",
        "from_id",
        "to_kind",
        "to_id",
        "link_type",
        "confidence",
    ),
}

OMITTED_DATA_CLASSES = (
    "raw message and chunk text",
    "titles, descriptions, notes, summaries, and free-form memory content",
    "metadata and provenance payloads",
    "attachments and source filesystem paths",
    "embedding vectors and model metadata",
    "retrieval events, feedback, audit logs, and run traces",
    "credentials, passwords, tokens, keys, certificates, auth, and tunnel configuration",
    "original identifiers and exact event timestamps",
)

_CANONICAL_DOMAINS = {
    "ai_memory_systems",
    "career_work",
    "cooking_baking",
    "creative_writing",
    "finance_investing",
    "fitness_training",
    "health_supplements",
    "home_projects",
    "lab_automation",
    "law_lsat",
    "misc",
    "pets",
    "relationships_life",
    "style_wardrobe",
}
_MEMORY_TYPES = {
    "analogy",
    "constraint",
    "decision",
    "episodic",
    "failure",
    "financial_note",
    "health_note",
    "hypothesis",
    "open_loop",
    "preference",
    "procedure",
    "project",
    "relationship",
    "risk",
    "semantic_fact",
    "skill",
    "source_note",
    "workaround",
}
_REASON_TYPES = {
    "accident",
    "assistant_suggestion",
    "constraint_response",
    "error_recovery",
    "experiment",
    "ideal_procedure",
    "preference_choice",
    "unknown",
    "user_reported_outcome",
    "workaround",
}
_RECORD_TYPES = {
    "contact_note",
    "decision",
    "lesson",
    "open_loop",
    "preference",
    "project",
    "research_note",
    "workflow",
}
_SENSITIVE_PATH_TERMS = {
    ".ssh",
    "audit",
    "auth",
    "certificate",
    "credential",
    "key",
    "logs",
    "oauth",
    "password",
    "secret",
    "token",
    "trace",
    "tunnel",
}
_SAFE_LABEL = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


class StagingSnapshotError(ValueError):
    """Raised when an export would cross the staging safety boundary."""


@dataclass(frozen=True)
class StagingSnapshotConfig:
    source_db: Path
    source_root: Path
    destination: Path
    staging_root: Path
    snapshot_at: str
    administrator_authorized: bool = False
    max_rows_per_dataset: int = DEFAULT_MAX_ROWS_PER_DATASET
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    content_mode: Literal["synthetic"] = "synthetic"
    id_namespace: str = "minillm-staging-v1"


def export_staging_snapshot(config: StagingSnapshotConfig) -> dict[str, Any]:
    """Export a deterministic, structural fixture without production write authority."""

    validated = _validate_config(config)
    source_db = validated["source_db"]
    destination = validated["destination"]
    snapshot_at = validated["snapshot_at"]

    with closing(_read_only_connection(source_db)) as connection:
        _require_tables(connection)
        source_schema_versions = _source_schema_versions(connection)
        datasets, counts = _export_datasets(connection, config)

    dataset_digest = _sha256_json(datasets)
    snapshot_id = _stable_id(
        "snapshot",
        f"{snapshot_at}:{dataset_digest}",
        namespace=config.id_namespace,
    )
    payload: dict[str, Any] = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "provenance": {
            "snapshot_at": snapshot_at,
            "exporter_version": SNAPSHOT_EXPORTER_VERSION,
            "source_schema_versions": source_schema_versions,
            "sanitized_dataset_sha256": dataset_digest,
        },
        "authority": {
            "administrator_triggered": True,
            "source_access": "sqlite_query_only",
            "content_mode": "synthetic",
            "write_back_authority": False,
            "production_import_authority": False,
            "production_credentials_included": False,
        },
        "limits": {
            "max_rows_per_dataset": config.max_rows_per_dataset,
            "max_output_bytes": config.max_output_bytes,
        },
        "field_allowlist": {key: list(value) for key, value in FIELD_ALLOWLIST.items()},
        "omitted_data_classes": list(OMITTED_DATA_CLASSES),
        "counts": counts,
        "datasets": datasets,
    }
    rendered = (json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("utf-8")
    if len(rendered) > config.max_output_bytes:
        raise StagingSnapshotError(
            f"sanitized snapshot is {len(rendered)} bytes; limit is {config.max_output_bytes}"
        )

    _atomic_private_write(destination, rendered)
    return payload


def _validate_config(config: StagingSnapshotConfig) -> dict[str, Any]:
    if not config.administrator_authorized:
        raise StagingSnapshotError("administrator_authorized must be explicitly true")
    if config.content_mode != "synthetic":
        raise StagingSnapshotError("only synthetic content mode is permitted")
    if not 1 <= config.max_rows_per_dataset <= ABSOLUTE_MAX_ROWS_PER_DATASET:
        raise StagingSnapshotError(
            f"max_rows_per_dataset must be between 1 and {ABSOLUTE_MAX_ROWS_PER_DATASET}"
        )
    if not 1_024 <= config.max_output_bytes <= ABSOLUTE_MAX_OUTPUT_BYTES:
        raise StagingSnapshotError(
            f"max_output_bytes must be between 1024 and {ABSOLUTE_MAX_OUTPUT_BYTES}"
        )
    if not _SAFE_LABEL.fullmatch(config.id_namespace):
        raise StagingSnapshotError("id_namespace must be a bounded lowercase label")

    snapshot_at = _canonical_snapshot_time(config.snapshot_at)
    source_db = Path(config.source_db).expanduser()
    source_root = Path(config.source_root).expanduser()
    destination = Path(config.destination).expanduser()
    staging_root = Path(config.staging_root).expanduser()

    if not source_root.exists() or not source_root.is_dir() or source_root.is_symlink():
        raise StagingSnapshotError("source_root must be an existing non-symlink directory")
    if not source_db.exists() or not source_db.is_file() or source_db.is_symlink():
        raise StagingSnapshotError("source_db must be an existing non-symlink regular file")
    if source_db.suffix.casefold() not in {".db", ".sqlite", ".sqlite3"}:
        raise StagingSnapshotError("source_db must be a SQLite database file")
    if staging_root.exists() and (not staging_root.is_dir() or staging_root.is_symlink()):
        raise StagingSnapshotError("staging_root must be a directory and cannot be a symlink")
    if destination.suffix.casefold() != ".json":
        raise StagingSnapshotError("destination must be a JSON file")
    if destination.exists() and (not destination.is_file() or destination.is_symlink()):
        raise StagingSnapshotError("destination cannot be a directory or symlink")

    source_root_resolved = source_root.resolve(strict=True)
    source_db_resolved = source_db.resolve(strict=True)
    staging_root_resolved = staging_root.resolve(strict=False)
    destination_resolved = destination.resolve(strict=False)
    _require_within(source_db_resolved, source_root_resolved, "source_db")
    _require_within(destination_resolved, staging_root_resolved, "destination")
    if _paths_overlap(source_root_resolved, staging_root_resolved):
        raise StagingSnapshotError("source_root and staging_root must be disjoint")
    if source_db_resolved == destination_resolved:
        raise StagingSnapshotError("source and destination must differ")

    _reject_sensitive_path(source_db_resolved, "source_db")
    _reject_sensitive_path(destination_resolved, "destination")
    return {
        "source_db": source_db_resolved,
        "destination": destination_resolved,
        "snapshot_at": snapshot_at,
    }


def _read_only_connection(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(path), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _require_tables(connection: sqlite3.Connection) -> None:
    placeholders = ",".join("?" for _ in _REQUIRED_TABLES)
    rows = connection.execute(
        f"SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ({placeholders})",
        _REQUIRED_TABLES,
    ).fetchall()
    present = {str(row[0]) for row in rows}
    missing = sorted(set(_REQUIRED_TABLES) - present)
    if missing:
        raise StagingSnapshotError("source database is missing required memory tables: " + ", ".join(missing))


def _source_schema_versions(connection: sqlite3.Connection) -> list[int]:
    rows = connection.execute("SELECT version FROM schema_migrations ORDER BY version LIMIT 100").fetchall()
    return sorted({int(row[0]) for row in rows})


def _export_datasets(
    connection: sqlite3.Connection,
    config: StagingSnapshotConfig,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, int | bool]]]:
    remap = lambda kind, value: _stable_id(kind, value, namespace=config.id_namespace)
    limit = config.max_rows_per_dataset
    datasets: dict[str, list[dict[str, Any]]] = {}

    subject_rows = _bounded_query(
        connection,
        "SELECT id, kind FROM subjects ORDER BY id LIMIT ?",
        limit,
    )
    datasets["subjects"] = [
        {
            "id": remap("subject", row["id"]),
            "kind": _enum(row["kind"], {"subject", "project", "workflow"}),
            "synthetic_name": f"Synthetic {_enum(row['kind'], {'subject', 'project', 'workflow'})} "
            f"{remap('subject', row['id'])[-8:]}",
        }
        for row in subject_rows
    ]

    conversation_subject_rows = _bounded_query(
        connection,
        """
        SELECT conversation_id, subject_id, confidence, source
        FROM conversation_subjects
        ORDER BY conversation_id, subject_id
        LIMIT ?
        """,
        limit,
    )
    datasets["conversation_subjects"] = [
        {
            "conversation_id": remap("conversation", row["conversation_id"]),
            "subject_id": remap("subject", row["subject_id"]),
            "confidence": _confidence(row["confidence"]),
            "assignment_source_class": _source_class(row["source"]),
        }
        for row in conversation_subject_rows
    ]

    chunk_subject_rows = _bounded_query(
        connection,
        """
        SELECT chunk_id, subject_id, confidence, source
        FROM chunk_subjects
        ORDER BY chunk_id, subject_id
        LIMIT ?
        """,
        limit,
    )
    datasets["chunk_subjects"] = [
        {
            "chunk_id": remap("chunk", row["chunk_id"]),
            "subject_id": remap("subject", row["subject_id"]),
            "confidence": _confidence(row["confidence"]),
            "assignment_source_class": _source_class(row["source"]),
        }
        for row in chunk_subject_rows
    ]

    candidate_rows = _bounded_query(
        connection,
        """
        SELECT id, import_id, conversation_id, message_id, chunk_id, source_role,
               memory_type, reason_type, domain_primary, domains_json, confidence,
               review_status, origin, assistant_suggestion, source_kind
        FROM candidate_memories
        ORDER BY id
        LIMIT ?
        """,
        limit,
    )
    datasets["candidate_memories"] = []
    for row in candidate_rows:
        candidate_id = remap("candidate", row["id"])
        memory_type = _enum(row["memory_type"], _MEMORY_TYPES)
        domain = _enum(row["domain_primary"], _CANONICAL_DOMAINS, fallback="misc")
        datasets["candidate_memories"].append(
            {
                "id": candidate_id,
                "import_id": remap("import", row["import_id"]),
                "conversation_id": remap("conversation", row["conversation_id"]),
                "message_id": remap("message", row["message_id"]),
                "chunk_id": remap("chunk", row["chunk_id"]),
                "source_role": _enum(row["source_role"], {"assistant", "system", "tool", "user"}),
                "memory_type": memory_type,
                "reason_type": _enum(row["reason_type"], _REASON_TYPES),
                "domain_primary": domain,
                "domains": _safe_domains(row["domains_json"]),
                "confidence": _confidence(row["confidence"]),
                "review_status": _enum(row["review_status"], {"approved", "merged", "pending", "rejected"}),
                "origin_class": _source_class(row["origin"]),
                "assistant_suggestion": bool(row["assistant_suggestion"]),
                "source_class": _source_class(row["source_kind"]),
                "synthetic_text": f"Synthetic {memory_type} candidate {candidate_id[-8:]} in {domain}.",
            }
        )

    record_rows = _bounded_query(
        connection,
        """
        SELECT id, record_type, subject_id, trust_level, source_kind, source_ref,
               status, created_by, valid_from, valid_to, last_verified_at
        FROM memory_records
        ORDER BY id
        LIMIT ?
        """,
        limit,
    )
    datasets["memory_records"] = []
    for row in record_rows:
        record_id = remap("memory_record", row["id"])
        record_type = _enum(row["record_type"], _RECORD_TYPES)
        datasets["memory_records"].append(
            {
                "id": record_id,
                "record_type": record_type,
                "subject_id": remap("subject", row["subject_id"]) if row["subject_id"] else None,
                "trust_level": _enum(row["trust_level"], {"canonical", "high", "low", "medium"}),
                "source_class": _source_class(row["source_kind"]),
                "source_ref_id": remap("source_ref", row["source_ref"]) if row["source_ref"] else None,
                "status": _enum(row["status"], {"active", "archived", "deleted", "stale", "superseded"}),
                "created_by_class": _enum(row["created_by"], {"administrator", "assistant", "system", "user"}),
                "has_valid_from": row["valid_from"] is not None,
                "has_valid_to": row["valid_to"] is not None,
                "has_verification_time": row["last_verified_at"] is not None,
                "synthetic_title": f"Synthetic {record_type} record {record_id[-8:]}",
                "synthetic_body": f"Synthetic {record_type} fixture with no production text.",
            }
        )

    link_rows = _bounded_query(
        connection,
        """
        SELECT from_kind, from_id, to_kind, to_id, link_type, confidence
        FROM memory_links
        ORDER BY from_kind, from_id, to_kind, to_id, link_type
        LIMIT ?
        """,
        limit,
    )
    datasets["memory_links"] = []
    for row in link_rows:
        from_kind = _safe_label(row["from_kind"])
        to_kind = _safe_label(row["to_kind"])
        datasets["memory_links"].append(
            {
                "from_kind": from_kind,
                "from_id": remap(from_kind, row["from_id"]),
                "to_kind": to_kind,
                "to_id": remap(to_kind, row["to_id"]),
                "link_type": _safe_label(row["link_type"]),
                "confidence": _confidence(row["confidence"]) if row["confidence"] is not None else None,
            }
        )

    counts: dict[str, dict[str, int | bool]] = {}
    for dataset_name, rows in datasets.items():
        available = int(connection.execute(f"SELECT COUNT(*) FROM {dataset_name}").fetchone()[0])
        counts[dataset_name] = {
            "available": available,
            "exported": len(rows),
            "truncated": available > len(rows),
        }
    return datasets, counts


def _bounded_query(connection: sqlite3.Connection, query: str, limit: int) -> list[sqlite3.Row]:
    return list(connection.execute(query, (limit,)).fetchall())


def _stable_id(kind: str, value: Any, *, namespace: str) -> str:
    normalized_kind = _safe_label(kind)
    digest = hashlib.sha256(f"{namespace}\x1f{normalized_kind}\x1f{value}".encode("utf-8")).hexdigest()[:20]
    return f"stg_{normalized_kind}_{digest}"


def _safe_domains(raw: Any) -> list[str]:
    encoded = str(raw)
    if len(encoded) > 4_096:
        return ["misc"]
    try:
        values = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError):
        return ["misc"]
    if not isinstance(values, list):
        return ["misc"]
    domains = sorted({_enum(value, _CANONICAL_DOMAINS, fallback="misc") for value in values})
    return domains or ["misc"]


def _enum(value: Any, allowed: set[str], *, fallback: str = "other") -> str:
    normalized = str(value or "").strip().casefold()
    return normalized if normalized in allowed else fallback


def _safe_label(value: Any) -> str:
    normalized = str(value or "").strip().casefold().replace("-", "_")
    if not _SAFE_LABEL.fullmatch(normalized):
        return "other"
    if any(term in normalized for term in _SENSITIVE_PATH_TERMS):
        return "other"
    return normalized


def _source_class(value: Any) -> str:
    normalized = str(value or "").strip().casefold()
    if any(term in normalized for term in _SENSITIVE_PATH_TERMS):
        return "other"
    if "test" in normalized or "fixture" in normalized:
        return "test_fixture"
    if "chatgpt" in normalized or "import" in normalized or "export" in normalized:
        return "imported_chat"
    if "manual" in normalized or normalized in {"user", "administrator"}:
        return "manual"
    if "model" in normalized or "classifier" in normalized or "inferred" in normalized:
        return "model_assisted"
    if "rule" in normalized or "heuristic" in normalized or "deterministic" in normalized:
        return "deterministic"
    return "other"


def _confidence(value: Any) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0.0
    return round(max(0.0, min(1.0, numeric)), 6)


def _canonical_snapshot_time(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise StagingSnapshotError("snapshot_at is required")
    try:
        parsed = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
    except ValueError as exc:
        raise StagingSnapshotError("snapshot_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise StagingSnapshotError("snapshot_at must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _require_within(path: Path, root: Path, field: str) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise StagingSnapshotError(f"{field} must resolve within its declared root") from exc


def _paths_overlap(first: Path, second: Path) -> bool:
    return _is_relative_to(first, second) or _is_relative_to(second, first)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _reject_sensitive_path(path: Path, field: str) -> None:
    for part in path.parts:
        normalized = part.casefold()
        tokens = set(filter(None, re.split(r"[^a-z0-9.]+", normalized)))
        if tokens & _SENSITIVE_PATH_TERMS:
            raise StagingSnapshotError(f"{field} cannot use auth, secret, tunnel, audit, or log paths")


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _atomic_private_write(destination: Path, rendered: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".snapshot-", dir=destination.parent, delete=False) as handle:
            temporary_path = Path(handle.name)
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.chmod(0o600)
        temporary_path.replace(destination)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
