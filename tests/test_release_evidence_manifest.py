from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any


SCRIPT = Path(__file__).parents[1] / "scripts" / "build_release_evidence_manifest.py"
AS_OF = "2026-08-24T12:00:00Z"
ARTIFACT_TYPES = (
    "license_review",
    "full_tests",
    "security_subset",
    "coverage",
    "four_variant_eval",
    "deterministic_demo",
    "clean_clone",
    "hosted_ci",
    "browser_acceptance",
)


def _git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_EMAIL": "release-test@example.invalid",
        "GIT_AUTHOR_NAME": "Release Test",
        "GIT_COMMITTER_EMAIL": "release-test@example.invalid",
        "GIT_COMMITTER_NAME": "Release Test",
    }
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return result.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "--initial-branch=main")
    files = {
        ".github/workflows/ci.yml": "name: ci\n",
        "LICENSE": "MIT License\n\nSynthetic test fixture.\n",
        "README.md": "# MiniLLM\n",
        "SECURITY.md": "# Security\n",
        "docs/evaluation/offline_synthetic_retrieval_v1.md": "# Evaluation\n",
        "docs/portfolio/ai-assisted-development.md": "# AI-assisted development\n",
        "docs/roadmap/v0.2.0a1.md": "# Roadmap\n",
        "docs/security/threat_model.md": "# Threat model\n",
        "examples/public_demo/run_demo.py": "print('demo')\n",
        "pyproject.toml": "[project]\nname='minillm'\n",
        "scripts/audit_public_artifacts.py": "# fixture\n",
        "uv.lock": "version = 1\n",
    }
    for relative, content in files.items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "release candidate")
    return repo


def _identity(repo: Path) -> dict[str, Any]:
    lock = (repo / "uv.lock").read_bytes()
    return {
        "git_commit": _git(repo, "rev-parse", "HEAD^{commit}"),
        "git_tree": _git(repo, "rev-parse", "HEAD^{tree}"),
        "git_dirty": False,
        "uv_lock_sha256": hashlib.sha256(lock).hexdigest(),
    }


def _metrics(artifact_type: str) -> dict[str, Any]:
    values: dict[str, dict[str, Any]] = {
        "license_review": {
            "ownership_confirmed": True,
            "dependency_compatibility_confirmed": True,
            "repository_content_approved": True,
        },
        "full_tests": {"tests_passed": 619, "tests_failed": 0, "tests_errors": 0},
        "security_subset": {"tests_passed": 118, "tests_failed": 0, "tests_errors": 0},
        "coverage": {"coverage_percent": 74.46, "branch_coverage_enabled": True, "measured_files": 42},
        "four_variant_eval": {
            "case_count": 28,
            "hard_gate_case_count": 28,
            "hard_gate_pass_count": 28,
            "executed_variants": ["no_memory", "raw_fts", "vector_only", "governed_hybrid"],
            "fixture_immutable": True,
            "raw_canaries_reached_ingestion": True,
            "persisted_canaries_absent": True,
            "report_canaries_absent": True,
            "governed_mrr": 0.973684,
            "governed_precision_at_5": 0.231579,
            "governed_recall_at_5": 1.0,
            "governed_abstention_accuracy": 1.0,
            "governed_provenance_accuracy": 1.0,
            "governed_wrong_domain_rate": 0.0,
            "governed_stale_selection_rate": 0.0,
            "governed_sensitive_leakage_rate": 0.0,
            "governed_contract_pass_rate": 1.0,
        },
        "deterministic_demo": {
            "case_count": 28,
            "executed_variants": ["no_memory", "raw_fts", "vector_only", "governed_hybrid"],
            "canonical_evaluation_passed": True,
            "provenance_exposed": True,
            "wrong_domain_suppressed": True,
            "stale_or_superseded_suppressed": True,
            "no_answer_abstained": True,
            "unsupported_advice_refused": True,
            "failed_contract_visible": True,
            "network_required": False,
            "credentials_required": False,
            "private_data_used": False,
        },
        "clean_clone": {
            "fresh_clone": True,
            "locked_install_passed": True,
            "full_tests_passed": True,
            "demo_passed": True,
            "privacy_scan_passed": True,
            "single_parentless_public_commit": True,
        },
        "hosted_ci": {
            "failed_jobs": 0,
            "successful_jobs": 7,
            "lint_passed": True,
            "format_passed": True,
            "typecheck_passed": True,
            "security_subset_passed": True,
            "operating_systems": ["linux", "macos"],
            "python_versions": ["3.11", "3.12", "3.13", "3.14"],
        },
        "browser_acceptance": {
            "overall_result": "pass",
            "partial": False,
            "hard_failures": 0,
            "run_ids_retained": True,
            "trace_ids_retained": True,
            "report_sanitized": True,
            "memory_truth_mutated": False,
        },
    }
    return values[artifact_type]


