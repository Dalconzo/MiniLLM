from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER_SCRIPT = REPO_ROOT / "scripts" / "run_release_evidence.py"
MANIFEST_SCRIPT = REPO_ROOT / "scripts" / "build_release_evidence_manifest.py"


def _load_runner() -> ModuleType:
    spec = importlib.util.spec_from_file_location("minillm_release_evidence_runner", RUNNER_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


RUNNER = _load_runner()


def _git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_EMAIL": "runner-test@example.invalid",
        "GIT_AUTHOR_NAME": "Runner Test",
        "GIT_COMMITTER_EMAIL": "runner-test@example.invalid",
        "GIT_COMMITTER_NAME": "Runner Test",
    }
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return result.stdout.strip()


def _helper_source() -> str:
    return r"""
import json
import sys
from pathlib import Path


def junit(path, count):
    Path(path).write_text(
        f'<testsuites tests="{count}" failures="0" errors="0" skipped="0">'
        f'<testsuite tests="{count}" failures="0" errors="0" skipped="0" />'
        '</testsuites>',
        encoding='utf-8',
    )


mode = sys.argv[1]
if mode == 'junit':
    junit(sys.argv[2], int(sys.argv[3]))
elif mode == 'coverage':
    junit(sys.argv[2], 180)
    Path(sys.argv[3]).write_text(json.dumps({
        'meta': {'branch_coverage': True},
        'files': {'src/a.py': {}, 'src/b.py': {}},
        'totals': {'percent_covered': 75.25},
    }), encoding='utf-8')
elif mode == 'eval':
    output = Path(sys.argv[2])
    output.mkdir(exist_ok=True)
    metrics = {
        'mrr': 0.9,
        'precision_at_5': 0.25,
        'recall_at_5': 0.9,
        'abstention_accuracy': 1.0,
        'provenance_accuracy': 1.0,
        'wrong_domain_rate': 0.0,
        'stale_selection_rate': 0.0,
        'sensitive_leakage_rate': 0.0,
        'retrieval_contract_pass_rate': 1.0,
    }
    variants = [
        {'variant': name, 'executed': True, 'executed_case_count': 28, 'metrics': metrics}
        for name in ['no_memory', 'raw_fts', 'vector_only', 'governed_hybrid']
    ]
    report = {
        'status': 'pass',
        'case_count': 28,
        'release_assessment': {
            'status': 'pass',
            'hard_gate_case_count': 28,
            'failed_hard_gate_case_ids': [],
            'fixture_immutable': True,
            'raw_canaries_reached_ingestion': True,
            'persisted_canaries_absent': True,
            'report_canaries_absent': True,
        },
        'fixture_integrity': {},
        'variants': variants,
    }
    (output / 'memory-eval-v1.json').write_text(json.dumps(report), encoding='utf-8')
elif mode == 'demo':
    print(json.dumps({
        'status': 'pass',
        'canonical_core': {
            'case_count': 28,
            'executed_variants': ['no_memory', 'raw_fts', 'vector_only', 'governed_hybrid'],
        },
        'checks': {
            'canonical_evaluation_passed': True,
            'provenance_exposed': True,
            'wrong_domain_suppressed': True,
            'stale_or_superseded_suppressed': True,
            'no_answer_abstained': True,
            'unsupported_advice_refused': True,
            'failed_contract_visible': True,
        },
        'runtime_contract': {
            'network_required': False,
            'credentials_required': False,
            'private_data_used': False,
        },
    }))
elif mode == 'marker':
    Path(sys.argv[2]).write_text('executed', encoding='utf-8')
else:
    raise SystemExit(2)
""".lstrip()


def _repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "--initial-branch=main")
    helper = repo / "tests" / "evidence_helper.py"
    files = {
        ".github/workflows/ci.yml": "name: ci\n",
        "LICENSE": "MIT License\n\nSynthetic test fixture.\n",
        "README.md": "# MiniLLM\n",
        "SECURITY.md": "# Security\n",
        "docs/evaluation/offline_synthetic_retrieval_v1.md": "# Evaluation\n",
        "docs/portfolio/ai-assisted-development.md": "# AI-assisted development\n",
        "docs/roadmap/v0.2.0a1.md": "# Roadmap\n",
        "docs/security/threat_model.md": "# Threat model\n",
        "examples/public_demo/run_demo.py": "print('fixture')\n",
        "scripts/run_evals.sh": "#!/bin/sh\nexit 0\n",
        "scripts/audit_public_artifacts.py": "# fixture\n",
        "scripts/run_release_evidence.py": "# fixture\n",
        "scripts/run_security_tests.sh": "#!/bin/sh\nexit 0\n",
        "tests/evidence_helper.py": _helper_source(),
        "pyproject.toml": (
            "[project]\nname='minillm'\nversion='0.0.0'\n"
            "[tool.coverage.run]\nbranch=true\nsource=['local_agent_lab']\n"
            "[tool.coverage.report]\nfail_under=70\nexclude_also=['pragma: no cover']\n"
        ),
        "uv.lock": "version = 1\n",
    }
    for relative, content in files.items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "release candidate")
    return repo, helper


