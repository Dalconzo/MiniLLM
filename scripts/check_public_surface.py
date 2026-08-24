#!/usr/bin/env python3
"""Fail when the public tree contains known private-trace indicators."""

from __future__ import annotations

from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
SELF = Path(__file__).resolve()

FORBIDDEN_PATHS = (
    re.compile(r"^docs/browser_.+_acceptance\.md$"),
    re.compile(r"^docs/memory_retrieval_integration_plan\.md$"),
    re.compile(r"^test-results/"),
)

FORBIDDEN_CONTENT = (
    ("personal absolute path", re.compile(r"/(?:Users|home)/[A-Za-z0-9._-]+/")),
    ("private IPv4 address", re.compile(r"\b(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})\b")),
    ("internal tracker identifier", re.compile(r"\blagent-\d+\b", re.IGNORECASE)),
    ("corpus-derived candidate identifier", re.compile(r"\bcand_[0-9a-f]{12,}\b", re.IGNORECASE)),
    ("browser result path", re.compile(r"test-results/browser", re.IGNORECASE)),
    ("prior private browser run", re.compile(r"previous browser run", re.IGNORECASE)),
    ("private corpus status", re.compile(r"corpus is stale by", re.IGNORECASE)),
    ("removed personal location fixture", re.compile(r"Santa Monica", re.IGNORECASE)),
    ("removed named-person fixture", re.compile(r"\b(?:Cristian|Angela)\b")),
)


def _text_files() -> list[Path]:
    files: list[Path] = []
    for path in ROOT.rglob("*"):
        excluded_parts = {".git", ".pytest_cache", ".venv", "__pycache__", ".mypy_cache", ".ruff_cache"}
        if not path.is_file() or path == SELF or excluded_parts.intersection(path.parts):
            continue
        files.append(path)
    return sorted(files)


def main() -> int:
    findings: list[str] = []
    for path in _text_files():
        relative = path.relative_to(ROOT).as_posix()
        if any(pattern.search(relative) for pattern in FORBIDDEN_PATHS):
            findings.append(f"forbidden public path: {relative}")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            for label, pattern in FORBIDDEN_CONTENT:
                if pattern.search(line):
                    findings.append(f"{relative}:{line_number}: {label}")

    if findings:
        print("Public-surface privacy check failed:", file=sys.stderr)
        for finding in findings:
            print(f"- {finding}", file=sys.stderr)
        return 1

    print("Public-surface privacy check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
