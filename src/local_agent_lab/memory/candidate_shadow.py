from __future__ import annotations

from collections import Counter
from typing import Any, Iterable, Sequence

from .candidate_canonicalization import build_shadow_comparison
from .candidate_extraction import (
    MAX_SOURCES_PER_BATCH,
    CandidateExtractionValidationError,
    heuristic_extract_candidates,
)


CANDIDATE_SHADOW_SCHEMA_VERSION = 1


def build_candidate_shadow_report(
    legacy_candidates: Sequence[Any],
    *,
    expected_subject: str | None = None,
    expected_domains: Iterable[str] | None = None,
    sample_limit: int = 20,
) -> dict[str, Any]:
    """Compare the legacy queue with strict extraction and canonicalization without writes."""
    if sample_limit < 1:
        raise ValueError("sample_limit must be at least 1")
    domain_scope = tuple(expected_domains or ())
    sources: list[dict[str, Any]] = []
    source_to_candidate: dict[tuple[str, str, str], str] = {}
    source_to_legacy: dict[tuple[str, str, str], Any] = {}
    invalid_sources: list[dict[str, Any]] = []

    for candidate in legacy_candidates:
        candidate_id = str(_value(candidate, "id", "") or "")
        source_links = dict(_value(candidate, "source_links", {}) or {})
        source_ids = {
            "conversation_id": source_links.get("conversation_id"),
            "message_id": source_links.get("message_id"),
            "chunk_id": source_links.get("chunk_id") or _value(candidate, "source_ref", None),
        }
        if not candidate_id or not all(source_ids.values()):
            invalid_sources.append(
                {
                    "candidate_id": candidate_id or None,
                    "reason": "missing_stable_source_ids",
                    "source_ids": source_ids,
                }
            )
            continue
        source_role = str(
            source_links.get("source_role")
            or ("assistant" if bool(_value(candidate, "assistant_suggestion", False)) else "user")
        )
        domains = list(_value(candidate, "domains", []) or [])
        metadata = dict(_value(candidate, "metadata", {}) or {})
        source = {
            "source_ids": source_ids,
            "source_role": source_role,
            "text": str(_value(candidate, "content", "") or ""),
            "title": metadata.get("conversation_title"),
            "subject": metadata.get("subject"),
            "subject_confidence": metadata.get("subject_confidence"),
            "domain": domains[0] if domains else None,
            "domain_confidence": metadata.get("domain_confidence", 0.65 if domains else None),
        }
        key = tuple(str(source_ids[name]) for name in ("conversation_id", "message_id", "chunk_id"))
        if key in source_to_candidate:
            invalid_sources.append(
                {
                    "candidate_id": candidate_id,
                    "reason": "duplicate_stable_source_ids",
                    "source_ids": source_ids,
                }
            )
            continue
        source_to_candidate[key] = candidate_id
        source_to_legacy[key] = candidate
        sources.append(source)

    extracted_candidates: list[dict[str, Any]] = []
    extraction_rejections: list[dict[str, Any]] = []
    for offset in range(0, len(sources), MAX_SOURCES_PER_BATCH):
        batch = heuristic_extract_candidates(sources[offset : offset + MAX_SOURCES_PER_BATCH])
        for candidate in batch.candidates:
            payload = candidate.to_dict()
            key = candidate.source_ids.key
            legacy_candidate = source_to_legacy[key]
            extracted_candidates.append(
                {
                    "id": source_to_candidate[key],
                    "content": payload["candidate_text"],
                    "memory_type": payload["memory_type"],
                    "reason_type": payload["reason_type"],
                    "domains": [payload["domain"]],
                    "confidence": payload["confidence"],
                    "valid_from": _value(legacy_candidate, "valid_from", None),
                    "valid_to": _value(legacy_candidate, "valid_to", None),
                    "source_kind": "candidate_extraction_shadow",
                    "source_ref": payload["source_ids"]["chunk_id"],
                    "source_role": payload["source_role"],
                    "source_links": {
                        **payload["source_ids"],
                        "source_role": payload["source_role"],
                    },
                    "assistant_suggestion": payload["assistant_suggestion"],
                    "metadata": {
                        "subject": payload["subject"],
                        "subject_confidence": payload["subject_confidence"],
                        "domain_confidence": payload["domain_confidence"],
                        "durability_score": payload["durability_score"],
                        "standalone_context_score": payload["standalone_context_score"],
                        "epistemic_status": payload["epistemic_status"],
                        "source_created_at": _value(legacy_candidate, "created_at", None),
                    },
                }
            )
        for rejection in batch.rejections:
            payload = rejection.to_dict()
            key = rejection.source_ids.key
            extraction_rejections.append(
                {
                    "candidate_id": source_to_candidate[key],
                    **payload,
                }
            )

    legacy_comparison = build_shadow_comparison(
        legacy_candidates,
        expected_subject=expected_subject,
        expected_domains=domain_scope,
    )
    proposed_comparison = build_shadow_comparison(
        extracted_candidates,
        expected_subject=expected_subject,
        expected_domains=domain_scope,
    )
    rejection_counts = Counter(
        reason for rejection in extraction_rejections for reason in rejection["rejection_reasons"]
    )
    legacy_count = len(legacy_candidates)
    extracted_count = len(extracted_candidates)
    proposed_count = proposed_comparison["counts"]["proposed_total_groups"]
    return {
        "schema_version": CANDIDATE_SHADOW_SCHEMA_VERSION,
        "status": "ok",
        "mode": "candidate_extraction_canonicalization_shadow",
        "mutates_memory_truth": False,
        "production_rewrite_performed": False,
        "expected_subject": expected_subject,
        "expected_domains": sorted(domain_scope),
        "counts": {
            "legacy_candidates": legacy_count,
            "sources_evaluated": len(sources),
            "invalid_sources": len(invalid_sources),
            "extracted_candidates": extracted_count,
            "extraction_rejections": len(extraction_rejections),
            "proposed_fact_groups": proposed_comparison["counts"]["proposed_fact_groups"],
            "proposed_assistant_suggestion_groups": proposed_comparison["counts"][
                "proposed_assistant_suggestion_groups"
            ],
            "proposed_total_groups": proposed_count,
            "candidate_reduction": legacy_count - proposed_count,
        },
        "extraction_rejection_reason_counts": dict(sorted(rejection_counts.items())),
        "invalid_sources": invalid_sources[:sample_limit],
        "extracted_candidate_sample": extracted_candidates[:sample_limit],
        "extraction_rejection_sample": extraction_rejections[:sample_limit],
        "legacy_comparison": _compact_comparison(legacy_comparison, sample_limit=sample_limit),
        "proposed_comparison": _compact_comparison(proposed_comparison, sample_limit=sample_limit),
        "policy": {
            "shadow_only": True,
            "assistant_suggestions_separate": True,
            "assistant_only_fact_promotion_allowed": False,
            "malformed_model_output_fails_closed": True,
            "requires_human_acceptance_before_queue_rewrite": True,
        },
    }


def _compact_comparison(report: dict[str, Any], *, sample_limit: int) -> dict[str, Any]:
    return {
        "counts": report["counts"],
        "accepted_reason_counts": report["accepted_reason_counts"],
        "rejected_reason_counts": report["rejected_reason_counts"],
        "decisions": report["decisions"][:sample_limit],
        "duplicate_groups": report["duplicate_groups"][:sample_limit],
        "conflicts": report["conflicts"][:sample_limit],
        "qualifications": report["qualifications"][:sample_limit],
        "subject_leakage_indicators": report["subject_leakage_indicators"][:sample_limit],
        "proposed": {
            "canonical_fact_groups": report["proposed"]["canonical_fact_groups"][:sample_limit],
            "assistant_suggestion_groups": report["proposed"]["assistant_suggestion_groups"][:sample_limit],
        },
        "policy": report["policy"],
    }


def _value(candidate: Any, name: str, default: Any) -> Any:
    if isinstance(candidate, dict):
        return candidate.get(name, default)
    return getattr(candidate, name, default)


__all__ = [
    "CANDIDATE_SHADOW_SCHEMA_VERSION",
    "CandidateExtractionValidationError",
    "build_candidate_shadow_report",
]
