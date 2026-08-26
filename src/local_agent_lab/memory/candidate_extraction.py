from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .ontology import (
    CANONICAL_DOMAINS,
    validate_domain,
    validate_memory_type,
    validate_reason_type,
)


EXTRACTION_SCHEMA_VERSION = "candidate_extraction_v1"
MAX_MODEL_OUTPUT_BYTES = 128_000
MAX_SOURCES_PER_BATCH = 64
MAX_CANDIDATES_PER_SOURCE = 3
MAX_CANDIDATE_CHARS = 600
MIN_CANDIDATE_CHARS = 12
MIN_DURABILITY_SCORE = 0.60
MIN_STANDALONE_CONTEXT_SCORE = 0.70

VALID_SOURCE_ROLES = ("user", "assistant")
VALID_EPISTEMIC_STATUSES = ("user_reported", "assistant_suggested")
VALID_REJECTION_REASONS = (
    "acceptance_test_or_meta_instruction",
    "context_dependent_reference",
    "empty_source",
    "insufficient_durability",
    "insufficient_standalone_context",
    "option_only_reference",
    "question_only",
    "source_too_long",
    "temporally_volatile_state",
    "transient_execution_chatter",
    "transient_request_or_planning",
    "mixed_transient_and_durable_claims",
)

_ROOT_KEYS = {"schema_version", "candidates", "rejections"}
_CANDIDATE_KEYS = {
    "candidate_text",
    "memory_type",
    "reason_type",
    "subject",
    "subject_confidence",
    "domain",
    "domain_confidence",
    "durability_score",
    "standalone_context_score",
    "confidence",
    "source_ids",
    "source_role",
    "epistemic_status",
    "assistant_suggestion",
}
_REJECTION_KEYS = {"source_ids", "source_role", "rejection_reasons"}
_SOURCE_ID_KEYS = {"conversation_id", "message_id", "chunk_id"}


class CandidateExtractionValidationError(ValueError):
    """Raised when model-shaped extraction output violates the fail-closed contract."""


@dataclass(frozen=True)
class SourceIds:
    conversation_id: str
    message_id: str
    chunk_id: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any], *, path: str = "source_ids") -> "SourceIds":
        _require_exact_keys(value, _SOURCE_ID_KEYS, path=path)
        return cls(
            conversation_id=_required_text(value.get("conversation_id"), path=f"{path}.conversation_id"),
            message_id=_required_text(value.get("message_id"), path=f"{path}.message_id"),
            chunk_id=_required_text(value.get("chunk_id"), path=f"{path}.chunk_id"),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "conversation_id": self.conversation_id,
            "message_id": self.message_id,
            "chunk_id": self.chunk_id,
        }

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.conversation_id, self.message_id, self.chunk_id)


@dataclass(frozen=True)
class CandidateSource:
    source_ids: SourceIds
    source_role: str
    text: str
    title: str | None = None
    subject: str | None = None
    subject_confidence: float | None = None
    domain: str | None = None
    domain_confidence: float | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CandidateSource":
        if not isinstance(value, Mapping):
            raise CandidateExtractionValidationError("source must be an object")
        if "source_ids" in value:
            source_ids_value = value["source_ids"]
        else:
            source_ids_value = {
                "conversation_id": value.get("conversation_id"),
                "message_id": value.get("message_id"),
                "chunk_id": value.get("chunk_id", value.get("id")),
            }
        if not isinstance(source_ids_value, Mapping):
            raise CandidateExtractionValidationError("source.source_ids must be an object")
        role = _validate_source_role(value.get("source_role", value.get("role")), path="source.source_role")
        text = str(value.get("text") or "").strip()
        subject_confidence = _optional_score(value.get("subject_confidence"), path="source.subject_confidence")
        domain_confidence = _optional_score(value.get("domain_confidence"), path="source.domain_confidence")
        domain_value = value.get("domain", value.get("domain_primary"))
        domain = None if domain_value is None else _canonical_domain(domain_value, path="source.domain")
        return cls(
            source_ids=SourceIds.from_mapping(source_ids_value, path="source.source_ids"),
            source_role=role,
            text=text,
            title=_optional_text(value.get("title")),
            subject=_optional_text(value.get("subject")),
            subject_confidence=subject_confidence,
            domain=domain,
            domain_confidence=domain_confidence,
        )


@dataclass(frozen=True)
class ExtractedCandidate:
    candidate_text: str
    memory_type: str
    reason_type: str
    subject: str
    subject_confidence: float
    domain: str
    domain_confidence: float
    durability_score: float
    standalone_context_score: float
    confidence: float
    source_ids: SourceIds
    source_role: str
    epistemic_status: str
    assistant_suggestion: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_text": self.candidate_text,
            "memory_type": self.memory_type,
            "reason_type": self.reason_type,
            "subject": self.subject,
            "subject_confidence": self.subject_confidence,
            "domain": self.domain,
            "domain_confidence": self.domain_confidence,
            "durability_score": self.durability_score,
            "standalone_context_score": self.standalone_context_score,
            "confidence": self.confidence,
            "source_ids": self.source_ids.to_dict(),
            "source_role": self.source_role,
            "epistemic_status": self.epistemic_status,
            "assistant_suggestion": self.assistant_suggestion,
        }


@dataclass(frozen=True)
class RejectedSource:
    source_ids: SourceIds
    source_role: str
    rejection_reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_ids": self.source_ids.to_dict(),
            "source_role": self.source_role,
            "rejection_reasons": list(self.rejection_reasons),
        }


