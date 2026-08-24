from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import Counter
from difflib import SequenceMatcher
from typing import Any, Iterable, Mapping, Sequence


DEFAULT_NEAR_DUPLICATE_THRESHOLD = 0.78

_SUBJECT_DOMAINS = {
    "ai-memory-and-local-llms": {"ai_memory_systems"},
    "career-and-job-search": {"career_work"},
    "finance-and-investing": {"finance_investing"},
    "health-and-supplements": {"health_supplements"},
    "home-projects-and-devices": {"home_projects"},
    "lab-automation": {"lab_automation"},
    "recipes-and-baking": {"cooking_baking"},
    "relationships-and-life": {"relationships_life"},
}

_DOMAIN_TERMS = {
    "ai_memory_systems": {"agent", "embedding", "llm", "memory", "model", "retrieval", "vector"},
    "career_work": {"career", "client", "job", "manager", "resume", "work"},
    "cooking_baking": {
        "bake",
        "baking",
        "bread",
        "cook",
        "dough",
        "fermentation",
        "flavor",
        "food",
        "oven",
        "recipe",
        "starter",
    },
    "finance_investing": {"finance", "fund", "invest", "market", "portfolio", "stock", "trade"},
    "health_supplements": {"dose", "health", "medication", "sleep", "supplement", "symptom"},
    "home_projects": {"device", "garage", "home", "repair", "sensor", "shelf", "tool"},
    "lab_automation": {"assay", "automation", "csv", "lab", "parser", "pipette", "plate", "robot"},
    "relationships_life": {"dating", "family", "friend", "partner", "relationship"},
}

_STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "but",
    "by",
    "can",
    "do",
    "does",
    "for",
    "from",
    "had",
    "has",
    "have",
    "i",
    "in",
    "is",
    "it",
    "my",
    "of",
    "on",
    "or",
    "that",
    "the",
    "this",
    "to",
    "user",
    "was",
    "we",
    "were",
    "with",
}

_TOKEN_ALIASES = {
    "liked": "like",
    "likes": "like",
    "loved": "love",
    "loves": "love",
    "preferred": "prefer",
    "prefers": "prefer",
    "repeatable": "repeat",
    "repeated": "repeat",
    "successful": "success",
    "succeeded": "success",
    "worked": "success",
    "working": "success",
}

_NEGATION_TERMS = {"avoid", "dislike", "doesnt", "dont", "hate", "never", "no", "not", "without"}
_CONTEXT_PRONOUNS = r"(?:it|this|that|these|those|they|them|he|she)"
_QUESTION_START = re.compile(
    r"^(?:can|could|did|do|does|how|is|should|was|were|what|when|where|which|who|why|will|would)\b",
    re.IGNORECASE,
)
_TEST_INSTRUCTION = re.compile(
    r"\b(?:acceptance test|candidate review filter test|call memory_|run[_ -]?id|trace[_ -]?id|tools?/list)\b",
    re.IGNORECASE,
)
_CONDITION_MARKER = re.compile(r"\b(?:except|for|if|unless|when|whenever|while|with|without)\b", re.IGNORECASE)
_CORRECTION_MARKER = re.compile(
    r"\b(?:actually|correction|instead|no longer|rather than|that was wrong|used to)\b",
    re.IGNORECASE,
)


