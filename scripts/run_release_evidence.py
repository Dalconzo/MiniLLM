#!/usr/bin/env python3
"""Run MiniLLM's local release gates and emit commit-bound evidence artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import tomllib
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, NoReturn, Sequence


ARTIFACT_SCHEMA_VERSION = "minillm_release_evidence_artifact_v1"
RUNNER_SCHEMA_VERSION = "minillm_local_release_evidence_run_v1"
RUNNER_VERSION = "1.0"
LOCAL_ARTIFACT_TYPES = (
    "full_tests",
    "security_subset",
    "coverage",
    "four_variant_eval",
    "deterministic_demo",
)
EXPECTED_VARIANTS = ("no_memory", "raw_fts", "vector_only", "governed_hybrid")
REQUIRED_TRACKED_INPUTS = (
    "scripts/audit_public_artifacts.py",
    "scripts/run_release_evidence.py",
    "scripts/run_security_tests.sh",
    "scripts/run_evals.sh",
    "examples/public_demo/run_demo.py",
    "pyproject.toml",
    "uv.lock",
)


class EvidenceRunnerError(RuntimeError):
    """A release-evidence run cannot make a defensible claim."""


@dataclass(frozen=True)
class CommandPlan:
    """Injectable command templates; placeholders are expanded at execution time."""

    full_tests: tuple[tuple[str, ...], ...]
    security_subset: tuple[tuple[str, ...], ...]
    coverage: tuple[tuple[str, ...], ...]
    four_variant_eval: tuple[tuple[str, ...], ...]
    deterministic_demo: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    evidence_argv: tuple[str, ...]
    exit_code: int
    stdout: bytes
    stderr: bytes
    environment: Mapping[str, str]
    evidence_environment: Mapping[str, str]

    def evidence(self) -> dict[str, Any]:
        return {
            "argv": list(self.evidence_argv),
            "environment": dict(sorted(self.evidence_environment.items())),
            "exit_code": self.exit_code,
            "stderr_bytes": len(self.stderr),
            "stderr_sha256": _sha256(self.stderr),
            "stdout_bytes": len(self.stdout),
            "stdout_sha256": _sha256(self.stdout),
        }


def default_command_plan() -> CommandPlan:
    """Return the canonical commands; paths are supplied as runtime placeholders."""
    return CommandPlan(
        full_tests=(("uv", "run", "--frozen", "pytest", "-q", "--junitxml", "{full_junit}"),),
        security_subset=(("bash", "scripts/run_security_tests.sh"),),
        coverage=(
            ("uv", "run", "--frozen", "coverage", "erase", "--rcfile=pyproject.toml"),
            (
                "uv",
                "run",
                "--frozen",
                "coverage",
                "run",
                "--rcfile=pyproject.toml",
                "-m",
                "pytest",
                "-q",
                "--junitxml",
                "{coverage_junit}",
            ),
            (
                "uv",
                "run",
                "--frozen",
                "coverage",
                "json",
                "--rcfile=pyproject.toml",
                "-o",
                "{coverage_json}",
            ),
        ),
        four_variant_eval=(("bash", "scripts/run_evals.sh", "{eval_output_dir}"),),
        deterministic_demo=(("uv", "run", "--frozen", "python", "examples/public_demo/run_demo.py", "--json"),),
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ("git", "-C", str(repo), *args),
            check=check,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = (
            exc.stderr.decode("utf-8", "replace").strip()
            if isinstance(exc, subprocess.CalledProcessError)
            else str(exc)
        )
        raise EvidenceRunnerError(f"git command failed: {' '.join(args)}: {detail}") from exc


def _git_text(repo: Path, *args: str) -> str:
    return _git(repo, *args).stdout.decode("utf-8", "strict").strip()


def _repository_identity(repo_argument: Path) -> tuple[Path, dict[str, Any]]:
    candidate = repo_argument.resolve(strict=True)
    repo = Path(_git_text(candidate, "rev-parse", "--show-toplevel")).resolve(strict=True)
    if candidate != repo:
        raise EvidenceRunnerError("--repo must identify the Git top level")
    lock_path = repo / "uv.lock"
    if not lock_path.is_file() or lock_path.is_symlink():
        raise EvidenceRunnerError("uv.lock must be a non-symlinked regular file")
    return repo, {
        "git_commit": _git_text(repo, "rev-parse", "HEAD^{commit}"),
        "git_tree": _git_text(repo, "rev-parse", "HEAD^{tree}"),
        "git_dirty": bool(_git_text(repo, "status", "--porcelain=v1", "--untracked-files=all")),
        "uv_lock_sha256": _sha256(lock_path.read_bytes()),
    }


def _tracked_inputs(repo: Path) -> dict[str, bool]:
    return {
        relative: _git(repo, "ls-files", "--error-unmatch", "--", relative, check=False).returncode == 0
        for relative in REQUIRED_TRACKED_INPUTS
    }


def _prepare_output_dir(path: Path, *, repo: Path, mode: str) -> Path:
    absolute = path.expanduser().absolute()
    if not absolute.exists():
        try:
            parent = absolute.parent.resolve(strict=True)
        except OSError as exc:
            raise EvidenceRunnerError("artifact directory parent is unavailable") from exc
        if parent != absolute.parent:
            raise EvidenceRunnerError("artifact directory parent must not contain symlink indirection")
        os.mkdir(absolute, 0o700)
    try:
        resolved = absolute.resolve(strict=True)
        metadata = absolute.lstat()
    except OSError as exc:
        raise EvidenceRunnerError("artifact directory is unavailable") from exc
    if resolved != absolute or not stat.S_ISDIR(metadata.st_mode):
        raise EvidenceRunnerError("artifact directory must be a non-symlinked directory")
    if mode == "final" and (resolved == repo or resolved.is_relative_to(repo)):
        raise EvidenceRunnerError("final evidence must be written outside the repository")
    return resolved


def _atomic_private_json(path: Path, value: Mapping[str, Any]) -> None:
    destination = path.absolute()
    parent = destination.parent.resolve(strict=True)
    if parent != destination.parent:
        raise EvidenceRunnerError("artifact output parent must not contain symlink indirection")
    if destination.exists() or destination.is_symlink():
        metadata = destination.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise EvidenceRunnerError("existing artifact must be a single-link regular file")
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    if temporary.exists() or temporary.is_symlink():
        raise EvidenceRunnerError("artifact temporary path already exists")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        os.chmod(destination, 0o600, follow_symlinks=False)
        directory_descriptor = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        os.close(descriptor)
        if temporary.exists():
            temporary.unlink()


def _expand(command: Sequence[str], values: Mapping[str, Path]) -> tuple[str, ...]:
    substitutions = {key: str(value) for key, value in values.items()}
    return tuple(part.format_map(substitutions) for part in command)


def _portable_text(value: str, *, repo: Path, replacements: Mapping[str, str]) -> str:
    """Replace execution-only paths before command evidence is constructed."""
    repo_text = str(repo)
    if value == repo_text:
        return "."
    if value.startswith(f"{repo_text}{os.sep}"):
        return value[len(repo_text) + 1 :].replace(os.sep, "/")
    portable = value
    for actual, label in sorted(replacements.items(), key=lambda item: len(item[0]), reverse=True):
        portable = portable.replace(actual, label)
    if os.path.isabs(portable):
        return "<runtime-executable>" if os.access(value, os.X_OK) else "<runtime-path>"
    return portable


def _portable_command(
    argv: Sequence[str],
    *,
    repo: Path,
    replacements: Mapping[str, str],
) -> tuple[str, ...]:
    return tuple(_portable_text(part, repo=repo, replacements=replacements) for part in argv)


def _run_commands(
    repo: Path,
    commands: Sequence[Sequence[str]],
    *,
    values: Mapping[str, Path],
    environment: Mapping[str, str] | None = None,
) -> list[CommandResult]:
    overrides = dict(environment or {})
    command_environment = {**os.environ, **overrides}
    replacements = {str(path): f"<scratch:{key.replace('_', '-')}>" for key, path in values.items()}
    results: list[CommandResult] = []
    for template in commands:
        argv = _expand(template, values)
        try:
            completed = subprocess.run(argv, cwd=repo, env=command_environment, capture_output=True, check=False)
        except OSError as exc:
            raise EvidenceRunnerError(f"command could not start: {argv[0]}: {exc}") from exc
        results.append(
            CommandResult(
                argv=argv,
                evidence_argv=_portable_command(argv, repo=repo, replacements=replacements),
                exit_code=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
                environment=overrides,
                evidence_environment={
                    key: _portable_text(value, repo=repo, replacements=replacements) for key, value in overrides.items()
                },
            )
        )
        if completed.returncode != 0:
            break
    return results


def _require_commands_passed(stage: str, results: Sequence[CommandResult]) -> None:
    """Report the failing stage without persisting potentially sensitive output."""
    if not results:
        raise EvidenceRunnerError(f"{stage} executed no commands")
    for index, result in enumerate(results, start=1):
        if result.exit_code == 0:
            continue
        raise EvidenceRunnerError(
            f"{stage} command {index} exited {result.exit_code}; "
            f"stdout_bytes={len(result.stdout)} stdout_sha256={_sha256(result.stdout)}; "
            f"stderr_bytes={len(result.stderr)} stderr_sha256={_sha256(result.stderr)}"
        )


def _junit_metrics(path: Path) -> dict[str, int]:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise EvidenceRunnerError("pytest did not produce valid JUnit XML") from exc
    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    if not suites:
        raise EvidenceRunnerError("JUnit XML contains no test suites")
    total = sum(int(suite.attrib.get("tests", "0")) for suite in suites)
    failed = sum(int(suite.attrib.get("failures", "0")) for suite in suites)
    errors = sum(int(suite.attrib.get("errors", "0")) for suite in suites)
    skipped = sum(int(suite.attrib.get("skipped", "0")) for suite in suites)
    if min(total, failed, errors, skipped) < 0 or failed + errors + skipped > total:
        raise EvidenceRunnerError("JUnit XML contains inconsistent counts")
    return {
        "tests_passed": total - failed - errors - skipped,
        "tests_failed": failed,
        "tests_errors": errors,
        "tests_skipped": skipped,
        "tests_collected": total,
    }


def _read_json(path: Path, description: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise EvidenceRunnerError(f"{description} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise EvidenceRunnerError(f"{description} must contain a JSON object")
    return value


def _coverage_metrics(report_path: Path, config_path: Path, junit_path: Path) -> dict[str, Any]:
    report = _read_json(report_path, "coverage report")
    meta = report.get("meta")
    totals = report.get("totals")
    files = report.get("files")
    if not isinstance(meta, dict) or not isinstance(totals, dict) or not isinstance(files, dict):
        raise EvidenceRunnerError("coverage report is missing meta, totals, or files")
    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
        coverage_config = config["tool"]["coverage"]
        run_config = coverage_config["run"]
        report_config = coverage_config.get("report", {})
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, KeyError, TypeError) as exc:
        raise EvidenceRunnerError("pyproject.toml does not contain a valid coverage configuration") from exc
    branch_enabled = meta.get("branch_coverage") is True and run_config.get("branch") is True
    percent = totals.get("percent_covered")
    if isinstance(percent, bool) or not isinstance(percent, (int, float)):
        raise EvidenceRunnerError("coverage report has no numeric percent_covered")
    test_metrics = _junit_metrics(junit_path)
    return {
        "coverage_percent": round(float(percent), 6),
        "branch_coverage_enabled": branch_enabled,
        "measured_files": len(files),
        "configured_source": run_config.get("source", []),
        "configured_omit": run_config.get("omit", []),
        "configured_exclusions": report_config.get("exclude_also", report_config.get("exclude_lines", [])),
        **test_metrics,
    }


def _eval_metrics(report: dict[str, Any]) -> dict[str, Any]:
    release = report.get("release_assessment")
    variants = report.get("variants")
    fixture = report.get("fixture_integrity")
    if not isinstance(release, dict) or not isinstance(variants, list) or not isinstance(fixture, dict):
        raise EvidenceRunnerError("evaluation report is missing release, variant, or fixture evidence")
    executed = [item.get("variant") for item in variants if isinstance(item, dict) and item.get("executed") is True]
    governed = next(
        (item for item in variants if isinstance(item, dict) and item.get("variant") == "governed_hybrid"),
        None,
    )
    governed_metrics = governed.get("metrics") if isinstance(governed, dict) else None
    if not isinstance(governed_metrics, dict):
        raise EvidenceRunnerError("evaluation report has no governed_hybrid metrics")
    hard_gate_count = release.get("hard_gate_case_count")
    failed_ids = release.get("failed_hard_gate_case_ids")
    if not isinstance(hard_gate_count, int) or not isinstance(failed_ids, list):
        raise EvidenceRunnerError("evaluation report has invalid hard-gate evidence")
    return {
        "case_count": report.get("case_count"),
        "hard_gate_case_count": hard_gate_count,
        "hard_gate_pass_count": hard_gate_count - len(failed_ids),
        "executed_variants": executed,
        "fixture_immutable": release.get("fixture_immutable") is True,
        "raw_canaries_reached_ingestion": release.get("raw_canaries_reached_ingestion") is True,
        "persisted_canaries_absent": release.get("persisted_canaries_absent") is True,
        "report_canaries_absent": release.get("report_canaries_absent") is True,
        "governed_mrr": governed_metrics.get("mrr"),
        "governed_precision_at_5": governed_metrics.get("precision_at_5"),
        "governed_recall_at_5": governed_metrics.get("recall_at_5"),
        "governed_abstention_accuracy": governed_metrics.get("abstention_accuracy"),
        "governed_provenance_accuracy": governed_metrics.get("provenance_accuracy"),
        "governed_wrong_domain_rate": governed_metrics.get("wrong_domain_rate"),
        "governed_stale_selection_rate": governed_metrics.get("stale_selection_rate"),
        "governed_sensitive_leakage_rate": governed_metrics.get("sensitive_leakage_rate"),
        "governed_contract_pass_rate": governed_metrics.get("retrieval_contract_pass_rate"),
    }


def _demo_metrics(report: dict[str, Any]) -> dict[str, Any]:
    core = report.get("canonical_core")
    checks = report.get("checks")
    runtime = report.get("runtime_contract")
    if not isinstance(core, dict) or not isinstance(checks, dict) or not isinstance(runtime, dict):
        raise EvidenceRunnerError("demo report is missing canonical-core, checks, or runtime evidence")
    return {
        "case_count": core.get("case_count"),
        "executed_variants": core.get("executed_variants"),
        "canonical_evaluation_passed": checks.get("canonical_evaluation_passed") is True,
        "provenance_exposed": checks.get("provenance_exposed") is True,
        "wrong_domain_suppressed": checks.get("wrong_domain_suppressed") is True,
        "stale_or_superseded_suppressed": checks.get("stale_or_superseded_suppressed") is True,
        "no_answer_abstained": checks.get("no_answer_abstained") is True,
        "unsupported_advice_refused": checks.get("unsupported_advice_refused") is True,
        "failed_contract_visible": checks.get("failed_contract_visible") is True,
        "network_required": runtime.get("network_required"),
        "credentials_required": runtime.get("credentials_required"),
        "private_data_used": runtime.get("private_data_used"),
    }


def _primary_command(artifact_type: str, results: Sequence[CommandResult]) -> CommandResult:
    if not results:
        raise EvidenceRunnerError(f"{artifact_type} executed no commands")
    if artifact_type == "coverage" and len(results) > 1:
        return results[1]
    return results[0]


def _artifact(
    artifact_type: str,
    *,
    mode: str,
    repository: Mapping[str, Any],
    results: Sequence[CommandResult],
    metrics: Mapping[str, Any],
    semantic_passed: bool,
    tracked_inputs: Mapping[str, bool],
) -> dict[str, Any]:
    primary = _primary_command(artifact_type, results)
    commands_passed = bool(results) and all(result.exit_code == 0 for result in results)
    final_eligible = mode == "final" and commands_passed and semantic_passed and not repository["git_dirty"]
    return {
        "artifact_type": artifact_type,
        "command": {"argv": list(primary.evidence_argv), "exit_code": primary.exit_code},
        "details": {
            "canonical_inputs_committed": all(tracked_inputs.values()),
            "commands": [result.evidence() for result in results],
            "evidence_mode": mode,
            "final_manifest_eligible": final_eligible,
            "tracked_inputs": dict(sorted(tracked_inputs.items())),
        },
        "generated_at": _timestamp(),
        "metrics": dict(metrics),
        "repository": dict(repository),
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "status": "pass" if final_eligible else ("development_pass" if commands_passed and semantic_passed else "fail"),
    }


def run_release_evidence(
    *,
    repo: Path,
    output_dir: Path,
    mode: str,
    command_plan: CommandPlan | None = None,
) -> dict[str, Any]:
    """Execute local gates and write one manifest-compatible artifact per gate."""
    if mode not in {"development", "final"}:
        raise EvidenceRunnerError("mode must be development or final")
    repository_path, identity = _repository_identity(repo)
    destination = _prepare_output_dir(output_dir, repo=repository_path, mode=mode)
    tracked_inputs = _tracked_inputs(repository_path)
    if mode == "final":
        if identity["git_dirty"]:
            raise EvidenceRunnerError("final evidence requires a clean repository before commands run")
        missing = [path for path, tracked in tracked_inputs.items() if not tracked]
        if missing:
            raise EvidenceRunnerError(f"final evidence inputs are not committed: {', '.join(missing)}")

    plan = command_plan or default_command_plan()
    artifacts: dict[str, dict[str, Any]] = {}
    artifact_paths: dict[str, str] = {}
    with tempfile.TemporaryDirectory(prefix="minillm-release-evidence-") as temporary_text:
        work = Path(temporary_text)
        values = {
            "full_junit": work / "full-tests.junit.xml",
            "security_junit": work / "security-subset.junit.xml",
            "coverage_junit": work / "coverage.junit.xml",
            "coverage_json": work / "coverage.json",
            "eval_output_dir": work / "evaluation",
        }
        values["eval_output_dir"].mkdir(mode=0o700)

        full_results = _run_commands(repository_path, plan.full_tests, values=values)
        _require_commands_passed("full_tests", full_results)
        full_metrics = _junit_metrics(values["full_junit"])
        artifacts["full_tests"] = _artifact(
            "full_tests",
            mode=mode,
            repository=identity,
            results=full_results,
            metrics=full_metrics,
            semantic_passed=full_metrics["tests_failed"] == full_metrics["tests_errors"] == 0,
            tracked_inputs=tracked_inputs,
        )

        security_results = _run_commands(
            repository_path,
            plan.security_subset,
            values=values,
            environment={"PYTEST_ADDOPTS": f"--junitxml={values['security_junit']}"},
        )
        _require_commands_passed("security_subset", security_results)
        security_metrics = _junit_metrics(values["security_junit"])
        artifacts["security_subset"] = _artifact(
            "security_subset",
            mode=mode,
            repository=identity,
            results=security_results,
            metrics=security_metrics,
            semantic_passed=security_metrics["tests_failed"] == security_metrics["tests_errors"] == 0,
            tracked_inputs=tracked_inputs,
        )

        coverage_results = _run_commands(
            repository_path,
            plan.coverage,
            values=values,
            environment={"COVERAGE_FILE": str(work / ".coverage")},
        )
        _require_commands_passed("coverage", coverage_results)
        coverage_metrics = _coverage_metrics(
            values["coverage_json"], repository_path / "pyproject.toml", values["coverage_junit"]
        )
        artifacts["coverage"] = _artifact(
            "coverage",
            mode=mode,
            repository=identity,
            results=coverage_results,
            metrics=coverage_metrics,
            semantic_passed=(
                coverage_metrics["branch_coverage_enabled"]
                and coverage_metrics["tests_failed"] == coverage_metrics["tests_errors"] == 0
            ),
            tracked_inputs=tracked_inputs,
        )

        eval_results = _run_commands(repository_path, plan.four_variant_eval, values=values)
        _require_commands_passed("four_variant_eval", eval_results)
        eval_report = _read_json(values["eval_output_dir"] / "memory-eval-v1.json", "evaluation report")
        eval_metrics = _eval_metrics(eval_report)
        eval_semantic_passed = (
            eval_report.get("status") == "pass"
            and eval_report.get("release_assessment", {}).get("status") == "pass"
            and set(eval_metrics["executed_variants"]) == set(EXPECTED_VARIANTS)
        )
        artifacts["four_variant_eval"] = _artifact(
            "four_variant_eval",
            mode=mode,
            repository=identity,
            results=eval_results,
            metrics=eval_metrics,
            semantic_passed=eval_semantic_passed,
            tracked_inputs=tracked_inputs,
        )

        demo_results = _run_commands(repository_path, plan.deterministic_demo, values=values)
        _require_commands_passed("deterministic_demo", demo_results)
        try:
            demo_report = json.loads(demo_results[-1].stdout.decode("utf-8", "strict"))
        except (IndexError, UnicodeDecodeError, ValueError) as exc:
            raise EvidenceRunnerError("demo did not emit valid UTF-8 JSON") from exc
        if not isinstance(demo_report, dict):
            raise EvidenceRunnerError("demo output must be a JSON object")
        demo_metrics = _demo_metrics(demo_report)
        demo_semantic_passed = demo_report.get("status") == "pass" and all(
            demo_metrics[key] is True
            for key in (
                "canonical_evaluation_passed",
                "provenance_exposed",
                "wrong_domain_suppressed",
                "stale_or_superseded_suppressed",
                "no_answer_abstained",
                "unsupported_advice_refused",
                "failed_contract_visible",
            )
        )
        artifacts["deterministic_demo"] = _artifact(
            "deterministic_demo",
            mode=mode,
            repository=identity,
            results=demo_results,
            metrics=demo_metrics,
            semantic_passed=demo_semantic_passed,
            tracked_inputs=tracked_inputs,
        )

    _, final_identity = _repository_identity(repository_path)
    if final_identity != identity:
        raise EvidenceRunnerError("repository identity changed while release evidence was running")

    for artifact_type in LOCAL_ARTIFACT_TYPES:
        filename = f"{artifact_type.replace('_', '-')}.json"
        path = destination / filename
        _atomic_private_json(path, artifacts[artifact_type])
        artifact_paths[artifact_type] = filename
    final_passed = all(artifact["status"] == "pass" for artifact in artifacts.values())
    summary = {
        "artifact_paths": artifact_paths,
        "artifacts_succeeded": sum(
            artifact["status"] in {"pass", "development_pass"} for artifact in artifacts.values()
        ),
        "artifacts_passed": sum(artifact["status"] == "pass" for artifact in artifacts.values()),
        "artifacts_run": len(artifacts),
        "final_manifest_eligible": mode == "final" and final_passed,
        "mode": mode,
        "repository": identity,
        "runner_schema_version": RUNNER_SCHEMA_VERSION,
        "runner_version": RUNNER_VERSION,
        "status": "pass" if final_passed else ("development_complete" if mode == "development" else "fail"),
    }
    _atomic_private_json(destination / "local-release-evidence-run.json", summary)
    return summary


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--mode", choices=("development", "final"), required=True)
    return parser


def _fail(message: str) -> NoReturn:
    print(json.dumps({"error": message, "status": "error"}, sort_keys=True), file=sys.stderr)
    raise SystemExit(2)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        summary = run_release_evidence(repo=args.repo, output_dir=args.output_dir, mode=args.mode)
    except (EvidenceRunnerError, OSError, ValueError) as exc:
        _fail(str(exc))
    print(json.dumps(summary, sort_keys=True))
    return 0 if summary["status"] in {"pass", "development_complete"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
