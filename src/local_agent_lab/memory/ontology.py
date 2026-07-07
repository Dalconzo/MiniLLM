from __future__ import annotations

from typing import Any


CANONICAL_DOMAINS = (
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
)

CANONICAL_MEMORY_TYPES = (
    "analogy",
    "decision",
    "episodic",
    "failure",
    "hypothesis",
    "open_loop",
    "preference",
    "procedure",
    "project",
    "risk",
    "semantic_fact",
    "skill",
    "source_note",
    "constraint",
    "workaround",
    "relationship",
    "health_note",
    "financial_note",
)

CURATED_RECORD_TYPES = (
    "contact_note",
    "decision",
    "lesson",
    "open_loop",
    "preference",
    "project",
    "research_note",
    "workflow",
)

CANONICAL_REASON_TYPES = (
    "accident",
    "assistant_suggestion",
    "constraint_response",
    "error_recovery",
    "experiment",
    "ideal_procedure",
    "unknown",
    "user_reported_outcome",
    "preference_choice",
    "workaround",
)

CANONICAL_LENSES = (
    "analogy",
    "constraint",
    "creativity",
    "failure_mode",
    "financial_caution",
    "health_caution",
    "identity_pattern",
    "operational",
    "planning",
    "preference",
    "procedural",
    "relationship_context",
    "risk",
    "skill_progression",
    "source_authority",
    "temporal",
    "contradiction",
    "workaround",
)

CANONICAL_EPISTEMIC_STATUSES = (
    "assistant_inferred",
    "assistant_suggested",
    "confirmed",
    "contradicted",
    "counterfactual",
    "externally_sourced",
    "observed",
    "speculative",
    "system_inferred",
    "unknown",
    "user_reported",
)

CANONICAL_CONFIDENCE_BASES = (
    "assistant_inference",
    "assistant_suggestion_only",
    "contradicted_once",
    "direct_user_outcome_report",
    "multiple_sources_agree",
    "old_import",
    "recent_confirmation",
    "single_user_statement",
    "uncertain_context",
    "repeated_user_confirmation",
)

VALID_SUBJECT_KINDS = ("subject", "project", "workflow")
VALID_TRUST_LEVELS = ("low", "medium", "high", "canonical")
VALID_RECORD_STATUSES = ("active", "stale", "superseded", "archived", "deleted")


def canonical_contract() -> dict[str, tuple[str, ...]]:
    return {
        "domains": CANONICAL_DOMAINS,
        "memory_types": CANONICAL_MEMORY_TYPES,
        "curated_record_types": CURATED_RECORD_TYPES,
        "reason_types": CANONICAL_REASON_TYPES,
        "lenses": CANONICAL_LENSES,
        "epistemic_statuses": CANONICAL_EPISTEMIC_STATUSES,
        "confidence_bases": CANONICAL_CONFIDENCE_BASES,
        "subject_kinds": VALID_SUBJECT_KINDS,
        "trust_levels": VALID_TRUST_LEVELS,
        "record_statuses": VALID_RECORD_STATUSES,
    }


def classify_source_monitoring(
    source_role: str | None,
    *,
    confirmed_by_user: bool = False,
    epistemic_status: str | None = None,
    confidence_basis: str | None = None,
) -> dict[str, str]:
    normalized_role = (source_role or "unknown").strip().lower()

    if epistemic_status is None:
        if confirmed_by_user or normalized_role == "user":
            epistemic_status = "user_reported"
        elif normalized_role == "assistant":
            epistemic_status = "assistant_suggested"
        elif normalized_role in {"tool", "system"}:
            epistemic_status = "system_inferred"
        else:
            epistemic_status = "unknown"
    epistemic_status = validate_epistemic_status(epistemic_status)

    if confidence_basis is None:
        if epistemic_status == "user_reported":
            confidence_basis = "direct_user_outcome_report" if confirmed_by_user else "single_user_statement"
        elif epistemic_status == "assistant_suggested":
            confidence_basis = "assistant_suggestion_only"
        elif epistemic_status in {"assistant_inferred", "system_inferred"}:
            confidence_basis = "assistant_inference"
        else:
            confidence_basis = "uncertain_context"
    confidence_basis = validate_confidence_basis(confidence_basis)

    return {
        "source_role": normalized_role,
        "epistemic_status": epistemic_status,
        "confidence_basis": confidence_basis,
    }


def validate_domain(domain: str) -> str:
    normalized = _normalize_enum(domain)
    if normalized not in CANONICAL_DOMAINS:
        raise ValueError(f"invalid memory domain: {domain}")
    return normalized


def validate_memory_type(memory_type: str) -> str:
    normalized = _normalize_enum(memory_type)
    if normalized not in CANONICAL_MEMORY_TYPES:
        raise ValueError(f"invalid memory type: {memory_type}")
    return normalized


def validate_curated_record_type(record_type: str) -> str:
    normalized = _normalize_enum(record_type)
    if normalized not in CURATED_RECORD_TYPES:
        raise ValueError(f"invalid memory record type: {record_type}")
    return normalized


def validate_reason_type(reason_type: str) -> str:
    normalized = _normalize_enum(reason_type)
    if normalized not in CANONICAL_REASON_TYPES:
        raise ValueError(f"invalid memory reason type: {reason_type}")
    return normalized


def validate_lens(lens: str) -> str:
    normalized = _normalize_enum(lens)
    if normalized not in CANONICAL_LENSES:
        raise ValueError(f"invalid memory lens: {lens}")
    return normalized


def validate_epistemic_status(status: str) -> str:
    normalized = _normalize_enum(status)
    if normalized not in CANONICAL_EPISTEMIC_STATUSES:
        raise ValueError(f"invalid epistemic status: {status}")
    return normalized


def validate_confidence_basis(basis: str) -> str:
    normalized = _normalize_enum(basis)
    if normalized not in CANONICAL_CONFIDENCE_BASES:
        raise ValueError(f"invalid confidence basis: {basis}")
    return normalized


def validate_subject_kind(kind: str) -> str:
    normalized = _normalize_enum(kind)
    if normalized not in VALID_SUBJECT_KINDS:
        raise ValueError(f"invalid subject kind: {kind}")
    return normalized


def validate_trust_level(trust_level: str) -> str:
    normalized = _normalize_enum(trust_level)
    if normalized not in VALID_TRUST_LEVELS:
        raise ValueError(f"invalid memory trust level: {trust_level}")
    return normalized


def validate_record_status(status: str) -> str:
    normalized = _normalize_enum(status)
    if normalized not in VALID_RECORD_STATUSES:
        raise ValueError(f"invalid memory record status: {status}")
    return normalized


def _normalize_enum(value: str) -> str:
    return value.strip().lower().replace("-", "_").replace(" ", "_")
