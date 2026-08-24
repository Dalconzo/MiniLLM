from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path


SECTION_CATEGORY = {
    "likely bugs": ("likely_bug", "high"),
    "edge cases": ("edge_case", "medium"),
    "missing tests": ("missing_test", "medium"),
    "simplifications": ("simplification", "low"),
    "questions / uncertainty": ("uncertainty", "info"),
}


@dataclass(frozen=True)
class ReviewFinding:
    category: str
    severity: str
    title: str
    details: str
    file: str | None = None
    line: int | None = None
    suggested_fix: str | None = None
    test_idea: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "category": self.category,
            "severity": self.severity,
            "title": self.title,
            "details": self.details,
            "file": self.file,
            "line": self.line,
            "suggested_fix": self.suggested_fix,
            "test_idea": self.test_idea,
        }


@dataclass(frozen=True)
class ReviewResult:
    summary: str
    findings: list[ReviewFinding]
    raw_response: str

    def to_dict(self) -> dict[str, object]:
        return {
            "summary": self.summary,
            "findings": [finding.to_dict() for finding in self.findings],
            "raw_response": self.raw_response,
        }


def build_file_review_prompt(
    *,
    repo: Path,
    relative_path: str,
    file_content: str,
    retrieved_context: list[dict[str, object]],
) -> str:
    parts = [
        "Review the target file for correctness risks, regressions, missing tests, and meaningful simplifications.",
        "Be concrete. Skip generic style commentary.",
        "Return markdown using these sections exactly: Summary, Likely bugs, Edge cases, Missing tests, Simplifications, Questions / uncertainty.",
        "For each bullet, start with `path:line - ...` when you can anchor it.",
        f"Repository root: {repo}",
        f"Target file: {relative_path}",
        "",
        "Target file with line numbers:",
        _numbered_text(file_content),
    ]
    if retrieved_context:
        parts.extend(["", "Retrieved context:"])
        for hit in retrieved_context:
            parts.append(
                f"- {hit['relative_path']} [chunk {hit['chunk_index']}]: {hit['snippet']}"
            )
    return "\n".join(parts).strip()


def build_diff_review_prompt(
    *,
    repo: Path,
    diff_text: str,
    retrieved_context: list[dict[str, object]],
) -> str:
    parts = [
        "Review this git diff for correctness risks, likely regressions, missing tests, and risky assumptions.",
        "Prioritize real bugs over style.",
        "Return markdown using these sections exactly: Summary, Likely bugs, Edge cases, Missing tests, Simplifications, Questions / uncertainty.",
        "For each bullet, start with `path:line - ...` when you can anchor it.",
        f"Repository root: {repo}",
        "",
        "Unified diff:",
        diff_text.strip(),
    ]
    if retrieved_context:
        parts.extend(["", "Retrieved context:"])
        for hit in retrieved_context:
            parts.append(
                f"- {hit['relative_path']} [chunk {hit['chunk_index']}]: {hit['snippet']}"
            )
    return "\n".join(parts).strip()


def parse_review_response(response: str) -> ReviewResult:
    parsed = _parse_json_payload(response)
    if parsed is not None:
        return parsed
    return _parse_markdown_payload(response)


def render_review_output(result: ReviewResult) -> str:
    lines: list[str] = []
    if result.findings:
        for finding in result.findings:
            location = finding.file or "unknown"
            if finding.line is not None:
                location = f"{location}:{finding.line}"
            lines.append(f"[{finding.severity}] {location} {finding.title}")
            lines.append(finding.details)
            if finding.suggested_fix:
                lines.append(f"Suggested fix: {finding.suggested_fix}")
            if finding.test_idea:
                lines.append(f"Suggested test: {finding.test_idea}")
            lines.append("")
    else:
        lines.append("No clear review findings.")
        lines.append("")
    if result.summary:
        lines.append("Summary:")
        lines.append(result.summary)
    return "\n".join(lines).strip()


