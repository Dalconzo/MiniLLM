from __future__ import annotations

import json

import pytest

from local_agent_lab.memory.candidate_extraction import (
    CandidateExtractionValidationError,
    CandidateSource,
    classify_rejection_reasons,
    heuristic_extract_candidate,
    heuristic_extract_candidates,
    parse_model_extraction_output,
    validate_model_extraction_payload,
)


def _source(
    text: str,
    *,
    role: str = "user",
    suffix: str = "1",
    title: str = "Sourdough starter tracking",
) -> dict[str, object]:
    return {
        "conversation_id": f"conv-{suffix}",
        "message_id": f"msg-{suffix}",
        "id": f"chunk-{suffix}",
        "role": role,
        "text": text,
        "title": title,
    }


def _candidate(source: dict[str, object], **overrides: object) -> dict[str, object]:
    assistant = source.get("role") == "assistant"
    value: dict[str, object] = {
        "candidate_text": "The sourdough starter rose about 75 percent after its most recent feeding.",
        "memory_type": "episodic",
        "reason_type": "assistant_suggestion" if assistant else "user_reported_outcome",
        "subject": "Recipes and Baking",
        "subject_confidence": 0.95,
        "domain": "cooking_baking",
        "domain_confidence": 0.96,
        "durability_score": 0.82,
        "standalone_context_score": 0.94,
        "confidence": 0.84,
        "source_ids": {
            "conversation_id": source["conversation_id"],
            "message_id": source["message_id"],
            "chunk_id": source["id"],
        },
        "source_role": source["role"],
        "epistemic_status": "assistant_suggested" if assistant else "user_reported",
        "assistant_suggestion": assistant,
    }
    value.update(overrides)
    return value


def _payload(
    *, candidates: list[dict[str, object]], rejections: list[dict[str, object]] | None = None
) -> dict[str, object]:
    return {
        "schema_version": "candidate_extraction_v1",
        "candidates": candidates,
        "rejections": rejections or [],
    }


def _rejection(source: dict[str, object], *reasons: str) -> dict[str, object]:
    return {
        "source_ids": {
            "conversation_id": source["conversation_id"],
            "message_id": source["message_id"],
            "chunk_id": source["id"],
        },
        "source_role": source["role"],
        "rejection_reasons": list(reasons),
    }


def test_heuristic_extracts_standalone_user_outcome_with_provenance() -> None:
    result = heuristic_extract_candidate(
        _source("my sourdough starter rose 75 percent and then fell after the feeding")
    )

    assert not result.rejections
    candidate = result.candidates[0]
    assert candidate.candidate_text == "My sourdough starter rose 75 percent and then fell after the feeding."
    assert candidate.memory_type == "episodic"
    assert candidate.reason_type == "user_reported_outcome"
    assert candidate.domain == "cooking_baking"
    assert candidate.subject == "Recipes and Baking"
    assert candidate.source_ids.chunk_id == "chunk-1"
    assert candidate.epistemic_status == "user_reported"
    assert candidate.assistant_suggestion is False


def test_heuristic_keeps_assistant_content_separate_as_suggestion() -> None:
    result = heuristic_extract_candidate(
        _source(
            "Feed the sourdough starter at a one-to-two-to-two ratio before the next bake.",
            role="assistant",
        )
    )

    candidate = result.candidates[0]
    assert candidate.memory_type == "hypothesis"
    assert candidate.reason_type == "assistant_suggestion"
    assert candidate.source_role == "assistant"
    assert candidate.epistemic_status == "assistant_suggested"
    assert candidate.assistant_suggestion is True


