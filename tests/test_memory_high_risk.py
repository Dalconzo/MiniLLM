from __future__ import annotations

import pytest

from local_agent_lab.memory.high_risk import (
    DOMAIN_POLICIES,
    HIGH_RISK_POLICY_VERSION,
    apply_high_risk_policy,
    evaluate_high_risk_evidence,
    high_risk_policy_for_domain,
)


def test_high_risk_domain_policies_are_explicit_and_inspectable() -> None:
    assert set(DOMAIN_POLICIES) == {
        "health_supplements",
        "finance_investing",
        "law_lsat",
        "relationships_life",
    }
    assert high_risk_policy_for_domain("health").to_dict() == {
        "domain": "health_supplements",
        "caution_labels": ["health_caution"],
        "max_age_days": 30,
        "minimum_trust": "high",
        "authoritative_source_required_for_advice": True,
    }
    assert high_risk_policy_for_domain("not_high_risk") is None


@pytest.mark.parametrize(
    ("domain", "label"),
    [
        ("health_supplements", "health_caution"),
        ("finance_investing", "financial_caution"),
        ("law_lsat", "legal_caution"),
        ("relationships_life", "relationship_caution"),
    ],
)
def test_recent_user_reported_facts_are_allowed_without_invented_curated_trust(domain: str, label: str) -> None:
    decision = evaluate_high_risk_evidence(
        query_domains=[domain],
        candidate_domains=[domain],
        source_role="user",
        epistemic_status="user_reported",
        age_days=2,
        source_id="chunk-user",
    )

    assert decision.disposition == "allow_fact"
    assert decision.include_in_factual_context is True
    assert decision.include_in_uncertainty is False
    assert decision.refuse_unsupported_advice is False
    assert label in decision.caution_labels
    assert decision.checks["trust"]["raw_user_evidence_exempt"] is True
    assert decision.to_dict()["policy"] == HIGH_RISK_POLICY_VERSION


def test_assistant_evidence_is_suggestion_only_and_cannot_support_health_advice() -> None:
    decision = evaluate_high_risk_evidence(
        query_domains=["health"],
        candidate_domains=["health_supplements"],
        source_role="assistant",
        epistemic_status="assistant_suggested",
        age_days=1,
        advice_requested=True,
        source_id="chunk-assistant",
    )

    assert decision.disposition == "suggestion_only"
    assert decision.include_in_factual_context is False
    assert decision.include_in_uncertainty is True
    assert decision.supports_advice is False
    assert decision.refuse_unsupported_advice is True
    assert decision.review_required is True
    assert "assistant_or_inferred_evidence_only" in decision.reasons
    assert decision.checks["advice_support"]["status"] == "refuse"


def test_user_confirmation_can_promote_assistant_source_to_user_fact_but_not_advice() -> None:
    decision = evaluate_high_risk_evidence(
        query_domains=["relationships"],
        candidate_domains=["relationships_life"],
        source_role="assistant",
        epistemic_status="assistant_suggested",
        confirmed_by_user=True,
        age_days=3,
        advice_requested=True,
    )

    assert decision.disposition == "allow_fact"
    assert decision.include_in_factual_context is True
    assert decision.supports_advice is False
    assert decision.refuse_unsupported_advice is True


@pytest.mark.parametrize("status", ["stale", "superseded", "archived", "deleted"])
def test_inactive_high_risk_evidence_is_rejected(status: str) -> None:
    decision = evaluate_high_risk_evidence(
        query_domains=["finance"],
        candidate_domains=["finance_investing"],
        source_role="user",
        age_days=1,
        status=status,
    )

    assert decision.disposition == "reject"
    assert "inactive_high_risk_evidence" in decision.reasons
    assert decision.checks["record_status"] == {"status": "fail", "value": status}


@pytest.mark.parametrize(
    ("domain", "age_days", "max_age_days"),
    [
        ("finance_investing", 8, 7),
        ("health_supplements", 31, 30),
        ("law_lsat", 31, 30),
        ("relationships_life", 91, 90),
    ],
)
def test_domain_specific_staleness_thresholds_reject_old_evidence(
    domain: str,
    age_days: float,
    max_age_days: float,
) -> None:
    decision = evaluate_high_risk_evidence(
        query_domains=[domain],
        candidate_domains=[domain],
        source_role="user",
        age_days=age_days,
    )

    assert decision.disposition == "reject"
    assert "stale_high_risk_evidence" in decision.reasons
    assert decision.checks["temporal"]["max_age_days"] == max_age_days


