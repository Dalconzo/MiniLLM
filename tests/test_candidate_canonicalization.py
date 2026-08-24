from __future__ import annotations

import copy
import json

import pytest

from local_agent_lab.memory.candidate_canonicalization import (
    build_shadow_comparison,
    canonicalize_candidate_set,
    compare_candidate_sets,
    normalize_candidate_claim,
)
from local_agent_lab.memory.candidates import CandidateMemory


def _candidate(
    candidate_id: str,
    content: str,
    *,
    role: str = "user",
    domains: list[str] | None = None,
    confidence: float = 0.7,
    memory_type: str = "preference",
    metadata: dict | None = None,
) -> dict:
    assistant = role == "assistant"
    return {
        "id": candidate_id,
        "content": content,
        "memory_type": "assistant_suggestion" if assistant else memory_type,
        "reason_type": "assistant_suggestion" if assistant else "preference_choice",
        "domains": domains or ["cooking_baking"],
        "confidence": confidence,
        "valid_from": "2026-08-02T00:00:00Z",
        "valid_to": None,
        "source_kind": "chatgpt_export",
        "source_ref": f"chk_{candidate_id}",
        "source_role": role,
        "source_links": {
            "conversation_id": f"conv_{candidate_id}",
            "message_id": f"msg_{candidate_id}",
            "chunk_id": f"chk_{candidate_id}",
            "source_role": role,
        },
        "assistant_suggestion": assistant,
        "metadata": metadata or {},
    }


def test_normalize_standalone_user_claim_and_preserve_provenance() -> None:
    source = _candidate("pref_crisp", "  User: I prefer crisp edges on rosemary focaccia  ")

    result = normalize_candidate_claim(source, expected_subject="Recipes and Baking")

    assert result["accepted"] is True
    assert result["normalized_claim"] == "The user prefers crisp edges on rosemary focaccia."
    assert result["canonical_fact_eligible"] is True
    assert result["accepted_reasons"] == ["standalone_candidate", "user_evidence_fact_eligible"]
    assert result["provenance"] == {
        "candidate_id": "pref_crisp",
        "source_kind": "chatgpt_export",
        "source_ref": "chk_pref_crisp",
        "source_role": "user",
        "source_links": source["source_links"],
        "assistant_suggestion": False,
    }


def test_normalization_rewrites_internal_first_person_after_preference_prefix() -> None:
    result = normalize_candidate_claim(
        _candidate(
            "summery",
            "I like summery things but I want a range of summer flavors besides the standard.",
        )
    )

    assert result["accepted"] is True
    assert result["normalized_claim"] == (
        "The user likes summery things and wants a range of summer flavors besides the standard."
    )


def test_normalization_removes_vague_idea_wrapper_from_named_preference() -> None:
    result = normalize_candidate_claim(_candidate("stew", "I like the idea of a good stew."))

    assert result["accepted"] is True
    assert result["normalized_claim"] == "The user likes a good stew."


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        ("like this?", "question_only"),
        ("these are delicious", "unresolved_context_reference"),
        ("massive success, these are delicious and could become a staple", "unresolved_context_reference"),
        ("I like 1, 2 and 4", "unresolved_context_reference"),
        ("I like 1 2 and 4", "unresolved_context_reference"),
        ("Call memory_review_subjects and report the run_id", "test_or_tool_instruction"),
    ],
)
def test_normalization_rejects_non_standalone_or_meta_candidates(content: str, reason: str) -> None:
    result = normalize_candidate_claim(_candidate("noisy", content))

    assert result["accepted"] is False
    assert reason in result["rejected_reasons"]


def test_explicit_object_resolution_makes_deictic_claim_standalone() -> None:
    candidate = _candidate(
        "resolved",
        "these are delicious and could become a staple",
        metadata={"resolved_object": "Hybrid sourdough kouign-amann"},
    )

    result = normalize_candidate_claim(candidate)

    assert result["accepted"] is True
    assert result["normalized_claim"] == "Hybrid sourdough kouign-amann are delicious and could become a staple."


