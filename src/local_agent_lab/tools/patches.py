from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from difflib import unified_diff
from pathlib import Path


@dataclass(frozen=True)
class PatchFile:
    relative_path: str
    content: str


def build_unified_patch(repo: Path, files: list[PatchFile]) -> str:
    hunks: list[str] = []
    for file in files:
        target = repo / file.relative_path
        original = target.read_text(encoding="utf-8") if target.exists() else ""
        diff_lines = list(
            unified_diff(
                original.splitlines(keepends=True),
                file.content.splitlines(keepends=True),
                fromfile=f"a/{file.relative_path}",
                tofile=f"b/{file.relative_path}",
            )
        )
        if diff_lines:
            hunks.extend(diff_lines)
            if not diff_lines[-1].endswith("\n"):
                hunks.append("\n")
    return "".join(hunks)


def apply_files(repo: Path, files: list[PatchFile]) -> list[str]:
    written: list[str] = []
    for file in files:
        target = repo / file.relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(file.content, encoding="utf-8")
        written.append(file.relative_path)
    return written


def patch_filename(command: str, run_id: str) -> str:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_command = command.replace("-", "_")
    return f"{timestamp}_{safe_command}_{run_id}.patch"