@dataclass(frozen=True)
class CandidateExtractionBatch:
    candidates: tuple[ExtractedCandidate, ...]
    rejections: tuple[RejectedSource, ...]
    schema_version: str = EXTRACTION_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "rejections": [rejection.to_dict() for rejection in self.rejections],
        }


def heuristic_extract_candidate(source: CandidateSource | Mapping[str, Any]) -> CandidateExtractionBatch:
    """Produce a conservative, deterministic shadow decision for one source observation."""
    normalized_source = source if isinstance(source, CandidateSource) else CandidateSource.from_mapping(source)
    normalized_observation = _normalize_candidate_text(normalized_source.text)
    source_reasons = classify_rejection_reasons(normalized_observation)
    hard_source_reasons = [
        reason
        for reason in source_reasons
        if reason
        in {
            "acceptance_test_or_meta_instruction",
            "empty_source",
            "option_only_reference",
            "source_too_long",
        }
    ]
    if hard_source_reasons:
        return _rejected_batch(normalized_source, hard_source_reasons)
    salvageable_procedure = bool(
        re.match(r"^we need\b", normalized_observation, flags=re.IGNORECASE)
        and re.search(
            r"\band then to figure out a way\b",
            normalized_observation,
            flags=re.IGNORECASE,
        )
    )
    salvageable_reuse_preference = bool(
        re.search(
            r"\b(?:prefer|want|like|plan|aim)\b[^.!?]*\b(?:reuse|reusing|recycle|repurpose)\b",
            normalized_observation,
            flags=re.IGNORECASE,
        )
    )
    if (
        source_reasons
        and not _has_durable_memory_marker(normalized_observation)
        and not salvageable_procedure
        and not salvageable_reuse_preference
    ):
        return _rejected_batch(normalized_source, source_reasons)
    text = _select_candidate_proposition(normalized_observation, title=normalized_source.title)
    if re.search(r"\b(?:the )?other flavors?\b", normalized_observation, flags=re.IGNORECASE) and re.match(
        r"^I (?:don['’]t|do not) like\b",
        text,
        flags=re.IGNORECASE,
    ):
        return _rejected_batch(
            normalized_source,
            [*source_reasons, "insufficient_standalone_context"],
        )
    reasons = classify_rejection_reasons(text)
    if reasons:
        return _rejected_batch(normalized_source, [*source_reasons, *reasons])

    memory_type, reason_type, durability_score = _classify_memory(text, normalized_source.source_role)
    standalone_score = _standalone_context_score(text)
    if durability_score < MIN_DURABILITY_SCORE:
        reasons.append("insufficient_durability")
    if standalone_score < MIN_STANDALONE_CONTEXT_SCORE:
        reasons.append("insufficient_standalone_context")
    if reasons:
        return _rejected_batch(normalized_source, reasons)

    domain, domain_confidence = _infer_domain(normalized_source)
    subject, subject_confidence = _infer_subject(normalized_source, domain, domain_confidence)
    assistant_suggestion = normalized_source.source_role == "assistant"
    confidence = round(
        min(durability_score, standalone_score, max(domain_confidence, 0.55), max(subject_confidence, 0.55)),
        3,
    )
    candidate = ExtractedCandidate(
        candidate_text=text,
        memory_type=memory_type,
        reason_type=reason_type,
        subject=subject,
        subject_confidence=subject_confidence,
        domain=domain,
        domain_confidence=domain_confidence,
        durability_score=durability_score,
        standalone_context_score=standalone_score,
        confidence=confidence,
        source_ids=normalized_source.source_ids,
        source_role=normalized_source.source_role,
        epistemic_status="assistant_suggested" if assistant_suggestion else "user_reported",
        assistant_suggestion=assistant_suggestion,
    )
    return CandidateExtractionBatch(candidates=(candidate,), rejections=())


def heuristic_extract_candidates(
    sources: Sequence[CandidateSource | Mapping[str, Any]],
) -> CandidateExtractionBatch:
    """Extract a bounded batch without invoking a model or mutating storage."""
    if len(sources) > MAX_SOURCES_PER_BATCH:
        raise CandidateExtractionValidationError(f"source batch exceeds limit of {MAX_SOURCES_PER_BATCH}")
    candidates: list[ExtractedCandidate] = []
    rejections: list[RejectedSource] = []
    seen: set[tuple[str, str, str]] = set()
    for value in sources:
        source = value if isinstance(value, CandidateSource) else CandidateSource.from_mapping(value)
        if source.source_ids.key in seen:
            raise CandidateExtractionValidationError(f"duplicate source IDs: {source.source_ids.key}")
        seen.add(source.source_ids.key)
        result = heuristic_extract_candidate(source)
        candidates.extend(result.candidates)
        rejections.extend(result.rejections)
    return CandidateExtractionBatch(candidates=tuple(candidates), rejections=tuple(rejections))


def parse_model_extraction_output(
    raw_output: str | bytes | Mapping[str, Any],
    *,
    sources: Sequence[CandidateSource | Mapping[str, Any]],
) -> CandidateExtractionBatch:
    """Parse and validate model output against exact source provenance, failing closed."""
    payload = _decode_model_output(raw_output)
    return validate_model_extraction_payload(payload, sources=sources)


