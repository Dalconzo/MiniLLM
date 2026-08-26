#!/usr/bin/env python3
"""Create and audit a privacy-clean, single-commit public release candidate."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.parse
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import NoReturn, Sequence


SCHEMA_VERSION = 1
TOOL_VERSION = "1.0"
APPROVED_TOP_LEVEL_FILES = frozenset(
    {
        ".gitignore",
        "CONTRIBUTING.md",
        "LICENSE",
        "LICENSE.txt",
        "NOTICE",
        "README.md",
        "SECURITY.md",
        "justfile",
        "pyproject.toml",
        "uv.lock",
    }
)
APPROVED_TOP_LEVEL_DIRS = frozenset({".github", "config", "docs", "examples", "scripts", "src", "tests"})
EXCLUDED_TOP_LEVEL_DIRS = frozenset(
    {
        ".agents",
        ".beads",
        ".codex",
        "build",
        "data",
        "dist",
        "exports",
        "runs",
        "test-results",
    }
)
EXCLUDED_PARTS = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "node_modules",
        "venv",
    }
)
EXCLUDED_PUBLIC_PATHS = frozenset(
    {
        "docs/baking_observation_workstations.md",
        "docs/local_staging_memory.md",
        "docs/mac_mini_private_memory_smoke_checklist.md",
        "docs/memory_retrieval_integration_plan.md",
        "examples/human_memory_cognition_extensions_spec.md",
    }
)
EXCLUDED_PUBLIC_PREFIXES = (
    "docs/browser_",
    "docs/evaluation/evidence/",
)
SECRET_BASENAMES = frozenset(
    {
        ".dockercfg",
        ".env",
        ".envrc",
        ".netrc",
        ".npmrc",
        ".pypirc",
        "access-token.txt",
        "credentials",
        "credentials.ini",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
        "private_key.txt",
        "secrets",
        "secrets.txt",
    }
)
SECRET_GLOBS = (
    ".env.*",
    "*.jks",
    "*.kdbx",
    "*.key",
    "*.keystore",
    "*.ovpn",
    "*.p12",
    "*.p8",
    "*.pem",
    "*.pfx",
    "*.ppk",
    "access-token.*",
    "access_token.*",
    "auth-token.*",
    "auth_token.*",
    "*credential*.json",
    "*secret*.json",
    "*service-account*.json",
    "*token*.json",
    "id_dsa.*",
    "id_ecdsa.*",
    "id_ed25519.*",
    "id_rsa.*",
    "private-key.*",
    "private_key.*",
)
SECRET_PATH_GLOBS = (
    ".aws/*",
    ".config/gcloud/*",
    ".kube/*",
    ".ssh/*",
    "config/*credential*",
    "config/*secret*",
    "config/*token*",
)
ROOT_EXPORT_RE = re.compile(r"^[0-9a-f]{32,}-20\d\d-\d\d-\d\d-")
RUN_ID_RE = re.compile(rb"20\d{6}T\d{6}Z-[0-9a-f]{8,}", re.IGNORECASE)
PERSONAL_PATH_RE = re.compile(rb"(?:^|[^A-Za-z0-9])(?:/Users|/home)/[A-Za-z0-9._-]+(?:/|\b)")
PRIVATE_TMP_RE = re.compile(rb"(?:^|[^A-Za-z0-9])/private/" rb"tmp(?:/|\b)")
TRACKER_ID_RE = re.compile(rb"\blagent-[0-9]+\b", re.IGNORECASE)
PRIVATE_ACCEPTANCE_ID_RE = re.compile(rb"\b(?:cand|chk|ctx|afbk)_[0-9a-f]{8,}\b", re.IGNORECASE)
PRIVATE_ACCEPTANCE_PATH_RE = re.compile(
    rb"(?:projects:)?_acceptance" rb"_tests/" rb"results|data/logs/home_" rb"mcp/" rb"20\d{6}T\d{6}Z-",
    re.IGNORECASE,
)
PRIVATE_KEY_RE = re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")
API_TOKEN_RE = re.compile(rb"\b(?:sk-(?:proj-)?|gh[opusr]_)[A-Za-z0-9_-]{16,}\b")
AWS_ACCESS_KEY_RE = re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")
SLACK_TOKEN_RE = re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{16,}\b")
URL_CREDENTIAL_RE = re.compile(rb"[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@", re.IGNORECASE)
AUTH_HEADER_RE = re.compile(rb"(?:authorization\s*[:=]\s*bearer|bearer\s+)[A-Za-z0-9._~+/-]{16,}", re.IGNORECASE)


class ReleaseCandidateError(RuntimeError):
    """A fail-closed release candidate policy violation."""


@dataclass(frozen=True)
class SourceBlob:
    path: str
    mode: str
    object_id: str
    data: bytes


@dataclass(frozen=True)
class LiteralPolicy:
    values: tuple[bytes, ...]
    file_digests: tuple[str, ...]


def _git(repo: Path, *args: str, input_data: bytes | None = None, env: dict[str, str] | None = None) -> bytes:
    command = ["git", "-C", str(repo), *args]
    try:
        result = subprocess.run(
            command,
            input=input_data,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = ""
        if isinstance(exc, subprocess.CalledProcessError):
            detail = exc.stderr.decode("utf-8", errors="replace").strip()
        raise ReleaseCandidateError(f"git command failed: {' '.join(args)}: {detail}") from exc
    return result.stdout


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _resolve_source_repo(path: Path) -> Path:
    candidate = path.resolve(strict=True)
    root = Path(_git(candidate, "rev-parse", "--show-toplevel").decode().strip()).resolve(strict=True)
    if root != candidate:
        raise ReleaseCandidateError(f"source repository must be its Git top level: {root}")
    return root


def _resolve_source_commit(repo: Path, value: str) -> tuple[str, str, int]:
    commit = _git(repo, "rev-parse", "--verify", f"{value}^{{commit}}").decode().strip()
    tree = _git(repo, "rev-parse", "--verify", f"{commit}^{{tree}}").decode().strip()
    timestamp_text = _git(repo, "show", "-s", "--format=%ct", commit).decode().strip()
    try:
        timestamp = int(timestamp_text)
    except ValueError as exc:
        raise ReleaseCandidateError("source commit has an invalid timestamp") from exc
    return commit, tree, timestamp


def _source_is_dirty(repo: Path) -> bool:
    return bool(_git(repo, "status", "--porcelain=v1", "--untracked-files=all").strip())


def _validate_logical_path(path: str) -> PurePosixPath:
    if not path or path.startswith(("/", "\\")) or "\\" in path:
        raise ReleaseCandidateError(f"unsafe tracked path: {path!r}")
    if any(ord(character) < 32 or ord(character) == 127 for character in path):
        raise ReleaseCandidateError(f"tracked path contains control characters: {path!r}")
    logical = PurePosixPath(path)
    if any(part in {"", ".", ".."} for part in logical.parts):
        raise ReleaseCandidateError(f"unsafe tracked path: {path!r}")
    decoded = path
    for _iteration in range(3):
        next_value = urllib.parse.unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    decoded_logical = PurePosixPath(decoded)
    if (
        decoded.startswith(("/", "\\"))
        or "\\" in decoded
        or any(part in {"", ".", ".."} for part in decoded_logical.parts)
    ):
        raise ReleaseCandidateError(f"encoded unsafe tracked path: {path!r}")
    return logical


def _is_secret_path(logical: PurePosixPath) -> bool:
    path = logical.as_posix().casefold()
    basename = logical.name.casefold()
    return (
        basename in SECRET_BASENAMES
        or any(fnmatch.fnmatchcase(basename, pattern) for pattern in SECRET_GLOBS)
        or any(fnmatch.fnmatchcase(path, pattern) for pattern in SECRET_PATH_GLOBS)
    )


def _path_decision(path: str) -> str:
    logical = _validate_logical_path(path)
    if _is_secret_path(logical):
        return "reject_secret_path"
    if logical.parts[0] in EXCLUDED_TOP_LEVEL_DIRS or ROOT_EXPORT_RE.match(logical.parts[0]):
        return "exclude_private_or_runtime_root"
    if any(part in EXCLUDED_PARTS for part in logical.parts):
        return "exclude_runtime_artifact"
    if path in EXCLUDED_PUBLIC_PATHS or path.startswith(EXCLUDED_PUBLIC_PREFIXES):
        return "exclude_private_or_internal_document"
    if len(logical.parts) == 1:
        return "include" if path in APPROVED_TOP_LEVEL_FILES else "exclude_unapproved"
    return "include" if logical.parts[0] in APPROVED_TOP_LEVEL_DIRS else "exclude_unapproved"


def _load_literal_policy(paths: Sequence[Path]) -> LiteralPolicy:
    if not paths:
        raise ReleaseCandidateError("at least one --forbidden-literals-file is required")
    values: list[bytes] = []
    digests: list[str] = []
    for path in paths:
        data = path.resolve(strict=True).read_bytes()
        digests.append(_sha256(data))
        try:
            lines = data.decode("utf-8").splitlines()
        except UnicodeDecodeError as exc:
            raise ReleaseCandidateError("forbidden-literals files must be UTF-8") from exc
        for line in lines:
            value = line.strip()
            if not value or value.startswith("#"):
                continue
            encoded = value.casefold().encode("utf-8")
            if len(encoded) < 8:
                raise ReleaseCandidateError("forbidden literals must contain at least eight bytes")
            values.append(encoded)
    if not values:
        raise ReleaseCandidateError("forbidden-literals files contain no enforceable literals")
    return LiteralPolicy(tuple(sorted(set(values))), tuple(sorted(digests)))


def _scan_content(data: bytes, label: str, literals: LiteralPolicy) -> None:
    checks = (
        (RUN_ID_RE, "browser/run identifier"),
        (PERSONAL_PATH_RE, "absolute personal path"),
        (PRIVATE_TMP_RE, "machine-local temporary path"),
        (TRACKER_ID_RE, "internal tracker identifier"),
        (PRIVATE_ACCEPTANCE_ID_RE, "private acceptance identifier"),
        (PRIVATE_ACCEPTANCE_PATH_RE, "private acceptance path"),
        (PRIVATE_KEY_RE, "private key"),
        (API_TOKEN_RE, "API token"),
        (AWS_ACCESS_KEY_RE, "AWS access key"),
        (SLACK_TOKEN_RE, "Slack token"),
        (URL_CREDENTIAL_RE, "URL credential"),
        (AUTH_HEADER_RE, "authorization credential"),
    )
    for pattern, description in checks:
        if pattern.search(data):
            raise ReleaseCandidateError(f"{description} found in {label}")
    folded = data.decode("utf-8", errors="ignore").casefold().encode("utf-8")
    for value in literals.values:
        if value in folded:
            raise ReleaseCandidateError(f"private corpus literal found in {label}")


def _read_source_blobs(repo: Path, commit: str, literals: LiteralPolicy) -> tuple[list[SourceBlob], Counter[str]]:
    raw = _git(repo, "ls-tree", "-rz", "--full-tree", commit)
    blobs: list[SourceBlob] = []
    exclusions: Counter[str] = Counter()
    for entry in raw.split(b"\0"):
        if not entry:
            continue
        metadata, raw_path = entry.split(b"\t", 1)
        mode, object_type, object_id = metadata.decode("ascii").split()
        path = raw_path.decode("utf-8")
        decision = _path_decision(path)
        if decision == "reject_secret_path":
            raise ReleaseCandidateError(f"secret-like tracked path is not distributable: {path}")
        if decision != "include":
            exclusions[decision] += 1
            continue
        if object_type != "blob" or mode not in {"100644", "100755"}:
            raise ReleaseCandidateError(
                f"non-regular tracked entry is not distributable: {path} ({mode} {object_type})"
            )
        data = _git(repo, "cat-file", "blob", object_id)
        _scan_content(data, path, literals)
        blobs.append(SourceBlob(path, mode, object_id, data))
    if not blobs:
        raise ReleaseCandidateError("source commit contains no approved distributable files")
    blobs.sort(key=lambda item: item.path)
    return blobs, exclusions


def _write_blob(root: Path, blob: SourceBlob) -> None:
    logical = _validate_logical_path(blob.path)
    destination = root.joinpath(*logical.parts)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        raise ReleaseCandidateError(f"duplicate or unsafe output path: {blob.path}")
    destination.write_bytes(blob.data)
    destination.chmod(0o755 if blob.mode == "100755" else 0o644)


def _source_file_records(blobs: Sequence[SourceBlob]) -> list[dict[str, object]]:
    return [
        {"mode": blob.mode, "path": blob.path, "sha256": _sha256(blob.data), "size": len(blob.data)} for blob in blobs
    ]


def _git_identity_env(home: Path, source_timestamp: int) -> dict[str, str]:
    timestamp = f"@{source_timestamp} +0000"
    return {
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_DATE": timestamp,
        "GIT_AUTHOR_EMAIL": "release@minillm.invalid",
        "GIT_AUTHOR_NAME": "MiniLLM Release Builder",
        "GIT_COMMITTER_DATE": timestamp,
        "GIT_COMMITTER_EMAIL": "release@minillm.invalid",
        "GIT_COMMITTER_NAME": "MiniLLM Release Builder",
        "HOME": str(home),
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": os.environ.get("PATH", ""),
    }


def _initialize_single_commit(repo: Path, source_tree: str, timestamp: int, home: Path) -> str:
    env = _git_identity_env(home, timestamp)
    _git(repo.parent, "init", "-q", "--initial-branch=main", str(repo), env=env)
    _git(repo, "-c", "core.autocrlf=false", "add", "-f", "--all", env=env)
    tree = _git(repo, "write-tree", env=env).decode().strip()
    message = f"MiniLLM public release candidate\n\nSource-Tree: {source_tree}\n".encode()
    commit = _git(repo, "commit-tree", tree, input_data=message, env=env).decode().strip()
    _git(repo, "update-ref", "refs/heads/main", commit, env=env)
    _git(repo, "symbolic-ref", "HEAD", "refs/heads/main", env=env)
    if _git(repo, "status", "--porcelain=v1", "--untracked-files=all", env=env).strip():
        raise ReleaseCandidateError("new release repository is unexpectedly dirty")
    return commit


def _object_inventory(repo: Path) -> dict[str, str]:
    lines = _git(repo, "cat-file", "--batch-all-objects", "--batch-check=%(objectname) %(objecttype)")
    inventory: dict[str, str] = {}
    for line in lines.decode("ascii").splitlines():
        object_id, object_type = line.split()
        inventory[object_id] = object_type
    return inventory


def _reachable_objects(repo: Path) -> set[str]:
    lines = _git(repo, "rev-list", "--objects", "--all").decode("utf-8").splitlines()
    return {line.split(" ", 1)[0] for line in lines if line}


def _scan_candidate(repo: Path, literals: LiteralPolicy) -> tuple[str, str, list[dict[str, object]]]:
    if not repo.is_dir() or not (repo / ".git").is_dir():
        raise ReleaseCandidateError("output is not a standalone Git repository")
    refs = _git(repo, "for-each-ref", "--format=%(refname)").decode().splitlines()
    if refs != ["refs/heads/main"]:
        raise ReleaseCandidateError(f"candidate must contain only refs/heads/main, found: {refs}")
    if _git(repo, "remote").strip():
        raise ReleaseCandidateError("candidate must not contain Git remotes")
    commits = _git(repo, "rev-list", "--all").decode().splitlines()
    if len(commits) != 1:
        raise ReleaseCandidateError(f"candidate history must contain exactly one commit, found {len(commits)}")
    commit = commits[0]
    parents = _git(repo, "show", "-s", "--format=%P", commit).decode().strip()
    if parents:
        raise ReleaseCandidateError("candidate commit must be parentless")
    if _git(repo, "status", "--porcelain=v1", "--untracked-files=all").strip():
        raise ReleaseCandidateError("candidate worktree is dirty")

    inventory = _object_inventory(repo)
    reachable = _reachable_objects(repo)
    unreachable = set(inventory) - reachable
    if unreachable:
        raise ReleaseCandidateError(f"candidate contains {len(unreachable)} unreachable Git objects")

    for object_id, object_type in inventory.items():
        if object_type in {"blob", "commit", "tag"}:
            _scan_content(_git(repo, "cat-file", object_type, object_id), f"Git {object_type} {object_id}", literals)

    raw = _git(repo, "ls-tree", "-rz", "--full-tree", "-r", commit)
    files: list[dict[str, object]] = []
    for entry in raw.split(b"\0"):
        if not entry:
            continue
        metadata, raw_path = entry.split(b"\t", 1)
        mode, object_type, object_id = metadata.decode("ascii").split()
        path = raw_path.decode("utf-8")
        if _path_decision(path) != "include":
            raise ReleaseCandidateError(f"candidate contains a non-distributable path: {path}")
        if object_type != "blob" or mode not in {"100644", "100755"}:
            raise ReleaseCandidateError(f"candidate contains non-regular entry: {path}")
        data = _git(repo, "cat-file", "blob", object_id)
        _scan_content(data, path, literals)
        worktree_path = repo.joinpath(*PurePosixPath(path).parts)
        try:
            metadata = worktree_path.lstat()
            resolved = worktree_path.resolve(strict=True)
            resolved.relative_to(repo)
        except (OSError, ValueError) as exc:
            raise ReleaseCandidateError(f"candidate worktree path escapes or is unavailable: {path}") from exc
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ReleaseCandidateError(f"candidate worktree path is not a single-link regular file: {path}")
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(worktree_path, flags)
        try:
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                worktree_data = handle.read()
        finally:
            os.close(descriptor)
        if worktree_data != data:
            raise ReleaseCandidateError(f"candidate worktree content differs from Git: {path}")
        files.append({"mode": mode, "path": path, "sha256": _sha256(data), "size": len(data)})
    files.sort(key=lambda item: str(item["path"]))
    tree = _git(repo, "rev-parse", f"{commit}^{{tree}}").decode().strip()
    return commit, tree, files


def _manifest(
    *,
    source_commit: str,
    source_tree: str,
    output_commit: str,
    output_tree: str,
    files: list[dict[str, object]],
    exclusions: Counter[str],
    literals: LiteralPolicy,
) -> dict[str, object]:
    lock = next((item for item in files if item["path"] == "uv.lock"), None)
    return {
        "schema_version": SCHEMA_VERSION,
        "tool_version": TOOL_VERSION,
        "source": {"commit": source_commit, "tree": source_tree},
        "output": {
            "branch": "main",
            "commit": output_commit,
            "parent_count": 0,
            "tree": output_tree,
        },
        "files": files,
        "lock": {"path": "uv.lock", "sha256": lock["sha256"] if lock else None},
        "source_exclusions": {"counts_by_reason": dict(sorted(exclusions.items())), "paths_disclosed": False},
        "scan": {
            "checks": [
                "approved_paths",
                "credential_patterns",
                "forbidden_corpus_literals",
                "full_git_object_reachability",
                "internal_tracker_identifiers",
                "machine_local_temporary_paths",
                "personal_absolute_paths",
                "private_acceptance_identifiers",
                "private_export_and_runtime_roots",
                "run_identifiers",
                "single_parentless_commit",
            ],
            "forbidden_literal_file_sha256": list(literals.file_digests),
            "forbidden_literal_count": len(literals.values),
            "passed": True,
        },
    }


def _manifest_bytes(value: dict[str, object]) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _write_exclusive(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)


def _default_manifest_path(output: Path) -> Path:
    return output.with_name(f"{output.name}.release-manifest.json")


def _ensure_safe_destinations(source: Path, output: Path, manifest: Path) -> None:
    output_parent = output.parent.resolve(strict=True)
    manifest_parent = manifest.parent.resolve(strict=True)
    resolved_output = output_parent / output.name
    resolved_manifest = manifest_parent / manifest.name
    try:
        resolved_output.relative_to(source)
    except ValueError:
        pass
    else:
        raise ReleaseCandidateError("output directory must not be inside the source repository")
    if resolved_manifest == resolved_output or resolved_manifest.is_relative_to(resolved_output):
        raise ReleaseCandidateError("manifest must be outside the output repository")
    if output.exists() or output.is_symlink():
        raise ReleaseCandidateError(f"output already exists: {output}")
    if manifest.exists() or manifest.is_symlink():
        raise ReleaseCandidateError(f"manifest already exists: {manifest}")


def create_candidate(
    source_repo: Path,
    source_value: str,
    output: Path,
    manifest_path: Path,
    literals: LiteralPolicy,
) -> dict[str, object]:
    if _source_is_dirty(source_repo):
        raise ReleaseCandidateError("source repository is dirty; commit or remove changes before creating a candidate")
    source_commit, source_tree, timestamp = _resolve_source_commit(source_repo, source_value)
    _ensure_safe_destinations(source_repo, output, manifest_path)
    blobs, exclusions = _read_source_blobs(source_repo, source_commit, literals)

    temp_root = Path(tempfile.mkdtemp(prefix=f".{output.name}.candidate-", dir=output.parent))
    temp_repo = temp_root / "repo"
    temp_repo.mkdir(mode=0o700)
    manifest_temp = manifest_path.with_name(f".{manifest_path.name}.{os.getpid()}.tmp")
    try:
        for blob in blobs:
            _write_blob(temp_repo, blob)
        with tempfile.TemporaryDirectory(prefix="minillm-release-git-home-") as git_home:
            expected_commit = _initialize_single_commit(temp_repo, source_tree, timestamp, Path(git_home))
        output_commit, output_tree, files = _scan_candidate(temp_repo, literals)
        if output_commit != expected_commit:
            raise ReleaseCandidateError("candidate HEAD changed during audit")
        if files != _source_file_records(blobs):
            raise ReleaseCandidateError("candidate files do not exactly match approved source blobs")
        manifest = _manifest(
            source_commit=source_commit,
            source_tree=source_tree,
            output_commit=output_commit,
            output_tree=output_tree,
            files=files,
            exclusions=exclusions,
            literals=literals,
        )
        manifest_data = _manifest_bytes(manifest)
        _scan_content(manifest_data, "release manifest", literals)
        _write_exclusive(manifest_temp, manifest_data)
        temp_repo.rename(output)
        manifest_temp.replace(manifest_path)
        return manifest
    finally:
        if manifest_temp.exists():
            manifest_temp.unlink()
        shutil.rmtree(temp_root, ignore_errors=True)


def check_candidate(
    source_repo: Path,
    source_value: str,
    output: Path,
    manifest_path: Path,
    literals: LiteralPolicy,
) -> dict[str, object]:
    source_commit, source_tree, _timestamp = _resolve_source_commit(source_repo, source_value)
    if output.is_symlink():
        raise ReleaseCandidateError("output repository cannot be a symlink")
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ReleaseCandidateError(f"release manifest is unavailable: {manifest_path}")
    try:
        stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseCandidateError("release manifest is not valid UTF-8 JSON") from exc
    blobs, exclusions = _read_source_blobs(source_repo, source_commit, literals)
    output_commit, output_tree, files = _scan_candidate(output.resolve(strict=True), literals)
    if files != _source_file_records(blobs):
        raise ReleaseCandidateError("candidate files do not exactly match approved source blobs")
    expected = _manifest(
        source_commit=source_commit,
        source_tree=source_tree,
        output_commit=output_commit,
        output_tree=output_tree,
        files=files,
        exclusions=exclusions,
        literals=literals,
    )
    if stored != expected:
        raise ReleaseCandidateError("release manifest does not match the source and audited output")
    _scan_content(_manifest_bytes(stored), "release manifest", literals)
    return expected


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-repo", type=Path, required=True)
    parser.add_argument("--source-commit", required=True, help="Explicit Git commit or commit-resolving tree-ish")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, help="Manifest path outside output (default: sibling file)")
    parser.add_argument(
        "--forbidden-literals-file",
        action="append",
        default=[],
        type=Path,
        help="UTF-8 local file with one private corpus canary per line; repeatable and never copied",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Audit an existing candidate and manifest; this is the only mode allowed with a dirty source worktree",
    )
    return parser


def _fail(message: str) -> NoReturn:
    print(json.dumps({"error": message, "status": "error"}, sort_keys=True), file=sys.stderr)
    raise SystemExit(1)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        source = _resolve_source_repo(args.source_repo)
        output = args.output_dir.absolute()
        manifest_path = args.manifest.absolute() if args.manifest else _default_manifest_path(output)
        literals = _load_literal_policy(args.forbidden_literals_file)
        if args.check_only:
            result = check_candidate(source, args.source_commit, output, manifest_path, literals)
        else:
            result = create_candidate(source, args.source_commit, output, manifest_path, literals)
    except (OSError, ReleaseCandidateError) as exc:
        _fail(str(exc))
    print(
        json.dumps(
            {
                "file_count": len(result["files"]),
                "manifest": "<public-candidate-manifest>",
                "output_commit": result["output"]["commit"],
                "output_dir": "<public-candidate>",
                "source_commit": result["source"]["commit"],
                "status": "ok",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
