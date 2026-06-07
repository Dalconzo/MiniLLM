from __future__ import annotations


READ_ONLY_PREFIXES = (
    "cat ",
    "sed ",
    "rg ",
    "fd ",
    "ls",
    "find ",
    "git status",
    "git diff",
    "git show",
    "python -m pytest",
)


def is_read_only_command(command: str) -> bool:
    normalized = command.strip()
    return normalized.startswith(READ_ONLY_PREFIXES)