def _plan(helper: Path) -> Any:
    python = sys.executable
    return RUNNER.CommandPlan(
        full_tests=((python, str(helper), "junit", "{full_junit}", "200"),),
        security_subset=((python, str(helper), "junit", "{security_junit}", "30"),),
        coverage=((python, str(helper), "coverage", "{coverage_junit}", "{coverage_json}"),),
        four_variant_eval=((python, str(helper), "eval", "{eval_output_dir}"),),
        deterministic_demo=((python, str(helper), "demo"),),
    )


def _run_final(tmp_path: Path) -> tuple[Path, Path, dict[str, Any]]:
    repo, helper = _repo(tmp_path)
    output = tmp_path / "evidence"
    summary = RUNNER.run_release_evidence(
        repo=repo,
        output_dir=output,
        mode="final",
        command_plan=_plan(helper),
    )
    return repo, output, summary


def test_final_runner_executes_and_writes_private_machine_evidence(tmp_path: Path) -> None:
    _, output, summary = _run_final(tmp_path)

    assert summary["status"] == "pass"
    assert summary["final_manifest_eligible"] is True
    assert summary["artifacts_succeeded"] == summary["artifacts_run"] == 5
    assert summary["artifacts_passed"] == summary["artifacts_run"] == 5
    for artifact_type, path_text in summary["artifact_paths"].items():
        path = output / path_text
        artifact = json.loads(path.read_text(encoding="utf-8"))
        assert path.parent == output
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert artifact["artifact_type"] == artifact_type
        assert artifact["schema_version"] == "minillm_release_evidence_artifact_v1"
        assert artifact["status"] == "pass"
        assert artifact["repository"]["git_dirty"] is False
        assert artifact["details"]["canonical_inputs_committed"] is True
        assert artifact["details"]["final_manifest_eligible"] is True
        assert artifact["command"] == {
            "argv": artifact["details"]["commands"][0]["argv"],
            "exit_code": 0,
        }
        assert "stdout" not in artifact["details"]["commands"][0]
        assert "stderr" not in artifact["details"]["commands"][0]
        serialized = path.read_text(encoding="utf-8")
        assert str(tmp_path) not in serialized
        assert "/Users/" not in serialized
        assert "/private/" + "tmp/" not in serialized

    assert json.loads((output / "full-tests.json").read_text())["metrics"]["tests_passed"] == 200
    assert json.loads((output / "security-subset.json").read_text())["metrics"]["tests_passed"] == 30
    coverage = json.loads((output / "coverage.json").read_text())["metrics"]
    assert coverage["coverage_percent"] == 75.25
    assert coverage["branch_coverage_enabled"] is True
    assert coverage["configured_exclusions"] == ["pragma: no cover"]


def test_final_runner_rejects_dirty_repo_before_any_command(tmp_path: Path) -> None:
    repo, helper = _repo(tmp_path)
    marker = tmp_path / "marker"
    (repo / "README.md").write_text("dirty\n", encoding="utf-8")
    marker_plan = RUNNER.CommandPlan(
        full_tests=((sys.executable, str(helper), "marker", str(marker)),),
        security_subset=(),
        coverage=(),
        four_variant_eval=(),
        deterministic_demo=(),
    )

    try:
        RUNNER.run_release_evidence(
            repo=repo,
            output_dir=tmp_path / "evidence",
            mode="final",
            command_plan=marker_plan,
        )
    except RUNNER.EvidenceRunnerError as exc:
        assert "clean repository" in str(exc)
    else:
        raise AssertionError("dirty final run unexpectedly succeeded")
    assert not marker.exists()


def test_development_evidence_is_explicitly_ineligible_for_final_manifest(tmp_path: Path) -> None:
    repo, helper = _repo(tmp_path)
    (repo / "README.md").write_text("dirty\n", encoding="utf-8")

    summary = RUNNER.run_release_evidence(
        repo=repo,
        output_dir=tmp_path / "evidence",
        mode="development",
        command_plan=_plan(helper),
    )

    assert summary["status"] == "development_complete"
    assert summary["final_manifest_eligible"] is False
    assert summary["artifacts_succeeded"] == summary["artifacts_run"] == 5
    assert summary["artifacts_passed"] == 0
    artifact = json.loads((tmp_path / "evidence" / summary["artifact_paths"]["full_tests"]).read_text(encoding="utf-8"))
    assert artifact["status"] == "development_pass"
    assert artifact["repository"]["git_dirty"] is True
    assert artifact["details"]["evidence_mode"] == "development"
    assert artifact["details"]["final_manifest_eligible"] is False