def validate_model_extraction_payload(
    payload: Mapping[str, Any],
    *,
    sources: Sequence[CandidateSource | Mapping[str, Any]],
) -> CandidateExtractionBatch:
    if not isinstance(payload, Mapping):
        raise CandidateExtractionValidationError("model output must be a JSON object")
    _require_exact_keys(payload, _ROOT_KEYS, path="output")
    if payload.get("schema_version") != EXTRACTION_SCHEMA_VERSION:
        raise CandidateExtractionValidationError(f"output.schema_version must equal {EXTRACTION_SCHEMA_VERSION!r}")

    normalized_sources = [
        source if isinstance(source, CandidateSource) else CandidateSource.from_mapping(source) for source in sources
    ]
    if len(normalized_sources) > MAX_SOURCES_PER_BATCH:
        raise CandidateExtractionValidationError(f"source batch exceeds limit of {MAX_SOURCES_PER_BATCH}")
    source_map: dict[tuple[str, str, str], CandidateSource] = {}
    for source in normalized_sources:
        if source.source_ids.key in source_map:
            raise CandidateExtractionValidationError(f"duplicate source IDs: {source.source_ids.key}")
        source_map[source.source_ids.key] = source

    raw_candidates = _required_list(payload.get("candidates"), path="output.candidates")
    raw_rejections = _required_list(payload.get("rejections"), path="output.rejections")
    if len(raw_candidates) > len(normalized_sources) * MAX_CANDIDATES_PER_SOURCE:
        raise CandidateExtractionValidationError("output contains too many candidates")

    candidates = tuple(
        _validate_model_candidate(value, index=index, source_map=source_map)
        for index, value in enumerate(raw_candidates)
    )
    rejections = tuple(
        _validate_model_rejection(value, index=index, source_map=source_map)
        for index, value in enumerate(raw_rejections)
    )

    candidate_counts: dict[tuple[str, str, str], int] = {}
    for candidate in candidates:
        key = candidate.source_ids.key
        candidate_counts[key] = candidate_counts.get(key, 0) + 1
        if candidate_counts[key] > MAX_CANDIDATES_PER_SOURCE:
            raise CandidateExtractionValidationError(f"source {key} exceeds {MAX_CANDIDATES_PER_SOURCE} candidates")
    rejected_keys = [rejection.source_ids.key for rejection in rejections]
    if len(rejected_keys) != len(set(rejected_keys)):
        raise CandidateExtractionValidationError("a source may be rejected only once")
    overlap = set(candidate_counts).intersection(rejected_keys)
    if overlap:
        raise CandidateExtractionValidationError(f"sources cannot be both accepted and rejected: {sorted(overlap)}")
    accounted_for = set(candidate_counts).union(rejected_keys)
    missing = set(source_map).difference(accounted_for)
    if missing:
        raise CandidateExtractionValidationError(f"model output omitted source decisions: {sorted(missing)}")
    return CandidateExtractionBatch(candidates=candidates, rejections=rejections)


def classify_rejection_reasons(text: str) -> list[str]:
    """Return stable rejection codes for non-memory conversational material."""
    normalized = " ".join(str(text or "").split()).strip()
    lowered = normalized.lower()
    reasons: list[str] = []
    if not normalized:
        reasons.append("empty_source")
        return reasons
    if len(normalized) > MAX_CANDIDATE_CHARS:
        reasons.append("source_too_long")
    if _is_meta_instruction(lowered):
        reasons.append("acceptance_test_or_meta_instruction")
    if _is_option_reference(lowered):
        reasons.append("option_only_reference")
    if _is_context_dependent(lowered):
        reasons.append("context_dependent_reference")
    if _is_question_only(normalized):
        reasons.append("question_only")
    if _is_transient_execution_chatter(lowered):
        reasons.append("transient_execution_chatter")
    if _is_transient_request_or_planning(lowered):
        reasons.append("transient_request_or_planning")
    if _is_temporally_volatile_state(lowered):
        reasons.append("temporally_volatile_state")
    if _has_mixed_transient_and_durable_claims(lowered):
        reasons.append("mixed_transient_and_durable_claims")
    if _is_underspecified_episode(lowered):
        reasons.append("insufficient_standalone_context")
    return _ordered_unique(reasons)


