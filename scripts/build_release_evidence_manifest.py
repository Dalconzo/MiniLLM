#!/usr/bin/env python3
"""Validate explicit release evidence and emit a commit-bound MiniLLM manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NoReturn, Sequence


SCHEMA_VERSION = "minillm_release_evidence_manifest_v1"
ARTIFACT_SCHEMA_VERSION = "minillm_release_evidence_artifact_v1"
TOOL_VERSION = "1.0"
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
FUTURE_TOLERANCE = timedelta(minutes=5)

REQUIRED_REPOSITORY_FILES = (
    ".github/workflows/ci.yml",
    "LICENSE",
    "README.md",
    "SECURITY.md",
    "docs/evaluation/offline_synthetic_retrieval_v1.md",
    "docs/portfolio/ai-assisted-development.md",
    "docs/roadmap/v0.2.0a1.md",
    "docs/security/threat_model.md",
    "examples/public_demo/run_demo.py",
    "pyproject.toml",
    "scripts/audit_public_artifacts.py",
    "uv.lock",
)

# These thresholds are part of the versioned release contract. Changing one
# requires a schema/version review, not an ad-hoc final-run adjustment.
ARTIFACT_POLICY: dict[str, dict[str, Any]] = {
    "license_review": {
        "max_age_hours": 2160,
        "checks": (
            ("metrics.ownership_confirmed", "eq", True),
            ("metrics.dependency_compatibility_confirmed", "eq", True),
            ("metrics.repository_content_approved", "eq", True),
        ),
    },
    "full_tests": {
        "max_age_hours": 168,
        "checks": (
            ("metrics.tests_passed", "min", 176),
            ("metrics.tests_failed", "max", 0),
            ("metrics.tests_errors", "max", 0),
        ),
    },
    "security_subset": {
        "max_age_hours": 168,
        "checks": (
            ("metrics.tests_passed", "min", 25),
            ("metrics.tests_failed", "max", 0),
            ("metrics.tests_errors", "max", 0),
        ),
    },
    "coverage": {
        "max_age_hours": 168,
        "checks": (
            ("metrics.coverage_percent", "min", 70.0),
            ("metrics.branch_coverage_enabled", "eq", True),
            ("metrics.measured_files", "min", 1),
        ),
    },
    "four_variant_eval": {
        "max_age_hours": 168,
        "checks": (
            ("metrics.case_count", "min", 28),
            ("metrics.hard_gate_case_count", "min", 28),
            ("metrics.hard_gate_pass_count", "eq_path", "metrics.hard_gate_case_count"),
            (
                "metrics.executed_variants",
                "set_eq",
                ["governed_hybrid", "no_memory", "raw_fts", "vector_only"],
            ),
            ("metrics.fixture_immutable", "eq", True),
            ("metrics.raw_canaries_reached_ingestion", "eq", True),
            ("metrics.persisted_canaries_absent", "eq", True),
            ("metrics.report_canaries_absent", "eq", True),
            ("metrics.governed_mrr", "min", 0.65),
            ("metrics.governed_precision_at_5", "min", 0.15),
            ("metrics.governed_recall_at_5", "min", 0.70),
            ("metrics.governed_abstention_accuracy", "min", 0.75),
            ("metrics.governed_provenance_accuracy", "min", 1.0),
            ("metrics.governed_wrong_domain_rate", "max", 0.0),
            ("metrics.governed_stale_selection_rate", "max", 0.0),
            ("metrics.governed_sensitive_leakage_rate", "max", 0.0),
            ("metrics.governed_contract_pass_rate", "min", 1.0),
        ),
    },
    "deterministic_demo": {
        "max_age_hours": 168,
        "checks": (
            ("metrics.case_count", "min", 28),
            (
                "metrics.executed_variants",
                "set_eq",
                ["governed_hybrid", "no_memory", "raw_fts", "vector_only"],
            ),
            ("metrics.canonical_evaluation_passed", "eq", True),
            ("metrics.provenance_exposed", "eq", True),
            ("metrics.wrong_domain_suppressed", "eq", True),
            ("metrics.stale_or_superseded_suppressed", "eq", True),
            ("metrics.no_answer_abstained", "eq", True),
            ("metrics.unsupported_advice_refused", "eq", True),
            ("metrics.failed_contract_visible", "eq", True),
            ("metrics.network_required", "eq", False),
            ("metrics.credentials_required", "eq", False),
            ("metrics.private_data_used", "eq", False),
        ),
    },
    "clean_clone": {
        "max_age_hours": 168,
        "checks": (
            ("metrics.fresh_clone", "eq", True),
            ("metrics.locked_install_passed", "eq", True),
            ("metrics.full_tests_passed", "eq", True),
            ("metrics.demo_passed", "eq", True),
            ("metrics.privacy_scan_passed", "eq", True),
            ("metrics.single_parentless_public_commit", "eq", True),
        ),
    },
    "hosted_ci": {
        "max_age_hours": 168,
        "checks": (
            ("metrics.failed_jobs", "max", 0),
            ("metrics.successful_jobs", "min", 1),
            ("metrics.lint_passed", "eq", True),
            ("metrics.format_passed", "eq", True),
            ("metrics.typecheck_passed", "eq", True),
            ("metrics.security_subset_passed", "eq", True),
            ("metrics.operating_systems", "contains", ["linux", "macos"]),
            ("metrics.python_versions", "contains", ["3.11", "3.12", "3.13", "3.14"]),
        ),
    },
    "browser_acceptance": {
        "max_age_hours": 168,
        "checks": (
            ("metrics.overall_result", "eq", "pass"),
            ("metrics.partial", "eq", False),
            ("metrics.hard_failures", "max", 0),
            ("metrics.run_ids_retained", "eq", True),
            ("metrics.trace_ids_retained", "eq", True),
            ("metrics.report_sanitized", "eq", True),
            ("metrics.memory_truth_mutated", "eq", False),
        ),
    },
}

ARGUMENT_BY_ARTIFACT = {
    "license_review": "license_review",
    "full_tests": "full_tests",
    "security_subset": "security_subset",
    "coverage": "coverage",
    "four_variant_eval": "four_variant_eval",
    "deterministic_demo": "deterministic_demo",
    "clean_clone": "clean_clone",
    "hosted_ci": "hosted_ci",
    "browser_acceptance": "browser_acceptance",
}


class ManifestError(RuntimeError):
    """A fatal manifest-construction error rather than a release blocker."""


def _git(repo: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        raise ManifestError(f"git command failed: {' '.join(args)}: {detail}") from exc
    return result.stdout.strip()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("timestamp must be a non-empty ISO-8601 string")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _timestamp_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_regular_file(path: Path, *, maximum_bytes: int | None = None) -> tuple[Path, os.stat_result]:
    absolute = path.absolute()
    try:
        resolved = absolute.resolve(strict=True)
        metadata = absolute.lstat()
    except OSError as exc:
        raise ManifestError("evidence file is unavailable") from exc
    if resolved != absolute:
        raise ManifestError("evidence path must not contain symlink indirection")
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ManifestError("evidence file must be a single-link regular file")
    if maximum_bytes is not None and metadata.st_size > maximum_bytes:
        raise ManifestError("evidence file exceeds the size limit")
    return absolute, metadata


def _read_regular_bytes(path: Path, *, maximum_bytes: int | None = None) -> bytes:
    absolute, before = _safe_regular_file(path, maximum_bytes=maximum_bytes)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(absolute, flags)
    try:
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            data = handle.read(maximum_bytes + 1 if maximum_bytes is not None else -1)
            after = os.fstat(handle.fileno())
    finally:
        os.close(descriptor)
    if maximum_bytes is not None and len(data) > maximum_bytes:
        raise ManifestError("evidence file exceeds the size limit")
    if (before.st_dev, before.st_ino, before.st_size) != (after.st_dev, after.st_ino, after.st_size):
        raise ManifestError("evidence file changed while being read")
    return data


def _reject_json_constant(value: str) -> NoReturn:
    raise ValueError(f"non-finite JSON number is not allowed: {value}")


def _read_artifact(path: Path) -> tuple[dict[str, Any], str]:
    data = _read_regular_bytes(path, maximum_bytes=MAX_ARTIFACT_BYTES)
    try:
        value = json.loads(data.decode("utf-8"), parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ManifestError("evidence file must contain valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ManifestError("evidence JSON must be an object")
    return value, _sha256(data)


def _repository_identity(repo_argument: Path) -> tuple[Path, dict[str, Any]]:
    candidate = repo_argument.resolve(strict=True)
    repo = Path(_git(candidate, "rev-parse", "--show-toplevel")).resolve(strict=True)
    if candidate != repo:
        raise ManifestError("--repo must identify the Git top level")
    lock_data = _read_regular_bytes(repo / "uv.lock")
    identity = {
        "git_commit": _git(repo, "rev-parse", "HEAD^{commit}"),
        "git_tree": _git(repo, "rev-parse", "HEAD^{tree}"),
        "git_dirty": bool(_git(repo, "status", "--porcelain=v1", "--untracked-files=all")),
        "uv_lock_sha256": _sha256(lock_data),
    }
    return repo, identity


def _get_path(value: dict[str, Any], path: str) -> Any:
    current: Any = value
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _normalize_actual(value: Any) -> Any:
    if isinstance(value, list):
        return sorted(value) if all(isinstance(item, str) for item in value) else value
    return value


def _evaluate_rule(artifact: dict[str, Any], path: str, operation: str, expected: Any) -> tuple[bool, Any, Any]:
    actual = _get_path(artifact, path)
    resolved_expected = _get_path(artifact, expected) if operation == "eq_path" else expected
    try:
        if operation in {"min", "max"} and (isinstance(actual, bool) or not isinstance(actual, (int, float))):
            return False, actual, resolved_expected
        if operation == "eq":
            passed = type(actual) is type(resolved_expected) and actual == resolved_expected
        elif operation == "eq_path":
            passed = actual is not None and actual == resolved_expected
        elif operation == "min":
            passed = actual >= resolved_expected
        elif operation == "max":
            passed = actual <= resolved_expected
        elif operation == "set_eq":
            passed = (
                isinstance(actual, list) and set(actual) == set(resolved_expected) and len(actual) == len(set(actual))
            )
        elif operation == "contains":
            passed = isinstance(actual, list) and set(resolved_expected).issubset(set(actual))
        else:
            raise ManifestError(f"unsupported policy operation: {operation}")
    except TypeError:
        passed = False
    return passed, _normalize_actual(actual), _normalize_actual(resolved_expected)


def _blocker(code: str, gate: str, detail: str) -> dict[str, str]:
    return {"code": code, "detail": detail, "gate": gate}


def _repository_gates(repo: Path, identity: dict[str, Any]) -> tuple[list[dict[str, str]], dict[str, Any]]:
    blockers: list[dict[str, str]] = []
    file_digests: dict[str, str | None] = {}
    for relative in REQUIRED_REPOSITORY_FILES:
        path = repo / relative
        try:
            file_digests[relative] = _sha256(_read_regular_bytes(path))
        except ManifestError:
            file_digests[relative] = None
            blockers.append(_blocker("required_repository_file_missing", "repository", relative))
    if identity["git_dirty"]:
        blockers.append(_blocker("repository_dirty", "repository", "final evidence requires a clean worktree"))
    return blockers, {"required_file_sha256": file_digests}


def _artifact_record(
    artifact_type: str,
    path: Path | None,
    *,
    as_of: datetime,
    repository: dict[str, Any],
    repo_details: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    if path is None:
        return {"artifact_type": artifact_type, "gate_passed": False, "status": "missing"}, [
            _blocker("artifact_missing", artifact_type, "required evidence was not supplied")
        ]
    blockers: list[dict[str, str]] = []
    try:
        artifact, digest = _read_artifact(path)
    except ManifestError as exc:
        return {"artifact_type": artifact_type, "gate_passed": False, "status": "invalid"}, [
            _blocker("artifact_invalid", artifact_type, str(exc))
        ]

    checks: list[dict[str, Any]] = []

    def record_common(name: str, passed: bool, actual: Any, expected: Any, code: str) -> None:
        checks.append({"actual": actual, "check": name, "expected": expected, "passed": passed})
        if not passed:
            blockers.append(_blocker(code, artifact_type, name))

    record_common(
        "schema_version",
        artifact.get("schema_version") == ARTIFACT_SCHEMA_VERSION,
        artifact.get("schema_version"),
        ARTIFACT_SCHEMA_VERSION,
        "artifact_schema_mismatch",
    )
    record_common(
        "artifact_type",
        artifact.get("artifact_type") == artifact_type,
        artifact.get("artifact_type"),
        artifact_type,
        "artifact_type_mismatch",
    )
    record_common("status", artifact.get("status") == "pass", artifact.get("status"), "pass", "artifact_failed")
    command = artifact.get("command")
    argv = command.get("argv") if isinstance(command, dict) else None
    exit_code = command.get("exit_code") if isinstance(command, dict) else None
    record_common(
        "command_recorded",
        isinstance(argv, list) and bool(argv) and all(isinstance(item, str) and item for item in argv),
        isinstance(argv, list) and bool(argv),
        True,
        "command_evidence_missing",
    )
    record_common("command_exit_code", exit_code == 0, exit_code, 0, "command_failed")

    artifact_repo = artifact.get("repository")
    for key in ("git_commit", "git_tree", "uv_lock_sha256"):
        actual = artifact_repo.get(key) if isinstance(artifact_repo, dict) else None
        record_common(
            f"repository.{key}",
            actual == repository[key],
            actual,
            repository[key],
            "repository_identity_mismatch",
        )
    artifact_dirty = artifact_repo.get("git_dirty") if isinstance(artifact_repo, dict) else None
    record_common("repository.git_dirty", artifact_dirty is False, artifact_dirty, False, "dirty_artifact")

    age_hours: float | None = None
    generated_text: str | None = None
    try:
        generated = _parse_timestamp(artifact.get("generated_at"))
        generated_text = _timestamp_text(generated)
        age = as_of - generated
        age_hours = round(age.total_seconds() / 3600, 6)
        not_future = age >= -FUTURE_TOLERANCE
        recent = age <= timedelta(hours=ARTIFACT_POLICY[artifact_type]["max_age_hours"])
        record_common("generated_at_not_future", not_future, age_hours, ">= -0.083333 hours", "artifact_from_future")
        record_common(
            "artifact_age_hours",
            recent,
            age_hours,
            f"<= {ARTIFACT_POLICY[artifact_type]['max_age_hours']}",
            "artifact_stale",
        )
    except (TypeError, ValueError):
        record_common("generated_at", False, None, "timezone-aware ISO-8601", "artifact_timestamp_invalid")

    for metric_path, operation, expected in ARTIFACT_POLICY[artifact_type]["checks"]:
        passed, actual, resolved_expected = _evaluate_rule(artifact, metric_path, operation, expected)
        check_name = f"{metric_path}:{operation}"
        checks.append(
            {
                "actual": actual,
                "check": check_name,
                "expected": resolved_expected,
                "passed": passed,
            }
        )
        if not passed:
            blockers.append(_blocker("threshold_failed", artifact_type, check_name))

    if artifact_type == "license_review":
        details = artifact.get("details")
        license_path = details.get("license_path") if isinstance(details, dict) else None
        license_sha = details.get("license_sha256") if isinstance(details, dict) else None
        expected_sha = repo_details["required_file_sha256"].get("LICENSE")
        passed = license_path == "LICENSE" and expected_sha is not None and license_sha == expected_sha
        checks.append(
            {
                "actual": {"license_path": license_path, "license_sha256": license_sha},
                "check": "license_file_binding",
                "expected": {"license_path": "LICENSE", "license_sha256": expected_sha},
                "passed": passed,
            }
        )
        if not passed:
            blockers.append(_blocker("license_binding_mismatch", artifact_type, "license_file_binding"))

    blockers.sort(key=lambda item: (item["gate"], item["code"], item["detail"]))
    return (
        {
            "age_hours": age_hours,
            "artifact_sha256": digest,
            "artifact_type": artifact_type,
            "checks": checks,
            "gate_passed": not blockers,
            "generated_at": generated_text,
            "status": "pass" if not blockers else "fail",
        },
        blockers,
    )


def _policy_manifest() -> dict[str, Any]:
    artifacts: dict[str, Any] = {}
    for artifact_type, policy in ARTIFACT_POLICY.items():
        artifacts[artifact_type] = {
            "checks": [
                {"field": path, "operation": operation, "value": expected}
                for path, operation, expected in policy["checks"]
            ],
            "max_age_hours": policy["max_age_hours"],
        }
    return {
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
        "artifacts": artifacts,
        "future_tolerance_minutes": int(FUTURE_TOLERANCE.total_seconds() / 60),
        "required_artifacts": list(ARTIFACT_POLICY),
        "required_repository_files": list(REQUIRED_REPOSITORY_FILES),
    }


def build_manifest(
    *,
    repo: Path,
    mode: str,
    as_of: datetime,
    artifact_paths: dict[str, Path | None],
) -> dict[str, Any]:
    repository_path, repository = _repository_identity(repo)
    blockers, repo_details = _repository_gates(repository_path, repository)
    records: list[dict[str, Any]] = []
    for artifact_type in ARTIFACT_POLICY:
        record, artifact_blockers = _artifact_record(
            artifact_type,
            artifact_paths.get(artifact_type),
            as_of=as_of,
            repository=repository,
            repo_details=repo_details,
        )
        records.append(record)
        blockers.extend(artifact_blockers)
    blockers.sort(key=lambda item: (item["gate"], item["code"], item["detail"]))
    ready = not blockers
    if mode == "final":
        status = "pass" if ready else "fail"
    else:
        status = "pre_release_ready_for_final" if ready else "pre_release_blocked"
    return {
        "artifacts": records,
        "as_of": _timestamp_text(as_of),
        "blockers": blockers,
        "mode": mode,
        "policy": _policy_manifest(),
        "release_ready": mode == "final" and ready,
        "repository": {**repository, **repo_details},
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "summary": {
            "artifact_gates_passed": sum(1 for item in records if item["gate_passed"]),
            "artifact_gates_required": len(records),
            "blocker_count": len(blockers),
        },
        "tool_version": TOOL_VERSION,
    }


def _manifest_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _validate_output_parent(path: Path) -> Path:
    absolute = path.absolute()
    try:
        parent = absolute.parent.resolve(strict=True)
    except OSError as exc:
        raise ManifestError("output parent is unavailable") from exc
    if parent != absolute.parent:
        raise ManifestError("output parent must not contain symlink indirection")
    metadata = parent.lstat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise ManifestError("output parent must be a directory")
    return parent / absolute.name


def _atomic_private_write(path: Path, data: bytes) -> None:
    destination = _validate_output_parent(path)
    if destination.exists() or destination.is_symlink():
        metadata = destination.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ManifestError("existing output must be a single-link regular file")
    temp = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    if temp.exists() or temp.is_symlink():
        raise ManifestError("private output temporary path already exists")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temp, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, destination)
        directory_descriptor = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        os.close(descriptor)
        if temp.exists():
            temp.unlink()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True, help="clean Git repository to bind")
    parser.add_argument("--mode", choices=("pre-release", "final"), required=True)
    parser.add_argument("--as-of", required=True, help="timezone-aware ISO-8601 validation time")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--license-review", type=Path)
    parser.add_argument("--full-tests", type=Path)
    parser.add_argument("--security-subset", type=Path)
    parser.add_argument("--coverage", type=Path)
    parser.add_argument("--four-variant-eval", type=Path)
    parser.add_argument("--deterministic-demo", type=Path)
    parser.add_argument("--clean-clone", type=Path)
    parser.add_argument("--hosted-ci", type=Path)
    parser.add_argument("--browser-acceptance", type=Path)
    return parser


def _fail(message: str) -> NoReturn:
    print(json.dumps({"error": message, "status": "error"}, sort_keys=True), file=sys.stderr)
    raise SystemExit(2)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        as_of = _parse_timestamp(args.as_of)
        artifact_paths = {
            artifact_type: getattr(args, argument_name) for artifact_type, argument_name in ARGUMENT_BY_ARTIFACT.items()
        }
        manifest = build_manifest(repo=args.repo, mode=args.mode, as_of=as_of, artifact_paths=artifact_paths)
        _atomic_private_write(args.output, _manifest_bytes(manifest))
    except (OSError, ValueError, ManifestError) as exc:
        _fail(str(exc))
    print(
        json.dumps(
            {
                "blocker_count": manifest["summary"]["blocker_count"],
                "release_ready": manifest["release_ready"],
                "status": manifest["status"],
            },
            sort_keys=True,
        )
    )
    return 0 if args.mode == "pre-release" or manifest["release_ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
