from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence


HIGH_RISK_POLICY_VERSION = "conservative_high_risk_v2"
HIGH_RISK_DOMAINS = (
    "health_supplements",
    "finance_investing",
    "law_lsat",
    "relationships_life",
)

_DOMAIN_ALIASES = {
    "finance": "finance_investing",
    "financial": "finance_investing",
    "health": "health_supplements",
    "legal": "law_lsat",
    "law": "law_lsat",
    "relationship": "relationships_life",
    "relationships": "relationships_life",
}
_TRUST_RANK = {"low": 0, "medium": 1, "high": 2, "canonical": 3}
_INACTIVE_STATUSES = {"stale", "superseded", "archived", "deleted"}
_SUGGESTION_EPISTEMIC_STATUSES = {
    "assistant_inferred",
    "assistant_suggested",
    "speculative",
    "system_inferred",
    "unknown",
}
_FACT_EPISTEMIC_STATUSES = {
    "confirmed",
    "externally_sourced",
    "observed",
    "user_reported",
}


@dataclass(frozen=True)
class HighRiskDomainPolicy:
    domain: str
    caution_labels: tuple[str, ...]
    max_age_days: float
    minimum_trust: str = "high"
    authoritative_source_required_for_advice: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "caution_labels": list(self.caution_labels),
            "max_age_days": self.max_age_days,
            "minimum_trust": self.minimum_trust,
            "authoritative_source_required_for_advice": self.authoritative_source_required_for_advice,
        }


DOMAIN_POLICIES: dict[str, HighRiskDomainPolicy] = {
    "health_supplements": HighRiskDomainPolicy(
        domain="health_supplements",
        caution_labels=("health_caution",),
        max_age_days=30,
    ),
    "finance_investing": HighRiskDomainPolicy(
        domain="finance_investing",
        caution_labels=("financial_caution",),
        max_age_days=7,
    ),
    "law_lsat": HighRiskDomainPolicy(
        domain="law_lsat",
        caution_labels=("legal_caution", "source_authority"),
        max_age_days=30,
    ),
    "relationships_life": HighRiskDomainPolicy(
        domain="relationships_life",
        caution_labels=("relationship_caution", "relationship_context"),
        max_age_days=90,
    ),
}


@dataclass(frozen=True)
class HighRiskDecision:
    disposition: str
    include_in_factual_context: bool
    include_in_uncertainty: bool
    supports_advice: bool
    refuse_unsupported_advice: bool
    review_required: bool
    policy_domains: tuple[str, ...]
    matched_domains: tuple[str, ...]
    caution_labels: tuple[str, ...]
    reasons: tuple[str, ...]
    checks: Mapping[str, Mapping[str, Any]]
    source_id: str | None = None
    policy: str = HIGH_RISK_POLICY_VERSION
    mutates_memory_truth: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy": self.policy,
            "disposition": self.disposition,
            "include_in_factual_context": self.include_in_factual_context,
            "include_in_uncertainty": self.include_in_uncertainty,
            "supports_advice": self.supports_advice,
            "refuse_unsupported_advice": self.refuse_unsupported_advice,
            "review_required": self.review_required,
            "policy_domains": list(self.policy_domains),
            "matched_domains": list(self.matched_domains),
            "caution_labels": list(self.caution_labels),
            "reasons": list(self.reasons),
            "checks": {name: dict(payload) for name, payload in self.checks.items()},
            "source_id": self.source_id,
            "mutates_memory_truth": self.mutates_memory_truth,
        }


def high_risk_policy_for_domain(domain: str) -> HighRiskDomainPolicy | None:
    return DOMAIN_POLICIES.get(_normalize_domain(domain))