def _validate_model_candidate(
    value: Any,
    *,
    index: int,
    source_map: Mapping[tuple[str, str, str], CandidateSource],
) -> ExtractedCandidate:
    path = f"output.candidates[{index}]"
    if not isinstance(value, Mapping):
        raise CandidateExtractionValidationError(f"{path} must be an object")
    _require_exact_keys(value, _CANDIDATE_KEYS, path=path)
    source_ids = _source_ids_for_output(value.get("source_ids"), path=path, source_map=source_map)
    source = source_map[source_ids.key]
    source_role = _validate_source_role(value.get("source_role"), path=f"{path}.source_role")
    if source_role != source.source_role:
        raise CandidateExtractionValidationError(f"{path}.source_role does not match source evidence")
    assistant_suggestion = _required_bool(value.get("assistant_suggestion"), path=f"{path}.assistant_suggestion")
    expected_assistant = source_role == "assistant"
    if assistant_suggestion != expected_assistant:
        raise CandidateExtractionValidationError(f"{path}.assistant_suggestion contradicts source role")
    epistemic_status = _required_text(value.get("epistemic_status"), path=f"{path}.epistemic_status").lower()
    expected_status = "assistant_suggested" if expected_assistant else "user_reported"
    if epistemic_status != expected_status:
        raise CandidateExtractionValidationError(f"{path}.epistemic_status must be {expected_status}")

    candidate_text = _normalize_candidate_text(
        _required_text(value.get("candidate_text"), path=f"{path}.candidate_text")
    )
    if not MIN_CANDIDATE_CHARS <= len(candidate_text) <= MAX_CANDIDATE_CHARS:
        raise CandidateExtractionValidationError(
            f"{path}.candidate_text must be {MIN_CANDIDATE_CHARS}-{MAX_CANDIDATE_CHARS} characters"
        )
    rejection_reasons = classify_rejection_reasons(candidate_text)
    if rejection_reasons:
        raise CandidateExtractionValidationError(
            f"{path}.candidate_text fails standalone gates: {', '.join(rejection_reasons)}"
        )
    durability_score = _required_score(value.get("durability_score"), path=f"{path}.durability_score")
    standalone_score = _required_score(value.get("standalone_context_score"), path=f"{path}.standalone_context_score")
    if durability_score < MIN_DURABILITY_SCORE:
        raise CandidateExtractionValidationError(f"{path}.durability_score is below acceptance threshold")
    if standalone_score < MIN_STANDALONE_CONTEXT_SCORE:
        raise CandidateExtractionValidationError(f"{path}.standalone_context_score is below acceptance threshold")
    subject = _required_text(value.get("subject"), path=f"{path}.subject")
    if len(subject) > 120:
        raise CandidateExtractionValidationError(f"{path}.subject exceeds 120 characters")
    reason_type = _validated_enum(value.get("reason_type"), validate_reason_type, path=f"{path}.reason_type")
    if expected_assistant and reason_type != "assistant_suggestion":
        raise CandidateExtractionValidationError(
            f"{path}.reason_type must be assistant_suggestion for assistant evidence"
        )
    if not expected_assistant and reason_type == "assistant_suggestion":
        raise CandidateExtractionValidationError(f"{path}.reason_type cannot be assistant_suggestion for user evidence")
    return ExtractedCandidate(
        candidate_text=candidate_text,
        memory_type=_validated_enum(value.get("memory_type"), validate_memory_type, path=f"{path}.memory_type"),
        reason_type=reason_type,
        subject=subject,
        subject_confidence=_required_score(value.get("subject_confidence"), path=f"{path}.subject_confidence"),
        domain=_canonical_domain(value.get("domain"), path=f"{path}.domain"),
        domain_confidence=_required_score(value.get("domain_confidence"), path=f"{path}.domain_confidence"),
        durability_score=durability_score,
        standalone_context_score=standalone_score,
        confidence=_required_score(value.get("confidence"), path=f"{path}.confidence"),
        source_ids=source_ids,
        source_role=source_role,
        epistemic_status=epistemic_status,
        assistant_suggestion=assistant_suggestion,
    )


def _validate_model_rejection(
    value: Any,
    *,
    index: int,
    source_map: Mapping[tuple[str, str, str], CandidateSource],
) -> RejectedSource:
    path = f"output.rejections[{index}]"
    if not isinstance(value, Mapping):
        raise CandidateExtractionValidationError(f"{path} must be an object")
    _require_exact_keys(value, _REJECTION_KEYS, path=path)
    source_ids = _source_ids_for_output(value.get("source_ids"), path=path, source_map=source_map)
    source = source_map[source_ids.key]
    source_role = _validate_source_role(value.get("source_role"), path=f"{path}.source_role")
    if source_role != source.source_role:
        raise CandidateExtractionValidationError(f"{path}.source_role does not match source evidence")
    raw_reasons = _required_list(value.get("rejection_reasons"), path=f"{path}.rejection_reasons")
    if not raw_reasons:
        raise CandidateExtractionValidationError(f"{path}.rejection_reasons must not be empty")
    reasons: list[str] = []
    for reason_index, raw_reason in enumerate(raw_reasons):
        reason = _required_text(raw_reason, path=f"{path}.rejection_reasons[{reason_index}]").lower()
        if reason not in VALID_REJECTION_REASONS:
            raise CandidateExtractionValidationError(f"invalid rejection reason at {path}: {reason}")
        reasons.append(reason)
    if len(reasons) != len(set(reasons)):
        raise CandidateExtractionValidationError(f"{path}.rejection_reasons contains duplicates")
    return RejectedSource(source_ids=source_ids, source_role=source_role, rejection_reasons=tuple(reasons))


def _decode_model_output(raw_output: str | bytes | Mapping[str, Any]) -> Mapping[str, Any]:
    if isinstance(raw_output, Mapping):
        return raw_output
    if isinstance(raw_output, bytes):
        if len(raw_output) > MAX_MODEL_OUTPUT_BYTES:
            raise CandidateExtractionValidationError("model output exceeds byte limit")
        try:
            text = raw_output.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CandidateExtractionValidationError("model output is not UTF-8") from exc
    elif isinstance(raw_output, str):
        text = raw_output
        if len(text.encode("utf-8")) > MAX_MODEL_OUTPUT_BYTES:
            raise CandidateExtractionValidationError("model output exceeds byte limit")
    else:
        raise CandidateExtractionValidationError("model output must be JSON text, bytes, or an object")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CandidateExtractionValidationError(f"model output is not valid JSON: {exc.msg}") from exc
    if not isinstance(payload, Mapping):
        raise CandidateExtractionValidationError("model output must be a JSON object")
    return payload


def _source_ids_for_output(
    value: Any,
    *,
    path: str,
    source_map: Mapping[tuple[str, str, str], CandidateSource],
) -> SourceIds:
    if not isinstance(value, Mapping):
        raise CandidateExtractionValidationError(f"{path}.source_ids must be an object")
    source_ids = SourceIds.from_mapping(value, path=f"{path}.source_ids")
    if source_ids.key not in source_map:
        raise CandidateExtractionValidationError(f"{path}.source_ids do not match supplied evidence")
    return source_ids


