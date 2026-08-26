#!/usr/bin/env python3
"""Fail closed when a generated public tree or evidence bundle leaks private material."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, Sequence


MAX_FILE_BYTES = 16 * 1024 * 1024
PATTERNS = (
    ("local_user_path", re.compile(rb"(?:^|[^A-Za-z0-9])(?:/Users|/home)/[A-Za-z0-9._-]+(?:/|\b)")),
    ("windows_user_path", re.compile(rb"\b[A-Za-z]:\\Users\\[A-Za-z0-9._-]+\\", re.IGNORECASE)),
    ("machine_temporary_path", re.compile(rb"(?:^|[^A-Za-z0-9])/private/" rb"tmp(?:/|\b)")),
    ("internal_tracker_id", re.compile(rb"\blagent-[0-9]+\b", re.IGNORECASE)),
    ("private_run_id", re.compile(rb"\b20\d{6}T\d{6}Z-[0-9a-f]{8,}\b", re.IGNORECASE)),
    (
        "private_acceptance_id",
        re.compile(rb"\b(?:cand|chk|ctx|afbk)_[0-9a-f]{8,}\b", re.IGNORECASE),
    ),
    (
        "private_acceptance_path",
        re.compile(
            rb"(?:projects:)?_acceptance" rb"_tests/" rb"results|data/logs/home_" rb"mcp/" rb"20\d{6}T\d{6}Z-",
            re.IGNORECASE,
        ),
    ),
    ("private_key", re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("api_token", re.compile(rb"\b(?:sk-(?:proj-)?|gh[opusr]_)[A-Za-z0-9_-]{16,}\b")),
    ("aws_access_key", re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("slack_token", re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{16,}\b")),
    (
        "authorization_credential",
        re.compile(rb"(?:authorization\s*[:=]\s*bearer|bearer\s+)[A-Za-z0-9._~+/-]{16,}", re.IGNORECASE),
    ),
    ("url_credential", re.compile(rb"[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@", re.IGNORECASE)),
)


class PublicArtifactAuditError(RuntimeError):
    """A public artifact violated the portable privacy contract."""


@dataclass(frozen=True)
class AuditInput:
    label: str
    data: bytes


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_regular(path: Path) -> bytes:
    absolute = path.absolute()
    try:
        metadata = absolute.lstat()
        resolved = absolute.resolve(strict=True)
    except OSError as exc:
        raise PublicArtifactAuditError("audit input is unavailable") from exc
    if resolved != absolute or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise PublicArtifactAuditError("audit inputs must be non-symlinked, single-link regular files")
    if metadata.st_size > MAX_FILE_BYTES:
        raise PublicArtifactAuditError("audit input exceeds the public artifact size limit")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(absolute, flags)
    try:
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            data = handle.read(MAX_FILE_BYTES + 1)
            after = os.fstat(handle.fileno())
    finally:
        os.close(descriptor)
    if len(data) > MAX_FILE_BYTES:
        raise PublicArtifactAuditError("audit input exceeds the public artifact size limit")
    if (metadata.st_dev, metadata.st_ino, metadata.st_size) != (after.st_dev, after.st_ino, after.st_size):
        raise PublicArtifactAuditError("audit input changed while being read")
    return data


def _directory_inputs(root: Path) -> list[AuditInput]:
    try:
        absolute = root.absolute()
        metadata = absolute.lstat()
        resolved = absolute.resolve(strict=True)
    except OSError as exc:
        raise PublicArtifactAuditError("audit directory is unavailable") from exc
    if resolved != absolute or not stat.S_ISDIR(metadata.st_mode):
        raise PublicArtifactAuditError("audit directories must not be symlinks")
    inputs: list[AuditInput] = []
    for current, directory_names, file_names in os.walk(absolute, followlinks=False):
        current_path = Path(current)
        directory_names[:] = sorted(name for name in directory_names if name != ".git")
        for directory_name in directory_names:
            if (current_path / directory_name).is_symlink():
                raise PublicArtifactAuditError("public artifact directories must not contain symlinks")
        for file_name in sorted(file_names):
            path = current_path / file_name
            relative = path.relative_to(absolute).as_posix()
            inputs.append(AuditInput(f"{absolute.name}/{relative}", _read_regular(path)))
    return inputs


def _git_object_inputs(repo: Path) -> list[AuditInput]:
    if not (repo / ".git").is_dir():
        return []
    try:
        inventory = subprocess.run(
            ("git", "-C", str(repo), "cat-file", "--batch-all-objects", "--batch-check=%(objectname) %(objecttype)"),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise PublicArtifactAuditError("Git object inventory failed") from exc
    inputs: list[AuditInput] = []
    for line in inventory:
        object_id, object_type = line.split()
        if object_type not in {"blob", "commit", "tag"}:
            continue
        try:
            data = subprocess.run(
                ("git", "-C", str(repo), "cat-file", object_type, object_id),
                check=True,
                capture_output=True,
            ).stdout
        except (OSError, subprocess.CalledProcessError) as exc:
            raise PublicArtifactAuditError("Git object read failed") from exc
        inputs.append(AuditInput(f"{repo.name}/git-{object_type}", data))
    return inputs


def _load_canaries(paths: Sequence[Path]) -> tuple[bytes, ...]:
    canaries: set[bytes] = set()
    for path in paths:
        data = _read_regular(path)
        try:
            values = data.decode("utf-8", "strict").splitlines()
        except UnicodeDecodeError as exc:
            raise PublicArtifactAuditError("private-overlap canary files must be UTF-8") from exc
        for raw in values:
            value = raw.strip()
            if not value or value.startswith("#"):
                continue
            encoded = value.casefold().encode("utf-8")
            if len(encoded) < 8:
                raise PublicArtifactAuditError("private-overlap canaries must contain at least eight bytes")
            canaries.add(encoded)
    return tuple(sorted(canaries))


def audit(paths: Sequence[Path], canary_paths: Sequence[Path]) -> dict[str, object]:
    if not paths:
        raise PublicArtifactAuditError("at least one public artifact path is required")
    canaries = _load_canaries(canary_paths)
    inputs: list[AuditInput] = []
    git_objects = 0
    for path in paths:
        absolute = path.absolute()
        if absolute.is_symlink():
            raise PublicArtifactAuditError("public artifact inputs must not be symlinks")
        if absolute.is_dir():
            inputs.extend(_directory_inputs(absolute))
            objects = _git_object_inputs(absolute)
            git_objects += len(objects)
            inputs.extend(objects)
        elif absolute.is_file():
            inputs.append(AuditInput(absolute.name, _read_regular(absolute)))
        else:
            raise PublicArtifactAuditError("public artifact input is unavailable")
    for item in inputs:
        for code, pattern in PATTERNS:
            if pattern.search(item.data):
                raise PublicArtifactAuditError(f"{code} found in {item.label}")
        folded = item.data.decode("utf-8", errors="ignore").casefold().encode("utf-8")
        for canary in canaries:
            if canary in folded:
                raise PublicArtifactAuditError(f"private_corpus_overlap found in {item.label}")
    return {
        "canary_set_sha256": _sha256(b"\0".join(canaries)) if canaries else None,
        "files_and_objects_scanned": len(inputs),
        "git_objects_scanned": git_objects,
        "private_overlap_canary_count": len(canaries),
        "status": "pass",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", action="append", default=[], type=Path)
    parser.add_argument("--forbidden-literals-file", action="append", default=[], type=Path)
    return parser


def _fail(message: str) -> NoReturn:
    print(json.dumps({"error": message, "status": "error"}, sort_keys=True), file=sys.stderr)
    raise SystemExit(1)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = audit(args.path, args.forbidden_literals_file)
    except (OSError, PublicArtifactAuditError) as exc:
        _fail(str(exc))
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
