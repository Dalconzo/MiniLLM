from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .candidates import CandidateMemory
from .subjects import normalize_subject_slug


@dataclass(frozen=True)
class CandidateReviewSuggestion:
    status: str
    score: float
    durability: float
    standalone_clarity: float
    subject_centrality: float
    review_value: float
    evidence_type: str
    suggested_action: str
    rationale: str
    mutates_memory_truth: bool = False
    mutates_ranking: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "score": self.score,
            "durability": self.durability,
            "standalone_clarity": self.standalone_clarity,
            "subject_centrality": self.subject_centrality,
            "review_value": self.review_value,
            "evidence_type": self.evidence_type,
            "suggested_action": self.suggested_action,
            "rationale": self.rationale,
            "mutates_memory_truth": self.mutates_memory_truth,
            "mutates_ranking": self.mutates_ranking,
        }


def score_candidate_for_review(candidate: CandidateMemory, *, subject_name: str | None = None) -> CandidateReviewSuggestion:
    """Return a suggestion-only reviewability score for a candidate memory.

    This is deliberately not wired to candidate status, curated memory truth, or live ranking.
    It gives browser/agent reviewers a conservative triage hint while keeping the human
    confirmation boundary intact.
    """
    text = candidate.content.strip()
    lowered = text.lower()
    tokens = _tokens(text)
    word_count = len(tokens)
    subject_slug = normalize_subject_slug(subject_name or "")

    durability = _durability_score(candidate, lowered)
    clarity = _standalone_clarity_score(lowered, word_count=word_count)
    centrality = _subject_centrality_score(candidate, lowered, subject_slug=subject_slug)
    value = _review_value_score(candidate, lowered)
    score = round((0.35 * durability) + (0.25 * clarity) + (0.25 * centrality) + (0.15 * value), 3)

    blockers = _blocking_reasons(candidate, lowered, word_count=word_count, subject_slug=subject_slug)
    if blockers:
        score = min(score, 0.44)
    if candidate.assistant_suggestion:
        score = min(score, 0.25)

    suggested_action = "review"
    if score < 0.45:
        suggested_action = "skip"
    elif score < 0.65:
        suggested_action = "needs_context"

    evidence_type = _evidence_type(candidate, lowered)
    rationale = _rationale(
        blockers=blockers,
        durability=durability,
        clarity=clarity,
        centrality=centrality,
        value=value,
        evidence_type=evidence_type,
        suggested_action=suggested_action,
    )
    return CandidateReviewSuggestion(
        status="suggested",
        score=score,
        durability=round(durability, 3),
        standalone_clarity=round(clarity, 3),
        subject_centrality=round(centrality, 3),
        review_value=round(value, 3),
        evidence_type=evidence_type,
        suggested_action=suggested_action,
        rationale=rationale,
    )


def add_review_suggestions(
    candidates: list[dict[str, Any]],
    *,
    subject_name: str | None = None,
) -> list[dict[str, Any]]:
    scored: list[dict[str, Any]] = []
    for item in candidates:
        candidate = _candidate_from_dict(item)
        suggestion = score_candidate_for_review(candidate, subject_name=subject_name).to_dict()
        enriched = dict(item)
        enriched["assistant_review"] = suggestion
        enriched["assistant_review_score"] = suggestion["score"]
        enriched["assistant_review_rationale"] = suggestion["rationale"]
        scored.append(enriched)
    return scored


def _candidate_from_dict(item: dict[str, Any]) -> CandidateMemory:
    source_links = dict(item.get("source_links") or {})
    return CandidateMemory(
        id=str(item["id"]),
        content=str(item["content"]),
        memory_type=str(item["memory_type"]),
        reason_type=str(item["reason_type"]),
        domains=[str(value) for value in item.get("domains", [])],
        confidence=float(item["confidence"]),
        valid_from=item.get("valid_from"),
        valid_to=item.get("valid_to"),
        last_confirmed_at=item.get("last_confirmed_at"),
        source_kind=str(item["source_kind"]),
        source_ref=str(item["source_ref"]),
        source_links=source_links,
        review_status=str(item["review_status"]),
        review_notes=item.get("review_notes"),
        origin=str(item["origin"]),
        assistant_suggestion=bool(item.get("assistant_suggestion")),
        created_at=str(item["created_at"]),
        updated_at=str(item["updated_at"]),
        metadata=dict(item.get("metadata") or {}),
    )