def test_heuristic_extracts_durable_sentence_without_trailing_question() -> None:
    result = heuristic_extract_candidate(_source("I prefer sourdough with a dark crust. What should I bake next?"))

    assert result.candidates[0].candidate_text == "I prefer sourdough with a dark crust."


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("Does this look right?", "question_only"),
        ("I like 1, 2 and 4", "option_only_reference"),
        ("Use that one", "context_dependent_reference"),
        ("Acceptance test: call memory_review_subjects now", "acceptance_test_or_meta_instruction"),
        ("Do it", "transient_execution_chatter"),
        ("Run the next smoke test", "transient_execution_chatter"),
        (
            "The local shop stocks it; is there a late-opening market for stone-ground bread?",
            "context_dependent_reference",
        ),
        (
            "Would stone-ground bread be better? The fictional neighborhood shop stocks it; which late-opening market should I try?",
            "question_only",
        ),
        (
            "Great result, these are delicious and it was easy once the dough was cold enough.",
            "context_dependent_reference",
        ),
        ("I like this one the most, alongside the first.", "context_dependent_reference"),
        ("Know what actually, let's make tomato bisque and grilled cheese tonight.", "transient_request_or_planning"),
        ("Could I keep this mixture for several days, or must I use it now?", "question_only"),
        ("Walk me through the laminated pastry; I have the ingredients in the bowl.", "transient_request_or_planning"),
        ("Give me a few options for a bake.", "transient_request_or_planning"),
        ("Suggest ingredients I could add to this recipe.", "transient_request_or_planning"),
        ("Actually let's just do the clafoutis.", "transient_request_or_planning"),
        ("Kinda messed up the first cut.", "context_dependent_reference"),
        ("I need a shopping list I can get at Trader Joe's.", "transient_request_or_planning"),
        ("Took the apples off because they were cooking, how do they look", "question_only"),
        ("Morning after, looks like they have grown quite a lot.", "context_dependent_reference"),
        ("I like option A but want fewer ingredients.", "context_dependent_reference"),
        ("I wonder if this could be an Adderall interaction.", "question_only"),
        ("Swap operator A and operator B and list each person's next task.", "transient_request_or_planning"),
        ("I kind of like the second two suggestions.", "context_dependent_reference"),
        (
            "I want to keep a starter going and use half of what you prescribed for the Amann.",
            "context_dependent_reference",
        ),
        ("I am not sure I want something that sharp from those fruits.", "context_dependent_reference"),
        ("Not sure I have something large enough to hold all of them.", "context_dependent_reference"),
        ("I was gonna make the choux nuts again before I go.", "transient_request_or_planning"),
        (
            "I don't like the same day options, but I prefer the sourdough idea and want new options.",
            "mixed_transient_and_durable_claims",
        ),
        ("I already have miso, white chocolate, pistachio, rosewater, and oranges.", "temporally_volatile_state"),
        ("The unnamed culture is taking a long time to rise in the chamber.", "insufficient_standalone_context"),
        ("Something cold, we like gin the best or vodka.", "context_dependent_reference"),
        ("I think this drink is a miss for the group.", "context_dependent_reference"),
        ("Starter rose to here by hour 12, same as the others.", "context_dependent_reference"),
        ("I have two 8-inch rounds.", "context_dependent_reference"),
        ("Tomorrow we have all day for a bake or dinner.", "transient_request_or_planning"),
        ("I still need groceries though.", "transient_request_or_planning"),
        (
            "This need not involve baking; I am looking for help brainstorming new projects.",
            "transient_request_or_planning",
        ),
        ("I’ll get the 4 channel MOSFET board too.", "transient_request_or_planning"),
        ("I think for the next bake we should iterate on a recipe.", "transient_request_or_planning"),
        ("I think the rye doubled in that window this time.", "context_dependent_reference"),
        ("We can make a rye loaf or something.", "context_dependent_reference"),
        ("I have starter we could use in this.", "context_dependent_reference"),
        ("After mix the whole wheat smells very yeasty.", "context_dependent_reference"),
    ],
)
def test_heuristic_rejects_known_conversational_noise(text: str, reason: str) -> None:
    result = heuristic_extract_candidate(_source(text))

    assert not result.candidates
    assert reason in result.rejections[0].rejection_reasons


def test_heuristic_rejects_empty_and_oversized_sources() -> None:
    empty = heuristic_extract_candidate(_source("  "))
    oversized = heuristic_extract_candidate(_source("starter " * 100))

    assert empty.rejections[0].rejection_reasons == ("empty_source",)
    assert "source_too_long" in oversized.rejections[0].rejection_reasons


def test_heuristic_keeps_self_contained_success_even_when_it_contains_a_constraint() -> None:
    result = heuristic_extract_candidate(
        _source(
            "The synthetic laminated pastry worked reliably once I learned when the dough needed to be chilled again."
        )
    )

    assert not result.rejections
    assert result.candidates[0].memory_type == "episodic"
    assert result.candidates[0].reason_type == "user_reported_outcome"