def test_command_failure_reports_stage_and_digests_before_artifact_parsing(tmp_path: Path) -> None:
    repo, helper = _repo(tmp_path)
    plan = _plan(helper)
    failed_plan = RUNNER.CommandPlan(
        full_tests=(
            (
                sys.executable,
                "-c",
                "import sys; sys.stderr.write('cache denied'); raise SystemExit(23)",
            ),
        ),
        security_subset=plan.security_subset,
        coverage=plan.coverage,
        four_variant_eval=plan.four_variant_eval,
        deterministic_demo=plan.deterministic_demo,
    )

    try:
        RUNNER.run_release_evidence(
            repo=repo,
            output_dir=tmp_path / "evidence",
            mode="final",
            command_plan=failed_plan,
        )
    except RUNNER.EvidenceRunnerError as exc:
        message = str(exc)
        assert "full_tests command 1 exited 23" in message
        assert "stdout_bytes=0" in message
        assert "stderr_bytes=12" in message
        assert "stderr_sha256=" in message
        assert "cache denied" not in message
        assert "JUnit" not in message
    else:
        raise AssertionError("failed evidence command unexpectedly passed")


def test_five_final_artifacts_pass_the_real_manifest_contract(tmp_path: Path) -> None:
    repo, output, _ = _run_final(tmp_path)
    manifest = tmp_path / "manifest.json"
    as_of = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    command = [
        sys.executable,
        str(MANIFEST_SCRIPT),
        "--repo",
        str(repo),
        "--mode",
        "pre-release",
        "--as-of",
        as_of,
        "--output",
        str(manifest),
    ]
    for artifact_type in RUNNER.LOCAL_ARTIFACT_TYPES:
        command.extend(
            [
                f"--{artifact_type.replace('_', '-')}",
                str(output / f"{artifact_type.replace('_', '-')}.json"),
            ]
        )
    completed = subprocess.run(command, check=False, capture_output=True, text=True)

    assert completed.returncode == 0, completed.stderr
    report = json.loads(manifest.read_text(encoding="utf-8"))
    records = {item["artifact_type"]: item for item in report["artifacts"]}
    assert all(records[artifact_type]["gate_passed"] for artifact_type in RUNNER.LOCAL_ARTIFACT_TYPES)
    missing = {item["gate"] for item in report["blockers"] if item["code"] == "artifact_missing"}
    assert missing == {"license_review", "clean_clone", "hosted_ci", "browser_acceptance"}
    assert report["summary"] == {
        "artifact_gates_passed": 5,
        "artifact_gates_required": 9,
        "blocker_count": 4,
    }


def test_repository_identity_is_bound_to_commit_tree_and_lock(tmp_path: Path) -> None:
    repo, output, _ = _run_final(tmp_path)
    artifact = json.loads((output / "four-variant-eval.json").read_text(encoding="utf-8"))

    assert artifact["repository"] == {
        "git_commit": _git(repo, "rev-parse", "HEAD^{commit}"),
        "git_dirty": False,
        "git_tree": _git(repo, "rev-parse", "HEAD^{tree}"),
        "uv_lock_sha256": hashlib.sha256((repo / "uv.lock").read_bytes()).hexdigest(),
    }
    assert set(artifact["metrics"]["executed_variants"]) == set(RUNNER.EXPECTED_VARIANTS)
    assert artifact["metrics"]["hard_gate_pass_count"] == artifact["metrics"]["hard_gate_case_count"] == 28


def test_summary_and_command_evidence_use_portable_labels(tmp_path: Path) -> None:
    _, output, summary = _run_final(tmp_path)

    assert set(summary["artifact_paths"].values()) == {
        "coverage.json",
        "deterministic-demo.json",
        "four-variant-eval.json",
        "full-tests.json",
        "security-subset.json",
    }
    summary_text = (output / "local-release-evidence-run.json").read_text(encoding="utf-8")
    assert str(tmp_path) not in summary_text
    artifact = json.loads((output / "full-tests.json").read_text(encoding="utf-8"))
    assert artifact["command"]["argv"][0] == "<runtime-executable>"
    assert artifact["command"]["argv"][1] == "tests/evidence_helper.py"
    assert artifact["command"]["argv"][3] == "<scratch:full-junit>"