@pytest.mark.parametrize(
    ("candidate_id", "content", "reason"),
    [
        (
            "fixture_context_01",
            "I want to keep a test culture going too, using half of what you prescribed for the earlier batch.",
            "unresolved_context_reference",
        ),
        (
            "fixture_context_03",
            "Not sure I have a container large enough for all of them unless the shallow option is sufficient.",
            "unresolved_context_reference",
        ),
        (
            "fixture_plan_01",
            "I was going to repeat the batch, but setup is inconvenient before I leave.",
            "transient_request_or_planning",
        ),
        (
            "fixture_mixed_01",
            "I do not like the immediate options, but I prefer long-fermentation methods; show me alternatives.",
            "mixed_transient_and_durable_claims",
        ),
        (
            "fixture_inventory_01",
            "I already have flour, sugar, salt, yeast, and oil.",
            "temporally_volatile_state",
        ),
        (
            "fixture_state_01",
            "The test culture is taking quite a lot of time to change in the chamber.",
            "episodic_state_missing_conditions",
        ),
        ("cold_fragment", "Something cold, we like gin the best or vodka.", "unresolved_context_reference"),
        ("cocktail_fragment", "I think this cocktail is a swing and a miss.", "unresolved_context_reference"),
        ("tomorrow_plan", "Tomorrow we have all day for a bake or dinner.", "transient_request_or_planning"),
        ("grocery_open_loop", "I still need groceries though.", "transient_request_or_planning"),
        ("next_bake", "I think for the next bake we should iterate on one recipe.", "transient_request_or_planning"),
    ],
)
def test_synthetic_noisy_claims_fail_closed(candidate_id: str, content: str, reason: str) -> None:
    result = normalize_candidate_claim(_candidate(candidate_id, content))

    assert result["accepted"] is False
    assert reason in result["rejected_reasons"]


@pytest.mark.parametrize(
    ("candidate_id", "content", "reason"),
    [
        ("unnamed_mix", "Mix has fallen 25% and smells more acidic about 15 hours later.", "unresolved_context_reference"),
        ("unnamed_rye", "The rye is very tangy and the mix is extremely yeasty.", "unresolved_context_reference"),
        (
            "room_equipment",
            "I have a room for A/C control and can get whatever equipment makes this likely to succeed.",
            "unresolved_context_reference",
        ),
        (
            "ambient_range",
            "I’m using ambient temperature because we’re hitting a high of 81 during the day and a low of 72 at night.",
            "temporally_volatile_state",
        ),
        ("flour_inventory", "We can go a little complex to start I have many types of flour.", "transient_request_or_planning"),
        (
            "mixed_formula_plan",
            "We need overripe bananas and extra strong matcha, then she can prep it herself one or two days a week.",
            "unresolved_context_reference",
        ),
    ],
)
def test_latest_browser_final_group_defects_fail_closed(candidate_id: str, content: str, reason: str) -> None:
    result = normalize_candidate_claim(_candidate(candidate_id, content))

    assert result["accepted"] is False
    assert reason in result["rejected_reasons"]


def test_explicit_negative_preferences_are_normalized_without_correcting_source_words() -> None:
    result = normalize_candidate_claim(
        _candidate("flavor_dislikes", "I don't like mango chili lime, coconut panda, black sesame vanilla.")
    )

    assert result["accepted"] is True
    assert result["normalized_claim"] == (
        "The user does not like mango chili lime, coconut panda, black sesame vanilla."
    )


def test_exact_and_near_duplicates_form_one_user_fact_group() -> None:
    candidates = [
        _candidate("a", "I prefer crisp edges on rosemary focaccia."),
        _candidate("b", "I prefer rosemary focaccia with crisp edges."),
        _candidate("c", "I prefer crisp edges on rosemary focaccia."),
    ]

    result = canonicalize_candidate_set(candidates)

    assert result["proposed_fact_count"] == 1
    assert result["assistant_suggestion_group_count"] == 0
    assert len(result["duplicate_groups"]) == 1
    group = result["canonical_fact_groups"][0]
    assert group["candidate_ids"] == ["a", "b", "c"]
    assert group["duplicate_kind"] == "near"
    assert group["canonical_fact_eligible"] is True
    assert {item["candidate_id"] for item in group["source_provenance"]} == {"a", "b", "c"}