def _rejected_batch(source: CandidateSource, reasons: Sequence[str]) -> CandidateExtractionBatch:
    return CandidateExtractionBatch(
        candidates=(),
        rejections=(
            RejectedSource(
                source_ids=source.source_ids,
                source_role=source.source_role,
                rejection_reasons=tuple(_ordered_unique(reasons)),
            ),
        ),
    )


def _classify_memory(text: str, role: str) -> tuple[str, str, float]:
    lowered = text.lower()
    if role == "assistant":
        return "hypothesis", "assistant_suggestion", 0.68
    if re.search(
        r"\b(i prefer|i like|i (?:don['’]t|do not) like|i love|i dislike|i hate|i avoid|i usually|i always|"
        r"the user (?:prefers|likes|does not like|loves|dislikes|hates|avoids|is interested|is willing|wants))\b",
        lowered,
    ):
        return "preference", "preference_choice", 0.86
    if re.search(r"\b(i decided|we decided|i chose|we chose|settled on|will use going forward)\b", lowered):
        return "decision", "preference_choice", 0.84
    if re.search(r"\b(failed|did not work|didn't work|broke|error|underproofed|overproofed)\b", lowered):
        return "failure", "user_reported_outcome", 0.82
    if re.search(r"\b(workaround|instead i|instead we|resolved by|fixed by)\b", lowered):
        return "workaround", "workaround", 0.82
    if re.search(r"\bwe need\b", lowered) or re.match(r"^the .+ recipe needs\b", lowered):
        return "procedure", "ideal_procedure", 0.76
    if re.search(r"\b(i already have|i have|we already have|we have)\b", lowered):
        return "semantic_fact", "unknown", 0.72
    if re.search(r"\b(worked|succeeded|success|produced|rose|fell|completed|finished|confirmed)\b", lowered):
        return "episodic", "user_reported_outcome", 0.84
    if re.search(r"\bwas (?:good|great|delicious|successful|a success|a failure)\b", lowered):
        return "episodic", "user_reported_outcome", 0.80
    if re.search(r"\b(must|cannot|can't|allergic|requires?|need(?:s)? to)\b", lowered):
        return "constraint", "constraint_response", 0.76
    if re.search(r"\b(open loop|still need|need to revisit|not yet resolved)\b", lowered):
        return "open_loop", "unknown", 0.70
    return "semantic_fact", "unknown", 0.66


def _has_durable_memory_marker(text: str) -> bool:
    return bool(
        re.search(
            r"\b(i prefer|i like|i (?:don['’]t|do not) like|i love|i dislike|i hate|i avoid|i usually|i always|i decided|"
            r"we decided|worked|succeeded|success|failed|did not work|didn't work|rose|fell|confirmed|"
            r"was (?:good|great|delicious|successful|a success|a failure)|because i|i already have|"
            r"i have|we already have|we have|(?:the|my) (?:starter|dough|recipe|project|workflow|system|"
            r"application|device|oven|cake|bread|rye|pavlova|pavlovas) (?:is|was|had|rose|fell))\b",
            text.lower(),
        )
    )


def _standalone_context_score(text: str) -> float:
    lowered = text.lower()
    score = 0.86
    if len(text.split()) < 7:
        score -= 0.12
    if re.search(r"\b(this|that|these|those|it|they|them)\b", lowered):
        score -= 0.08
    if re.search(r"\b(my|our|the)\s+[a-z][a-z-]+", lowered):
        score += 0.06
    if re.search(r"\b(starter|recipe|project|workflow|system|oven|dough|application|device)\b", lowered):
        score += 0.05
    return round(max(0.0, min(score, 1.0)), 3)


_DOMAIN_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "cooking_baking",
        (
            "recipe",
            "bake",
            "baking",
            "starter",
            "sourdough",
            "oven",
            "dough",
            "cake",
            "cook",
            "cooking",
            "bread",
            "flour",
            "ingredient",
        ),
    ),
    ("lab_automation", ("assay", "pipette", "plate reader", "lab automation", "robotic lab")),
    ("ai_memory_systems", ("memory system", "retrieval", "embedding", "ollama", "local llm", "rag")),
    ("career_work", ("career", "resume", "interview", "manager", "job search")),
    ("finance_investing", ("portfolio", "investment", "stock", "401k", "ira")),
    (
        "health_supplements",
        (
            "supplement",
            "vitamin",
            "medication",
            "symptom",
            "diagnosis",
            "treatment",
            "dose",
            "glycemic",
            "sleep",
            "substance use",
        ),
    ),
    ("relationships_life", ("relationship", "partner", "girlfriend", "family", "owned my side", "she thought")),
    ("law_lsat", ("lsat", "legal brief", "case law")),
    ("style_wardrobe", ("outfit", "wardrobe", "jacket", "shirt")),
    ("creative_writing", ("novel", "story", "character arc", "poem")),
    (
        "home_projects",
        ("home repair", "garage", "shelf", "carpentry", "mosfet", "esp32", "circuit board", "relay board"),
    ),
    ("fitness_training", ("workout", "training plan", "gym", "running")),
    ("pets", ("dog", "cat", "veterinarian", "pet")),
)

_SUBJECT_NAMES = {
    "ai_memory_systems": "AI Memory and Local LLMs",
    "career_work": "Career and Job Search",
    "cooking_baking": "Recipes and Baking",
    "creative_writing": "Creative Writing",
    "finance_investing": "Finance and Investing",
    "fitness_training": "Fitness and Training",
    "health_supplements": "Health and Supplements",
    "home_projects": "Home Projects and Devices",
    "lab_automation": "Lab Automation",
    "law_lsat": "Law and LSAT",
    "misc": "Uncategorized",
    "pets": "Pets and Animal Care",
    "relationships_life": "Relationships and Life",
    "style_wardrobe": "Style and Wardrobe",
}


