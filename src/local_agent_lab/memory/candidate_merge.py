from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import Counter
from typing import Any

from .audit import init_audit_schema
from .candidates import CandidateMemory, get_candidate_memory, init_candidate_memory_schema
from .curated import get_memory_record, init_curated_memory_schema, list_memory_links
from .observability import MemoryObservationError, utc_now
from .ontology import validate_curated_record_type, validate_trust_level
from .subjects import resolve_subject


def init_candidate_merge_schema(connection: sqlite3.Connection) -> None:
    """Initialize durable merge audit state before any apply transaction begins."""
    init_audit_schema(connection)
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS candidate_merge_events (
            operation_id TEXT PRIMARY KEY,
            record_id TEXT NOT NULL UNIQUE,
            preview_token TEXT NOT NULL,
            merge_fingerprint TEXT NOT NULL UNIQUE,
            caller_run_id TEXT,
            created_by TEXT NOT NULL,
            candidate_ids_json TEXT NOT NULL,
            accepted_warning_codes_json TEXT NOT NULL DEFAULT '[]',
            source_snapshots_json TEXT NOT NULL,
            candidate_states_before_json TEXT NOT NULL,
            candidate_states_after_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_candidate_merge_events_record
            ON candidate_merge_events(record_id);
        CREATE INDEX IF NOT EXISTS idx_candidate_merge_events_preview
            ON candidate_merge_events(preview_token);
        """
    )
    connection.commit()


def preview_candidate_merge(
    connection: sqlite3.Connection,
    candidate_ids: list[str],
    *,
    title: str | None = None,
    body: str | None = None,
    record_type: str | None = None,
    trust_level: str = "medium",
    subject: str | None = None,
    created_by: str = "user",
) -> dict[str, Any]:
    """Build a deterministic, non-mutating preview for merging candidates."""
    init_candidate_memory_schema(connection)
    cleaned_ids = _clean_candidate_ids(candidate_ids)
    if len(cleaned_ids) < 2:
        raise ValueError("candidate merge preview requires at least two candidate_ids")

    before_counts = _truth_counts(connection)
    try:
        candidates = [get_candidate_memory(connection, candidate_id) for candidate_id in cleaned_ids]
    except KeyError as exc:
        raise _merge_error(
            str(exc),
            error_code="candidate_merge_candidate_not_found",
            source_ref="|".join(cleaned_ids),
        ) from exc
    source_snapshots = _candidate_source_snapshots(connection, cleaned_ids)
    evidence = [_candidate_evidence(candidate, source_snapshots.get(candidate.id)) for candidate in candidates]
    role_counts = Counter(item["source_role"] for item in evidence)
    status_counts = Counter(candidate.review_status for candidate in candidates)
    memory_type_counts = Counter(candidate.memory_type for candidate in candidates)
    domain_counts = Counter(domain for candidate in candidates for domain in candidate.domains)
    warnings = _merge_warnings(candidates, role_counts=role_counts, status_counts=status_counts, domain_counts=domain_counts)
    user_candidates = [candidate for candidate in candidates if not candidate.assistant_suggestion]
    assistant_candidates = [candidate for candidate in candidates if candidate.assistant_suggestion]
    basis_candidates = user_candidates or candidates
    final_record_type = record_type or _default_curated_record_type(
        _most_common(memory_type_counts, default="research_note")
    )
    final_title = (title or _proposed_title(basis_candidates)).strip()
    final_body = (body or _proposed_body(user_candidates=user_candidates, assistant_candidates=assistant_candidates)).strip()
    proposed_record = {
        "record_type": final_record_type,
        "title": final_title,
        "body": final_body,
        "subject": subject,
        "trust_level": trust_level,
        "source_kind": "chatgpt_candidate_merge",
        "source_ref": _merge_source_ref(cleaned_ids),
        "created_by": created_by,
        "status": "active",
        "provenance": {
            "source_candidate_ids": cleaned_ids,
            "user_candidate_ids": [candidate.id for candidate in user_candidates],
            "assistant_candidate_ids": [candidate.id for candidate in assistant_candidates],
            "assistant_suggestions_separate": True,
            "evidence_role_counts": dict(sorted(role_counts.items())),
            "confidence_basis": _confidence_basis(candidates, role_counts=role_counts),
        },
        "metadata": {
            "merge_preview_only": True,
            "candidate_review_statuses": dict(sorted(status_counts.items())),
            "candidate_domains": dict(sorted(domain_counts.items())),
            "conflict_warning_count": len(warnings),
        },
    }
    preview_token = _preview_token(candidates, proposed_record, source_snapshots)

    preview = {
        "mode": "preview",
        "mutates_memory_truth": False,
        "preview_token": preview_token,
        "candidate_ids": cleaned_ids,
        "candidate_count": len(candidates),
        "evidence": evidence,
        "source_snapshots": [source_snapshots[candidate_id] for candidate_id in cleaned_ids],
        "evidence_role_counts": dict(sorted(role_counts.items())),
        "assistant_suggestions_separate": True,
        "assistant_candidate_ids": [candidate.id for candidate in assistant_candidates],
        "user_candidate_ids": [candidate.id for candidate in user_candidates],
        "assistant_evidence_draft": [
            {
                "candidate_id": candidate.id,
                "content": candidate.content,
                "status": "suggestion_only_not_in_canonical_body",
            }
            for candidate in assistant_candidates
        ],
        "status_counts": dict(sorted(status_counts.items())),
        "domain_counts": dict(sorted(domain_counts.items())),
        "memory_type_counts": dict(sorted(memory_type_counts.items())),
        "conflict_warnings": warnings,
        "proposed_record": proposed_record,
        "planned_writes": [
            {
                "table": "memory_records",
                "operation": "insert",
                "count": 1,
                "condition": "only if applied with this exact preview_token and confirmed=true",
            },
            {
                "table": "memory_links",
                "operation": "insert",
                "count": len(candidates),
                "condition": "link each source candidate/evidence item to the curated record on apply",
            },
            {
                "table": "candidate_memories",
                "operation": "update",
                "count": len(candidates),
                "fields": ["review_status", "review_notes", "last_confirmed_at"],
                "condition": "mark selected candidates merged only on explicit apply",
            },
        ],
        "mutation_policy": {
            "append_only": False,
            "mutates_memory_truth": False,
            "mutates_candidates": False,
            "mutates_ranking": False,
            "requires_explicit_apply": True,
            "requires_preview_token": True,
            "assistant_suggestions_promoted_to_fact": False,
        },
        "db_counts_before": before_counts,
        "db_counts_after": _truth_counts(connection),
    }
    return preview


def apply_candidate_merge(
    connection: sqlite3.Connection,
    candidate_ids: list[str],
    *,
    preview_token: str,
    confirmed: bool,
    title: str,
    body: str,
    record_type: str,
    trust_level: str = "medium",
    subject: str | None = None,
    created_by: str = "user",
    allow_conflicts: bool = False,
    accepted_warning_codes: list[str] | None = None,
    allow_assistant_only: bool = False,
    caller_run_id: str | None = None,
) -> dict[str, Any]:
    """Atomically apply an explicitly previewed candidate merge."""
    init_candidate_memory_schema(connection)
    init_curated_memory_schema(connection)
    init_candidate_merge_schema(connection)
    connection.execute("PRAGMA busy_timeout = 5000")
    cleaned_ids = _clean_candidate_ids(candidate_ids)
    if len(cleaned_ids) < 2:
        raise _merge_error(
            "candidate merge apply requires at least two candidate_ids",
            error_code="candidate_merge_too_few_candidates",
            source_ref="|".join(cleaned_ids),
        )
    if not confirmed:
        raise _merge_error(
            "candidate merge apply requires explicit confirmation",
            error_code="candidate_merge_confirmation_required",
            source_ref="|".join(cleaned_ids),
        )
    cleaned_token = str(preview_token).strip()
    if not cleaned_token:
        raise _merge_error(
            "candidate merge apply requires the preview_token returned by preview",
            error_code="candidate_merge_preview_required",
            source_ref="|".join(cleaned_ids),
        )

    final_title = _required_text(title, "title")
    final_body = _required_text(body, "body")
    final_record_type = validate_curated_record_type(record_type)
    final_trust_level = validate_trust_level(trust_level)
    try:
        candidates = [get_candidate_memory(connection, candidate_id) for candidate_id in cleaned_ids]
    except KeyError as exc:
        raise _merge_error(
            str(exc),
            error_code="candidate_merge_candidate_not_found",
            source_ref="|".join(cleaned_ids),
        ) from exc
    source_snapshots = _candidate_source_snapshots(connection, cleaned_ids)
    _validate_source_snapshots(source_snapshots, candidates)
    prior_states = {
        candidate.id: {
            "review_status": candidate.review_status,
            "review_notes": candidate.review_notes,
            "last_confirmed_at": candidate.last_confirmed_at,
            "metadata": candidate.metadata,
            "updated_at": candidate.updated_at,
        }
        for candidate in candidates
    }

    repeated = _repeated_apply_target(candidates, preview_token=cleaned_token)
    if repeated is not None:
        return _applied_payload(
            connection,
            candidates=candidates,
            record_id=repeated,
            preview_token=cleaned_token,
            prior_states=prior_states,
            counts_before=_truth_counts(connection),
            idempotent=True,
        )

    invalid_statuses = {
        candidate.id: candidate.review_status
        for candidate in candidates
        if candidate.review_status not in {"pending", "approved"}
    }
    if invalid_statuses:
        raise _merge_error(
            f"candidate merge cannot apply from current review statuses: {invalid_statuses}",
            error_code="candidate_merge_status_conflict",
            source_ref="|".join(sorted(invalid_statuses)),
        )

    user_candidates = [candidate for candidate in candidates if not candidate.assistant_suggestion]
    assistant_candidates = [candidate for candidate in candidates if candidate.assistant_suggestion]
    if not user_candidates and not allow_assistant_only:
        raise _merge_error(
            "assistant-only candidate merges require allow_assistant_only=true",
            error_code="candidate_merge_assistant_only_blocked",
            source_ref="|".join(cleaned_ids),
        )

    preview = preview_candidate_merge(
        connection,
        cleaned_ids,
        title=final_title,
        body=final_body,
        record_type=final_record_type,
        trust_level=final_trust_level,
        subject=subject,
        created_by=created_by,
    )
    if preview["preview_token"] != cleaned_token:
        raise _merge_error(
            "preview_token does not match the selected candidates and canonical fields",
            error_code="candidate_merge_preview_mismatch",
            source_ref="|".join(cleaned_ids),
        )

    blocking_warnings = [
        warning
        for warning in preview["conflict_warnings"]
        if warning["code"] in {"mixed_domains", "possible_textual_conflict"}
    ]
    accepted_codes = {str(code).strip() for code in (accepted_warning_codes or []) if str(code).strip()}
    if allow_conflicts:
        accepted_codes.update(str(warning["code"]) for warning in blocking_warnings)
    unresolved_warnings = [warning for warning in blocking_warnings if warning["code"] not in accepted_codes]
    if unresolved_warnings:
        codes = ", ".join(str(warning["code"]) for warning in unresolved_warnings)
        raise _merge_error(
            f"candidate merge has unresolved conflict warnings: {codes}",
            error_code="candidate_merge_unresolved_conflict",
            source_ref="|".join(cleaned_ids),
        )

    subject_id = None
    subject_alias = None
    if subject:
        resolved_subject, subject_alias = resolve_subject(connection, subject)
        subject_id = resolved_subject.id

    counts_before = _truth_counts(connection)
    applied_at = utc_now()
    merge_fingerprint = _merge_fingerprint(
        cleaned_ids,
        title=final_title,
        body=final_body,
        record_type=final_record_type,
        trust_level=final_trust_level,
        subject_id=subject_id,
    )
    record_id = f"mem_merge_{merge_fingerprint[:16]}"
    evidence = [_candidate_evidence(candidate, source_snapshots.get(candidate.id)) for candidate in candidates]
    role_counts = Counter(item["source_role"] for item in evidence)
    provenance = {
        "source_candidate_ids": cleaned_ids,
        "user_candidate_ids": [candidate.id for candidate in user_candidates],
        "assistant_candidate_ids": [candidate.id for candidate in assistant_candidates],
        "assistant_suggestions_separate": True,
        "merge": {
            "preview_token": cleaned_token,
            "merge_fingerprint": merge_fingerprint,
            "applied_at": applied_at,
            "applied_by": created_by,
            "source_candidate_ids": cleaned_ids,
            "user_candidate_ids": [candidate.id for candidate in user_candidates],
            "assistant_candidate_ids": [candidate.id for candidate in assistant_candidates],
            "assistant_suggestions_separate": True,
            "accepted_warning_codes": sorted(accepted_codes),
            "allow_assistant_only": allow_assistant_only,
            "conflict_warnings": preview["conflict_warnings"],
            "confidence_basis": _confidence_basis(candidates, role_counts=role_counts),
        },
        "evidence": evidence,
    }
    record_metadata = {
        "candidate_merge": True,
        "preview_token": cleaned_token,
        "merge_fingerprint": merge_fingerprint,
        "candidate_count": len(candidates),
        "candidate_review_statuses_before": {
            candidate_id: state["review_status"] for candidate_id, state in prior_states.items()
        },
        "assistant_suggestions_separate": True,
        "subject_alias_target": subject_alias,
        "rollback": {
            "record_id": record_id,
            "candidate_states_before": prior_states,
        },
    }
    links = _merge_links(record_id, candidates)

    try:
        connection.execute("BEGIN IMMEDIATE")
        locked_snapshots = _candidate_source_snapshots(connection, cleaned_ids)
        _validate_source_snapshots(locked_snapshots, candidates)
        if locked_snapshots != source_snapshots:
            raise _merge_error(
                "candidate source evidence changed after preview validation",
                error_code="candidate_merge_stale_source_evidence",
                source_ref="|".join(cleaned_ids),
            )
        current_rows = connection.execute(
            f"SELECT id, review_status, updated_at FROM candidate_memories WHERE id IN ({','.join('?' for _ in cleaned_ids)})",
            cleaned_ids,
        ).fetchall()
        current = {str(row[0]): {"review_status": str(row[1]), "updated_at": str(row[2])} for row in current_rows}
        if len(current) != len(cleaned_ids) or any(
            current[candidate_id]["review_status"] != prior_states[candidate_id]["review_status"]
            or current[candidate_id]["updated_at"] != prior_states[candidate_id]["updated_at"]
            for candidate_id in cleaned_ids
        ):
            raise _merge_error(
                "candidate state changed after preview validation; preview again before applying",
                error_code="candidate_merge_stale_preview",
                source_ref="|".join(cleaned_ids),
            )
        existing = connection.execute("SELECT id FROM memory_records WHERE id = ?", (record_id,)).fetchone()
        if existing is not None:
            raise _merge_error(
                f"merge target already exists without matching applied candidate state: {record_id}",
                error_code="candidate_merge_target_conflict",
                source_ref=record_id,
            )

        connection.execute(
            """
            INSERT INTO memory_records (
                id, record_type, title, body, subject_id, trust_level, source_kind,
                source_ref, provenance_json, status, valid_from, valid_to,
                last_verified_at, created_by, created_at, updated_at, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', NULL, NULL, ?, ?, ?, ?, ?)
            """,
            (
                record_id,
                final_record_type,
                final_title,
                final_body,
                subject_id,
                final_trust_level,
                "chatgpt_candidate_merge",
                _merge_source_ref(cleaned_ids),
                json.dumps(provenance, sort_keys=True),
                applied_at,
                created_by,
                applied_at,
                applied_at,
                json.dumps(record_metadata, sort_keys=True),
            ),
        )
        connection.executemany(
            """
            INSERT INTO memory_links (from_kind, from_id, to_kind, to_id, link_type, confidence, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    link["from_kind"],
                    link["from_id"],
                    link["to_kind"],
                    link["to_id"],
                    link["link_type"],
                    link["confidence"],
                    link["notes"],
                )
                for link in links
            ],
        )
        for candidate in candidates:
            metadata = {
                **candidate.metadata,
                "merge_target_id": record_id,
                "merge_fingerprint": merge_fingerprint,
                "merge_preview_token": cleaned_token,
                "merged_at": applied_at,
                "merged_by": created_by,
                "assistant_evidence": candidate.assistant_suggestion,
            }
            review_note = _merge_review_note(candidate.review_notes, record_id)
            result = connection.execute(
                """
                UPDATE candidate_memories
                SET review_status = 'merged', review_notes = ?, last_confirmed_at = ?,
                    metadata_json = ?, updated_at = ?
                WHERE id = ? AND review_status IN ('pending', 'approved')
                """,
                (review_note, applied_at, json.dumps(metadata, sort_keys=True), applied_at, candidate.id),
            )
            if result.rowcount != 1:
                raise _merge_error(
                    f"candidate changed while merge was applying: {candidate.id}",
                    error_code="candidate_merge_concurrent_update",
                    source_ref=candidate.id,
                )
        operation_id = f"mrg_{merge_fingerprint[:20]}"
        after_states = {
            candidate.id: {
                "review_status": "merged",
                "last_confirmed_at": applied_at,
                "merge_target_id": record_id,
            }
            for candidate in candidates
        }
        connection.execute(
            """
            INSERT INTO candidate_merge_events (
                operation_id, record_id, preview_token, merge_fingerprint, caller_run_id,
                created_by, candidate_ids_json, accepted_warning_codes_json,
                source_snapshots_json, candidate_states_before_json,
                candidate_states_after_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                operation_id,
                record_id,
                cleaned_token,
                merge_fingerprint,
                caller_run_id,
                created_by,
                json.dumps(cleaned_ids, sort_keys=True),
                json.dumps(sorted(accepted_codes)),
                json.dumps(source_snapshots, sort_keys=True),
                json.dumps(prior_states, sort_keys=True),
                json.dumps(after_states, sort_keys=True),
                applied_at,
            ),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise

    applied_candidates = [get_candidate_memory(connection, candidate_id) for candidate_id in cleaned_ids]
    return _applied_payload(
        connection,
        candidates=applied_candidates,
        record_id=record_id,
        preview_token=cleaned_token,
        prior_states=prior_states,
        counts_before=counts_before,
        idempotent=False,
    )


def _clean_candidate_ids(candidate_ids: list[str]) -> list[str]:
    cleaned: list[str] = []
    for candidate_id in candidate_ids:
        text = str(candidate_id).strip()
        if text and text not in cleaned:
            cleaned.append(text)
    return cleaned


def _truth_counts(connection: sqlite3.Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in ("candidate_memories", "memory_records", "memory_links"):
        row = connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if row and int(row[0]) > 0:
            counts[table] = int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        else:
            counts[table] = 0
    return counts


def _candidate_evidence(candidate: CandidateMemory, source_snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    snapshot = source_snapshot or {}
    source_role = str(
        snapshot.get("candidate_source_role")
        or candidate.source_links.get("source_role")
        or ("assistant" if candidate.assistant_suggestion else "unknown")
    )
    return {
        "candidate_id": candidate.id,
        "content": candidate.content,
        "memory_type": candidate.memory_type,
        "reason_type": candidate.reason_type,
        "domain_primary": candidate.domain_primary,
        "domains": candidate.domains,
        "confidence": candidate.confidence,
        "review_status": candidate.review_status,
        "assistant_suggestion": candidate.assistant_suggestion,
        "source_kind": candidate.source_kind,
        "source_ref": candidate.source_ref,
        "source_role": source_role,
        "source_ids": {
            "import_id": snapshot.get("import_id"),
            "conversation_id": snapshot.get("conversation_id") or candidate.source_links.get("conversation_id"),
            "message_id": snapshot.get("message_id") or candidate.source_links.get("message_id"),
            "chunk_id": snapshot.get("chunk_id") or candidate.source_links.get("chunk_id"),
        },
        "source_text_sha256": snapshot.get("chunk_text_sha256"),
        "source_snapshot": snapshot,
        "created_at": candidate.created_at,
        "updated_at": candidate.updated_at,
    }


def _merge_warnings(
    candidates: list[CandidateMemory],
    *,
    role_counts: Counter[str],
    status_counts: Counter[str],
    domain_counts: Counter[str],
) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    if role_counts.get("assistant", 0) or any(candidate.assistant_suggestion for candidate in candidates):
        warnings.append(
            {
                "code": "assistant_evidence_present",
                "severity": "medium",
                "message": "Assistant-authored evidence is present and must remain separate from user-reported fact.",
            }
        )
    non_pending = {status: count for status, count in status_counts.items() if status != "pending"}
    if non_pending:
        warnings.append(
            {
                "code": "non_pending_candidates",
                "severity": "medium",
                "message": "Some selected candidates are not pending; applying a merge should confirm intent.",
                "statuses": non_pending,
            }
        )
    if len(domain_counts) > 1:
        warnings.append(
            {
                "code": "mixed_domains",
                "severity": "medium",
                "message": "Selected candidates span multiple domains; verify this is one canonical object.",
                "domains": dict(sorted(domain_counts.items())),
            }
        )
    if _has_textual_tension(candidates):
        warnings.append(
            {
                "code": "possible_textual_conflict",
                "severity": "low",
                "message": "Selected evidence contains both positive and negative/corrective language.",
            }
        )
    return warnings


def _has_textual_tension(candidates: list[CandidateMemory]) -> bool:
    joined = " ".join(candidate.content.lower() for candidate in candidates)
    positive = re.search(r"\b(worked|succeeded|success|good|confirmed|prefer|like|love)\b", joined)
    negative = re.search(r"\b(failed|problem|bad|wrong|not|instead|changed|avoid|underproof|overproof)\b", joined)
    return bool(positive and negative)


def _most_common(counts: Counter[str], *, default: str) -> str:
    if not counts:
        return default
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]


def _proposed_title(candidates: list[CandidateMemory]) -> str:
    if not candidates:
        return "Merged memory candidate"
    common_domain = _most_common(Counter(candidate.domain_primary for candidate in candidates), default="memory")
    readable_domain = common_domain.replace("_", " ").title()
    first = _first_sentence(candidates[0].content)
    if len(first) > 72:
        first = first[:69].rstrip() + "..."
    return f"{readable_domain}: {first}"


def _proposed_body(*, user_candidates: list[CandidateMemory], assistant_candidates: list[CandidateMemory]) -> str:
    lines: list[str] = []
    if user_candidates:
        lines.append("User-reported evidence:")
        for candidate in user_candidates:
            lines.append(f"- {_first_sentence(candidate.content)}")
    else:
        lines.append("No user-reported evidence selected.")
    if assistant_candidates and not user_candidates:
        lines.append("Assistant suggestions are excluded from the canonical body pending explicit confirmation.")
    return "\n".join(lines)


def _first_sentence(text: str) -> str:
    cleaned = " ".join(str(text).split())
    if not cleaned:
        return ""
    match = re.search(r"(.+?[.!?])(?:\s|$)", cleaned)
    return match.group(1).strip() if match else cleaned


def _confidence_basis(candidates: list[CandidateMemory], *, role_counts: Counter[str]) -> dict[str, Any]:
    confidences = [candidate.confidence for candidate in candidates]
    return {
        "candidate_count": len(candidates),
        "average_candidate_confidence": round(sum(confidences) / len(confidences), 4) if confidences else 0.0,
        "role_counts": dict(sorted(role_counts.items())),
        "basis": "preview_only_user_evidence_preferred_assistant_evidence_separate",
    }


def _default_curated_record_type(memory_type: str) -> str:
    mapping = {
        "decision": "decision",
        "preference": "preference",
        "procedure": "workflow",
        "workaround": "lesson",
        "failure": "lesson",
        "open_loop": "open_loop",
        "skill": "workflow",
        "analogy": "lesson",
        "project": "project",
        "constraint": "lesson",
        "relationship": "contact_note",
    }
    return mapping.get(memory_type, "research_note")


def _required_text(value: str, field: str) -> str:
    cleaned = str(value).strip()
    if not cleaned:
        raise _merge_error(
            f"{field} must not be empty",
            error_code="candidate_merge_invalid_canonical_fields",
            source_ref=field,
        )
    return cleaned


def _merge_source_ref(candidate_ids: list[str]) -> str:
    digest = hashlib.sha256(json.dumps(sorted(candidate_ids)).encode("utf-8")).hexdigest()[:20]
    return f"candidate_merge:{digest}"


def _preview_token(
    candidates: list[CandidateMemory],
    proposed_record: dict[str, Any],
    source_snapshots: dict[str, dict[str, Any]],
) -> str:
    payload = {
        "candidate_states": [
            {
                "id": candidate.id,
                "review_status": candidate.review_status,
                "updated_at": candidate.updated_at,
                "content_sha256": hashlib.sha256(candidate.content.encode("utf-8")).hexdigest(),
                "source_snapshot": source_snapshots.get(candidate.id),
            }
            for candidate in candidates
        ],
        "canonical_fields": {
            "record_type": proposed_record["record_type"],
            "title": proposed_record["title"],
            "body": proposed_record["body"],
            "subject": proposed_record["subject"],
            "trust_level": proposed_record["trust_level"],
            "created_by": proposed_record["created_by"],
        },
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
    return f"merge_preview_{digest}"


def _merge_fingerprint(
    candidate_ids: list[str],
    *,
    title: str,
    body: str,
    record_type: str,
    trust_level: str,
    subject_id: str | None,
) -> str:
    payload = {
        "candidate_ids": sorted(candidate_ids),
        "title": title,
        "body": body,
        "record_type": record_type,
        "trust_level": trust_level,
        "subject_id": subject_id,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _candidate_source_snapshots(
    connection: sqlite3.Connection,
    candidate_ids: list[str],
) -> dict[str, dict[str, Any]]:
    if not candidate_ids:
        return {}
    placeholders = ",".join("?" for _ in candidate_ids)
    rows = connection.execute(
        f"""
        SELECT
            cm.id,
            cm.import_id,
            cm.conversation_id,
            cm.message_id,
            cm.chunk_id,
            cm.source_role,
            cm.source_kind,
            cm.source_ref,
            cm.valid_from,
            cm.valid_to,
            cm.last_confirmed_at,
            cm.confidence,
            m.id,
            m.role,
            COALESCE(m.is_deleted, 0),
            mc.id,
            mc.text_sha256,
            COALESCE(mc.is_deleted, 0),
            c.id,
            COALESCE(c.is_deleted, 0)
        FROM candidate_memories cm
        LEFT JOIN messages m ON m.id = cm.message_id
        LEFT JOIN message_chunks mc ON mc.id = cm.chunk_id
        LEFT JOIN conversations c ON c.id = cm.conversation_id
        WHERE cm.id IN ({placeholders})
        """,
        candidate_ids,
    ).fetchall()
    tombstoned: set[str] = set()
    if _table_exists(connection, "deletions"):
        tombstoned = {str(row[0]) for row in connection.execute("SELECT source_id FROM deletions").fetchall()}
    snapshots: dict[str, dict[str, Any]] = {}
    for row in rows:
        source_ids = {str(value) for value in (row[0], row[1], row[2], row[3], row[4], row[7]) if value}
        snapshots[str(row[0])] = {
            "candidate_id": str(row[0]),
            "import_id": str(row[1]),
            "conversation_id": str(row[2]),
            "message_id": str(row[3]),
            "chunk_id": str(row[4]),
            "candidate_source_role": str(row[5]),
            "source_kind": str(row[6]),
            "source_ref": str(row[7]),
            "valid_from": row[8],
            "valid_to": row[9],
            "last_confirmed_at": row[10],
            "confidence": float(row[11]),
            "message_exists": row[12] is not None,
            "message_role": str(row[13]) if row[13] is not None else None,
            "message_deleted": bool(row[14]),
            "chunk_exists": row[15] is not None,
            "chunk_text_sha256": str(row[16]) if row[16] is not None else None,
            "chunk_deleted": bool(row[17]),
            "conversation_exists": row[18] is not None,
            "conversation_deleted": bool(row[19]),
            "tombstoned": bool(source_ids & tombstoned),
        }
    return snapshots


def _validate_source_snapshots(
    snapshots: dict[str, dict[str, Any]],
    candidates: list[CandidateMemory],
) -> None:
    for candidate in candidates:
        snapshot = snapshots.get(candidate.id)
        if snapshot is None:
            raise _merge_error(
                f"source snapshot missing for candidate: {candidate.id}",
                error_code="candidate_merge_source_missing",
                source_ref=candidate.id,
            )
        fixture_source = str(snapshot["source_kind"]).startswith("test_fixture")
        if not fixture_source and not all(
            (snapshot["message_exists"], snapshot["chunk_exists"], snapshot["conversation_exists"])
        ):
            raise _merge_error(
                f"underlying source rows are missing for candidate: {candidate.id}",
                error_code="candidate_merge_source_missing",
                source_ref=candidate.id,
            )
        if snapshot["tombstoned"] or any(
            (snapshot["message_deleted"], snapshot["chunk_deleted"], snapshot["conversation_deleted"])
        ):
            raise _merge_error(
                f"deleted or tombstoned evidence cannot be merged: {candidate.id}",
                error_code="candidate_merge_source_blocked",
                source_ref=candidate.id,
            )
        if snapshot["message_role"] and snapshot["candidate_source_role"] != snapshot["message_role"]:
            raise _merge_error(
                f"candidate/message role mismatch: {candidate.id}",
                error_code="candidate_merge_role_mismatch",
                source_ref=candidate.id,
            )
        if snapshot["candidate_source_role"] == "assistant" and not candidate.assistant_suggestion:
            raise _merge_error(
                f"assistant-authored candidate lacks assistant_suggestion flag: {candidate.id}",
                error_code="candidate_merge_role_mismatch",
                source_ref=candidate.id,
            )


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _merge_links(record_id: str, candidates: list[CandidateMemory]) -> list[dict[str, Any]]:
    links: dict[tuple[str, str, str], dict[str, Any]] = {}

    def add(to_kind: str, to_id: Any, link_type: str, confidence: float, notes: str) -> None:
        cleaned_id = str(to_id or "").strip()
        if not cleaned_id:
            return
        key = (to_kind, cleaned_id, link_type)
        links[key] = {
            "from_kind": "memory_record",
            "from_id": record_id,
            "to_kind": to_kind,
            "to_id": cleaned_id,
            "link_type": link_type,
            "confidence": confidence,
            "notes": notes,
        }

    for candidate in candidates:
        add(
            "candidate_memory",
            candidate.id,
            "merged_from",
            candidate.confidence,
            "Selected candidate evidence for confirmed canonical merge.",
        )
        add(
            "message_chunk",
            candidate.source_links.get("chunk_id"),
            "derived_from",
            candidate.confidence,
            "Source chunk preserved from candidate merge evidence.",
        )
        add(
            "message",
            candidate.source_links.get("message_id"),
            "derived_from",
            candidate.confidence,
            "Source message preserved from candidate merge evidence.",
        )
        add(
            "conversation",
            candidate.source_links.get("conversation_id"),
            "derived_from",
            candidate.confidence,
            "Source conversation preserved from candidate merge evidence.",
        )
    return [links[key] for key in sorted(links)]


def _merge_review_note(existing: str | None, record_id: str) -> str:
    merge_note = f"Merged into {record_id} by explicit confirmed candidate merge."
    cleaned = str(existing or "").strip()
    return f"{cleaned}\n{merge_note}" if cleaned else merge_note


def _repeated_apply_target(candidates: list[CandidateMemory], *, preview_token: str) -> str | None:
    if not candidates or any(candidate.review_status != "merged" for candidate in candidates):
        return None
    targets = {str(candidate.metadata.get("merge_target_id") or "") for candidate in candidates}
    tokens = {str(candidate.metadata.get("merge_preview_token") or "") for candidate in candidates}
    if len(targets) == 1 and "" not in targets and tokens == {preview_token}:
        return next(iter(targets))
    return None


def _applied_payload(
    connection: sqlite3.Connection,
    *,
    candidates: list[CandidateMemory],
    record_id: str,
    preview_token: str,
    prior_states: dict[str, dict[str, Any]],
    counts_before: dict[str, int],
    idempotent: bool,
) -> dict[str, Any]:
    record = get_memory_record(connection, record_id)
    links = list_memory_links(connection, from_kind="memory_record", from_id=record_id)
    candidate_ids = [candidate.id for candidate in candidates]
    event = connection.execute(
        """
        SELECT operation_id, caller_run_id, created_at
        FROM candidate_merge_events
        WHERE record_id = ?
        """,
        (record_id,),
    ).fetchone()
    return {
        "mode": "apply",
        "mutates_memory_truth": True,
        "database_changed": not idempotent,
        "idempotent": idempotent,
        "idempotent_replay": idempotent,
        "preview_token": preview_token,
        "candidate_ids": candidate_ids,
        "source_candidate_ids": candidate_ids,
        "candidate_count": len(candidate_ids),
        "memory_record": record.to_dict(),
        "memory_links": [link.to_dict() for link in links],
        "candidate_memories": [candidate.to_dict() for candidate in candidates],
        "assistant_suggestions_separate": True,
        "assistant_candidate_ids": [candidate.id for candidate in candidates if candidate.assistant_suggestion],
        "user_candidate_ids": [candidate.id for candidate in candidates if not candidate.assistant_suggestion],
        "audit": {
            "operation_id": str(event[0]) if event else None,
            "caller_run_id": str(event[1]) if event and event[1] else None,
            "committed_at": str(event[2]) if event else None,
            "merge_target_id": record_id,
            "source_candidate_ids": candidate_ids,
            "candidate_states_before": prior_states,
            "candidate_states_after": {
                candidate.id: {
                    "review_status": candidate.review_status,
                    "last_confirmed_at": candidate.last_confirmed_at,
                    "merge_target_id": candidate.metadata.get("merge_target_id"),
                }
                for candidate in candidates
            },
            "rollback_metadata_recorded": True,
        },
        "mutation_policy": {
            "mutates_memory_truth": True,
            "explicitly_confirmed": True,
            "requires_preview_token": True,
            "requires_explicit_confirmation": True,
            "assistant_suggestions_promoted_to_fact": False,
        },
        "db_counts_before": counts_before,
        "db_counts_after": _truth_counts(connection),
    }


def _merge_error(message: str, *, error_code: str, source_ref: str | None) -> MemoryObservationError:
    return MemoryObservationError(
        message,
        stage="candidate_merge_apply",
        error_code=error_code,
        source_ref=source_ref,
    )
