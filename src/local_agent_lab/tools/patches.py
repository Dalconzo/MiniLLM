from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from difflib import unified_diff
from pathlib import Path

from ..indexing.visibility import RepositoryVisibilityPolicy, VisibilityPolicyError
from ..security.paths import (
    PathPolicyError,
    normalize_generated_destination,
    safe_read_text,
    safe_write_text,
)


@dataclass(frozen=True)
class PatchFile:
    relative_path: str
    content: str


def build_unified_patch(
    repo: Path,
    files: list[PatchFile],
    *,
    authorized_relative_paths: Collection[str] = (),
) -> str:
    hunks: list[str] = []
    policy = RepositoryVisibilityPolicy(repo)
    for file in files:
        relative_path = normalize_generated_destination(
            file.relative_path,
            authorized_relative_paths=authorized_relative_paths,
        )
        _require_visible_destination(policy, relative_path)
        try:
            original = safe_read_text(repo, relative_path)
        except FileNotFoundError:
            original = ""
        except PathPolicyError:
            raise
        diff_lines = list(
            unified_diff(
                original.splitlines(keepends=True),
                file.content.splitlines(keepends=True),
                fromfile=f"a/{relative_path}",
                tofile=f"b/{relative_path}",
            )
        )
        if diff_lines:
            hunks.extend(diff_lines)
            if not diff_lines[-1].endswith("\n"):
                hunks.append("\n")
    return "".join(hunks)


def apply_files(
    repo: Path,
    files: list[PatchFile],
    *,
    authorized_relative_paths: Collection[str] = (),
) -> list[str]:
    written: list[str] = []
    policy = RepositoryVisibilityPolicy(repo)
    for file in files:
        relative_path = normalize_generated_destination(
            file.relative_path,
            authorized_relative_paths=authorized_relative_paths,
        )
        _require_visible_destination(policy, relative_path)
        safe_write_text(repo, relative_path, file.content, create_parents=False)
        written.append(relative_path)
    return written


def patch_filename(command: str, run_id: str) -> str:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_command = command.replace("-", "_")
    return f"{timestamp}_{safe_command}_{run_id}.patch"


def _require_visible_destination(policy: RepositoryVisibilityPolicy, relative_path: str) -> None:
    decision = policy.evaluate_logical_path(relative_path)
    if not decision.visible:
        raise VisibilityPolicyError(f"refusing generated destination: {decision.reason}")
