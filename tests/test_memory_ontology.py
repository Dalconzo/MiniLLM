from __future__ import annotations

import pytest

from local_agent_lab.memory.ontology import (
    CANONICAL_CONFIDENCE_BASES,
    CANONICAL_DOMAINS,
    CANONICAL_EPISTEMIC_STATUSES,
    CANONICAL_LENSES,
    CANONICAL_MEMORY_TYPES,
    CANONICAL_REASON_TYPES,
    CURATED_RECORD_TYPES,
    classify_source_monitoring,
    validate_confidence_basis,
    validate_domain,
    validate_epistemic_status,
    validate_lens,
    validate_memory_type,
    validate_reason_type,
)
from local_agent_lab.memory.domain_scoping import scope_candidate_domains


def test_canonical_ontology_includes_initial_release_domains_and_types() -> None:
    assert "cooking_baking" in CANONICAL_DOMAINS
    assert "lab_automation" in CANONICAL_DOMAINS
    assert "finance_investing" in CANONICAL_DOMAINS
    assert "episodic" in CANONICAL_MEMORY_TYPES
    assert "assistant_suggestion" in CANONICAL_REASON_TYPES
    assert "analogy" in CANONICAL_LENSES
    assert "assistant_suggested" in CANONICAL_EPISTEMIC_STATUSES
    assert "assistant_suggestion_only" in CANONICAL_CONFIDENCE_BASES
    assert "decision" in CURATED_RECORD_TYPES


def test_validate_ontology_values_rejects_unknown_tokens() -> None:
    assert validate_domain("cooking_baking") == "cooking_baking"
    assert validate_memory_type("semantic_fact") == "semantic_fact"
    assert validate_reason_type("user_reported_outcome") == "user_reported_outcome"
    assert validate_lens("health_caution") == "health_caution"
    assert validate_epistemic_status("assistant_suggested") == "assistant_suggested"
    assert validate_confidence_basis("direct_user_outcome_report") == "direct_user_outcome_report"

    with pytest.raises(ValueError):
        validate_domain("random_topic")


def test_classify_source_monitoring_distinguishes_user_and_assistant_sources() -> None:
    user = classify_source_monitoring("user")
    assistant = classify_source_monitoring("assistant")
    confirmed = classify_source_monitoring("assistant", confirmed_by_user=True)

    assert user == {
        "source_role": "user",
        "epistemic_status": "user_reported",
        "confidence_basis": "single_user_statement",
    }
    assert assistant == {
        "source_role": "assistant",
        "epistemic_status": "assistant_suggested",
        "confidence_basis": "assistant_suggestion_only",
    }
    assert confirmed["epistemic_status"] == "user_reported"
    assert confirmed["confidence_basis"] == "direct_user_outcome_report"


def test_scope_candidate_domains_blocks_unrelated_domains_by_default() -> None:
    allowed, relation = scope_candidate_domains(["lab_automation"], ["finance_investing"], effort=2)
    relaxed, relaxed_relation = scope_candidate_domains(["lab_automation"], ["finance_investing"], effort=4)
    adjacent, adjacent_relation = scope_candidate_domains(["cooking_baking"], ["health_supplements"], effort=3)

    assert not allowed
    assert relation == "excluded"
    assert relaxed
    assert relaxed_relation == "analogy"
    assert adjacent
    assert adjacent_relation == "transfer"