def normalize_candidate_claim(
    candidate: Any,
    *,
    expected_subject: str | None = None,
    expected_domains: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Normalize and assess one candidate without changing the input object.

    The result intentionally retains rejected candidates and their provenance so a
    shadow run can explain every difference from the legacy queue.
    """

    candidate_id = _candidate_id(candidate)
    raw_content = str(_value(candidate, "content", "") or "")
    normalized_claim = _normalize_text(raw_content)
    metadata = _as_mapping(_value(candidate, "metadata", {}))
    source_links = _as_mapping(_value(candidate, "source_links", {}))
    assistant_suggestion = bool(_value(candidate, "assistant_suggestion", False))
    source_role = _source_role(candidate, assistant_suggestion=assistant_suggestion)
    if source_role == "assistant":
        assistant_suggestion = True
    evidence_class = "assistant_suggestion" if assistant_suggestion else "user_evidence"
    domains = _candidate_domains(candidate)
    subjects = _candidate_subjects(metadata, source_links)
    resolved_object = _resolved_object(metadata)

    normalized_claim, reference_resolved = _resolve_leading_reference(normalized_claim, resolved_object)
    normalized_claim = _normalize_user_perspective(normalized_claim, source_role=source_role)
    normalized_claim = _finish_sentence(normalized_claim)

    expected = _resolve_expected_domains(expected_subject, expected_domains)
    leakage = _subject_leakage_indicators(
        candidate_id=candidate_id,
        claim=normalized_claim,
        domains=domains,
        subjects=subjects,
        expected_subject=expected_subject,
        expected_domains=expected,
    )
    rejection_reasons = _rejection_reasons(
        normalized_claim,
        raw_content=raw_content,
        domains=domains,
        expected_domains=expected,
        reference_resolved=reference_resolved,
    )
    accepted = not rejection_reasons
    accepted_reasons = []
    if accepted:
        accepted_reasons.append("standalone_candidate")
        accepted_reasons.append(
            "assistant_suggestion_separated" if assistant_suggestion else "user_evidence_fact_eligible"
        )

    return {
        "candidate_id": candidate_id,
        "raw_content": raw_content,
        "normalized_claim": normalized_claim,
        "normalized_fingerprint": _fingerprint(normalized_claim),
        "accepted": accepted,
        "accepted_reasons": accepted_reasons,
        "rejected_reasons": rejection_reasons,
        "evidence_class": evidence_class,
        "source_role": source_role,
        "assistant_suggestion": assistant_suggestion,
        "canonical_fact_eligible": accepted and not assistant_suggestion,
        "memory_type": str(_value(candidate, "memory_type", "unknown") or "unknown"),
        "reason_type": str(_value(candidate, "reason_type", "unknown") or "unknown"),
        "confidence": _bounded_float(_value(candidate, "confidence", 0.0)),
        "domains": domains,
        "subjects": subjects,
        "valid_from": _optional_text(_value(candidate, "valid_from", None)),
        "valid_to": _optional_text(_value(candidate, "valid_to", None)),
        "subject_leakage_indicators": leakage,
        "provenance": {
            "candidate_id": candidate_id,
            "source_kind": _optional_text(_value(candidate, "source_kind", None)),
            "source_ref": _optional_text(_value(candidate, "source_ref", None)),
            "source_role": source_role,
            "source_links": dict(source_links),
            "assistant_suggestion": assistant_suggestion,
        },
    }


def canonicalize_candidate_set(
    candidates: Sequence[Any],
    *,
    expected_subject: str | None = None,
    expected_domains: Iterable[str] | None = None,
    near_duplicate_threshold: float = DEFAULT_NEAR_DUPLICATE_THRESHOLD,
) -> dict[str, Any]:
    """Produce deterministic proposed groups while keeping evidence roles apart."""

    threshold = _validate_threshold(near_duplicate_threshold)
    domain_scope = tuple(expected_domains or ())
    normalized = [
        normalize_candidate_claim(
            candidate,
            expected_subject=expected_subject,
            expected_domains=domain_scope,
        )
        for candidate in candidates
    ]
    normalized.sort(key=lambda item: item["candidate_id"])
    accepted = [item for item in normalized if item["accepted"]]
    rejected = [item for item in normalized if not item["accepted"]]

    group_indexes = _duplicate_components(accepted, threshold=threshold)
    groups = [_build_group([accepted[index] for index in indexes]) for indexes in group_indexes]
    groups.sort(key=lambda group: (group["evidence_class"], group["canonical_group_id"]))
    user_groups = [group for group in groups if group["evidence_class"] == "user_evidence"]
    assistant_groups = [group for group in groups if group["evidence_class"] == "assistant_suggestion"]
    relations = _detect_claim_relations(accepted, group_indexes, threshold=threshold)

    return {
        "schema_version": 1,
        "mode": "pure_shadow_canonicalization",
        "mutates_memory_truth": False,
        "input_candidate_count": len(normalized),
        "accepted_candidate_count": len(accepted),
        "rejected_candidate_count": len(rejected),
        "proposed_group_count": len(groups),
        "proposed_fact_count": len(user_groups),
        "assistant_suggestion_group_count": len(assistant_groups),
        "normalized_candidates": normalized,
        "accepted_candidates": accepted,
        "rejected_candidates": rejected,
        "canonical_fact_groups": user_groups,
        "assistant_suggestion_groups": assistant_groups,
        "duplicate_groups": [group for group in groups if group["candidate_count"] > 1],
        "claim_relations": relations,
        "subject_leakage_indicators": [
            indicator
            for item in normalized
            for indicator in item["subject_leakage_indicators"]
        ],
        "policy": {
            "near_duplicate_threshold": threshold,
            "assistant_user_groups_separate": True,
            "assistant_only_fact_promotion_allowed": False,
            "conflicts_merged_silently": False,
        },
    }


def compare_candidate_sets(
    legacy_candidates: Sequence[Any],
    proposed: Mapping[str, Any] | None = None,
    *,
    expected_subject: str | None = None,
    expected_domains: Iterable[str] | None = None,
    near_duplicate_threshold: float = DEFAULT_NEAR_DUPLICATE_THRESHOLD,
) -> dict[str, Any]:
    """Return an inspectable legacy-versus-proposed shadow comparison report."""

    domain_scope = tuple(expected_domains or ())
    result = dict(
        proposed
        or canonicalize_candidate_set(
            legacy_candidates,
            expected_subject=expected_subject,
            expected_domains=domain_scope,
            near_duplicate_threshold=near_duplicate_threshold,
        )
    )
    normalized = list(result.get("normalized_candidates", []))
    duplicate_groups = list(result.get("duplicate_groups", []))
    relations = list(result.get("claim_relations", []))
    accepted_reason_counts = Counter(
        reason for item in normalized for reason in item.get("accepted_reasons", [])
    )
    rejected_reason_counts = Counter(
        reason for item in normalized for reason in item.get("rejected_reasons", [])
    )
    legacy_count = len(legacy_candidates)
    proposed_fact_count = int(result.get("proposed_fact_count", 0))
    assistant_group_count = int(result.get("assistant_suggestion_group_count", 0))
    proposed_total = proposed_fact_count + assistant_group_count

    return {
        "schema_version": 1,
        "report_type": "candidate_canonicalization_shadow_comparison",
        "mutates_memory_truth": False,
        "expected_subject": expected_subject,
        "expected_domains": sorted(_resolve_expected_domains(expected_subject, domain_scope)),
        "counts": {
            "legacy_candidates": legacy_count,
            "accepted_candidates": int(result.get("accepted_candidate_count", 0)),
            "rejected_candidates": int(result.get("rejected_candidate_count", 0)),
            "proposed_fact_groups": proposed_fact_count,
            "proposed_assistant_suggestion_groups": assistant_group_count,
            "proposed_total_groups": proposed_total,
            "candidate_reduction": legacy_count - proposed_total,
        },
        "accepted_reason_counts": dict(sorted(accepted_reason_counts.items())),
        "rejected_reason_counts": dict(sorted(rejected_reason_counts.items())),
        "decisions": [
            {
                "candidate_id": item["candidate_id"],
                "decision": "accepted" if item["accepted"] else "rejected",
                "reasons": item["accepted_reasons"] if item["accepted"] else item["rejected_reasons"],
                "proposed_evidence_class": item["evidence_class"] if item["accepted"] else None,
            }
            for item in normalized
        ],
        "duplicate_groups": duplicate_groups,
        "conflicts": [relation for relation in relations if relation["relation_type"] == "textual_conflict"],
        "qualifications": [relation for relation in relations if "qualification" in relation["relation_type"]],
        "subject_leakage_indicators": list(result.get("subject_leakage_indicators", [])),
        "proposed": {
            "canonical_fact_groups": list(result.get("canonical_fact_groups", [])),
            "assistant_suggestion_groups": list(result.get("assistant_suggestion_groups", [])),
        },
        "policy": dict(result.get("policy", {})),
    }


def build_shadow_comparison(
    candidates: Sequence[Any],
    *,
    expected_subject: str | None = None,
    expected_domains: Iterable[str] | None = None,
    near_duplicate_threshold: float = DEFAULT_NEAR_DUPLICATE_THRESHOLD,
) -> dict[str, Any]:
    """Convenience wrapper for a complete non-mutating shadow run."""

    domain_scope = tuple(expected_domains or ())
    proposed = canonicalize_candidate_set(
        candidates,
        expected_subject=expected_subject,
        expected_domains=domain_scope,
        near_duplicate_threshold=near_duplicate_threshold,
    )
    return compare_candidate_sets(
        candidates,
        proposed,
        expected_subject=expected_subject,
        expected_domains=domain_scope,
        near_duplicate_threshold=near_duplicate_threshold,
    )


def _build_group(items: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(items, key=lambda item: item["candidate_id"])
    evidence_class = ordered[0]["evidence_class"]
    representative = sorted(
        ordered,
        key=lambda item: (
            -_representative_score(item),
            item["candidate_id"],
        ),
    )[0]
    candidate_ids = [item["candidate_id"] for item in ordered]
    digest = hashlib.sha256("|".join(candidate_ids).encode("utf-8")).hexdigest()[:16]
    fingerprints = {item["normalized_fingerprint"] for item in ordered}
    duplicate_kind = "single"
    if len(ordered) > 1:
        duplicate_kind = "exact" if len(fingerprints) == 1 else "near"
    canonical_fact_eligible = evidence_class == "user_evidence"
    return {
        "canonical_group_id": f"canon_shadow_{digest}",
        "evidence_class": evidence_class,
        "canonical_fact_eligible": canonical_fact_eligible,
        "canonical_claim": representative["normalized_claim"] if canonical_fact_eligible else None,
        "assistant_suggestion": representative["normalized_claim"] if not canonical_fact_eligible else None,
        "candidate_ids": candidate_ids,
        "candidate_count": len(ordered),
        "duplicate_kind": duplicate_kind,
        "domains": sorted({domain for item in ordered for domain in item["domains"]}),
        "memory_types": sorted({item["memory_type"] for item in ordered}),
        "confidence_basis": {
            "evidence_count": len(ordered),
            "average_candidate_confidence": round(
                sum(item["confidence"] for item in ordered) / len(ordered), 4
            ),
            "assistant_suggestion_only": not canonical_fact_eligible,
        },
        "source_provenance": [item["provenance"] for item in ordered],
    }


def _duplicate_components(items: list[dict[str, Any]], *, threshold: float) -> list[list[int]]:
    parents = list(range(len(items)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parents[max(left_root, right_root)] = min(left_root, right_root)

    for left in range(len(items)):
        for right in range(left + 1, len(items)):
            if items[left]["evidence_class"] != items[right]["evidence_class"]:
                continue
            if _polarity(items[left]["normalized_claim"]) != _polarity(items[right]["normalized_claim"]):
                continue
            if not _domains_compatible(items[left]["domains"], items[right]["domains"]):
                continue
            if _claim_similarity(items[left]["normalized_claim"], items[right]["normalized_claim"]) >= threshold:
                union(left, right)

    components: dict[int, list[int]] = {}
    for index in range(len(items)):
        components.setdefault(find(index), []).append(index)
    return [components[root] for root in sorted(components)]


def _detect_claim_relations(
    items: list[dict[str, Any]],
    components: list[list[int]],
    *,
    threshold: float,
) -> list[dict[str, Any]]:
    component_by_index = {
        item_index: component_index
        for component_index, component in enumerate(components)
        for item_index in component
    }
    relations: list[dict[str, Any]] = []
    for left in range(len(items)):
        if items[left]["evidence_class"] != "user_evidence":
            continue
        for right in range(left + 1, len(items)):
            if items[right]["evidence_class"] != "user_evidence":
                continue
            if component_by_index[left] == component_by_index[right]:
                continue
            relation_type, reason, similarity = _claim_relation(items[left], items[right], threshold=threshold)
            if relation_type is None:
                continue
            relations.append(
                {
                    "relation_type": relation_type,
                    "candidate_ids": sorted([items[left]["candidate_id"], items[right]["candidate_id"]]),
                    "reason": reason,
                    "anchor_similarity": round(similarity, 4),
                    "merge_allowed": False,
                }
            )
    relations.sort(key=lambda item: (item["relation_type"], item["candidate_ids"]))
    return relations


def _claim_relation(
    left: dict[str, Any],
    right: dict[str, Any],
    *,
    threshold: float,
) -> tuple[str | None, str | None, float]:
    left_text = left["normalized_claim"]
    right_text = right["normalized_claim"]
    similarity = _claim_similarity(_relation_anchor(left_text), _relation_anchor(right_text))
    if not _domains_compatible(left["domains"], right["domains"]):
        return None, None, similarity
    if _polarity(left_text) != _polarity(right_text) and similarity >= min(0.55, threshold):
        return "textual_conflict", "opposing polarity over similar proposition anchors", similarity
    if (_CORRECTION_MARKER.search(left_text) or _CORRECTION_MARKER.search(right_text)) and similarity >= 0.42:
        return "supersession_qualification", "corrective language may qualify or supersede earlier evidence", similarity
    if _CONDITION_MARKER.search(left_text) and _CONDITION_MARKER.search(right_text) and similarity >= 0.42:
        return "conditional_qualification", "similar claims have distinct explicit conditions", similarity
    return None, None, similarity


def _rejection_reasons(
    claim: str,
    *,
    raw_content: str,
    domains: list[str],
    expected_domains: set[str],
    reference_resolved: bool,
) -> list[str]:
    reasons: list[str] = []
    words = re.findall(r"\b[\w'-]+\b", claim)
    if not claim:
        reasons.append("empty_content")
    elif len(words) < 4:
        reasons.append("too_short_for_standalone_memory")
    if _is_question_only(claim):
        reasons.append("question_only")
    if _has_unresolved_reference(claim) and not reference_resolved:
        reasons.append("unresolved_context_reference")
    if _is_transient_or_planning_claim(claim):
        reasons.append("transient_request_or_planning")
    if _is_temporally_volatile_claim(claim):
        reasons.append("temporally_volatile_state")
    if _is_mixed_transient_and_durable_claim(claim):
        reasons.append("mixed_transient_and_durable_claims")
    if _is_underspecified_episode_claim(claim):
        reasons.append("episodic_state_missing_conditions")
    if _TEST_INSTRUCTION.search(raw_content):
        reasons.append("test_or_tool_instruction")
    if expected_domains and domains and not expected_domains.intersection(domains):
        reasons.append("subject_domain_mismatch")
    return _stable_unique(reasons)


def _subject_leakage_indicators(
    *,
    candidate_id: str,
    claim: str,
    domains: list[str],
    subjects: list[str],
    expected_subject: str | None,
    expected_domains: set[str],
) -> list[dict[str, Any]]:
    if not expected_subject and not expected_domains:
        return []
    indicators: list[dict[str, Any]] = []
    domain_set = set(domains)
    if expected_domains and domain_set and not expected_domains.intersection(domain_set):
        indicators.append(
            {
                "candidate_id": candidate_id,
                "code": "subject_domain_mismatch",
                "expected_domains": sorted(expected_domains),
                "candidate_domains": domains,
            }
        )
    expected_slug = _slug(expected_subject) if expected_subject else None
    subject_slugs = {_slug(subject) for subject in subjects}
    if expected_slug and subject_slugs and expected_slug not in subject_slugs:
        indicators.append(
            {
                "candidate_id": candidate_id,
                "code": "explicit_subject_mismatch",
                "expected_subject": expected_subject,
                "candidate_subjects": subjects,
            }
        )
    expected_terms = {term for domain in expected_domains for term in _DOMAIN_TERMS.get(domain, set())}
    claim_terms = set(_tokens(claim, include_stop_words=True))
    if expected_terms and not expected_terms.intersection(claim_terms):
        indicators.append(
            {
                "candidate_id": candidate_id,
                "code": "weak_subject_text_evidence",
                "expected_domains": sorted(expected_domains),
            }
        )
    return indicators


def _normalize_text(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = re.sub(r"^\s*(?:[-*]>?\s*)+", "", text)
    text = re.sub(r"^\s*(?:user|assistant)\s*:\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _normalize_user_perspective(claim: str, *, source_role: str) -> str:
    if source_role != "user":
        return claim
    replacements = (
        (r"^I prefer\b", "The user prefers"),
        (r"^I like\b", "The user likes"),
        (r"^I don['’]t like\b", "The user does not like"),
        (r"^I do not like\b", "The user does not like"),
        (r"^I love\b", "The user loves"),
        (r"^I dislike\b", "The user dislikes"),
        (r"^I hate\b", "The user hates"),
        (r"^My\b", "The user's"),
    )
    for pattern, replacement in replacements:
        if re.search(pattern, claim, flags=re.IGNORECASE):
            normalized = re.sub(pattern, replacement, claim, count=1, flags=re.IGNORECASE)
            normalized = re.sub(r"\bbut I want\b", "and wants", normalized, flags=re.IGNORECASE)
            normalized = re.sub(
                r"^The user likes the idea of\b",
                "The user likes",
                normalized,
                flags=re.IGNORECASE,
            )
            return normalized
    return claim


def _resolve_leading_reference(claim: str, resolved_object: str | None) -> tuple[str, bool]:
    if not resolved_object:
        return claim, False
    if re.match(rf"^{_CONTEXT_PRONOUNS}\b", claim, flags=re.IGNORECASE):
        return re.sub(rf"^{_CONTEXT_PRONOUNS}\b", resolved_object, claim, count=1, flags=re.IGNORECASE), True
    return claim, False


def _finish_sentence(claim: str) -> str:
    if not claim:
        return claim
    claim = claim[0].upper() + claim[1:]
    if claim[-1] not in ".!?":
        claim += "."
    return claim


def _is_question_only(claim: str) -> bool:
    stripped = claim.strip()
    if stripped.endswith("?"):
        return True
    return bool(_QUESTION_START.match(stripped)) and len(re.findall(r"[.!?]", stripped)) <= 1


def _has_unresolved_reference(claim: str) -> bool:
    return bool(
        re.match(rf"^(?:so\s+)?{_CONTEXT_PRONOUNS}\b", claim, flags=re.IGNORECASE)
        or re.match(r"^(?:something|anything|or\s+(?:a|an|the))\b", claim, re.IGNORECASE)
        or re.search(rf"[,;:]\s*{_CONTEXT_PRONOUNS}\b", claim, flags=re.IGNORECASE)
        or re.search(r"\b(?:option|choice|number)\s+[1-9](?:\s*(?:,|and)\s*[1-9])+\b", claim, re.IGNORECASE)
        or re.search(r"\b(?:like|likes|prefer|prefers)\s+[1-9](?:\s*(?:,|and)?\s*[1-9]){1,}\b", claim, re.IGNORECASE)
        or re.search(r"\b(?:like|do)\s+(?:this|that)\b", claim, re.IGNORECASE)
        or re.search(
            r"\bwhat (?:you|we|i) (?:prescribed|suggested|recommended|mentioned|discussed)\b",
            claim,
            re.IGNORECASE,
        )
        or re.search(
            r"\b(?:all|both|any|one|some|something|everything|nothing) of (?:it|them|these|those)\b",
            claim,
            re.IGNORECASE,
        )
        or re.search(r"\b(?:something|anything) (?:that|which|from)\b", claim, re.IGNORECASE)
        or re.search(r"\b(?:the )?other (?:flavors?|ideas?|ones?|options?)\b", claim, re.IGNORECASE)
        or re.search(r"\bwhatever (?:equipment|ingredients?|supplies|tools?)\b", claim, re.IGNORECASE)
        or re.search(r"\b(?:make|makes|making) this\b", claim, re.IGNORECASE)
        or re.search(r"\b(?:he|her|him|she)\b", claim, re.IGNORECASE)
        or re.search(r"\b(?:they|them|these|those)\b", claim, re.IGNORECASE)
        or re.search(r"(?:^|[.!?]\s+)(?:it|they|them|these|those)\b", claim, re.IGNORECASE)
        or re.search(r"\b(?:this|that) (?:cocktail|dish|bake|batch|version|idea|option)\b", claim, re.IGNORECASE)
        or re.search(r"\b(?:the|that|this) [a-z][a-z-]* idea\b", claim, re.IGNORECASE)
        or re.search(r"\bsame as (?:the )?(?:others|other ones|last time)\b", claim, re.IGNORECASE)
        or re.search(r"\b(?:that|this) (?:window|time|one)\b|\bin this\b|\bor something\b", claim, re.IGNORECASE)
        or re.match(r"^After mix\b", claim, re.IGNORECASE)
        or re.match(
            r"^(?:The\s+)?(?:mix|rye)\b(?!\s+(?:batch|bread|culture|dough|flour|starter)\b)",
            claim,
            re.IGNORECASE,
        )
        or re.search(r"\b\d+(?:\.\d+)?[ -]inch rounds?\b", claim, re.IGNORECASE)
    )


def _is_transient_or_planning_claim(claim: str) -> bool:
    return bool(
        re.search(
            r"\b(?:right now|for now|today|tonight|tomorrow|this evening|before i (?:go|leave|see)|"
            r"time permitting|same[- ]day options?|for the next bake)\b",
            claim,
            re.IGNORECASE,
        )
        or re.match(r"^I(?:['’]m| am) (?:also )?thinking\b", claim, re.IGNORECASE)
        or re.match(r"^I(?:['’]d| would) want to (?:make|try|do|start)\b", claim, re.IGNORECASE)
        or re.match(r"^I was (?:gonna|going to)\b", claim, re.IGNORECASE)
        or re.match(r"^I(?:['’]ll| will) (?:get|buy|pick up|order)\b", claim, re.IGNORECASE)
        or re.match(r"^I still need (?:groceries|ingredients|supplies)\b", claim, re.IGNORECASE)
        or re.match(r"^We can go\b.*\bto start\b", claim, re.IGNORECASE)
        or re.match(r"^(?:Doesn['’]t|Does not) need to be\b", claim, re.IGNORECASE)
        or re.search(r"\blooking for help brainstorming\b", claim, re.IGNORECASE)
    )


def _is_temporally_volatile_claim(claim: str) -> bool:
    if re.search(r"\b(?:currently|right now|on hand|at the moment|just got|just bought)\b", claim, re.IGNORECASE):
        return True
    if re.match(r"^I already have\b", claim, re.IGNORECASE) and claim.count(",") >= 2:
        return True
    if re.search(r"\bI have (?:many|multiple|several|various) (?:kinds|types) of\b", claim, re.IGNORECASE):
        return True
    if re.search(r"\b(?:hitting|reaching) (?:a )?(?:high|low) of \d+", claim, re.IGNORECASE):
        return True
    return bool(
        re.match(
            r"^I have (?:about|around|roughly|only) \d+\s+(?:minutes?|hours?|days?)\b",
            claim,
            re.IGNORECASE,
        )
    )


def _is_mixed_transient_and_durable_claim(claim: str) -> bool:
    if len(_tokens(claim, include_stop_words=True)) < 16:
        return False
    durable = re.search(
        r"\b(?:i (?:do )?(?:like|prefer|love|dislike|hate|avoid)|the user (?:likes|prefers|loves|dislikes|hates|avoids)|"
        r"worked|succeeded|failed|could become a staple)\b",
        claim,
        re.IGNORECASE,
    )
    transient = re.search(
        r"\b(?:same[- ]day options?|right now|today|tonight|time permitting|before i (?:go|leave|see)|"
        r"let(?:'s| us)|can you|could you|show me|give me|we can maybe|"
        r"one or two (?:batches|days) a week|prep it for herself|to figure out a way|"
        r"we should explore|maybe i can get)\b",
        claim,
        re.IGNORECASE,
    )
    return bool(durable and transient)


def _is_underspecified_episode_claim(claim: str) -> bool:
    vague_duration = re.search(
        r"\b(?:quite a lot of time|a long time|a while|slow(?:ly)?|taking forever)\b",
        claim,
        re.IGNORECASE,
    )
    measured_condition = re.search(
        r"\b\d+(?:\.\d+)?\s*(?:minutes?|hours?|days?|%|percent|degrees?|[cf])\b",
        claim,
        re.IGNORECASE,
    )
    return bool(vague_duration and not measured_condition)


def _fingerprint(claim: str) -> str:
    return " ".join(_tokens(claim, include_stop_words=True))


def _relation_anchor(claim: str) -> str:
    tokens = [
        token
        for token in _tokens(claim)
        if token not in _NEGATION_TERMS
        and token not in {"actually", "correction", "instead", "rather", "wrong", "used"}
    ]
    return " ".join(tokens)


def _claim_similarity(left: str, right: str) -> float:
    left_tokens = set(_tokens(left))
    right_tokens = set(_tokens(right))
    if not left_tokens or not right_tokens:
        return 0.0
    intersection = len(left_tokens.intersection(right_tokens))
    union = len(left_tokens.union(right_tokens))
    jaccard = intersection / union
    containment = intersection / min(len(left_tokens), len(right_tokens))
    sequence = SequenceMatcher(None, " ".join(sorted(left_tokens)), " ".join(sorted(right_tokens))).ratio()
    containment_score = containment * 0.92 if intersection >= 2 else 0.0
    return max(jaccard, containment_score, sequence)


def _tokens(value: str, *, include_stop_words: bool = False) -> list[str]:
    tokens = [token.lower().replace("’", "'") for token in re.findall(r"[a-zA-Z0-9']+", value)]
    normalized = [_TOKEN_ALIASES.get(token, token) for token in tokens]
    if include_stop_words:
        return normalized
    return [token for token in normalized if token not in _STOP_WORDS]


def _polarity(claim: str) -> str:
    tokens = set(_tokens(claim, include_stop_words=True))
    return "negative" if tokens.intersection(_NEGATION_TERMS) else "affirmative"


def _domains_compatible(left: list[str], right: list[str]) -> bool:
    left_set = set(left)
    right_set = set(right)
    if not left_set or not right_set:
        return True
    if left_set == {"misc"} or right_set == {"misc"}:
        return True
    return bool(left_set.intersection(right_set))


def _representative_score(item: Mapping[str, Any]) -> float:
    token_count = min(len(_tokens(str(item["normalized_claim"]))), 30)
    return token_count + float(item.get("confidence", 0.0)) * 5


def _candidate_id(candidate: Any) -> str:
    candidate_id = _optional_text(_value(candidate, "id", None))
    if candidate_id:
        return candidate_id
    content = str(_value(candidate, "content", "") or "")
    source_ref = str(_value(candidate, "source_ref", "") or "")
    digest = hashlib.sha256(f"{source_ref}|{content}".encode("utf-8")).hexdigest()[:16]
    return f"anonymous_candidate_{digest}"


def _source_role(candidate: Any, *, assistant_suggestion: bool) -> str:
    role = _optional_text(_value(candidate, "source_role", None))
    source_links = _as_mapping(_value(candidate, "source_links", {}))
    metadata = _as_mapping(_value(candidate, "metadata", {}))
    role = role or _optional_text(source_links.get("source_role")) or _optional_text(metadata.get("source_role"))
    if role:
        return role.lower()
    return "assistant" if assistant_suggestion else "user"


def _candidate_domains(candidate: Any) -> list[str]:
    raw = _value(candidate, "domains", None)
    if raw is None:
        raw = [_value(candidate, "domain_primary", "misc")]
    if isinstance(raw, str):
        raw = [raw]
    return _stable_unique([str(domain).strip().lower() for domain in raw or [] if str(domain).strip()]) or ["misc"]


def _candidate_subjects(metadata: Mapping[str, Any], source_links: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    for container in (metadata, source_links):
        for key in ("subject", "subject_name", "subjects", "subject_names"):
            value = container.get(key)
            if isinstance(value, str):
                values.append(value)
            elif isinstance(value, (list, tuple, set)):
                values.extend(str(item) for item in value)
    return _stable_unique([value.strip() for value in values if value.strip()])


def _resolved_object(metadata: Mapping[str, Any]) -> str | None:
    for key in ("resolved_object", "object_label", "entity_label", "canonical_subject"):
        value = _optional_text(metadata.get(key))
        if value:
            return value
    return None


def _resolve_expected_domains(
    expected_subject: str | None,
    expected_domains: Iterable[str] | None,
) -> set[str]:
    domains = {str(domain).strip().lower() for domain in expected_domains or [] if str(domain).strip()}
    if expected_subject:
        domains.update(_SUBJECT_DOMAINS.get(_slug(expected_subject), set()))
    return domains


def _value(candidate: Any, name: str, default: Any) -> Any:
    if isinstance(candidate, Mapping):
        return candidate.get(name, default)
    return getattr(candidate, name, default)


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _bounded_float(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, number))


def _validate_threshold(value: float) -> float:
    threshold = float(value)
    if not 0.0 <= threshold <= 1.0:
        raise ValueError("near_duplicate_threshold must be between 0 and 1")
    return threshold


def _slug(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", text.lower())).strip("-")


def _stable_unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))