def test_user_and_assistant_duplicates_are_never_grouped_together() -> None:
    content = "The starter should be tracked at room temperature after feeding."
    result = canonicalize_candidate_set(
        [
            _candidate("user", content, role="user", memory_type="procedure"),
            _candidate("assistant", content, role="assistant"),
        ]
    )

    assert result["proposed_group_count"] == 2
    assert result["proposed_fact_count"] == 1
    assert result["assistant_suggestion_group_count"] == 1
    assert result["canonical_fact_groups"][0]["candidate_ids"] == ["user"]
    suggestion = result["assistant_suggestion_groups"][0]
    assert suggestion["candidate_ids"] == ["assistant"]
    assert suggestion["canonical_fact_eligible"] is False
    assert suggestion["canonical_claim"] is None
    assert suggestion["assistant_suggestion"] == content
    assert result["policy"]["assistant_user_groups_separate"] is True


def test_assistant_only_evidence_never_produces_a_canonical_fact() -> None:
    result = canonicalize_candidate_set(
        [
            _candidate("assistant_a", "Record ambient temperature at each starter feeding.", role="assistant"),
            _candidate("assistant_b", "Record the ambient temperature at every starter feeding.", role="assistant"),
        ]
    )

    assert result["proposed_fact_count"] == 0
    assert result["assistant_suggestion_group_count"] == 1
    assert result["canonical_fact_groups"] == []
    assert result["assistant_suggestion_groups"][0]["confidence_basis"]["assistant_suggestion_only"] is True


def test_opposing_user_claims_are_reported_as_conflict_not_merged() -> None:
    result = canonicalize_candidate_set(
        [
            _candidate("old", "I prefer maintaining the starter at low hydration."),
            _candidate("new", "I do not prefer maintaining the starter at low hydration."),
        ]
    )

    assert result["proposed_fact_count"] == 2
    assert result["duplicate_groups"] == []
    assert result["claim_relations"] == [
        {
            "relation_type": "textual_conflict",
            "candidate_ids": ["new", "old"],
            "reason": "opposing polarity over similar proposition anchors",
            "anchor_similarity": 1.0,
            "merge_allowed": False,
        }
    ]


def test_conditional_variants_are_qualifications_not_duplicates() -> None:
    result = canonicalize_candidate_set(
        [
            _candidate("shallow", "For shallow financiers, an 18 minute bake works well.", memory_type="procedure"),
            _candidate("deep", "For deeper fruit-filled financiers, a 22 to 24 minute bake works well.", memory_type="procedure"),
        ]
    )

    assert result["proposed_fact_count"] == 2
    assert result["claim_relations"][0]["relation_type"] == "conditional_qualification"
    assert result["claim_relations"][0]["merge_allowed"] is False


def test_subject_leakage_is_reported_and_hard_domain_mismatch_is_rejected() -> None:
    candidate = _candidate(
        "career",
        "My manager wants the automation project delivered before Friday.",
        domains=["career_work", "lab_automation"],
        metadata={"subjects": ["Career and Job Search"]},
    )

    result = normalize_candidate_claim(candidate, expected_subject="Recipes and Baking")

    assert result["accepted"] is False
    assert "subject_domain_mismatch" in result["rejected_reasons"]
    codes = {indicator["code"] for indicator in result["subject_leakage_indicators"]}
    assert codes == {
        "explicit_subject_mismatch",
        "subject_domain_mismatch",
        "weak_subject_text_evidence",
    }


