from __future__ import annotations

import subprocess
from pathlib import Path


def repo_root(path: str | Path) -> Path:
    candidate = Path(path).resolve()
    if candidate.is_file():
        candidate = candidate.parent
    for current in [candidate, *candidate.parents]:
        if (current / ".git").exists():
            return current
    raise FileNotFoundError(f"no git repository found for {path}")


def git_diff(path: str | Path, *, revision: str | None = None) -> str:
    root = repo_root(path)
    command = ["git", "-C", str(root), "diff", "--no-ext-diff"]
    if revision:
        command.append(revision)
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "git diff failed"
        raise RuntimeError(message)
    return result.stdout


def changed_files_from_diff(diff_text: str) -> list[str]:
    files: list[str] = []
    for line in diff_text.splitlines():
        if not line.startswith("+++ b/"):
            continue
        relative_path = line[6:].strip()
        if relative_path == "/dev/null" or relative_path in files:
            continue
        files.append(relative_path)
    return files