def _durability_score(candidate: CandidateMemory, lowered: str) -> float:
    if candidate.assistant_suggestion:
        return 0.1
    score = {
        "preference": 0.72,
        "decision": 0.78,
        "failure": 0.82,
        "workaround": 0.76,
        "episodic": 0.55,
        "procedure": 0.42,
        "open_loop": 0.25,
    }.get(candidate.memory_type, 0.4)
    if _has_any(lowered, _DURABLE_OUTCOME_TERMS):
        score += 0.18
    if _has_any(lowered, _DURABLE_PREFERENCE_TERMS):
        score += 0.14
    if _question_shaped(lowered) and not _has_any(lowered, _DURABLE_TERMS):
        score -= 0.32
    if _has_any(lowered, _TRANSIENT_TERMS):
        score -= 0.18
    return _clamp(score)


def _standalone_clarity_score(lowered: str, *, word_count: int) -> float:
    if word_count < 6:
        return 0.1
    score = 0.62
    if word_count >= 14:
        score += 0.15
    if word_count >= 35:
        score += 0.08
    if _question_shaped(lowered):
        score -= 0.28
    if _has_any(lowered, _CONTEXT_DEPENDENT_TERMS):
        score -= 0.28
    if re.search(r"\b(option|number)\s*[0-9]\b", lowered):
        score -= 0.35
    return _clamp(score)


def _subject_centrality_score(candidate: CandidateMemory, lowered: str, *, subject_slug: str) -> float:
    if not subject_slug:
        return 0.55
    expected_domain = _SUBJECT_DOMAIN_HINTS.get(subject_slug)
    score = 0.45
    if expected_domain and expected_domain in candidate.domains:
        score += 0.25
    if "cooking" in subject_slug or "baking" in subject_slug or "recipe" in subject_slug:
        signals = sum(1 for pattern in _COOKING_PATTERNS if re.search(pattern, lowered))
        score += min(signals * 0.12, 0.36)
        if signals == 0:
            score -= 0.35
    if _has_any(lowered, _CROSS_TOPIC_NOISE_TERMS):
        score -= 0.3
    return _clamp(score)


def _review_value_score(candidate: CandidateMemory, lowered: str) -> float:
    score = min(max(candidate.confidence, 0.0), 1.0) * 0.55
    if candidate.memory_type in {"preference", "decision", "failure", "workaround"}:
        score += 0.2
    if _has_any(lowered, _DURABLE_OUTCOME_TERMS):
        score += 0.2
    if _has_any(lowered, _META_TEST_TERMS):
        score -= 0.45
    return _clamp(score)


def _blocking_reasons(candidate: CandidateMemory, lowered: str, *, word_count: int, subject_slug: str) -> list[str]:
    reasons: list[str] = []
    if candidate.assistant_suggestion:
        reasons.append("assistant-authored suggestion")
    if _has_any(lowered, _META_TEST_TERMS):
        reasons.append("test or connector artifact")
    if _question_shaped(lowered) and not _has_any(lowered, _DURABLE_TERMS):
        reasons.append("question-shaped transient request")
    if word_count < 6 or _has_any(lowered, _CONTEXT_DEPENDENT_TERMS):
        reasons.append("not standalone without surrounding chat")
    if ("cooking" in subject_slug or "baking" in subject_slug or "recipe" in subject_slug) and _has_any(lowered, _CROSS_TOPIC_NOISE_TERMS):
        reasons.append("subject match is probably incidental or cross-topic")
    if (
        ("cooking" in subject_slug or "baking" in subject_slug or "recipe" in subject_slug)
        and ("?" in lowered or _question_shaped(lowered))
        and _has_any(lowered, _SHOPPING_LOOKUP_TERMS)
    ):
        reasons.append("location or shopping lookup rather than durable memory")
    return reasons


