#!/usr/bin/env python3
"""Build, verify, and audit a fresh MiniLLM public release candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, NoReturn, Sequence


ARTIFACT_SCHEMA_VERSION = "minillm_release_evidence_artifact_v1"
ARTIFACT_TYPE = "clean_clone"
RUNNER_VERSION = "1.0"
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
CREATOR_PATH = "scripts/create_public_release_candidate.py"
VERIFY_PATH = "scripts/verify.sh"


class CleanCloneEvidenceError(RuntimeError):
    """A clean-clone claim could not be established."""


@dataclass(frozen=True)
class CommandResult:
    """Captured subprocess data that is reduced to hashes before persistence."""

    argv: tuple[str, ...]
    evidence_argv: tuple[str, ...]
    exit_code: int
    stdout: bytes
    stderr: bytes
    stage: str

    def evidence(self) -> dict[str, Any]:
        return {
            "argv": list(self.evidence_argv),
            "exit_code": self.exit_code,
            "stage": self.stage,
            "stderr_bytes": len(self.stderr),
            "stderr_sha256": _sha256(self.stderr),
            "stdout_bytes": len(self.stdout),
            "stdout_sha256": _sha256(self.stdout),
        }


CandidateMutator = Callable[[Path, Path], None]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _failure_message(
    stage: str,
    *,
    code: str,
    index: int = 0,
    exit_code: int = 1,
    stdout: bytes = b"",
    stderr: bytes = b"",
) -> str:
    return (
        f"stage={stage} index={index} exit={exit_code} code={code} "
        f"stdout_bytes={len(stdout)} stdout_sha256={_sha256(stdout)} "
        f"stderr_bytes={len(stderr)} stderr_sha256={_sha256(stderr)}"
    )


def _reject(stage: str, code: str) -> NoReturn:
    raise CleanCloneEvidenceError(_failure_message(stage, code=code))


def _run_command(
    stage: str,
    index: int,
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    evidence_argv: Sequence[str] | None = None,
) -> CommandResult:
    command = tuple(argv)
    try:
        completed = subprocess.run(command, cwd=cwd, capture_output=True, check=False)
    except OSError as exc:
        raise CleanCloneEvidenceError(
            _failure_message(stage, code="command_start_failed", index=index, exit_code=-1)
        ) from exc
    result = CommandResult(
        command,
        tuple(evidence_argv or command),
        completed.returncode,
        completed.stdout,
        completed.stderr,
        stage,
    )
    if completed.returncode != 0:
        raise CleanCloneEvidenceError(
            _failure_message(
                stage,
                code="command_failed",
                index=index,
                exit_code=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
            )
        )
    return result


def _decode(stage: str, data: bytes) -> str:
    try:
        return data.decode("utf-8", "strict").strip()
    except UnicodeDecodeError:
        _reject(stage, "non_utf8_command_output")


def _git(repo: Path, stage: str, index: int, *args: str) -> tuple[str, CommandResult]:
    result = _run_command(stage, index, ("git", "-C", str(repo), *args))
    return _decode(stage, result.stdout), result


def _safe_regular_bytes(path: Path, stage: str, *, maximum_bytes: int | None = None) -> bytes:
    absolute = path.expanduser().absolute()
    try:
        parent = absolute.parent.resolve(strict=True)
        metadata = absolute.lstat()
        resolved = absolute.resolve(strict=True)
    except OSError:
        _reject(stage, "regular_file_unavailable")
    if parent != absolute.parent or resolved != absolute:
        _reject(stage, "symlink_indirection")
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        _reject(stage, "not_single_link_regular_file")
    if maximum_bytes is not None and metadata.st_size > maximum_bytes:
        _reject(stage, "file_too_large")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(absolute, flags)
    except OSError:
        _reject(stage, "regular_file_open_failed")
    try:
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            data = handle.read(maximum_bytes + 1 if maximum_bytes is not None else -1)
            after = os.fstat(handle.fileno())
    finally:
        os.close(descriptor)
    if maximum_bytes is not None and len(data) > maximum_bytes:
        _reject(stage, "file_too_large")
    if (metadata.st_dev, metadata.st_ino, metadata.st_size) != (after.st_dev, after.st_ino, after.st_size):
        _reject(stage, "file_changed_during_read")
    return data


def _json_object(path: Path, stage: str) -> tuple[dict[str, Any], bytes]:
    data = _safe_regular_bytes(path, stage, maximum_bytes=MAX_MANIFEST_BYTES)
    try:
        value = json.loads(data.decode("utf-8", "strict"))
    except (UnicodeDecodeError, ValueError):
        _reject(stage, "invalid_json")
    if not isinstance(value, dict):
        _reject(stage, "json_not_object")
    return value, data


def _resolve_source(repo_argument: Path, source_value: str) -> tuple[Path, dict[str, Any]]:
    stage = "source_validation"
    try:
        candidate = repo_argument.expanduser().resolve(strict=True)
    except OSError:
        _reject(stage, "source_unavailable")
    root_text, _ = _git(candidate, stage, 1, "rev-parse", "--show-toplevel")
    try:
        root = Path(root_text).resolve(strict=True)
    except OSError:
        _reject(stage, "source_root_unavailable")
    if root != candidate:
        _reject(stage, "source_not_git_top_level")
    status, _ = _git(root, stage, 2, "status", "--porcelain=v1", "--untracked-files=all")
    if status:
        _reject(stage, "source_dirty")
    commit, _ = _git(root, stage, 3, "rev-parse", "--verify", f"{source_value}^{{commit}}")
    head, _ = _git(root, stage, 4, "rev-parse", "--verify", "HEAD^{commit}")
    if commit != head:
        _reject(stage, "source_commit_not_checked_out_head")
    tree, _ = _git(root, stage, 5, "rev-parse", "--verify", f"{commit}^{{tree}}")
    lock_result = _run_command(
        stage,
        6,
        ("git", "-C", str(root), "show", f"{commit}:uv.lock"),
    )
    lock_data = lock_result.stdout
    for relative in (CREATOR_PATH, VERIFY_PATH):
        tracked_result = _run_command(
            stage,
            7,
            ("git", "-C", str(root), "show", f"{commit}:{relative}"),
        )
        worktree_data = _safe_regular_bytes(root / relative, stage)
        if tracked_result.stdout != worktree_data:
            _reject(stage, "canonical_input_mismatch")
    return root, {
        "git_commit": commit,
        "git_dirty": False,
        "git_tree": tree,
        "uv_lock_sha256": _sha256(lock_data),
    }


def _validate_external_literals(paths: Sequence[Path], source: Path) -> tuple[list[Path], list[str]]:
    if not paths:
        _reject("input_validation", "forbidden_literals_missing")
    resolved_paths: list[Path] = []
    digests: list[str] = []
    for path in paths:
        absolute = path.expanduser().absolute()
        data = _safe_regular_bytes(absolute, "input_validation")
        try:
            absolute.relative_to(source)
        except ValueError:
            pass
        else:
            _reject("input_validation", "forbidden_literals_not_external")
        resolved_paths.append(absolute)
        digests.append(_sha256(data))
    if len(set(resolved_paths)) != len(resolved_paths):
        _reject("input_validation", "duplicate_forbidden_literals_file")
    return resolved_paths, sorted(digests)


def _validate_destinations(source: Path, candidate: Path, manifest: Path, artifact: Path) -> None:
    paths = (candidate.expanduser().absolute(), manifest.expanduser().absolute(), artifact.expanduser().absolute())
    if len(set(paths)) != len(paths):
        _reject("output_validation", "output_paths_overlap")
    for path in paths:
        try:
            parent = path.parent.resolve(strict=True)
        except OSError:
            _reject("output_validation", "output_parent_unavailable")
        if parent != path.parent:
            _reject("output_validation", "output_parent_symlinked")
        if path.exists() or path.is_symlink():
            _reject("output_validation", "output_preexists")
        try:
            path.relative_to(source)
        except ValueError:
            pass
        else:
            _reject("output_validation", "output_inside_source")
    try:
        manifest.relative_to(candidate)
    except ValueError:
        pass
    else:
        _reject("output_validation", "manifest_inside_candidate")
    try:
        artifact.relative_to(candidate)
    except ValueError:
        pass
    else:
        _reject("output_validation", "artifact_inside_candidate")


def _creator_command(
    source: Path,
    commit: str,
    candidate: Path,
    manifest: Path,
    literal_paths: Sequence[Path],
    *,
    check_only: bool,
) -> tuple[str, ...]:
    command = [
        sys.executable,
        str(source / CREATOR_PATH),
        "--source-repo",
        str(source),
        "--source-commit",
        commit,
        "--output-dir",
        str(candidate),
        "--manifest",
        str(manifest),
    ]
    for path in literal_paths:
        command.extend(("--forbidden-literals-file", str(path)))
    if check_only:
        command.append("--check-only")
    return tuple(command)


def _creator_evidence_command(commit: str, literal_count: int, *, check_only: bool) -> tuple[str, ...]:
    command = [
        "python",
        CREATOR_PATH,
        "--source-repo",
        ".",
        "--source-commit",
        commit,
        "--output-dir",
        "<public-candidate>",
        "--manifest",
        "<public-candidate-manifest>",
    ]
    for index in range(1, literal_count + 1):
        command.extend(("--forbidden-literals-file", f"<private-overlap-canaries:{index}>"))
    if check_only:
        command.append("--check-only")
    return tuple(command)


def _portable_verify_command(command: Sequence[str], source: Path) -> tuple[str, ...]:
    portable: list[str] = []
    source_text = str(source)
    for index, value in enumerate(command):
        if value == source_text:
            portable.append(".")
        elif value.startswith(f"{source_text}{os.sep}"):
            portable.append(value[len(source_text) + 1 :].replace(os.sep, "/"))
        elif os.path.isabs(value):
            portable.append("<runtime-executable>" if index == 0 else "<runtime-path>")
        else:
            portable.append(value)
    return tuple(portable)


def _manifest_fields(
    manifest_path: Path,
    candidate: Path,
    repository: Mapping[str, Any],
    literal_digests: Sequence[str],
) -> tuple[dict[str, Any], bytes, str, str]:
    stage = "candidate_validation"
    value, data = _json_object(manifest_path, stage)
    source = value.get("source")
    output = value.get("output")
    lock = value.get("lock")
    scan = value.get("scan")
    if value.get("schema_version") != 1 or not all(isinstance(item, dict) for item in (source, output, lock, scan)):
        _reject(stage, "candidate_manifest_contract")
    assert isinstance(source, dict) and isinstance(output, dict) and isinstance(lock, dict) and isinstance(scan, dict)
    if source != {"commit": repository["git_commit"], "tree": repository["git_tree"]}:
        _reject(stage, "candidate_source_identity_mismatch")
    if output.get("branch") != "main" or output.get("parent_count") != 0:
        _reject(stage, "candidate_history_manifest_invalid")
    if lock.get("path") != "uv.lock" or lock.get("sha256") != repository["uv_lock_sha256"]:
        _reject(stage, "candidate_lock_manifest_mismatch")
    if scan.get("passed") is not True:
        _reject(stage, "candidate_privacy_scan_failed")
    if scan.get("forbidden_literal_file_sha256") != list(literal_digests):
        _reject(stage, "forbidden_literal_binding_mismatch")
    commit, _ = _git(candidate, stage, 1, "rev-parse", "HEAD^{commit}")
    tree, _ = _git(candidate, stage, 2, "rev-parse", "HEAD^{tree}")
    commits, _ = _git(candidate, stage, 3, "rev-list", "--all")
    parents, _ = _git(candidate, stage, 4, "show", "-s", "--format=%P", "HEAD")
    remotes, _ = _git(candidate, stage, 5, "remote")
    status, _ = _git(candidate, stage, 6, "status", "--porcelain=v1", "--untracked-files=all")
    if len(commits.splitlines()) != 1 or parents or remotes or status:
        _reject(stage, "candidate_repository_contract")
    if commit != output.get("commit") or tree != output.get("tree"):
        _reject(stage, "candidate_output_identity_mismatch")
    lock_data = _safe_regular_bytes(candidate / "uv.lock", stage)
    if _sha256(lock_data) != repository["uv_lock_sha256"]:
        _reject(stage, "candidate_lock_digest_mismatch")
    return value, data, commit, tree


def _atomic_private_json(path: Path, value: Mapping[str, Any]) -> None:
    destination = path.expanduser().absolute()
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        parent = destination.parent.resolve(strict=True)
    except OSError:
        _reject("artifact_write", "artifact_parent_unavailable")
    if parent != destination.parent:
        _reject("artifact_write", "artifact_parent_symlinked")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    directory_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        directory_flags |= os.O_DIRECTORY
    try:
        directory_descriptor = os.open(parent, directory_flags)
    except OSError as exc:
        raise CleanCloneEvidenceError(_failure_message("artifact_write", code="artifact_parent_open_failed")) from exc
    try:
        try:
            descriptor = os.open(destination.name, flags, 0o600, dir_fd=directory_descriptor)
        except OSError as exc:
            raise CleanCloneEvidenceError(_failure_message("artifact_write", code="artifact_create_failed")) from exc
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as handle:
                handle.write(payload)
                handle.flush()
                os.fchmod(handle.fileno(), 0o600)
                metadata = os.fstat(handle.fileno())
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_nlink != 1
                    or stat.S_IMODE(metadata.st_mode) != 0o600
                ):
                    _reject("artifact_write", "artifact_permissions_invalid")
                os.fsync(handle.fileno())
            os.fsync(directory_descriptor)
        finally:
            os.close(descriptor)
    finally:
        os.close(directory_descriptor)


def run_clean_clone_evidence(
    *,
    source_repo: Path,
    source_commit: str,
    candidate_dir: Path,
    candidate_manifest: Path,
    artifact_path: Path,
    forbidden_literals_files: Sequence[Path],
    verify_command: Sequence[str] = ("./scripts/verify.sh",),
    candidate_mutator: CandidateMutator | None = None,
) -> dict[str, Any]:
    """Execute the real clean-candidate workflow and emit manifest-compatible evidence."""
    source, repository = _resolve_source(source_repo, source_commit)
    literals, literal_digests = _validate_external_literals(forbidden_literals_files, source)
    candidate = candidate_dir.expanduser().absolute()
    manifest = candidate_manifest.expanduser().absolute()
    artifact = artifact_path.expanduser().absolute()
    _validate_destinations(source, candidate, manifest, artifact)

    commands: list[CommandResult] = []
    commands.append(
        _run_command(
            "create_candidate",
            1,
            _creator_command(
                source,
                str(repository["git_commit"]),
                candidate,
                manifest,
                literals,
                check_only=False,
            ),
            evidence_argv=_creator_evidence_command(str(repository["git_commit"]), len(literals), check_only=False),
        )
    )
    _manifest_fields(manifest, candidate, repository, literal_digests)
    if (candidate / ".venv").exists() or (candidate / ".venv").is_symlink():
        _reject("pre_verify_validation", "preexisting_virtual_environment")
    if candidate_mutator is not None:
        candidate_mutator(candidate, manifest)

    verify = _run_command(
        "candidate_verify",
        1,
        verify_command,
        cwd=candidate,
        evidence_argv=_portable_verify_command(verify_command, source),
    )
    commands.append(verify)
    check = _run_command(
        "check_only",
        1,
        _creator_command(
            source,
            str(repository["git_commit"]),
            candidate,
            manifest,
            literals,
            check_only=True,
        ),
        evidence_argv=_creator_evidence_command(str(repository["git_commit"]), len(literals), check_only=True),
    )
    commands.append(check)
    manifest_value, manifest_data, candidate_commit, candidate_tree = _manifest_fields(
        manifest, candidate, repository, literal_digests
    )

    final_source, final_repository = _resolve_source(source, str(repository["git_commit"]))
    if final_source != source or final_repository != repository:
        _reject("final_source_validation", "source_identity_changed")

    metrics = {
        "demo_passed": True,
        "fresh_clone": True,
        "full_tests_passed": True,
        "locked_install_passed": True,
        "privacy_scan_passed": True,
        "single_parentless_public_commit": True,
    }
    evidence = {
        "artifact_type": ARTIFACT_TYPE,
        "command": {"argv": list(verify.evidence_argv), "exit_code": verify.exit_code},
        "details": {
            "candidate": {
                "commit": candidate_commit,
                "manifest_sha256": _sha256(manifest_data),
                "tree": candidate_tree,
                "uv_lock_sha256": manifest_value["lock"]["sha256"],
            },
            "commands": [result.evidence() for result in commands],
            "forbidden_literal_file_sha256": literal_digests,
            "preexisting_virtual_environment": False,
            "runner_version": RUNNER_VERSION,
        },
        "generated_at": _timestamp(),
        "metrics": metrics,
        "repository": repository,
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "status": "pass",
    }
    _atomic_private_json(artifact, evidence)
    return evidence


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-repo", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--candidate-dir", type=Path, required=True)
    parser.add_argument("--candidate-manifest", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--forbidden-literals-file", action="append", default=[], type=Path)
    return parser


def _fail(message: str) -> NoReturn:
    print(json.dumps({"error": message, "status": "error"}, sort_keys=True), file=sys.stderr)
    raise SystemExit(2)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        evidence = run_clean_clone_evidence(
            source_repo=args.source_repo,
            source_commit=args.source_commit,
            candidate_dir=args.candidate_dir,
            candidate_manifest=args.candidate_manifest,
            artifact_path=args.artifact,
            forbidden_literals_files=args.forbidden_literals_file,
        )
    except (CleanCloneEvidenceError, OSError, ValueError) as exc:
        _fail(str(exc))
    print(
        json.dumps(
            {
                "artifact_type": evidence["artifact_type"],
                "candidate_commit": evidence["details"]["candidate"]["commit"],
                "status": evidence["status"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