def test_heuristic_salvages_a_standalone_preference_before_a_followup_request() -> None:
    result = heuristic_extract_candidate(
        _source("I like the pavlova roulade, walk me through it and suggest low-effort tasks.")
    )

    assert not result.rejections
    assert result.candidates[0].candidate_text == "I like the pavlova roulade."


def test_heuristic_rejects_temporally_volatile_pantry_inventory() -> None:
    result = heuristic_extract_candidate(
        _source("I already have miso, rose petals, orange blossom water, and pistachios.")
    )

    assert not result.candidates
    assert result.rejections[0].rejection_reasons == ("temporally_volatile_state",)


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        ("mix has fallen 20 percent and smells sharper several hours later", "context_dependent_reference"),
        ("the rye is very tangy and the mix is extremely yeasty", "context_dependent_reference"),
        (
            "I’m going with ambient temperature because we’re hitting a high of 81 during the day and a low of 72 at night",
            "temporally_volatile_state",
        ),
        (
            "we can go fairly complex to start because the test pantry has several grain types",
            "transient_request_or_planning",
        ),
        (
            "I have a room for A/C control and we can get whatever equipment makes this likely to succeed",
            "context_dependent_reference",
        ),
    ],
)
def test_latest_browser_context_and_inventory_failures_are_rejected(content: str, reason: str) -> None:
    result = heuristic_extract_candidate(_source(content))

    assert result.candidates == ()
    assert reason in result.rejections[0].rejection_reasons


def test_mixed_flavor_message_with_ambiguous_names_fails_closed() -> None:
    source = _source(
        "I want only selected flavors and some new ones: I do not like smoked plum, sesame mint, "
        "or toasted rice, but the other flavors are fine and we should explore more"
    )

    result = heuristic_extract_candidate(source)

    assert result.candidates == ()
    assert "insufficient_standalone_context" in result.rejections[0].rejection_reasons


def test_mixed_formula_message_drops_weekly_prep_plan() -> None:
    source = _source(
        "we need roasted pears, concentrated hojicha with restrained syrup so tannin offsets the dairy, "
        "and then to figure out a way to provide weekly batch service",
        title="Pear Hojicha Recipe",
    )

    result = heuristic_extract_candidate(source)

    assert result.rejections == ()
    assert result.candidates[0].candidate_text == (
        "The Pear Hojicha recipe needs roasted pears and concentrated hojicha with restrained syrup so tannin offsets the dairy."
    )


@pytest.mark.parametrize(
    ("content", "title", "expected"),
    [
        (
            "I prefer restarting a practice batch when necessary so I can learn each fermentation stage.",
            "Synthetic Fermentation Practice",
            "I prefer restarting a practice batch when necessary so I can learn each fermentation stage.",
        ),
        (
            "I prefer individual meringue nests and poached meringues over a single large dessert.",
            "Synthetic Meringue Preferences",
            "I prefer individual meringue nests and poached meringues over a single large dessert.",
        ),
        (
            "I prefer experimental pastry projects using novel food-chemistry techniques.",
            "Synthetic Pastry Research",
            "I prefer experimental pastry projects using novel food-chemistry techniques.",
        ),
        (
            "I prefer future bakes to reuse leftover components from earlier recipes.",
            "Synthetic Leftover Reuse",
            "I prefer future bakes to reuse leftover components from earlier recipes.",
        ),
    ],
)
def test_self_contained_durable_claims_are_preserved_without_fixture_specific_rewrites(
    content: str,
    title: str,
    expected: str,
) -> None:
    result = heuristic_extract_candidate(_source(content, title=title))

    assert result.rejections == ()
    assert result.candidates[0].candidate_text == expected
    assert result.candidates[0].memory_type == "preference"


def test_heuristic_overrides_a_legacy_cooking_label_when_text_is_clearly_health_related() -> None:
    source = _source("My symptoms were consistent with a medication interaction after substance use.")
    source["domain"] = "cooking_baking"
    source["domain_confidence"] = 0.65
    result = heuristic_extract_candidate(source)

    assert result.candidates[0].domain == "health_supplements"
    assert result.candidates[0].subject == "Health and Supplements"