def _infer_domain(source: CandidateSource) -> tuple[str, float]:
    haystack = source.text.lower()
    scored: list[tuple[int, str]] = []
    for domain, patterns in _DOMAIN_PATTERNS:
        count = sum(1 for pattern in patterns if _contains_domain_pattern(haystack, pattern))
        if count:
            scored.append((count, domain))
    if source.domain is not None:
        source_count = next((count for count, domain in scored if domain == source.domain), 0)
        if scored:
            top_count, top_domain = max(scored, key=lambda item: (item[0], -CANONICAL_DOMAINS.index(item[1])))
            high_risk_tie = next(
                (
                    (count, domain)
                    for count, domain in scored
                    if domain in {"finance_investing", "health_supplements", "law_lsat", "relationships_life"}
                    and domain != source.domain
                    and count >= max(source_count, 1)
                ),
                None,
            )
            if high_risk_tie is not None:
                top_count, top_domain = high_risk_tie
                return top_domain, round(min(0.65 + (top_count - 1) * 0.1, 0.95), 3)
            if top_domain != source.domain and source_count == 0 and top_count >= 1:
                return top_domain, round(min(0.65 + (top_count - 1) * 0.1, 0.95), 3)
            if top_domain != source.domain and top_count >= source_count + 1:
                return top_domain, round(min(0.65 + (top_count - 1) * 0.1, 0.95), 3)
        elif source.title:
            title = source.title.lower()
            title_scores = [
                (sum(1 for pattern in patterns if _contains_domain_pattern(title, pattern)), domain)
                for domain, patterns in _DOMAIN_PATTERNS
            ]
            title_count, title_domain = max(title_scores, key=lambda item: (item[0], -CANONICAL_DOMAINS.index(item[1])))
            if title_count and title_domain != source.domain:
                return title_domain, round(min(0.65 + (title_count - 1) * 0.1, 0.95), 3)
        return source.domain, source.domain_confidence if source.domain_confidence is not None else 0.75
    if not scored:
        title = (source.title or "").lower()
        for domain, patterns in _DOMAIN_PATTERNS:
            count = sum(1 for pattern in patterns if _contains_domain_pattern(title, pattern))
            if count:
                scored.append((count, domain))
    if not scored:
        return "misc", 0.25
    count, domain = max(scored, key=lambda item: (item[0], -CANONICAL_DOMAINS.index(item[1])))
    return domain, round(min(0.65 + (count - 1) * 0.1, 0.95), 3)


def _infer_subject(source: CandidateSource, domain: str, domain_confidence: float) -> tuple[str, float]:
    if source.subject is not None:
        return source.subject, source.subject_confidence if source.subject_confidence is not None else 0.75
    return _SUBJECT_NAMES[domain], domain_confidence


def _contains_domain_pattern(text: str, pattern: str) -> bool:
    return re.search(rf"(?<![a-z0-9_]){re.escape(pattern)}(?![a-z0-9_])", text) is not None


def _is_question_only(text: str) -> bool:
    stripped = text.strip()
    lowered = stripped.lower()
    if not stripped:
        return False
    question_start = re.match(
        r"^(?:or\s+)?(what|what's|whats|why|when|where|who|which|how|can|could|should|would|will|is|are|am|do|does|did|have|has)\b",
        lowered,
    )
    speculative_question = re.match(r"^i wonder (?:if|whether)\b", lowered)
    durable_clause = re.search(
        r"\b(i prefer|i like|i love|i hate|i avoid|i decided|we decided|worked|failed|succeeded|"
        r"my (?:starter|dough|recipe|project|workflow|system|application|device|oven|cake|bread) "
        r"(?:is|was|rose|fell))\b",
        lowered,
    )
    embedded_question = re.search(
        r"\b(?:how|what|why|when|where) (?:do|does|did|should|can|could|is|are)\b|"
        r"\bdo i need\b",
        lowered,
    )
    return bool(("?" in stripped or question_start or speculative_question or embedded_question) and not durable_clause)


