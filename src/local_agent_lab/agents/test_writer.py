from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TestPlan:
    summary: str
    target_file: str
    test_target_file: str
    tests: str
    edge_cases: list[str]
    assumptions: list[str]
    raw_response: str

    def to_dict(self) -> dict[str, object]:
        return {
            "summary": self.summary,
            "target_file": self.target_file,
            "test_target_file": self.test_target_file,
            "tests": self.tests,
            "edge_cases": self.edge_cases,
            "assumptions": self.assumptions,
            "raw_response": self.raw_response,
        }


def build_test_writer_prompt(
    *,
    repo: Path,
    target_file: str,
    file_content: str,
    retrieved_context: list[dict[str, object]],
) -> str:
    context_lines = []
    for item in retrieved_context:
        snippet = str(item.get("snippet", "")).strip()
        context_lines.append(
            f"- {item['relative_path']} [chunk {item['chunk_index']}]\n{snippet}"
        )
    context_block = "\n\n".join(context_lines) if context_lines else "- No indexed context found."
    return (
        f"Repository root: {repo}\n\n"
        f"Target file: {target_file}\n\n"
        "Current file contents:\n"
        f"{file_content.strip()}\n\n"
        "Retrieved context:\n"
        f"{context_block}\n\n"
        "Return strict JSON with this schema:\n"
        "{\n"
        '  "summary": "short summary",\n'
        '  "target_file": "relative/path.py",\n'
        '  "test_target_file": "tests/test_name.py",\n'
        '  "tests": "FULL file contents for test_target_file",\n'
        '  "edge_cases": ["item"],\n'
        '  "assumptions": ["item"]\n'
        "}\n"
        "Rules:\n"
        "- Match the existing test framework and style when possible.\n"
        "- Prefer deterministic tests.\n"
        "- Return the full test file content, not a prose patch.\n"
    )


def parse_test_writer_response(response: str) -> TestPlan:
    try:
        payload = _load_json_payload(response)
        return TestPlan(
            summary=str(payload.get("summary", "")).strip(),
            target_file=str(payload.get("target_file", "")).strip(),
            test_target_file=str(payload.get("test_target_file", "")).strip(),
            tests=str(payload.get("tests", "")),
            edge_cases=[str(item).strip() for item in payload.get("edge_cases", []) if str(item).strip()],
            assumptions=[str(item).strip() for item in payload.get("assumptions", []) if str(item).strip()],
            raw_response=response.strip(),
        )
    except ValueError:
        return _parse_markdown_plan(response)


def render_test_plan(plan: TestPlan, patch_path: Path, applied: bool) -> str:
    lines = [
        f"Summary: {plan.summary or 'No summary provided.'}",
        f"Target file: {plan.target_file}",
        f"Test file: {plan.test_target_file}",
        f"Patch file: {patch_path}",
        f"Applied: {'yes' if applied else 'no'}",
    ]
    if plan.edge_cases:
        lines.append("Edge cases:")
        lines.extend(f"- {item}" for item in plan.edge_cases)
    if plan.assumptions:
        lines.append("Assumptions:")
        lines.extend(f"- {item}" for item in plan.assumptions)
    return "\n".join(lines)


def _load_json_payload(response: str) -> dict[str, object]:
    stripped = response.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    stripped = _normalize_triple_quoted_strings(stripped)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", response, re.DOTALL)
        if not match:
            raise ValueError("model response did not contain valid JSON")
        return json.loads(_normalize_triple_quoted_strings(match.group(0)))


def _parse_markdown_plan(response: str) -> TestPlan:
    summary = _extract_labeled_value(response, "summary")
    target_file = _extract_labeled_value(response, "target file")
    test_target_file = _extract_labeled_value(response, "test file") or _extract_labeled_value(response, "test target file")
    edge_cases = _extract_bullets(response, "edge cases")
    assumptions = _extract_bullets(response, "assumptions")
    code_blocks = re.findall(r"```(?:[a-zA-Z0-9_+-]+)?\n(.*?)```", response, re.DOTALL)
    tests = code_blocks[0].strip() + "\n" if code_blocks else ""
    if not test_target_file or not tests:
        raise ValueError("model response did not contain valid JSON")
    return TestPlan(
        summary=summary,
        target_file=target_file,
        test_target_file=test_target_file,
        tests=tests,
        edge_cases=edge_cases,
        assumptions=assumptions,
        raw_response=response.strip(),
    )


def _extract_labeled_value(text: str, label: str) -> str:
    pattern = rf"(?im)^(?:##\s*)?{re.escape(label)}\s*:?\s*(.+)$"
    match = re.search(pattern, text)
    return match.group(1).strip() if match else ""


def _extract_bullets(text: str, heading: str) -> list[str]:
    pattern = rf"(?ims)^(?:##\s*)?{re.escape(heading)}\s*:?\s*(.*?)(?:\n(?:##\s|[A-Za-z][A-Za-z /_-]*:|\Z))"
    match = re.search(pattern, text)
    if not match:
        return []
    items: list[str] = []
    for line in match.group(1).splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            items.append(stripped[2:].strip())
    return items


def _normalize_triple_quoted_strings(text: str) -> str:
    pattern = re.compile(r'(:\s*)"""(.*?)"""', re.DOTALL)

    def repl(match: re.Match[str]) -> str:
        return f"{match.group(1)}{json.dumps(match.group(2))}"

    return pattern.sub(repl, text)