def test_domain_inference_does_not_classify_rose_petals_as_pets() -> None:
    source = _source("I prefer rose petals with white chocolate and pistachios in baking.")
    source["domain"] = "cooking_baking"
    source["domain_confidence"] = 0.65
    result = heuristic_extract_candidate(source)

    assert result.candidates[0].domain == "cooking_baking"


def test_domain_inference_recognizes_electronics_inside_a_mislabeled_subject() -> None:
    source = _source("The ESP32 uses a four-channel MOSFET board for the lighting controller.")
    source["domain"] = "cooking_baking"
    source["domain_confidence"] = 0.65

    result = heuristic_extract_candidate(source)

    assert result.candidates[0].domain == "home_projects"


def test_high_risk_domain_wins_a_tie_against_legacy_cooking_scope() -> None:
    source = _source("I ate sourdough first because I wanted a lower glycemic meal.")
    source["domain"] = "cooking_baking"
    source["domain_confidence"] = 0.65
    result = heuristic_extract_candidate(source)

    assert result.candidates[0].domain == "health_supplements"


def test_rejection_classifier_returns_stable_multiple_reasons() -> None:
    reasons = classify_rejection_reasons("Acceptance test: does this look right?")

    assert reasons == [
        "acceptance_test_or_meta_instruction",
        "context_dependent_reference",
        "question_only",
    ]


def test_batch_is_bounded_and_rejects_duplicate_source_ids() -> None:
    duplicate = _source("I prefer sourdough with a dark crust.")

    with pytest.raises(CandidateExtractionValidationError, match="duplicate source IDs"):
        heuristic_extract_candidates([duplicate, duplicate])


def test_source_mapping_requires_traceable_ids_and_supported_role() -> None:
    with pytest.raises(CandidateExtractionValidationError, match="chunk_id"):
        CandidateSource.from_mapping(
            {"conversation_id": "c", "message_id": "m", "role": "user", "text": "A durable fact."}
        )
    with pytest.raises(CandidateExtractionValidationError, match="must be one of"):
        CandidateSource.from_mapping(_source("A durable fact.", role="tool"))


def test_valid_model_json_is_parsed_and_round_trips() -> None:
    source = _source("My sourdough starter rose after feeding.")
    raw = json.dumps(_payload(candidates=[_candidate(source)]))

    result = parse_model_extraction_output(raw, sources=[source])

    assert result.candidates[0].domain == "cooking_baking"
    assert result.to_dict() == json.loads(raw)


def test_valid_model_rejection_accounts_for_source() -> None:
    source = _source("Like this?")
    payload = _payload(candidates=[], rejections=[_rejection(source, "question_only", "context_dependent_reference")])

    result = validate_model_extraction_payload(payload, sources=[source])

    assert not result.candidates
    assert result.rejections[0].rejection_reasons == ("question_only", "context_dependent_reference")


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        '{"schema_version":"candidate_extraction_v1"}',
    ],
)
def test_malformed_model_output_fails_closed(raw: str) -> None:
    with pytest.raises(CandidateExtractionValidationError):
        parse_model_extraction_output(raw, sources=[_source("A durable source statement.")])


def test_model_output_rejects_unknown_root_and_candidate_fields() -> None:
    source = _source("My starter rose after feeding.")
    extra_root = {**_payload(candidates=[_candidate(source)]), "commentary": "looks good"}
    extra_candidate = _candidate(source, explanation="invented field")

    with pytest.raises(CandidateExtractionValidationError, match="unexpected"):
        validate_model_extraction_payload(extra_root, sources=[source])
    with pytest.raises(CandidateExtractionValidationError, match="unexpected"):
        validate_model_extraction_payload(_payload(candidates=[extra_candidate]), sources=[source])


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"domain": "invented_domain"}, "invalid memory domain"),
        ({"memory_type": "personal_fact"}, "invalid memory type"),
        ({"reason_type": "model_guess"}, "invalid memory reason type"),
        ({"durability_score": 0.59}, "below acceptance threshold"),
        ({"standalone_context_score": 0.69}, "below acceptance threshold"),
        ({"confidence": 2.0}, "number from 0 to 1"),
    ],
)
def test_model_candidate_requires_canonical_values_and_thresholds(override: dict[str, object], message: str) -> None:
    source = _source("My starter rose after feeding.")

    with pytest.raises(CandidateExtractionValidationError, match=message):
        validate_model_extraction_payload(_payload(candidates=[_candidate(source, **override)]), sources=[source])