def _artifacts(repo: Path, root: Path) -> dict[str, Path]:
    identity = _identity(repo)
    paths: dict[str, Path] = {}
    for artifact_type in ARTIFACT_TYPES:
        artifact: dict[str, Any] = {
            "schema_version": "minillm_release_evidence_artifact_v1",
            "artifact_type": artifact_type,
            "generated_at": "2026-08-24T11:00:00Z",
            "repository": identity,
            "command": {"argv": ["fixture", artifact_type], "exit_code": 0},
            "status": "pass",
            "metrics": _metrics(artifact_type),
        }
        if artifact_type == "license_review":
            artifact["details"] = {
                "license_path": "LICENSE",
                "license_sha256": hashlib.sha256((repo / "LICENSE").read_bytes()).hexdigest(),
                "spdx_id": "MIT",
            }
        path = root / f"{artifact_type}.json"
        path.write_text(json.dumps(artifact, sort_keys=True), encoding="utf-8")
        paths[artifact_type] = path
    return paths


def _command(repo: Path, output: Path, artifacts: dict[str, Path], *, mode: str) -> list[str]:
    command = [
        sys.executable,
        str(SCRIPT),
        "--repo",
        str(repo),
        "--mode",
        mode,
        "--as-of",
        AS_OF,
        "--output",
        str(output),
    ]
    for artifact_type, path in artifacts.items():
        command.extend([f"--{artifact_type.replace('_', '-')}", str(path)])
    return command


def _run(
    repo: Path,
    output: Path,
    artifacts: dict[str, Path],
    *,
    mode: str = "final",
    expected_code: int = 0,
) -> dict[str, Any]:
    result = subprocess.run(_command(repo, output, artifacts, mode=mode), capture_output=True, text=True)
    assert result.returncode == expected_code, result.stderr
    return json.loads(output.read_text(encoding="utf-8"))


def test_pre_release_lists_every_missing_artifact_without_claiming_pass(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    output = tmp_path / "manifest.json"

    manifest = _run(repo, output, {}, mode="pre-release")

    assert manifest["status"] == "pre_release_blocked"
    assert manifest["release_ready"] is False
    assert manifest["summary"]["artifact_gates_passed"] == 0
    assert {item["gate"] for item in manifest["blockers"] if item["code"] == "artifact_missing"} == set(ARTIFACT_TYPES)


def test_final_fails_closed_on_artifact_commit_mismatch(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    artifacts = _artifacts(repo, tmp_path)
    artifact = json.loads(artifacts["full_tests"].read_text(encoding="utf-8"))
    artifact["repository"]["git_commit"] = "0" * 40
    artifacts["full_tests"].write_text(json.dumps(artifact), encoding="utf-8")

    manifest = _run(repo, tmp_path / "manifest.json", artifacts, expected_code=1)

    assert manifest["status"] == "fail"
    assert any(
        item["gate"] == "full_tests" and item["code"] == "repository_identity_mismatch" for item in manifest["blockers"]
    )


def test_final_fails_closed_when_metric_misses_predeclared_threshold(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    artifacts = _artifacts(repo, tmp_path)
    artifact = json.loads(artifacts["coverage"].read_text(encoding="utf-8"))
    artifact["metrics"]["coverage_percent"] = 69.99
    artifacts["coverage"].write_text(json.dumps(artifact), encoding="utf-8")

    manifest = _run(repo, tmp_path / "manifest.json", artifacts, expected_code=1)

    failed = [item for item in manifest["blockers"] if item["gate"] == "coverage"]
    assert {item["code"] for item in failed} == {"threshold_failed"}
    assert manifest["policy"]["artifacts"]["coverage"]["checks"][0]["value"] == 70.0


def test_fully_synthetic_final_pass_is_deterministic_and_private(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    artifacts = _artifacts(repo, tmp_path)
    first = tmp_path / "manifest-one.json"
    second = tmp_path / "manifest-two.json"

    first_manifest = _run(repo, first, artifacts)
    _run(repo, second, artifacts)

    assert first_manifest["status"] == "pass"
    assert first_manifest["release_ready"] is True
    assert first_manifest["blockers"] == []
    assert first_manifest["summary"] == {
        "artifact_gates_passed": len(ARTIFACT_TYPES),
        "artifact_gates_required": len(ARTIFACT_TYPES),
        "blocker_count": 0,
    }
    assert first.read_bytes() == second.read_bytes()
    assert stat.S_IMODE(first.stat().st_mode) == 0o600


def test_final_rejects_stale_and_dirty_evidence(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    artifacts = _artifacts(repo, tmp_path)
    artifact = json.loads(artifacts["browser_acceptance"].read_text(encoding="utf-8"))
    artifact["generated_at"] = "2026-08-01T00:00:00Z"
    artifact["repository"]["git_dirty"] = True
    artifacts["browser_acceptance"].write_text(json.dumps(artifact), encoding="utf-8")

    manifest = _run(repo, tmp_path / "manifest.json", artifacts, expected_code=1)

    browser_codes = {item["code"] for item in manifest["blockers"] if item["gate"] == "browser_acceptance"}
    assert browser_codes == {"artifact_stale", "dirty_artifact"}