def _numbered_text(text: str) -> str:
    return "\n".join(f"{index:4d}: {line}" for index, line in enumerate(text.splitlines(), start=1))


def _parse_json_payload(response: str) -> ReviewResult | None:
    candidate = _extract_json_block(response)
    if candidate is None:
        return None
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    findings = []
    for item in payload.get("findings", []):
        if not isinstance(item, dict):
            continue
        findings.append(
            ReviewFinding(
                category=str(item.get("category", "review")),
                severity=str(item.get("severity", "medium")),
                title=str(item.get("title", "Review finding")),
                details=str(item.get("details", "")).strip() or str(item.get("title", "Review finding")),
                file=_as_optional_str(item.get("file")),
                line=_as_optional_int(item.get("line")),
                suggested_fix=_as_optional_str(item.get("suggested_fix")),
                test_idea=_as_optional_str(item.get("test_idea")),
            )
        )
    return ReviewResult(summary=str(payload.get("summary", "")).strip(), findings=findings, raw_response=response.strip())


def _extract_json_block(response: str) -> str | None:
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", response, re.DOTALL)
    if fence_match:
        return fence_match.group(1)
    stripped = response.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return stripped
    return None


def _parse_markdown_payload(response: str) -> ReviewResult:
    summary = ""
    findings: list[ReviewFinding] = []
    current_heading: str | None = None
    buffer: list[str] = []

    def flush_section() -> None:
        nonlocal summary
        if current_heading is None:
            return
        normalized = current_heading.strip().lower()
        content = "\n".join(buffer).strip()
        if not content:
            return
        if normalized == "summary":
            summary = content
            return
        category_info = SECTION_CATEGORY.get(normalized)
        if category_info is None:
            return
        category, severity = category_info
        for item in _split_bullets(content):
            findings.append(_finding_from_bullet(item, category=category, severity=severity))

    for raw_line in response.splitlines():
        line = raw_line.rstrip()
        if line.startswith("## "):
            flush_section()
            current_heading = line[3:].strip()
            buffer = []
        else:
            buffer.append(line)
    flush_section()
    return ReviewResult(summary=summary, findings=findings, raw_response=response.strip())


def _split_bullets(content: str) -> list[str]:
    items: list[str] = []
    current: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if re.match(r"^([-*]|\d+\.)\s+", stripped):
            if current:
                items.append(" ".join(current).strip())
            current = [re.sub(r"^([-*]|\d+\.)\s+", "", stripped)]
        else:
            current.append(stripped)
    if current:
        items.append(" ".join(current).strip())
    return items


def _finding_from_bullet(item: str, *, category: str, severity: str) -> ReviewFinding:
    match = re.match(r"(?P<file>[^:\s][^:]*?):(?P<line>\d+)\s*-\s*(?P<body>.+)", item)
    if match:
        file = match.group("file").strip()
        line = int(match.group("line"))
        body = match.group("body").strip()
    else:
        file = None
        line = None
        body = item.strip()
    title, details, suggested_fix, test_idea = _split_body_fields(body)
    return ReviewFinding(
        category=category,
        severity=severity,
        title=title,
        details=details,
        file=file,
        line=line,
        suggested_fix=suggested_fix,
        test_idea=test_idea,
    )


def _split_body_fields(body: str) -> tuple[str, str, str | None, str | None]:
    segments = [segment.strip() for segment in body.split(" | ") if segment.strip()]
    if not segments:
        return "Review finding", body, None, None
    headline = segments[0]
    title = headline.split(".")[0].strip() or "Review finding"
    suggested_fix = None
    test_idea = None
    for segment in segments[1:]:
        lowered = segment.lower()
        if lowered.startswith("fix:"):
            suggested_fix = segment[4:].strip()
        elif lowered.startswith("test:"):
            test_idea = segment[5:].strip()
    return title, body, suggested_fix, test_idea


def _as_optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_optional_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