def evaluate_high_risk_evidence(
    *,
    query_domains: Sequence[str],
    candidate_domains: Sequence[str],
    source_role: str | None,
    epistemic_status: str | None = None,
    trust_level: str | None = None,
    status: str | None = None,
    age_days: float | None = None,
    advice_requested: bool = False,
    authoritative_source: bool = False,
    confirmed_by_user: bool = False,
    source_id: str | None = None,
) -> HighRiskDecision:
    """Evaluate one candidate without mutating or consulting memory state.

    Missing trust is permitted only for direct user evidence because raw transcript
    chunks do not carry curated trust levels. An explicit trust value is always
    enforced. Assistant and inferred material can be retained only as uncertainty.
    """
    normalized_query = _normalize_domains(query_domains)
    normalized_candidate = _normalize_domains(candidate_domains)
    policy_domains = tuple(domain for domain in normalized_query if domain in DOMAIN_POLICIES)
    if not policy_domains:
        return _not_applicable_decision(source_id=source_id)

    policies = [DOMAIN_POLICIES[domain] for domain in policy_domains]
    matched_domains = tuple(domain for domain in policy_domains if domain in normalized_candidate)
    caution_labels = _dedupe(label for policy in policies for label in policy.caution_labels)
    checks: dict[str, dict[str, Any]] = {}
    reasons: list[str] = []

    domain_allowed = bool(matched_domains)
    checks["domain_boundary"] = {
        "status": "pass" if domain_allowed else "fail",
        "query_domains": list(normalized_query),
        "candidate_domains": list(normalized_candidate),
        "matched_domains": list(matched_domains),
        "cross_domain_allowed": False,
    }
    if not domain_allowed:
        reasons.append("cross_domain_blocked")

    normalized_status = _normalize_token(status or "active")
    status_allowed = normalized_status not in _INACTIVE_STATUSES
    checks["record_status"] = {
        "status": "pass" if status_allowed else "fail",
        "value": normalized_status,
    }
    if not status_allowed:
        reasons.append("inactive_high_risk_evidence")

    maximum_age = min(policy.max_age_days for policy in policies)
    age_allowed = age_days is not None and 0 <= age_days <= maximum_age
    checks["temporal"] = {
        "status": "pass" if age_allowed else "fail",
        "age_days": age_days,
        "max_age_days": maximum_age,
    }
    if age_days is None:
        reasons.append("temporal_validity_unknown")
    elif age_days < 0:
        reasons.append("invalid_evidence_age")
    elif age_days > maximum_age:
        reasons.append("stale_high_risk_evidence")

    normalized_role = _normalize_token(source_role or "unknown")
    normalized_epistemic = _normalize_token(epistemic_status or _default_epistemic_status(normalized_role))
    if confirmed_by_user:
        evidence_class = "user_fact"
    elif normalized_role == "assistant" or normalized_epistemic in _SUGGESTION_EPISTEMIC_STATUSES:
        evidence_class = "suggestion_only"
    elif normalized_role == "user" and normalized_epistemic in _FACT_EPISTEMIC_STATUSES:
        evidence_class = "user_fact"
    elif normalized_epistemic == "externally_sourced" and authoritative_source:
        evidence_class = "authoritative_fact"
    elif normalized_epistemic in {"confirmed", "observed"}:
        evidence_class = "confirmed_fact"
    else:
        evidence_class = "unsupported"
    checks["epistemic"] = {
        "status": evidence_class,
        "source_role": normalized_role,
        "epistemic_status": normalized_epistemic,
        "confirmed_by_user": confirmed_by_user,
    }

    normalized_trust = _normalize_token(trust_level) if trust_level else None
    minimum_trust = max((policy.minimum_trust for policy in policies), key=lambda value: _TRUST_RANK[value])
    trust_required = normalized_trust is not None or evidence_class in {"authoritative_fact", "confirmed_fact"}
    trust_allowed = not trust_required or _TRUST_RANK.get(normalized_trust or "", -1) >= _TRUST_RANK[minimum_trust]
    checks["trust"] = {
        "status": "pass" if trust_allowed else "fail",
        "value": normalized_trust,
        "minimum": minimum_trust,
        "required": trust_required,
        "raw_user_evidence_exempt": not trust_required and evidence_class == "user_fact",
    }
    if not trust_allowed:
        reasons.append("low_trust_high_risk_evidence")

    supports_advice = (
        domain_allowed
        and status_allowed
        and age_allowed
        and trust_allowed
        and authoritative_source
        and evidence_class in {"authoritative_fact", "confirmed_fact"}
    )
    refuse_advice = advice_requested and not supports_advice
    checks["advice_support"] = {
        "status": "supported" if supports_advice else "refuse" if advice_requested else "not_requested",
        "advice_requested": advice_requested,
        "authoritative_source": authoritative_source,
        "authoritative_source_required": True,
    }
    if refuse_advice:
        reasons.append("unsupported_advice_refused")

    hard_failure = not domain_allowed or not status_allowed or not age_allowed or not trust_allowed
    if hard_failure or evidence_class == "unsupported":
        if evidence_class == "unsupported":
            reasons.append("unsupported_epistemic_status")
        disposition = "reject"
        include_fact = False
        include_uncertainty = False
        review_required = False
    elif evidence_class == "suggestion_only":
        reasons.append("assistant_or_inferred_evidence_only")
        disposition = "suggestion_only"
        include_fact = False
        include_uncertainty = True
        review_required = True
    else:
        disposition = "allow_fact"
        include_fact = True
        include_uncertainty = False
        review_required = refuse_advice
        if not reasons:
            reasons.append("current_supported_fact")

    return HighRiskDecision(
        disposition=disposition,
        include_in_factual_context=include_fact,
        include_in_uncertainty=include_uncertainty,
        supports_advice=supports_advice,
        refuse_unsupported_advice=refuse_advice,
        review_required=review_required,
        policy_domains=policy_domains,
        matched_domains=matched_domains,
        caution_labels=caution_labels,
        reasons=tuple(dict.fromkeys(reasons)),
        checks=checks,
        source_id=source_id,
    )