def test_shadow_comparison_reports_counts_reasons_duplicates_and_leakage() -> None:
    candidates = [
        _candidate("pref_a", "I prefer crisp edges on rosemary focaccia."),
        _candidate("pref_b", "I prefer rosemary focaccia with crisp edges."),
        _candidate("question", "does this look right?"),
        _candidate("tip", "The assistant recommends weighing every starter feed.", role="assistant"),
        _candidate(
            "career",
            "My manager wants the automation project delivered before Friday.",
            domains=["career_work"],
        ),
    ]

    report = build_shadow_comparison(candidates, expected_subject="Recipes and Baking")

    assert report["mutates_memory_truth"] is False
    assert report["counts"] == {
        "legacy_candidates": 5,
        "accepted_candidates": 3,
        "rejected_candidates": 2,
        "proposed_fact_groups": 1,
        "proposed_assistant_suggestion_groups": 1,
        "proposed_total_groups": 2,
        "candidate_reduction": 3,
    }
    assert report["accepted_reason_counts"] == {
        "assistant_suggestion_separated": 1,
        "standalone_candidate": 3,
        "user_evidence_fact_eligible": 2,
    }
    assert report["rejected_reason_counts"] == {
        "question_only": 1,
        "subject_domain_mismatch": 1,
    }
    assert report["duplicate_groups"][0]["candidate_ids"] == ["pref_a", "pref_b"]
    assert {item["candidate_id"] for item in report["subject_leakage_indicators"]} >= {"career"}
    assert {decision["candidate_id"] for decision in report["decisions"]} == {
        "career",
        "pref_a",
        "pref_b",
        "question",
        "tip",
    }
    json.dumps(report)


def test_compare_accepts_precomputed_proposal_and_is_deterministic() -> None:
    candidates = [
        _candidate("b", "I prefer a dark chocolate finish on Black Forest cake."),
        _candidate("a", "I prefer Black Forest cake with a dark chocolate finish."),
    ]
    proposed = canonicalize_candidate_set(candidates)

    first = compare_candidate_sets(candidates, proposed)
    second = compare_candidate_sets(list(reversed(candidates)))

    assert first == second
    assert first["duplicate_groups"][0]["candidate_ids"] == ["a", "b"]


def test_pipeline_accepts_candidate_memory_dataclass_without_mutation() -> None:
    candidate = CandidateMemory(
        id="dataclass_candidate",
        content="I prefer less-sweet cola flavor without artificial cherry notes.",
        memory_type="preference",
        reason_type="preference_choice",
        domains=["cooking_baking"],
        confidence=0.75,
        valid_from="2026-06-01T00:00:00Z",
        valid_to=None,
        last_confirmed_at=None,
        source_kind="chatgpt_export",
        source_ref="chk_dataclass",
        source_links={"source_role": "user", "chunk_id": "chk_dataclass"},
        review_status="pending",
        review_notes=None,
        origin="chatgpt_export",
        assistant_suggestion=False,
        created_at="2026-06-01T00:00:00Z",
        updated_at="2026-06-01T00:00:00Z",
        metadata={},
    )
    before = copy.deepcopy(candidate.to_dict())

    report = build_shadow_comparison([candidate], expected_subject="Recipes and Baking")

    assert report["counts"]["proposed_fact_groups"] == 1
    assert candidate.to_dict() == before


def test_invalid_similarity_threshold_fails_before_producing_output() -> None:
    with pytest.raises(ValueError, match="between 0 and 1"):
        canonicalize_candidate_set([_candidate("a", "I prefer crisp focaccia edges.")], near_duplicate_threshold=1.1)


def test_generator_domain_scope_is_applied_to_every_candidate() -> None:
    candidates = [
        _candidate("career_a", "My manager scheduled the project review.", domains=["career_work"]),
        _candidate("career_b", "My resume should emphasize automation work.", domains=["career_work"]),
    ]

    result = canonicalize_candidate_set(
        candidates,
        expected_domains=(domain for domain in ["cooking_baking"]),
    )

    assert result["accepted_candidate_count"] == 0
    assert {
        item["candidate_id"]: item["rejected_reasons"]
        for item in result["rejected_candidates"]
    } == {
        "career_a": ["subject_domain_mismatch"],
        "career_b": ["subject_domain_mismatch"],
    }