def _is_context_dependent(text: str) -> bool:
    words = text.split()
    if re.match(r"^(?:it|this|that|these|those|they|them|something|anything|or\s+(?:a|an|the))\b", text):
        return True
    if re.match(r"^(?:massive|great|huge|total) success[,;:]?\s+(?:it|this|that|these|those|they|them)\b", text):
        return True
    if re.search(r"\boption\s+[a-z0-9]\b", text):
        return True
    if re.search(r"\b(?:first|second|third|last|next) (?:one|two|three|suggestions?|options?|ideas?)\b", text):
        return True
    if re.search(r"\b(?:looks?|seems?) like (?:it|they|this|that|these|those)\b", text):
        return True
    if re.search(
        r"\bwhat (?:you|we|i) (?:prescribed|suggested|recommended|mentioned|discussed)\b",
        text,
    ):
        return True
    if re.search(r"\b(?:all|both|any|one|some|something|everything|nothing) of (?:it|them|these|those)\b", text):
        return True
    if re.search(r"\b(?:something|anything) (?:that|which|from)\b", text):
        return True
    if re.search(r"\b(?:the )?other (?:flavors?|ideas?|ones?|options?)\b", text):
        return True
    if re.search(r"\bwhatever (?:equipment|ingredients?|supplies|tools?)\b", text):
        return True
    if re.search(r"\b(?:make|makes|making) this\b", text):
        return True
    if re.search(r"\b(?:they|them|these|those)\b", text):
        return True
    if re.search(r"\b(?:this|that) (?:cocktail|dish|bake|batch|version|idea|option)\b", text):
        return True
    if re.search(r"\b(?:the|that|this) [a-z][a-z-]* idea\b", text):
        return True
    if re.search(r"\bsame as (?:the )?(?:others|other ones|last time)\b", text):
        return True
    if re.search(r"\b(?:that|this) (?:window|time|one)\b|\bin this\b|\bor something\b", text):
        return True
    if re.match(r"^after mix\b", text):
        return True
    if re.match(r"^(?:the\s+)?(?:mix|rye)\b(?!\s+(?:batch|bread|culture|dough|flour|starter)\b)", text):
        return True
    if re.search(r"\b\d+(?:\.\d+)?[ -]inch rounds?\b", text):
        return True
    if re.match(
        r"^(?:kinda|kind of|sort of)?\s*(?:messed up|liked|loved|fixed|finished)\s+(?:the|my|our)?\s*(?:first|second|next|last)\b",
        text,
    ):
        return True
    if len(words) <= 18 and re.search(r"\b(this|that|these|those|it|them|the former|the latter)\b", text):
        explicit_object = re.search(
            r"\b(this|that|these|those)\s+(starter|recipe|project|workflow|device|method|batch|cake|bread|system)\b",
            text,
        )
        if not explicit_object:
            return True
    if re.search(r"(?:^|[.!?]\s+)(?:it|they|them|these|those)\b", text):
        return True
    return bool(re.search(r"\b(as (?:above|discussed|before)|the one (?:above|you mentioned)|same as before)\b", text))


def _is_option_reference(text: str) -> bool:
    return bool(
        re.fullmatch(r"(?:i\s+)?(?:like|prefer|choose|pick|want)?\s*[0-9,\s]+(?:and\s+[0-9]+)?[.!]?", text)
        or re.fullmatch(r"(?:i\s+)?(?:like|prefer|choose|pick|want)\s+options?\s+[0-9,\sand]+[.!]?", text)
        or re.fullmatch(r"(?:option|number|version)\s+[0-9]+[.!]?", text)
    )


def _is_meta_instruction(text: str) -> bool:
    markers = (
        "acceptance test",
        "acceptance report",
        "test matrix",
        "memory_review_subjects",
        "memory_search",
        "memory_context",
        "memory_status",
        "memory_trace",
        "submit_agent_feedback",
        "run_id",
        "trace_id",
        "mcp connector",
        "browser chatgpt",
        "follow this prompt",
        "transcript artifact",
        "synthetic network fixture",
    )
    return any(marker in text for marker in markers)


def _is_transient_execution_chatter(text: str) -> bool:
    words = text.split()
    if len(words) > 24:
        return False
    return bool(
        re.fullmatch(r"(?:do it|continue|try again|go ahead|next|what(?:'s| is) next|let(?:'s| us) do it)[.!?]?", text)
        or re.match(r"^(?:run|execute|restart|rerun|spin up|move on to)\b", text)
        or re.search(r"\b(?:right now|for now|today|tonight)\b", text)
        and re.match(r"^(?:can you|please|let(?:'s| us)|i want you to)\b", text)
    )


def _is_transient_request_or_planning(text: str) -> bool:
    if re.search(
        r"\b(?:right now|for now|today|tonight|tomorrow|this evening|before i (?:go|leave|see)|"
        r"time permitting|for the next bake)\b",
        text,
    ):
        return True
    return bool(
        re.match(
            r"^(?:(?:alright|okay|ok|actually)[, ]+)?(?:know what(?: actually)?[, ]+)?(?:let(?:['’]s| us)|can you|could you|please|give me|"
            r"find me|show me|help me|i want you to|walk me through|suggest|tell me|get me|"
            r"recommend|make me|write me|create me|list|swap)\b",
            text,
        )
        or re.match(r"^need (?:a|an|some|something)\b", text)
        or re.match(r"^i need (?:a|an|some) (?:shopping list|recipe|list|plan|idea|option)\b", text)
        or re.match(r"^i(?:['’]m| am) (?:also )?thinking\b", text)
        or re.match(r"^i(?:['’]d| would) want to (?:make|try|do|start)\b", text)
        or re.match(r"^i was (?:gonna|going to)\b", text)
        or re.match(r"^i(?:['’]ll| will) (?:get|buy|pick up|order)\b", text)
        or re.match(r"^i still need (?:groceries|ingredients|supplies)\b", text)
        or re.match(r"^we can go\b.*\bto start\b", text)
        or re.match(r"^(?:doesn['’]t|does not) need to be\b", text)
        or re.search(r"\blooking for help brainstorming\b", text)
        or re.search(r"\b(?:new )?same[- ]day options?\b", text)
        or re.search(r"\b(?:already shopped|just need to execute|recent food in mind)\b", text)
    )


def _is_temporally_volatile_state(text: str) -> bool:
    if re.search(r"\b(?:currently|right now|on hand|at the moment|just got|just bought)\b", text):
        return True
    if re.match(r"^i already have\b", text) and text.count(",") >= 2:
        return True
    if re.search(r"\bi have (?:many|multiple|several|various) (?:kinds|types) of\b", text):
        return True
    if re.search(r"\b(?:hitting|reaching) (?:a )?(?:high|low) of \d+", text):
        return True
    return bool(re.match(r"^i have (?:about|around|roughly|only) \d+\s+(?:minutes?|hours?|days?)\b", text))


