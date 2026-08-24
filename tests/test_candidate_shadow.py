from __future__ import annotations

from local_agent_lab.memory.candidate_shadow import build_candidate_shadow_report


def _candidate(candidate_id: str, content: str, *, role: str = "user", domain: str = "cooking_baking") -> dict:
    return {
        "id": candidate_id,
        "content": content,
        "memory_type": "preference",
        "reason_type": "assistant_suggestion" if role == "assistant" else "preference_choice",
        "domains": [domain],
        "confidence": 0.8,
        "valid_from": None,
        "valid_to": None,
        "source_kind": "fixture",
        "source_ref": f"chk_{candidate_id}",
        "source_links": {
            "conversation_id": f"conv_{candidate_id}",
            "message_id": f"msg_{candidate_id}",
            "chunk_id": f"chk_{candidate_id}",
            "source_role": role,
        },
        "assistant_suggestion": role == "assistant",
        "metadata": {"conversation_title": "Fixture"},
    }


def test_shadow_report_rejects_noise_and_groups_duplicates_without_mutation() -> None:
    candidates = [
        _candidate("a", "I prefer crisp edges on rosemary focaccia."),
        _candidate("b", "I prefer rosemary focaccia with crisp edges."),
        _candidate("question", "Does this look right?"),
        _candidate("assistant", "Record ambient temperature at every starter feeding.", role="assistant"),
    ]

    report = build_candidate_shadow_report(
        candidates,
        expected_subject="Recipes and Baking",
        expected_domains=["cooking_baking"],
    )

    assert report["mutates_memory_truth"] is False
    assert report["production_rewrite_performed"] is False
    assert report["counts"] == {
        "legacy_candidates": 4,
        "sources_evaluated": 4,
        "invalid_sources": 0,
        "extracted_candidates": 3,
        "extraction_rejections": 1,
        "proposed_fact_groups": 1,
        "proposed_assistant_suggestion_groups": 1,
        "proposed_total_groups": 2,
        "candidate_reduction": 2,
    }
    assert report["extraction_rejection_reason_counts"] == {
        "context_dependent_reference": 1,
        "question_only": 1,
    }
    assert report["proposed_comparison"]["policy"]["assistant_user_groups_separate"] is True
    assert [candidate["id"] for candidate in candidates] == ["a", "b", "question", "assistant"]


def test_shadow_report_exposes_missing_source_provenance_without_failing_batch() -> None:
    valid = _candidate("valid", "I prefer crisp edges on rosemary focaccia.")
    invalid = _candidate("invalid", "I prefer dark crust on sourdough bread.")
    invalid["source_links"].pop("message_id")

    report = build_candidate_shadow_report([valid, invalid], expected_subject="Recipes and Baking")

    assert report["counts"]["invalid_sources"] == 1
    assert report["invalid_sources"][0]["reason"] == "missing_stable_source_ids"
    assert report["counts"]["sources_evaluated"] == 1


def test_shadow_report_rejects_invalid_sample_limit() -> None:
    try:
        build_candidate_shadow_report([], sample_limit=0)
    except ValueError as exc:
        assert str(exc) == "sample_limit must be at least 1"
    else:
        raise AssertionError("expected ValueError")


def test_shadow_report_rejects_synthetic_context_and_planning_groups() -> None:
    candidates = [
        _candidate(
            "prescribed",
            "I want to keep a test culture going too, using half of what you prescribed for the earlier batch.",
        ),
        _candidate(
            "water_bath",
            "Not sure I have something large enough to process all of them unless the shallow option is sufficient.",
        ),
        _candidate(
            "same_day",
            "I don't like the same day options, but I prefer staged rollout; show me new options.",
        ),
        _candidate("durable", "I prefer crisp edges on rosemary focaccia."),
    ]

    report = build_candidate_shadow_report(
        candidates,
        expected_subject="Recipes and Baking",
        expected_domains=["cooking_baking"],
    )

    assert report["counts"]["proposed_fact_groups"] == 1
    groups = report["proposed_comparison"]["proposed"]["canonical_fact_groups"]
    assert groups[0]["candidate_ids"] == ["durable"]
    assert report["counts"]["extraction_rejections"] == 3
