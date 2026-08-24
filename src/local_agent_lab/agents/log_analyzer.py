from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TracebackFrame:
    file: str
    line: int
    function: str
    source: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "file": self.file,
            "line": self.line,
            "function": self.function,
            "source": self.source,
        }


@dataclass(frozen=True)
class ParsedLog:
    frames: list[TracebackFrame]
    error_type: str | None
    error_message: str | None
    raw_text: str

    def to_dict(self) -> dict[str, object]:
        return {
            "frames": [frame.to_dict() for frame in self.frames],
            "error_type": self.error_type,
            "error_message": self.error_message,
            "raw_text": self.raw_text,
        }


@dataclass(frozen=True)
class LogAnalysis:
    summary: str
    likely_failure_point: str
    probable_cause: str
    next_steps: list[str]
    raw_response: str

    def to_dict(self) -> dict[str, object]:
        return {
            "summary": self.summary,
            "likely_failure_point": self.likely_failure_point,
            "probable_cause": self.probable_cause,
            "next_steps": self.next_steps,
            "raw_response": self.raw_response,
        }


TRACEBACK_FRAME_RE = re.compile(
    r'^\s*File "(?P<file>.+?)", line (?P<line>\d+), in (?P<function>[^\n]+)\s*$'
)
EXCEPTION_LINE_RE = re.compile(r"^(?P<error>[A-Za-z_][A-Za-z0-9_.]*?(?:Error|Exception|Exit)):\s*(?P<message>.+)$")


def parse_log_text(log_text: str) -> ParsedLog:
    lines = log_text.splitlines()
    frames: list[TracebackFrame] = []
    error_type: str | None = None
    error_message: str | None = None

    for index, line in enumerate(lines):
        match = TRACEBACK_FRAME_RE.match(line)
        if not match:
            continue
        source: str | None = None
        if index + 1 < len(lines):
            source_candidate = lines[index + 1].rstrip()
            if source_candidate.strip() and not TRACEBACK_FRAME_RE.match(source_candidate):
                source = source_candidate.strip()
        frames.append(
            TracebackFrame(
                file=match.group("file").strip(),
                line=int(match.group("line")),
                function=match.group("function").strip(),
                source=source,
            )
        )

    for line in reversed(lines):
        stripped = line.strip()
        if not stripped:
            continue
        match = EXCEPTION_LINE_RE.match(stripped)
        if match:
            error_type = match.group("error").strip()
            error_message = match.group("message").strip()
            break

    return ParsedLog(frames=frames, error_type=error_type, error_message=error_message, raw_text=log_text.strip())


def build_log_analysis_prompt(
    *,
    log_file: Path,
    log_text: str,
    parsed_log: ParsedLog,
    retrieved_context: list[dict[str, object]],
    repo: Path | None = None,
) -> str:
    parts = [
        "Analyze this log output conservatively.",
        "Focus on the likely failure point, the probable cause, and the next checks to run.",
        "Do not invent code or files that are not in the log or retrieved context.",
        "Return strict JSON with this schema:",
        "{",
        '  "summary": "short summary",',
        '  "likely_failure_point": "file:line or subsystem",',
        '  "probable_cause": "short explanation",',
        '  "next_steps": ["step 1", "step 2"]',
        "}",
        f"Log file: {log_file}",
    ]
    if repo is not None:
        parts.append(f"Repository root: {repo}")
    if parsed_log.error_type or parsed_log.error_message:
        parts.extend(
            [
                "",
                "Parsed exception:",
                f"- type: {parsed_log.error_type or 'unknown'}",
                f"- message: {parsed_log.error_message or 'unknown'}",
            ]
        )
    if parsed_log.frames:
        parts.extend(["", "Parsed traceback frames:"])
        for frame in parsed_log.frames:
            source_suffix = f" | source: {frame.source}" if frame.source else ""
            parts.append(f"- {frame.file}:{frame.line} in {frame.function}{source_suffix}")
    parts.extend(["", "Log contents:", log_text.strip()])
    if retrieved_context:
        parts.extend(["", "Retrieved repo context:"])
        for hit in retrieved_context:
            parts.append(f"- {hit['relative_path']} [chunk {hit['chunk_index']}]: {hit['snippet']}")
    return "\n".join(parts).strip()


def parse_log_analysis_response(response: str) -> LogAnalysis:
    parsed = _parse_json_payload(response)
    if parsed is not None:
        return parsed
    return _parse_markdown_payload(response)


def render_log_analysis(analysis: LogAnalysis) -> str:
    lines = [f"Failure point: {analysis.likely_failure_point or 'Unknown'}"]
    lines.append(f"Probable cause: {analysis.probable_cause or 'Unknown'}")
    lines.append("")
    if analysis.next_steps:
        lines.append("Next steps:")
        lines.extend(f"- {step}" for step in analysis.next_steps)
        lines.append("")
    if analysis.summary:
        lines.append("Summary:")
        lines.append(analysis.summary)
    return "\n".join(lines).strip()


def _parse_json_payload(response: str) -> LogAnalysis | None:
    candidate = _extract_json_block(response)
    if candidate is None:
        return None
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    next_steps = [str(item).strip() for item in payload.get("next_steps", []) if str(item).strip()]
    return LogAnalysis(
        summary=str(payload.get("summary", "")).strip(),
        likely_failure_point=str(payload.get("likely_failure_point", "")).strip(),
        probable_cause=str(payload.get("probable_cause", "")).strip(),
        next_steps=next_steps,
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


def _parse_markdown_payload(response: str) -> LogAnalysis:
    summary = _extract_heading_text(response, "Summary")
    likely_failure_point = _extract_heading_text(response, "Likely failure point") or _extract_labeled_value(
        response, "Likely failure point"
    )
    probable_cause = _extract_heading_text(response, "Probable cause") or _extract_labeled_value(
        response, "Probable cause"
    )
    next_steps = _extract_bullets(response, "Next steps")
    if not likely_failure_point:
        likely_failure_point = _extract_labeled_value(response, "Failure point")
    return LogAnalysis(
        summary=summary,
        likely_failure_point=likely_failure_point,
        probable_cause=probable_cause,
        next_steps=next_steps,
        raw_response=response.strip(),
    )


def _extract_heading_text(text: str, heading: str) -> str:
    pattern = rf"(?ims)^##\s*{re.escape(heading)}\s*\n(.*?)(?:\n##\s|\Z)"
    match = re.search(pattern, text)
    if not match:
        return ""
    return match.group(1).strip()


def _extract_labeled_value(text: str, label: str) -> str:
    pattern = rf"(?im)^(?:##\s*)?{re.escape(label)}\s*:?\s*(.+)$"
    match = re.search(pattern, text)
    return match.group(1).strip() if match else ""


def _extract_bullets(text: str, heading: str) -> list[str]:
    block = _extract_heading_text(text, heading)
    if not block:
        return []
    items: list[str] = []
    for line in block.splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            items.append(stripped[2:].strip())
    return items
