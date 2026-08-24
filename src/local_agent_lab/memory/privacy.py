from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


TRACKING_PARAM_NAMES = {
    "_hsenc",
    "_hsmi",
    "dclid",
    "fbclid",
    "gclid",
    "gbraid",
    "igshid",
    "mc_cid",
    "mc_eid",
    "mkt_tok",
    "msclkid",
    "oly_anon_id",
    "oly_enc_id",
    "pk_campaign",
    "pk_kwd",
    "ref_src",
    "sc_campaign",
    "spm",
    "twclid",
    "vero_conv",
    "vero_id",
    "wbraid",
    "yclid",
}

TRACKING_PARAM_PREFIXES = ("utm_",)


@dataclass(frozen=True)
class SecretFinding:
    kind: str
    start: int
    end: int
    fingerprint: str

    def to_dict(self) -> dict[str, str | int]:
        return {
            "kind": self.kind,
            "start": self.start,
            "end": self.end,
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True)
class RedactionResult:
    text: str
    findings: tuple[SecretFinding, ...]

    @property
    def redacted_count(self) -> int:
        return len(self.findings)

    def to_dict(self) -> dict[str, object]:
        return {
            "text": self.text,
            "redacted_count": self.redacted_count,
            "findings": [finding.to_dict() for finding in self.findings],
        }


@dataclass(frozen=True)
class SourceExposure:
    run_id: str
    source_type: str
    source_id: str
    disclosure_tier: str
    fields_exposed: tuple[str, ...]
    chars_exposed: int
    subject: str | None = None
    redacted_secret_count: int = 0
    reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "subject": self.subject,
            "disclosure_tier": self.disclosure_tier,
            "fields_exposed": list(self.fields_exposed),
            "chars_exposed": self.chars_exposed,
            "redacted_secret_count": self.redacted_secret_count,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ExposureSummary:
    total_sources: int
    total_chars_exposed: int
    total_redacted_secrets: int
    by_source_type: dict[str, int] = field(default_factory=dict)
    by_disclosure_tier: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "total_sources": self.total_sources,
            "total_chars_exposed": self.total_chars_exposed,
            "total_redacted_secrets": self.total_redacted_secrets,
            "by_source_type": self.by_source_type,
            "by_disclosure_tier": self.by_disclosure_tier,
        }


@dataclass(frozen=True)
class PrivacyDecision:
    allowed: bool
    reason: str
    source_id: str | None = None
    subject: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "source_id": self.source_id,
            "subject": self.subject,
        }


SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openai_api_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b")),
    ("aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
    (
        "jwt",
        re.compile(
            r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"
        ),
    ),
    (
        "assigned_secret",
        re.compile(
            r"(?i)\b(password|passwd|pwd|api[_-]?key|secret|token)\b"
            r"\s*[:=]\s*([^\s'\"`]{6,})"
        ),
    ),
)


def strip_tracking_params(url: str, *, extra_tracking_params: Iterable[str] = ()) -> str:
    """Remove common ad/email/social tracking parameters without changing useful query params."""

    parts = urlsplit(url)
    if not parts.query:
        return url

    extra_names = {name.lower() for name in extra_tracking_params}
    kept_pairs = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not is_tracking_param(key, extra_tracking_params=extra_names)
    ]
    new_query = urlencode(kept_pairs, doseq=True)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))


def is_tracking_param(name: str, *, extra_tracking_params: Iterable[str] = ()) -> bool:
    normalized = name.lower()
    extra_names = {item.lower() for item in extra_tracking_params}
    return (
        normalized in TRACKING_PARAM_NAMES
        or normalized in extra_names
        or any(normalized.startswith(prefix) for prefix in TRACKING_PARAM_PREFIXES)
    )


