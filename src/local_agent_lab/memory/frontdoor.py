from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any


SAFE_MEMORY_ACTIONS = (
    "memory-status",
    "memory-analyze",
    "memory-patterns",
    "memory-runs",
    "memory-subjects",
    "memory-review-subjects",
    "memory-search",
    "memory-candidates",
    "memory-list",
    "memory-open-loops",
    "memory-audit",
    "memory-trace",
)


@dataclass(frozen=True)
class MemoryFrontdoorPlan:
    summary: str
    action: str
    arguments: dict[str, Any]
    rationale: str
    confidence: float
    needs_confirmation: bool
    result_style: str
    raw_response: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "action": self.action,
            "arguments": self.arguments,
            "rationale": self.rationale,
            "confidence": self.confidence,
            "needs_confirmation": self.needs_confirmation,
            "result_style": self.result_style,
            "raw_response": self.raw_response,
        }


def build_memory_frontdoor_prompt(*, user_request: str, corpus_state: dict[str, Any]) -> str:
    return (
        "You are a constrained router for a local memory system.\n"
        "Choose exactly one allowed action and propose the smallest safe arguments.\n"
        "Prefer read-only actions unless the user explicitly requests a write.\n"
        "Return strict JSON only with this schema:\n"
        "{\n"
        '  "summary": "short summary of intent",\n'
        '  "action": "one of the allowed actions",\n'
        '  "arguments": {"key": "value"},\n'
        '  "rationale": "why this action fits",\n'
        '  "confidence": 0.0,\n'
        '  "needs_confirmation": false,\n'
        '  "result_style": "summary|list|json"\n'
        "}\n"
        f"Allowed actions: {', '.join(SAFE_MEMORY_ACTIONS)}\n\n"
        f"User request:\n{user_request.strip()}\n\n"
        "Current corpus state:\n"
        f"{json.dumps(corpus_state, indent=2, sort_keys=True)}\n\n"
        "Rules:\n"
        "- Only choose from the allowed actions.\n"
        "- If the user asks for a broad understanding of the dataset, prefer memory-analyze or memory-subjects.\n"
        "- If the user asks for natural category discovery, recipe grouping, or project cataloging, prefer memory-patterns.\n"
        "- If the user asks for a specific subject, prefer memory-review-subjects.\n"
        "- If the user asks to search for terms or topics, prefer memory-search.\n"
        "- If the user asks for candidate extraction or browsing, prefer memory-candidates or memory-review-subjects.\n"
        "- Do not invent new commands.\n"
    )


def parse_memory_frontdoor_response(response: str) -> MemoryFrontdoorPlan:
    parsed = _parse_json_payload(response)
    if parsed is not None:
        return parsed
    return _parse_markdown_payload(response)


def render_memory_frontdoor_plan(plan: MemoryFrontdoorPlan) -> str:
    lines = [
        f"Summary: {plan.summary or 'No summary provided.'}",
        f"Action: {plan.action or 'Unknown'}",
        f"Confidence: {plan.confidence:.2f}",
        f"Needs confirmation: {'yes' if plan.needs_confirmation else 'no'}",
        f"Result style: {plan.result_style or 'summary'}",
        f"Rationale: {plan.rationale or 'No rationale provided.'}",
    ]
    if plan.arguments:
        lines.append("Arguments:")
        for key, value in sorted(plan.arguments.items()):
            lines.append(f"- {key}: {value}")
    return "\n".join(lines)


def _parse_json_payload(response: str) -> MemoryFrontdoorPlan | None:
    candidate = _extract_json_block(response)
    if candidate is None:
        return None
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return _plan_from_payload(payload, response)


def _parse_markdown_payload(response: str) -> MemoryFrontdoorPlan:
    action = _extract_labeled_value(response, "Action")
    summary = _extract_labeled_value(response, "Summary")
    rationale = _extract_labeled_value(response, "Rationale")
    confidence_text = _extract_labeled_value(response, "Confidence")
    result_style = _extract_labeled_value(response, "Result style") or "summary"
    needs_confirmation = _extract_labeled_value(response, "Needs confirmation").lower() in {"yes", "true", "1"}
    arguments = _extract_json_object(response, "Arguments") or {}
    confidence = _coerce_float(confidence_text, default=0.0)
    return MemoryFrontdoorPlan(
        summary=summary,
        action=action,
        arguments=arguments,
        rationale=rationale,
        confidence=confidence,
        needs_confirmation=needs_confirmation,
        result_style=result_style,
        raw_response=response.strip(),
    )


def _plan_from_payload(payload: dict[str, Any], response: str) -> MemoryFrontdoorPlan:
    arguments = payload.get("arguments") or {}
    if not isinstance(arguments, dict):
        arguments = {}
    action = str(payload.get("action", "")).strip()
    if action not in SAFE_MEMORY_ACTIONS:
        raise ValueError(f"unsupported action from model: {action}")
    confidence = _coerce_float(payload.get("confidence"), default=0.0)
    result_style = str(payload.get("result_style", "summary")).strip() or "summary"
    return MemoryFrontdoorPlan(
        summary=str(payload.get("summary", "")).strip(),
        action=action,
        arguments={str(key): value for key, value in arguments.items()},
        rationale=str(payload.get("rationale", "")).strip(),
        confidence=confidence,
        needs_confirmation=bool(payload.get("needs_confirmation", False)),
        result_style=result_style,
        raw_response=response.strip(),
    )


def _extract_json_block(response: str) -> str | None:
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", response, re.DOTALL)
    if fence_match:
        return fence_match.group(1)
    stripped = response.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return stripped
    return None


def _extract_labeled_value(text: str, label: str) -> str:
    pattern = rf"(?im)^(?:##\s*)?{re.escape(label)}\s*:?\s*(.+)$"
    match = re.search(pattern, text)
    return match.group(1).strip() if match else ""


def _extract_json_object(text: str, label: str) -> dict[str, Any] | None:
    pattern = rf"(?ims)^(?:##\s*)?{re.escape(label)}\s*:?\s*(\{{.*?\}})"
    match = re.search(pattern, text)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _coerce_float(value: Any, *, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
