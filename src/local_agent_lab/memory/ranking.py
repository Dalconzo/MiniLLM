from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence


DISCLOSURE_TIERS = ("far", "medium", "close", "full")
DEFAULT_RANKING_PROFILE = "hybrid_memory_v1"
DEFAULT_WEIGHTS = {
    "keyword_relevance": 0.35,
    "semantic_similarity": 0.25,
    "subject_match": 0.15,
    "curated_trust": 0.10,
    "feedback": 0.10,
    "recency": 0.05,
    "spam_penalty": -0.20,
    "paywall_penalty": -0.10,
}
TIER_THRESHOLDS = (
    ("full", 0.85),
    ("close", 0.65),
    ("medium", 0.40),
    ("far", 0.0),
)
EXPOSED_FIELDS_BY_TIER = {
    "far": ("title", "source_kind", "message_created_at", "ids"),
    "medium": ("title", "role", "snippet", "message_created_at", "ids"),
    "close": ("title", "role", "snippet", "message_created_at", "ids", "chunk_text", "nearby_turn_refs"),
    "full": (
        "title",
        "role",
        "snippet",
        "message_created_at",
        "ids",
        "chunk_text",
        "nearby_turn_refs",
        "conversation_window",
        "related_curated_memories",
    ),
}


@dataclass(frozen=True)
class RankingSignals:
    semantic_similarity: Mapping[str, float] = field(default_factory=dict)
    subject_match: Mapping[str, float] = field(default_factory=dict)
    curated_trust: Mapping[str, float] = field(default_factory=dict)
    feedback: Mapping[str, float] = field(default_factory=dict)
    spam_penalty: Mapping[str, float] = field(default_factory=dict)
    paywall_penalty: Mapping[str, float] = field(default_factory=dict)