def test_model_cannot_launder_assistant_suggestion_into_user_truth() -> None:
    source = _source("Feed the starter twice daily.", role="assistant")

    with pytest.raises(CandidateExtractionValidationError, match="assistant_suggestion contradicts"):
        validate_model_extraction_payload(
            _payload(candidates=[_candidate(source, assistant_suggestion=False)]),
            sources=[source],
        )
    with pytest.raises(CandidateExtractionValidationError, match="must be assistant_suggested"):
        validate_model_extraction_payload(
            _payload(candidates=[_candidate(source, epistemic_status="user_reported")]),
            sources=[source],
        )


def test_model_reason_type_must_match_source_authorship() -> None:
    assistant = _source("Feed the starter twice daily.", role="assistant", suffix="assistant")
    user = _source("My starter rose after feeding.", suffix="user")

    with pytest.raises(CandidateExtractionValidationError, match="must be assistant_suggestion"):
        validate_model_extraction_payload(
            _payload(candidates=[_candidate(assistant, reason_type="ideal_procedure")]),
            sources=[assistant],
        )
    with pytest.raises(CandidateExtractionValidationError, match="cannot be assistant_suggestion"):
        validate_model_extraction_payload(
            _payload(candidates=[_candidate(user, reason_type="assistant_suggestion")]),
            sources=[user],
        )


def test_model_cannot_invent_or_alter_source_ids() -> None:
    source = _source("My starter rose after feeding.")
    invented = _candidate(source)
    invented["source_ids"] = {
        "conversation_id": "other",
        "message_id": "other",
        "chunk_id": "other",
    }

    with pytest.raises(CandidateExtractionValidationError, match="do not match supplied evidence"):
        validate_model_extraction_payload(_payload(candidates=[invented]), sources=[source])


def test_model_must_account_for_every_source_exactly_once_by_decision() -> None:
    accepted = _source("My starter rose after feeding.", suffix="accepted")
    omitted = _source("Do it", suffix="omitted")

    with pytest.raises(CandidateExtractionValidationError, match="omitted source decisions"):
        validate_model_extraction_payload(_payload(candidates=[_candidate(accepted)]), sources=[accepted, omitted])


def test_model_source_cannot_be_both_candidate_and_rejection() -> None:
    source = _source("My starter rose after feeding.")
    payload = _payload(
        candidates=[_candidate(source)],
        rejections=[_rejection(source, "insufficient_durability")],
    )

    with pytest.raises(CandidateExtractionValidationError, match="both accepted and rejected"):
        validate_model_extraction_payload(payload, sources=[source])


def test_model_candidate_text_must_pass_noise_gates() -> None:
    source = _source("Does this look right?")

    with pytest.raises(CandidateExtractionValidationError, match="standalone gates"):
        validate_model_extraction_payload(
            _payload(candidates=[_candidate(source, candidate_text="Does this look right?")]),
            sources=[source],
        )


def test_rejections_require_known_unique_reason_codes() -> None:
    source = _source("Like this?")

    with pytest.raises(CandidateExtractionValidationError, match="invalid rejection reason"):
        validate_model_extraction_payload(
            _payload(candidates=[], rejections=[_rejection(source, "model_said_no")]),
            sources=[source],
        )
    with pytest.raises(CandidateExtractionValidationError, match="contains duplicates"):
        validate_model_extraction_payload(
            _payload(candidates=[], rejections=[_rejection(source, "question_only", "question_only")]),
            sources=[source],
        )


def test_model_output_candidate_count_is_bounded_per_source() -> None:
    source = _source("My starter rose after feeding.")
    candidates = [
        _candidate(source, candidate_text=f"The sourdough starter produced durable result number {index}.")
        for index in range(4)
    ]

    with pytest.raises(CandidateExtractionValidationError, match="too many candidates|exceeds"):
        validate_model_extraction_payload(_payload(candidates=candidates), sources=[source])


def test_contract_does_not_mutate_source_mappings() -> None:
    source = _source("my starter rose after feeding")
    original = dict(source)

    heuristic_extract_candidate(source)

    assert source == original