def _has_mixed_transient_and_durable_claims(text: str) -> bool:
    if len(text.split()) < 16 or not _has_durable_memory_marker(text):
        return False
    return bool(
        re.search(
            r"\b(?:same[- ]day options?|right now|today|tonight|time permitting|before i (?:go|leave|see))\b", text
        )
        or re.search(r"\b(?:let(?:'s| us)|can you|could you|show me|give me|we can maybe)\b", text)
        or re.search(
            r"\b(?:one or two (?:batches|days) a week|prep it for herself|to figure out a way|"
            r"we should explore|maybe i can get)\b",
            text,
        )
    )


def _is_underspecified_episode(text: str) -> bool:
    vague_duration = re.search(r"\b(?:quite a lot of time|a long time|a while|slow(?:ly)?|taking forever)\b", text)
    measured_condition = re.search(r"\b\d+(?:\.\d+)?\s*(?:minutes?|hours?|days?|%|percent|degrees?|[cf])\b", text)
    return bool(vague_duration and not measured_condition)


def _normalize_candidate_text(value: str) -> str:
    text = " ".join(str(value or "").split()).strip()
    if not text:
        return text
    if text[0].isalpha() and text[0].islower():
        text = text[0].upper() + text[1:]
    if text[-1] not in ".!?":
        text += "."
    return text


def _select_candidate_proposition(text: str, *, title: str | None = None) -> str:
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", text) if part.strip()]
    declarative = [sentence for sentence in sentences if not _is_question_only(sentence)]
    durable = [sentence for sentence in declarative if _has_durable_memory_marker(sentence)]
    if durable:
        selected = sorted(
            enumerate(durable),
            key=lambda item: (
                -_classify_memory(item[1], "user")[2],
                -_standalone_context_score(item[1]),
                item[0],
            ),
        )[0][1]
    else:
        selected = declarative[0] if declarative else text
    explicit_dislike = re.search(
        r"\bI (?:don['’]t|do not) like\b.+?(?=,\s*but\b|;\s*but\b|$)",
        selected,
        flags=re.IGNORECASE,
    )
    if explicit_dislike:
        selected = explicit_dislike.group(0)
    selected = re.split(
        r",?\s+and then to figure out a way\b",
        selected,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0].strip()
    selected = re.sub(
        r"^(we need [^,]+),\s+([^,]+\s+with\b)",
        r"\1 and \2",
        selected,
        count=1,
        flags=re.IGNORECASE,
    )
    if title and re.match(r"^we need\b", selected, flags=re.IGNORECASE):
        object_name = re.sub(r"\s+recipe$", " recipe", title.strip(), flags=re.IGNORECASE)
        if not object_name.lower().endswith("recipe"):
            object_name = f"{object_name} recipe"
        selected = re.sub(
            r"^we need\b",
            f"The {object_name} needs",
            selected,
            count=1,
            flags=re.IGNORECASE,
        )
    selected = re.split(
        r"[,;]\s*(?:and\s+)?(?:walk me through|give me|help me|let(?:['’]s| us)|suggest|tell me|"
        r"show me|get me|include directions|can you|could you)\b",
        selected,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0].strip()
    return _normalize_candidate_text(selected)


def _canonical_domain(value: Any, *, path: str) -> str:
    text = _required_text(value, path=path)
    try:
        return validate_domain(text)
    except ValueError as exc:
        raise CandidateExtractionValidationError(f"{path}: {exc}") from exc


def _validated_enum(value: Any, validator: Any, *, path: str) -> str:
    text = _required_text(value, path=path)
    try:
        return str(validator(text))
    except ValueError as exc:
        raise CandidateExtractionValidationError(f"{path}: {exc}") from exc


def _validate_source_role(value: Any, *, path: str) -> str:
    role = _required_text(value, path=path).lower()
    if role not in VALID_SOURCE_ROLES:
        raise CandidateExtractionValidationError(f"{path} must be one of {VALID_SOURCE_ROLES}")
    return role


def _required_score(value: Any, *, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CandidateExtractionValidationError(f"{path} must be a number from 0 to 1")
    score = float(value)
    if not 0.0 <= score <= 1.0:
        raise CandidateExtractionValidationError(f"{path} must be a number from 0 to 1")
    return score


def _optional_score(value: Any, *, path: str) -> float | None:
    return None if value is None else _required_score(value, path=path)


def _required_bool(value: Any, *, path: str) -> bool:
    if not isinstance(value, bool):
        raise CandidateExtractionValidationError(f"{path} must be a boolean")
    return value


def _required_text(value: Any, *, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CandidateExtractionValidationError(f"{path} must be a non-empty string")
    return value.strip()


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise CandidateExtractionValidationError("optional text values must be strings")
    return value.strip() or None


def _required_list(value: Any, *, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise CandidateExtractionValidationError(f"{path} must be an array")
    return value


def _require_exact_keys(value: Mapping[str, Any], expected: set[str], *, path: str) -> None:
    actual = set(value)
    missing = sorted(expected.difference(actual))
    extra = sorted(actual.difference(expected))
    if missing or extra:
        details = []
        if missing:
            details.append(f"missing={missing}")
        if extra:
            details.append(f"unexpected={extra}")
        raise CandidateExtractionValidationError(f"{path} has invalid fields ({', '.join(details)})")


def _ordered_unique(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(values))


__all__ = [
    "CandidateExtractionBatch",
    "CandidateExtractionValidationError",
    "CandidateSource",
    "ExtractedCandidate",
    "RejectedSource",
    "SourceIds",
    "classify_rejection_reasons",
    "heuristic_extract_candidate",
    "heuristic_extract_candidates",
    "parse_model_extraction_output",
    "validate_model_extraction_payload",
]