def rank_memory_hits(
    hits: Sequence[Mapping[str, Any]],
    *,
    signals: RankingSignals | None = None,
    depth: str = "full",
    now: datetime | None = None,
    weights: Mapping[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Rank memory candidates with an inspectable, deterministic score breakdown."""

    _validate_depth(depth)
    signal_values = signals or RankingSignals()
    active_weights = {**DEFAULT_WEIGHTS, **(weights or {})}
    keyword_scores = _keyword_scores(hits)
    ranked: list[dict[str, Any]] = []

    for index, hit in enumerate(hits):
        identifiers = _candidate_identifiers(hit)
        features = {
            "keyword_relevance": keyword_scores[index],
            "semantic_similarity": _lookup_signal(signal_values.semantic_similarity, identifiers, hit, "semantic_similarity"),
            "subject_match": _lookup_signal(signal_values.subject_match, identifiers, hit, "subject_match"),
            "curated_trust": _lookup_signal(signal_values.curated_trust, identifiers, hit, "curated_trust"),
            "feedback": _lookup_signal(signal_values.feedback, identifiers, hit, "feedback"),
            "recency": _recency_score(hit, now=now),
            "spam_penalty": _lookup_signal(signal_values.spam_penalty, identifiers, hit, "spam_penalty"),
            "paywall_penalty": _lookup_signal(signal_values.paywall_penalty, identifiers, hit, "paywall_penalty"),
        }
        score_breakdown = _score_breakdown(features, active_weights)
        score = score_breakdown["final_score"]
        tier = disclosure_tier_for_score(score, depth=depth)
        result = dict(hit)
        result.update(
            {
                "score": score,
                "score_breakdown": score_breakdown,
                "ranking_profile": DEFAULT_RANKING_PROFILE,
                "disclosure_tier": tier,
                "exposed_fields": list(EXPOSED_FIELDS_BY_TIER[tier]),
            }
        )
        ranked.append(result)

    ranked.sort(key=lambda item: (-float(item["score"]), _stable_sort_key(item)))
    for rank, item in enumerate(ranked, start=1):
        item["rank"] = rank
    return ranked


def disclosure_tier_for_score(score: float, *, depth: str = "full") -> str:
    """Map a score to a disclosure tier, capped by the requested retrieval depth."""

    _validate_depth(depth)
    requested_index = DISCLOSURE_TIERS.index(depth)
    score_tier = "far"
    for tier, threshold in TIER_THRESHOLDS:
        if score >= threshold:
            score_tier = tier
            break
    score_index = DISCLOSURE_TIERS.index(score_tier)
    return DISCLOSURE_TIERS[min(score_index, requested_index)]


def score_memory_hit(
    hit: Mapping[str, Any],
    *,
    keyword_relevance: float,
    semantic_similarity: float = 0.0,
    subject_match: float = 0.0,
    curated_trust: float = 0.0,
    feedback: float = 0.0,
    recency: float = 0.0,
    spam_penalty: float = 0.0,
    paywall_penalty: float = 0.0,
    weights: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    """Score one candidate when caller already has normalized feature values."""

    active_weights = {**DEFAULT_WEIGHTS, **(weights or {})}
    features = {
        "keyword_relevance": keyword_relevance,
        "semantic_similarity": semantic_similarity,
        "subject_match": subject_match,
        "curated_trust": curated_trust,
        "feedback": feedback,
        "recency": recency,
        "spam_penalty": spam_penalty,
        "paywall_penalty": paywall_penalty,
    }
    score_breakdown = _score_breakdown(features, active_weights)
    tier = disclosure_tier_for_score(score_breakdown["final_score"])
    result = dict(hit)
    result.update(
        {
            "score": score_breakdown["final_score"],
            "score_breakdown": score_breakdown,
            "ranking_profile": DEFAULT_RANKING_PROFILE,
            "disclosure_tier": tier,
            "exposed_fields": list(EXPOSED_FIELDS_BY_TIER[tier]),
        }
    )
    return result


def _score_breakdown(features: Mapping[str, float], weights: Mapping[str, float]) -> dict[str, Any]:
    components: dict[str, dict[str, float]] = {}
    raw_score = 0.0
    for name, raw_value in features.items():
        normalized = _clamp01(raw_value)
        weight = float(weights.get(name, 0.0))
        contribution = normalized * weight
        raw_score += contribution
        components[name] = {
            "value": round(normalized, 6),
            "weight": round(weight, 6),
            "contribution": round(contribution, 6),
        }
    final_score = _clamp01(raw_score)
    return {
        "profile": DEFAULT_RANKING_PROFILE,
        "components": components,
        "raw_score": round(raw_score, 6),
        "final_score": round(final_score, 6),
    }


def _keyword_scores(hits: Sequence[Mapping[str, Any]]) -> list[float]:
    explicit = [_optional_float(hit.get("keyword_relevance")) for hit in hits]
    if all(value is not None for value in explicit):
        return [_clamp01(value or 0.0) for value in explicit]

    bm25_values = [_candidate_bm25(hit) for hit in hits]
    numeric = [value for value in bm25_values if value is not None]
    if not numeric:
        return [0.0 for _ in hits]

    best = min(numeric)
    worst = max(numeric)
    if best == worst:
        return [1.0 if value is not None else 0.0 for value in bm25_values]

    return [
        _clamp01((worst - value) / (worst - best)) if value is not None else 0.0
        for value in bm25_values
    ]


def _candidate_bm25(hit: Mapping[str, Any]) -> float | None:
    if "bm25_score" in hit:
        return _optional_float(hit.get("bm25_score"))
    breakdown = hit.get("score_breakdown")
    if isinstance(breakdown, Mapping) and "fts_bm25" in breakdown:
        return _optional_float(breakdown.get("fts_bm25"))
    if "score" in hit:
        return _optional_float(hit.get("score"))
    return None


def _lookup_signal(values: Mapping[str, float], identifiers: Sequence[str], hit: Mapping[str, Any], field: str) -> float:
    for identifier in identifiers:
        if identifier in values:
            return _clamp01(values[identifier])
    explicit = _optional_float(hit.get(field))
    if explicit is not None:
        return _clamp01(explicit)
    return 0.0


def _candidate_identifiers(hit: Mapping[str, Any]) -> tuple[str, ...]:
    identifiers: list[str] = []
    for key in (
        "chunk_id",
        "message_id",
        "conversation_id",
        "source_conversation_id",
        "source_id",
        "domain",
        "title",
    ):
        value = hit.get(key)
        if value is not None:
            identifiers.append(str(value))
    return tuple(identifiers)


def _recency_score(hit: Mapping[str, Any], *, now: datetime | None) -> float:
    timestamp = (
        hit.get("message_created_at")
        or hit.get("updated_at")
        or hit.get("created_at")
        or hit.get("timestamp")
    )
    if timestamp is None:
        return 0.0
    parsed = _parse_datetime(timestamp)
    if parsed is None:
        return 0.0
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    age_days = max((reference - parsed).total_seconds() / 86_400, 0.0)
    return 1.0 / (1.0 + age_days / 365.0)


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _stable_sort_key(hit: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(hit.get("conversation_id") or ""),
        str(hit.get("message_id") or ""),
        str(hit.get("chunk_id") or ""),
    )


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clamp01(value: float) -> float:
    return min(max(float(value), 0.0), 1.0)


def _validate_depth(depth: str) -> None:
    if depth not in DISCLOSURE_TIERS:
        raise ValueError(f"depth must be one of: {', '.join(DISCLOSURE_TIERS)}")