def apply_high_risk_policy(
    candidates: Iterable[Mapping[str, Any]],
    *,
    query_domains: Sequence[str],
    advice_requested: bool = False,
) -> dict[str, Any]:
    """Partition candidate mappings into factual, suggestion-only, and rejected sets."""
    accepted: list[dict[str, Any]] = []
    suggestions: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    decisions: list[HighRiskDecision] = []

    for candidate in candidates:
        decision = evaluate_high_risk_evidence(
            query_domains=query_domains,
            candidate_domains=_candidate_domains(candidate),
            source_role=_optional_string(candidate.get("source_role") or candidate.get("role")),
            epistemic_status=_optional_string(candidate.get("epistemic_status")),
            trust_level=_optional_string(candidate.get("trust_level")),
            status=_optional_string(candidate.get("status")),
            age_days=_optional_float(candidate.get("age_days")),
            advice_requested=advice_requested,
            authoritative_source=bool(candidate.get("authoritative_source", False)),
            confirmed_by_user=bool(candidate.get("confirmed_by_user", False)),
            source_id=_optional_string(candidate.get("source_id") or candidate.get("chunk_id") or candidate.get("id")),
        )
        decisions.append(decision)
        enriched = dict(candidate)
        enriched["high_risk_decision"] = decision.to_dict()
        if decision.disposition in {"allow_fact", "not_applicable"}:
            accepted.append(enriched)
        elif decision.disposition == "suggestion_only":
            suggestions.append(enriched)
        else:
            rejected.append(decision.to_dict())

    policy_domains = _dedupe(
        domain for domain in _normalize_domains(query_domains) if domain in DOMAIN_POLICIES
    )
    caution_labels = _dedupe(label for decision in decisions for label in decision.caution_labels)
    has_supported_advice = any(decision.supports_advice for decision in decisions)
    result_quality = "reliable_results" if accepted else "suggestion_only_results" if suggestions else "no_reliable_results"
    return {
        "status": "ok",
        "policy": HIGH_RISK_POLICY_VERSION if policy_domains else "not_applicable",
        "high_risk": bool(policy_domains),
        "policy_domains": list(policy_domains),
        "caution_labels": list(caution_labels),
        "advice_requested": advice_requested,
        "refuse_unsupported_advice": advice_requested and not has_supported_advice,
        "result_quality": result_quality,
        "results": accepted,
        "suggestion_only": suggestions,
        "rejected_decisions": rejected,
        "counts": {
            "input": len(decisions),
            "factual": len(accepted),
            "suggestion_only": len(suggestions),
            "rejected": len(rejected),
        },
        "mutates_memory_truth": False,
    }


def _not_applicable_decision(*, source_id: str | None) -> HighRiskDecision:
    return HighRiskDecision(
        disposition="not_applicable",
        include_in_factual_context=True,
        include_in_uncertainty=False,
        supports_advice=False,
        refuse_unsupported_advice=False,
        review_required=False,
        policy_domains=(),
        matched_domains=(),
        caution_labels=(),
        reasons=("query_not_high_risk",),
        checks={"domain_boundary": {"status": "not_applicable"}},
        source_id=source_id,
        policy="not_applicable",
    )


def _candidate_domains(candidate: Mapping[str, Any]) -> list[str]:
    raw = candidate.get("domains")
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, Sequence):
        return [str(value) for value in raw]
    primary = candidate.get("domain_primary") or candidate.get("domain")
    return [str(primary)] if primary else []


def _normalize_domains(domains: Sequence[str]) -> tuple[str, ...]:
    return _dedupe(_normalize_domain(domain) for domain in domains)


def _normalize_domain(domain: str) -> str:
    normalized = _normalize_token(domain)
    return _DOMAIN_ALIASES.get(normalized, normalized)


def _normalize_token(value: str) -> str:
    return value.strip().lower().replace("-", "_").replace(" ", "_")


def _default_epistemic_status(source_role: str) -> str:
    if source_role == "user":
        return "user_reported"
    if source_role == "assistant":
        return "assistant_suggested"
    return "unknown"


def _dedupe(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def _optional_string(value: object) -> str | None:
    return str(value) if value is not None else None


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
