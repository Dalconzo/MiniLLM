from __future__ import annotations

import hashlib
from typing import Any


CONTEXT_PACKET_SCHEMA_VERSION = 2
CONTEXT_PACKET_SECTIONS = (
    "critical_constraints",
    "current_state",
    "relevant_preferences",
    "relevant_outcomes",
    "failures_and_lessons",
    "contradictions_and_qualifications",
    "inferred_patterns",
    "analogies",
    "uncertainty",
    "omitted_but_available",
)


def build_context_packet(
    *,
    query: str,
    retrieval_event_id: str,
    search_result: dict[str, Any],
    context_items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compile retrieved memory evidence into the AI Memory Contract packet shape."""
    packet_id = _packet_id(query=query, retrieval_event_id=retrieval_event_id, context_items=context_items)
    packet: dict[str, Any] = {
        "schema_version": CONTEXT_PACKET_SCHEMA_VERSION,
        "context_packet_id": packet_id,
        "task": {
            "query": query,
            "subject": _filter_value(search_result.get("filters_applied", []), "subject"),
            "depth": _first_non_empty(item.get("disclosure_tier") for item in context_items),
            "ranking_profile": search_result.get("ranking_profile"),
        },
        "critical_constraints": [],
        "current_state": [],
        "relevant_preferences": [],
        "relevant_outcomes": [],
        "failures_and_lessons": [],
        "contradictions_and_qualifications": [],
        "inferred_patterns": [],
        "analogies": [],
        "uncertainty": [],
        "omitted_but_available": [],
        "provenance": {
            "retrieval_event_id": retrieval_event_id,
            "context_packet_id": packet_id,
            "source_ids": [str(item.get("source_id")) for item in context_items if item.get("source_id")],
            "candidate_counts": search_result.get("candidate_counts", {}),
            "filters_applied": search_result.get("filters_applied", []),
            "domain_detection": search_result.get("domain_detection", {}),
            "governance": search_result.get("governance", {}),
        },
    }

    for item in context_items:
        entry = _packet_entry(item)
        section = _section_for_item(item, entry)
        packet[section].append(entry)
        if _is_uncertain(item, entry):
            packet["uncertainty"].append(_uncertainty_entry(item, entry))

    if search_result.get("governance", {}).get("high_risk"):
        packet["critical_constraints"].append(
            {
                "claim": "This query intersects a high-risk domain; downstream agents must avoid unsupported advice and preserve uncertainty.",
                "strength": "hard",
                "confidence": 1.0,
                "epistemic_status": "system_policy",
                "source_ids": [],
                "reason": "memory_governance_high_risk",
            }
        )

    _add_inferred_pattern(packet, context_items)
    _add_omission_note(packet, search_result, context_items)
    return packet


def compact_context_items(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_context_item(result) for result in results]


def _context_item(result: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "source_kind": result.get("source_kind"),
        "source_id": result.get("source_id") or result.get("chunk_id"),
        "conversation_id": result.get("conversation_id"),
        "message_id": result.get("message_id"),
        "title": result.get("title"),
        "score": result.get("score"),
        "score_breakdown": result.get("score_breakdown"),
        "disclosure_tier": result.get("disclosure_tier"),
        "exposed_fields": result.get("exposed_fields", []),
        "retrieval_sources": result.get("retrieval_sources", []),
        "epistemic_status": result.get("epistemic_status"),
        "confidence_basis": result.get("confidence_basis"),
        "record_type": result.get("record_type"),
        "trust_level": result.get("trust_level"),
        "status": result.get("status"),
        "source_role": result.get("source_role") or result.get("role"),
        "validation_checks": result.get("validation_checks", {}),
        "source_refs": result.get("source_refs", []),
        "provenance": result.get("provenance", {}),
    }
    if "snippet" in result:
        payload["snippet"] = result["snippet"]
    return payload


def _packet_entry(item: dict[str, Any]) -> dict[str, Any]:
    text = _claim_text(item)
    return {
        "claim": text,
        "confidence": _confidence(item),
        "epistemic_status": item.get("epistemic_status") or _epistemic_status(item),
        "confidence_basis": item.get("confidence_basis") or "retrieved_evidence",
        "source_ids": [str(item["source_id"])] if item.get("source_id") else [],
        "source_kind": item.get("source_kind"),
        "record_type": item.get("record_type"),
        "trust_level": item.get("trust_level"),
        "retrieval_sources": item.get("retrieval_sources", []),
    }


def _section_for_item(item: dict[str, Any], entry: dict[str, Any]) -> str:
    record_type = str(item.get("record_type") or "").lower()
    text = str(entry.get("claim") or "").lower()
    if record_type in {"constraint", "risk"} or any(token in text for token in ("must not", "do not", "blocked", "boundary", "require")):
        return "critical_constraints"
    if record_type in {"preference", "contact_note"} or "prefer" in text:
        entry["strength"] = "strong" if item.get("trust_level") in {"canonical", "high"} else "contextual"
        return "relevant_preferences"
    if record_type in {"lesson"} or any(token in text for token in ("failed", "failure", "wrong", "avoid", "lesson")):
        return "failures_and_lessons"
    if record_type in {"decision", "workflow", "project", "open_loop"}:
        return "current_state"
    if any(token in text for token in ("outcome", "worked", "completed", "attempted", "successful", "mixed")):
        return "relevant_outcomes"
    checks = item.get("validation_checks", {})
    contradiction = checks.get("contradiction", {}) if isinstance(checks, dict) else {}
    if contradiction.get("status") in {"possible", "review"}:
        return "contradictions_and_qualifications"
    if item.get("source_role") == "assistant":
        return "uncertainty"
    return "current_state"


def _uncertainty_entry(item: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    reason = "assistant-authored evidence requires confirmation" if item.get("source_role") == "assistant" else "low-confidence retrieved evidence"
    return {
        "claim": entry["claim"],
        "reason": reason,
        "confidence": entry["confidence"],
        "source_ids": entry["source_ids"],
        "epistemic_status": entry["epistemic_status"],
    }


def _add_inferred_pattern(packet: dict[str, Any], context_items: list[dict[str, Any]]) -> None:
    if len(context_items) < 2:
        return
    domains = sorted(
        {
            str(item.get("provenance", {}).get("domain_primary") or "")
            for item in context_items
            if isinstance(item.get("provenance"), dict) and item.get("provenance", {}).get("domain_primary")
        }
    )
    packet["inferred_patterns"].append(
        {
            "pattern": "Multiple retrieved memories appear relevant to this task; use the factual sections as evidence, not as an answer draft.",
            "confidence": 0.5,
            "supporting_ids": [str(item.get("source_id")) for item in context_items if item.get("source_id")][:5],
            "epistemic_status": "system_inference",
            "confidence_basis": "retrieval_set_pattern",
            "domains": domains,
        }
    )


def _add_omission_note(packet: dict[str, Any], search_result: dict[str, Any], context_items: list[dict[str, Any]]) -> None:
    counts = search_result.get("candidate_counts", {}) if isinstance(search_result.get("candidate_counts"), dict) else {}
    available = int(counts.get("fts", 0) or 0) + int(counts.get("vector", 0) or 0) + int(counts.get("curated", 0) or 0)
    omitted = max(0, available - len(context_items))
    if omitted:
        packet["omitted_but_available"].append(
            {
                "category": "retrieved_candidates",
                "count": omitted,
                "reason": "bounded_context_packet",
            }
        )
    if not context_items:
        packet["omitted_but_available"].append(
            {
                "category": "raw_history",
                "reason": "no context items survived retrieval, filtering, and governance",
            }
        )


def _claim_text(item: dict[str, Any]) -> str:
    snippet = str(item.get("snippet") or "").strip()
    title = str(item.get("title") or "").strip()
    if snippet:
        return snippet
    return title or str(item.get("source_id") or "retrieved memory item")


def _confidence(item: dict[str, Any]) -> float:
    trust = str(item.get("trust_level") or "").lower()
    if trust == "canonical":
        return 0.92
    if trust == "high":
        return 0.8
    if item.get("source_role") == "assistant":
        return 0.35
    if item.get("source_kind") == "curated_memory":
        return 0.65
    return 0.55


def _epistemic_status(item: dict[str, Any]) -> str:
    if item.get("source_role") == "assistant":
        return "assistant_hypothesis"
    if item.get("source_kind") == "curated_memory":
        return "current_canonical_claim" if item.get("trust_level") in {"canonical", "high"} else "current_claim"
    return "raw_evidence"


def _is_uncertain(item: dict[str, Any], entry: dict[str, Any]) -> bool:
    return item.get("source_role") == "assistant" or float(entry.get("confidence") or 0.0) < 0.5


def _packet_id(*, query: str, retrieval_event_id: str, context_items: list[dict[str, Any]]) -> str:
    source_ids = ",".join(str(item.get("source_id") or "") for item in context_items)
    digest = hashlib.sha256(f"{retrieval_event_id}\n{query}\n{source_ids}".encode("utf-8")).hexdigest()[:16]
    return f"ctx_{digest}"


def _filter_value(filters: Any, field: str) -> Any:
    if not isinstance(filters, list):
        return None
    for item in filters:
        if isinstance(item, dict) and item.get("field") == field:
            return item.get("value")
    return None


def _first_non_empty(values: Any) -> Any:
    for value in values:
        if value:
            return value
    return None
