from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from local_agent_lab.staging_snapshot import (
    FIELD_ALLOWLIST,
    SNAPSHOT_SCHEMA_VERSION,
    StagingSnapshotConfig,
    StagingSnapshotError,
    export_staging_snapshot,
)


SNAPSHOT_AT = "2026-08-20T18:00:00Z"


def _memory_database(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL,
            checksum TEXT NOT NULL
        );
        INSERT INTO schema_migrations VALUES (1, 'initial', '2026-08-20T00:00:00Z', 'checksum-secret');

        CREATE TABLE subjects (
            id TEXT PRIMARY KEY,
            slug TEXT,
            kind TEXT,
            name TEXT,
            description TEXT,
            created_at TEXT,
            updated_at TEXT,
            metadata_json TEXT
        );
        CREATE TABLE conversation_subjects (
            conversation_id TEXT,
            subject_id TEXT,
            confidence REAL,
            source TEXT,
            assigned_at TEXT,
            notes TEXT
        );
        CREATE TABLE chunk_subjects (
            chunk_id TEXT,
            subject_id TEXT,
            confidence REAL,
            source TEXT,
            assigned_at TEXT,
            notes TEXT
        );
        CREATE TABLE candidate_memories (
            id TEXT PRIMARY KEY,
            import_id TEXT,
            conversation_id TEXT,
            message_id TEXT,
            chunk_id TEXT,
            source_kind TEXT,
            source_ref TEXT,
            source_role TEXT,
            memory_type TEXT,
            reason_type TEXT,
            domain_primary TEXT,
            domains_json TEXT,
            confidence REAL,
            valid_from TEXT,
            valid_to TEXT,
            last_confirmed_at TEXT,
            review_status TEXT,
            review_notes TEXT,
            origin TEXT,
            assistant_suggestion INTEGER,
            source_links_json TEXT,
            content TEXT,
            metadata_json TEXT,
            created_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE memory_records (
            id TEXT PRIMARY KEY,
            record_type TEXT,
            title TEXT,
            body TEXT,
            subject_id TEXT,
            trust_level TEXT,
            source_kind TEXT,
            source_ref TEXT,
            provenance_json TEXT,
            status TEXT,
            valid_from TEXT,
            valid_to TEXT,
            last_verified_at TEXT,
            created_by TEXT,
            created_at TEXT,
            updated_at TEXT,
            metadata_json TEXT
        );
        CREATE TABLE memory_links (
            from_kind TEXT,
            from_id TEXT,
            to_kind TEXT,
            to_id TEXT,
            link_type TEXT,
            confidence REAL,
            notes TEXT
        );
        CREATE TABLE agent_feedback (
            id TEXT,
            observed_behavior TEXT,
            trace_id TEXT
        );
        """
    )
    connection.execute(
        "INSERT INTO subjects VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "subject-private-id",
            "secret-subject",
            "subject",
            "OAuth tunnel details",
            "SUBJECT-DESCRIPTION-SECRET",
            "2026-01-01",
            "2026-01-02",
            '{"api_token":"METADATA-TOKEN-SECRET"}',
        ),
    )
    connection.execute(
        "INSERT INTO conversation_subjects VALUES (?, ?, ?, ?, ?, ?)",
        (
            "conversation-private-id",
            "subject-private-id",
            0.9,
            "oauth-token-source",
            "2026-01-03",
            "ASSIGNMENT-NOTE-SECRET",
        ),
    )
    connection.execute(
        "INSERT INTO chunk_subjects VALUES (?, ?, ?, ?, ?, ?)",
        (
            "chunk-private-id",
            "subject-private-id",
            0.8,
            "deterministic_rule",
            "2026-01-03",
            "CHUNK-ASSIGNMENT-SECRET",
        ),
    )
    connection.execute(
        "INSERT INTO candidate_memories VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "candidate-private-id",
            "import-private-id",
            "conversation-private-id",
            "message-private-id",
            "chunk-private-id",
            "oauth-token-source",
            "SOURCE-REF-SECRET",
            "user",
            "preference",
            "preference_choice",
            "cooking_baking",
            '["cooking_baking"]',
            0.87,
            "2026-01-01",
            None,
            "2026-01-02",
            "pending",
            "REVIEW-NOTE-SECRET",
            "chatgpt_export",
            0,
            '{"message_id":"SOURCE-LINK-SECRET"}',
            "CANDIDATE-CONTENT-SECRET sk-proj-example",
            '{"password":"CANDIDATE-METADATA-SECRET"}',
            "2026-01-01",
            "2026-01-02",
        ),
    )
    connection.execute(
        "INSERT INTO memory_records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "record-private-id",
            "preference",
            "RECORD-TITLE-SECRET",
            "RECORD-BODY-SECRET password=example",
            "subject-private-id",
            "high",
            "manual",
            "SOURCE-REF-SECRET",
            '{"audit_log":"PROVENANCE-SECRET"}',
            "active",
            "2026-01-01",
            None,
            "2026-01-02",
            "user",
            "2026-01-01",
            "2026-01-02",
            '{"tunnel":"RECORD-METADATA-SECRET"}',
        ),
    )
    connection.execute(
        "INSERT INTO memory_links VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "memory_record",
            "record-private-id",
            "candidate_memory",
            "candidate-private-id",
            "supports",
            0.75,
            "LINK-NOTE-SECRET",
        ),
    )
    connection.execute(
        "INSERT INTO agent_feedback VALUES (?, ?, ?)",
        ("feedback-secret-id", "AUDIT-BEHAVIOR-SECRET", "TRACE-SECRET"),
    )
    connection.commit()
    connection.close()
    return path


def _config(tmp_path: Path, source_db: Path, *, destination_name: str = "snapshot.json", **overrides) -> StagingSnapshotConfig:
    values = {
        "source_db": source_db,
        "source_root": source_db.parent,
        "destination": tmp_path / "staging" / destination_name,
        "staging_root": tmp_path / "staging",
        "snapshot_at": SNAPSHOT_AT,
        "administrator_authorized": True,
    }
    values.update(overrides)
    return StagingSnapshotConfig(**values)


def test_snapshot_uses_explicit_allowlist_and_omits_sensitive_data(tmp_path: Path) -> None:
    source_db = _memory_database(tmp_path / "production" / "memory.sqlite3")
    config = _config(tmp_path, source_db)
    source_before = source_db.read_bytes()

    payload = export_staging_snapshot(config)
    raw = config.destination.read_text(encoding="utf-8")

    assert payload["schema_version"] == SNAPSHOT_SCHEMA_VERSION
    assert payload["authority"] == {
        "administrator_triggered": True,
        "source_access": "sqlite_query_only",
        "content_mode": "synthetic",
        "write_back_authority": False,
        "production_import_authority": False,
        "production_credentials_included": False,
    }
    assert payload["field_allowlist"] == {key: list(value) for key, value in FIELD_ALLOWLIST.items()}
    for dataset_name, rows in payload["datasets"].items():
        for row in rows:
            assert set(row) == set(FIELD_ALLOWLIST[dataset_name])

    for secret in (
        "SUBJECT-DESCRIPTION-SECRET",
        "METADATA-TOKEN-SECRET",
        "ASSIGNMENT-NOTE-SECRET",
        "CHUNK-ASSIGNMENT-SECRET",
        "SOURCE-REF-SECRET",
        "REVIEW-NOTE-SECRET",
        "SOURCE-LINK-SECRET",
        "CANDIDATE-CONTENT-SECRET",
        "CANDIDATE-METADATA-SECRET",
        "RECORD-TITLE-SECRET",
        "RECORD-BODY-SECRET",
        "PROVENANCE-SECRET",
        "RECORD-METADATA-SECRET",
        "LINK-NOTE-SECRET",
        "AUDIT-BEHAVIOR-SECRET",
        "TRACE-SECRET",
        "sk-proj-example",
        "subject-private-id",
        "candidate-private-id",
        "record-private-id",
    ):
        assert secret not in raw
    assert payload["datasets"]["candidate_memories"][0]["source_class"] == "other"
    assert config.destination.stat().st_mode & 0o777 == 0o600
    assert source_db.read_bytes() == source_before


def test_snapshot_regeneration_is_byte_deterministic_with_stable_remapped_ids(tmp_path: Path) -> None:
    source_db = _memory_database(tmp_path / "production" / "memory.sqlite3")
    first = _config(tmp_path, source_db, destination_name="first.json")
    second = _config(tmp_path, source_db, destination_name="second.json")

    first_payload = export_staging_snapshot(first)
    second_payload = export_staging_snapshot(second)

    assert first.destination.read_bytes() == second.destination.read_bytes()
    assert first_payload["snapshot_id"] == second_payload["snapshot_id"]
    assert first_payload["provenance"]["snapshot_at"] == SNAPSHOT_AT
    assert first_payload["provenance"]["sanitized_dataset_sha256"] == second_payload["provenance"][
        "sanitized_dataset_sha256"
    ]
    assert first_payload["datasets"]["candidate_memories"][0]["id"].startswith("stg_candidate_")


def test_snapshot_output_is_bounded_and_reports_truncation(tmp_path: Path) -> None:
    source_db = _memory_database(tmp_path / "production" / "memory.sqlite3")
    connection = sqlite3.connect(source_db)
    base = connection.execute("SELECT * FROM candidate_memories").fetchone()
    assert base is not None
    for index in range(1, 6):
        values = list(base)
        values[0] = f"candidate-{index}"
        values[1] = f"import-{index}"
        values[2] = f"conversation-{index}"
        values[3] = f"message-{index}"
        values[4] = f"chunk-{index}"
        connection.execute(
            "INSERT INTO candidate_memories VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            values,
        )
    connection.commit()
    connection.close()
    config = _config(tmp_path, source_db, max_rows_per_dataset=2, max_output_bytes=20_000)

    payload = export_staging_snapshot(config)

    assert len(payload["datasets"]["candidate_memories"]) == 2
    assert payload["counts"]["candidate_memories"] == {
        "available": 6,
        "exported": 2,
        "truncated": True,
    }
    assert config.destination.stat().st_size <= 20_000

    too_small = replace(config, destination=tmp_path / "staging" / "too-small.json", max_output_bytes=1_024)
    with pytest.raises(StagingSnapshotError, match="snapshot is .* bytes"):
        export_staging_snapshot(too_small)
    assert not too_small.destination.exists()


def test_snapshot_rejects_unsafe_authority_content_and_roots(tmp_path: Path) -> None:
    source_db = _memory_database(tmp_path / "production" / "memory.sqlite3")
    safe = _config(tmp_path, source_db)

    unsafe_configs = (
        replace(safe, administrator_authorized=False),
        replace(safe, content_mode="raw"),  # type: ignore[arg-type]
        replace(safe, source_root=tmp_path / "other-production"),
        replace(safe, destination=tmp_path / "outside.json"),
        replace(safe, staging_root=source_db.parent, destination=source_db.parent / "snapshot.json"),
    )
    for config in unsafe_configs:
        with pytest.raises(StagingSnapshotError):
            export_staging_snapshot(config)


def test_snapshot_rejects_sensitive_and_symlink_source_paths(tmp_path: Path) -> None:
    sensitive_db = _memory_database(tmp_path / "production" / "audit" / "memory.sqlite3")
    sensitive = _config(
        tmp_path,
        sensitive_db,
        source_root=tmp_path / "production",
    )
    with pytest.raises(StagingSnapshotError, match="auth, secret, tunnel, audit, or log"):
        export_staging_snapshot(sensitive)

    safe_db = _memory_database(tmp_path / "safe-production" / "memory.sqlite3")
    symlink = safe_db.parent / "linked.sqlite3"
    symlink.symlink_to(safe_db)
    linked = _config(tmp_path, symlink, source_root=safe_db.parent)
    with pytest.raises(StagingSnapshotError, match="non-symlink regular file"):
        export_staging_snapshot(linked)


def test_snapshot_rejects_incomplete_memory_schema(tmp_path: Path) -> None:
    source_db = tmp_path / "production" / "memory.sqlite3"
    source_db.parent.mkdir()
    connection = sqlite3.connect(source_db)
    connection.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()

    with pytest.raises(StagingSnapshotError, match="missing required memory tables"):
        export_staging_snapshot(_config(tmp_path, source_db))