@pytest.mark.parametrize("trust_level", ["low", "medium"])
def test_explicit_low_trust_high_risk_records_are_rejected(trust_level: str) -> None:
    decision = evaluate_high_risk_evidence(
        query_domains=["legal"],
        candidate_domains=["law_lsat"],
        source_role="user",
        trust_level=trust_level,
        age_days=1,
    )

    assert decision.disposition == "reject"
    assert "low_trust_high_risk_evidence" in decision.reasons
    assert decision.checks["trust"]["minimum"] == "high"


def test_current_authoritative_high_trust_source_can_support_advice() -> None:
    decision = evaluate_high_risk_evidence(
        query_domains=["legal"],
        candidate_domains=["law_lsat"],
        source_role="tool",
        epistemic_status="externally_sourced",
        trust_level="high",
        age_days=1,
        advice_requested=True,
        authoritative_source=True,
    )

    assert decision.disposition == "allow_fact"
    assert decision.supports_advice is True
    assert decision.refuse_unsupported_advice is False
    assert decision.checks["advice_support"]["status"] == "supported"


def test_cross_domain_candidate_is_blocked_even_when_current_and_canonical() -> None:
    decision = evaluate_high_risk_evidence(
        query_domains=["health_supplements"],
        candidate_domains=["finance_investing"],
        source_role="user",
        trust_level="canonical",
        age_days=0,
    )

    assert decision.disposition == "reject"
    assert decision.matched_domains == ()
    assert decision.reasons[0] == "cross_domain_blocked"
    assert decision.checks["domain_boundary"]["cross_domain_allowed"] is False


def test_unknown_temporal_validity_is_rejected_instead_of_assumed_current() -> None:
    decision = evaluate_high_risk_evidence(
        query_domains=["relationships_life"],
        candidate_domains=["relationships_life"],
        source_role="user",
        age_days=None,
    )

    assert decision.disposition == "reject"
    assert "temporal_validity_unknown" in decision.reasons


def test_batch_policy_separates_facts_suggestions_and_rejections() -> None:
    result = apply_high_risk_policy(
        [
            {
                "source_id": "user-fact",
                "domains": ["health_supplements"],
                "source_role": "user",
                "epistemic_status": "user_reported",
                "age_days": 1,
            },
            {
                "source_id": "assistant-idea",
                "domains": ["health_supplements"],
                "source_role": "assistant",
                "age_days": 1,
            },
            {
                "source_id": "old-note",
                "domains": ["health_supplements"],
                "source_role": "user",
                "age_days": 60,
            },
        ],
        query_domains=["health_supplements"],
        advice_requested=True,
    )

    assert result["result_quality"] == "reliable_results"
    assert [item["source_id"] for item in result["results"]] == ["user-fact"]
    assert [item["source_id"] for item in result["suggestion_only"]] == ["assistant-idea"]
    assert [item["source_id"] for item in result["rejected_decisions"]] == ["old-note"]
    assert result["counts"] == {"input": 3, "factual": 1, "suggestion_only": 1, "rejected": 1}
    assert result["refuse_unsupported_advice"] is True
    assert result["mutates_memory_truth"] is False


def test_batch_policy_returns_safe_empty_result_when_all_evidence_is_unreliable() -> None:
    result = apply_high_risk_policy(
        [
            {
                "source_id": "stale-finance",
                "domains": ["finance_investing"],
                "source_role": "user",
                "status": "stale",
                "age_days": 1,
            },
            {
                "source_id": "low-trust-finance",
                "domains": ["finance_investing"],
                "source_role": "user",
                "trust_level": "low",
                "age_days": 1,
            },
        ],
        query_domains=["finance"],
        advice_requested=True,
    )

    assert result["status"] == "ok"
    assert result["result_quality"] == "no_reliable_results"
    assert result["results"] == []
    assert result["suggestion_only"] == []
    assert result["counts"] == {"input": 2, "factual": 0, "suggestion_only": 0, "rejected": 2}
    assert result["refuse_unsupported_advice"] is True
    assert all(item["disposition"] == "reject" for item in result["rejected_decisions"])


def test_non_high_risk_query_is_left_unchanged() -> None:
    result = apply_high_risk_policy(
        [{"source_id": "recipe", "domains": ["cooking_baking"], "source_role": "assistant"}],
        query_domains=["cooking_baking"],
    )

    assert result["policy"] == "not_applicable"
    assert result["high_risk"] is False
    assert [item["source_id"] for item in result["results"]] == ["recipe"]