def detect_obvious_secrets(text: str) -> tuple[SecretFinding, ...]:
    findings: list[SecretFinding] = []
    occupied: list[range] = []

    for kind, pattern in SECRET_PATTERNS:
        for match in pattern.finditer(text):
            start, end = _secret_span(kind, match)
            span = range(start, end)
            if any(_ranges_overlap(span, existing) for existing in occupied):
                continue
            occupied.append(span)
            findings.append(
                SecretFinding(
                    kind=kind,
                    start=start,
                    end=end,
                    fingerprint=_fingerprint(text[start:end]),
                )
            )

    return tuple(sorted(findings, key=lambda finding: finding.start))


def redact_obvious_secrets(text: str) -> RedactionResult:
    findings = detect_obvious_secrets(text)
    redacted = text
    for finding in reversed(findings):
        replacement = f"[REDACTED:{finding.kind}:{finding.fingerprint}]"
        redacted = redacted[: finding.start] + replacement + redacted[finding.end :]
    return RedactionResult(text=redacted, findings=findings)


def summarize_source_exposure(exposures: Iterable[SourceExposure]) -> ExposureSummary:
    exposure_list = list(exposures)
    by_source_type: dict[str, int] = {}
    by_disclosure_tier: dict[str, int] = {}

    for exposure in exposure_list:
        by_source_type[exposure.source_type] = by_source_type.get(exposure.source_type, 0) + 1
        by_disclosure_tier[exposure.disclosure_tier] = by_disclosure_tier.get(exposure.disclosure_tier, 0) + 1

    return ExposureSummary(
        total_sources=len(exposure_list),
        total_chars_exposed=sum(exposure.chars_exposed for exposure in exposure_list),
        total_redacted_secrets=sum(exposure.redacted_secret_count for exposure in exposure_list),
        by_source_type=by_source_type,
        by_disclosure_tier=by_disclosure_tier,
    )


def should_expose_source(
    *,
    source_id: str,
    subject: str | None = None,
    tombstoned_source_ids: Iterable[str] = (),
    blocked_source_ids: Iterable[str] = (),
    blocked_subjects: Iterable[str] = (),
) -> PrivacyDecision:
    tombstones = set(tombstoned_source_ids)
    blocked_sources = set(blocked_source_ids)
    blocked_subject_set = {_normalize_subject(item) for item in blocked_subjects}
    normalized_subject = _normalize_subject(subject)

    if source_id in tombstones:
        return PrivacyDecision(False, "source_tombstoned", source_id=source_id, subject=subject)
    if source_id in blocked_sources:
        return PrivacyDecision(False, "source_blocked", source_id=source_id, subject=subject)
    if normalized_subject and normalized_subject in blocked_subject_set:
        return PrivacyDecision(False, "subject_blocked", source_id=source_id, subject=subject)
    return PrivacyDecision(True, "allowed", source_id=source_id, subject=subject)


def filter_allowed_sources(
    source_ids: Iterable[str],
    *,
    subjects_by_source_id: dict[str, str | None] | None = None,
    tombstoned_source_ids: Iterable[str] = (),
    blocked_source_ids: Iterable[str] = (),
    blocked_subjects: Iterable[str] = (),
) -> tuple[list[str], list[PrivacyDecision]]:
    allowed: list[str] = []
    denied: list[PrivacyDecision] = []
    subjects = subjects_by_source_id or {}

    for source_id in source_ids:
        decision = should_expose_source(
            source_id=source_id,
            subject=subjects.get(source_id),
            tombstoned_source_ids=tombstoned_source_ids,
            blocked_source_ids=blocked_source_ids,
            blocked_subjects=blocked_subjects,
        )
        if decision.allowed:
            allowed.append(source_id)
        else:
            denied.append(decision)

    return allowed, denied


def _secret_span(kind: str, match: re.Match[str]) -> tuple[int, int]:
    if kind == "assigned_secret" and match.lastindex and match.lastindex >= 2:
        return match.start(2), match.end(2)
    return match.start(), match.end()


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _ranges_overlap(left: range, right: range) -> bool:
    return left.start < right.stop and right.start < left.stop


def _normalize_subject(subject: str | None) -> str | None:
    if subject is None:
        return None
    normalized = " ".join(subject.lower().split())
    return normalized or None