def _evidence_type(candidate: CandidateMemory, lowered: str) -> str:
    if candidate.assistant_suggestion:
        return "assistant_suggestion"
    if _has_any(lowered, _DURABLE_OUTCOME_TERMS):
        return "user_reported_outcome"
    if candidate.memory_type == "preference":
        return "user_reported_preference"
    if candidate.memory_type == "decision":
        return "user_decision"
    if candidate.memory_type in {"failure", "workaround"}:
        return "user_reported_lesson"
    if _question_shaped(lowered):
        return "user_question"
    return "user_statement"


def _rationale(
    *,
    blockers: list[str],
    durability: float,
    clarity: float,
    centrality: float,
    value: float,
    evidence_type: str,
    suggested_action: str,
) -> str:
    if blockers:
        return f"{suggested_action}: {', '.join(blockers[:3])}."
    strengths: list[str] = []
    if durability >= 0.7:
        strengths.append("durable")
    if clarity >= 0.7:
        strengths.append("standalone")
    if centrality >= 0.7:
        strengths.append("central to subject")
    if value >= 0.65:
        strengths.append("review-worthy")
    if not strengths:
        strengths.append("moderate evidence")
    return f"{suggested_action}: {evidence_type}; {', '.join(strengths)}."


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def _question_shaped(lowered: str) -> bool:
    stripped = lowered.strip()
    return stripped.endswith("?") or re.match(r"^(what|what's|whats|when|where|why|how|can|could|should|would|will|is|are|do|does|did)\b", stripped) is not None


def _has_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


_SUBJECT_DOMAIN_HINTS = {
    "cooking-and-baking": "cooking_baking",
    "recipes-and-baking": "cooking_baking",
    "lab-automation": "lab_automation",
    "career-and-job-search": "career_work",
    "ai-memory-and-local-llms": "ai_memory_systems",
}

_COOKING_PATTERNS = (
    r"\bbak(?:e|ed|ing)\b",
    r"\bcook(?:ing|ed)?\b",
    r"\brecipe\b",
    r"\bsourdough\b",
    r"\bstarter\b",
    r"\bdough\b",
    r"\bbread\b",
    r"\bcake\b",
    r"\bpastr(?:y|ies)\b",
    r"\bkouign\b",
    r"\bferment(?:ation|ed|ing)?\b",
)
_DURABLE_OUTCOME_TERMS = (
    "worked",
    "failed",
    "succeeded",
    "success",
    "massive success",
    "staple",
    "next time",
    "lesson",
    "resolved",
    "confirmed",
    "rose",
    "risen",
    "fell",
    "peaked",
)
_DURABLE_PREFERENCE_TERMS = (
    "i prefer",
    "i like",
    "i love",
    "i hate",
    "i usually",
    "i always",
    "i avoid",
    "i want",
    "i need",
)
_DURABLE_TERMS = _DURABLE_OUTCOME_TERMS + _DURABLE_PREFERENCE_TERMS
_TRANSIENT_TERMS = ("today", "tonight", "tomorrow", "right now", "before i see", "this afternoon")
_CONTEXT_DEPENDENT_TERMS = ("like this", "this look", "the image", "the photo", "attached", "option 1", "option 2", "option 3")
_CROSS_TOPIC_NOISE_TERMS = (
    "acceptance",
    "mcp",
    "connector",
    "tailscale",
    "operational intelligence",
    "career",
    "cannabis",
    "autism",
    "woodworking",
    "ceramics",
)
_SHOPPING_LOOKUP_TERMS = (
    "supermarket",
    "local store",
    "where to get",
    "where should i buy",
    "has it late",
    "in the area",
)
_META_TEST_TERMS = (
    "acceptance report",
    "acceptance test",
    "memory_review_subjects",
    "memory_context",
    "memory_search",
    "memory_status",
    "run_id",
    "trace_id",
    "browser chatgpt",
    "mcp connector",
)
