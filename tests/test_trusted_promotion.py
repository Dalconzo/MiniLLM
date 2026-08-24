from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess

import pytest
from typer.testing import CliRunner

from local_agent_lab.deployment_gate import (
    DeploymentArtifact,
    DeploymentCandidate,
    RollbackMetadata,
)
from local_agent_lab.promotion_controller import HumanAuthorization
from local_agent_lab.trusted_promotion import (
    CommandSpec,
    GitCommittedTree,
    HashChainAuditJournal,
    TrustedPromotionAdapter,
    TrustedPromotionConfig,
    TrustedPromotionError,
    write_human_authorization,
)
from local_agent_lab.trusted_promotion_cli import _config as production_config
from local_agent_lab.trusted_promotion_cli import app as promotion_cli


class FixtureRunner:
    def __init__(self, *, failures: set[str] | None = None) -> None:
        self.failures = failures or set()
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: tuple[str, ...], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        if argv[0] == "git":
            return subprocess.run(list(argv), cwd=cwd, text=True, capture_output=True, check=False)
        name = argv[-1]
        failed = name in self.failures
        return subprocess.CompletedProcess(argv, 1 if failed else 0, "fixture stdout", "fixture stderr" if failed else "")


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, check=True)
    return result.stdout.strip()


def _fixture(tmp_path: Path, *, failures: set[str] | None = None):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "trusted-adapter@example.invalid")
    _git(repo, "config", "user.name", "Trusted Adapter Test")
    feature = repo / "src" / "local_agent_lab" / "feature.py"
    test_file = repo / "tests" / "test_feature.py"
    feature.parent.mkdir(parents=True)
    test_file.parent.mkdir(parents=True)
    feature.write_text("VALUE = 'old'\n", encoding="utf-8")
    test_file.write_text("def test_old():\n    assert True\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base release")
    previous = _git(repo, "rev-parse", "HEAD")

    feature.write_text("VALUE = 'new'\n", encoding="utf-8")
    test_file.write_text("def test_new():\n    assert True\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "candidate release")
    candidate_commit = _git(repo, "rev-parse", "HEAD")

    release_root = tmp_path / "releases"
    control_root = tmp_path / "control"
    audit_root = control_root / "audit"
    pending = control_root / "approvals" / "pending"
    consumed = control_root / "approvals" / "consumed"
    operations = control_root / "operations"
    for path in (release_root, control_root, audit_root, pending, consumed, operations):
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o700)
    previous_release = release_root / previous
    previous_release.mkdir(mode=0o755)
    (previous_release / "src" / "local_agent_lab").mkdir(parents=True)
    (previous_release / "tests").mkdir()
    (previous_release / "src" / "local_agent_lab" / "feature.py").write_text("VALUE = 'old'\n", encoding="utf-8")
    (previous_release / "tests" / "test_feature.py").write_text("def test_old():\n    assert True\n", encoding="utf-8")
    (release_root / "current").symlink_to(previous)
    current_build = control_root / "current_build"
    current_build.write_text(previous + "\n", encoding="ascii")
    current_build.chmod(0o600)

    candidate = DeploymentCandidate(
        repository="MiniLLM",
        branch="main",
        candidate_commit=candidate_commit,
        builder_identity="_minillm_builder",
        production_identity="_minillm_prod",
        staging_runtime_root="data/local_staging",
        production_runtime_root="data",
        artifacts=(
            DeploymentArtifact("src/local_agent_lab/feature.py", "src/local_agent_lab/feature.py"),
            DeploymentArtifact("tests/test_feature.py", "tests/test_feature.py"),
        ),
        rollback=RollbackMetadata(
            previous_build=previous,
            candidate_build=candidate_commit,
            migration_version="memory-v1",
            rollback_reference=f"git-ref:{previous}",
            data_compatibility_assumptions="No schema change.",
        ),
        tests_passed=True,
        evals_passed=True,
        security_checks_passed=True,
        migration_compatible=True,
    )
    runner = FixtureRunner(failures=failures)
    uid = os.geteuid()
    config = TrustedPromotionConfig(
        repository_dir=repo,
        release_root=release_root,
        control_root=control_root,
        audit_root=audit_root,
        service_label="com.example.minillm.production",
        verifier_identity="trusted-release-verifier",
        executor_identity="trusted-production-executor",
        preflight_commands=(CommandSpec("preflight", ("fixture", "preflight")),),
        post_smoke_commands=(
            CommandSpec("memory-status", ("fixture", "memory-status")),
            CommandSpec("home-mcp-smoke", ("fixture", "home-mcp-smoke")),
        ),
        expected_owner_uid=uid,
        require_executor_uid=uid,
    )
    adapter = TrustedPromotionAdapter(config, runner=runner)
    verification = GitCommittedTree(repo).verify(candidate, verified_by=config.verifier_identity, verified_at=_now())
    return repo, candidate, config, adapter, runner, verification


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _authorization(candidate: DeploymentCandidate, manifest: str, authorization_id: str, *, action: str = "promote") -> HumanAuthorization:
    now = datetime.now(timezone.utc)
    assert candidate.rollback is not None
    return HumanAuthorization(
        authorization_id=authorization_id,
        actor="release-reviewer",
        actor_type="human",
        decision="approve",
        action=action,  # type: ignore[arg-type]
        candidate_commit=candidate.candidate_commit,
        previous_build=candidate.rollback.previous_build,
        manifest_sha256=manifest,
        authorized_at=(now - timedelta(minutes=1)).isoformat(),
        expires_at=(now + timedelta(hours=1)).isoformat(),
        reason=f"Explicitly approve {action} for the tested fixture.",
        approval_reference=f"ticket:test-{authorization_id}",
    )


def _write_approval(config: TrustedPromotionConfig, authorization: HumanAuthorization) -> Path:
    return write_human_authorization(
        config.control_root,
        authorization,
        expected_owner_uid=config.expected_owner_uid,
        require_executor_uid=config.require_executor_uid,
    )


def test_trusted_adapter_promotes_only_committed_blobs_and_consumes_approval(tmp_path: Path) -> None:
    _repo, candidate, config, adapter, runner, verification = _fixture(tmp_path)
    approval = _authorization(candidate, verification.manifest_sha256, "promote-001")
    pending = _write_approval(config, approval)

    result = adapter.execute_promotion(candidate, authorization_id=approval.authorization_id)

    assert result.plan.ready is True
    assert result.outcome is not None and result.outcome.status == "completed"
    assert result.approval_consumed is True
    assert result.release_path is not None
    restored = adapter.load_promotion_result(result.operation_id)
    assert restored.plan == result.plan
    assert restored.outcome == result.outcome
    assert (result.release_path / "src" / "local_agent_lab" / "feature.py").read_text() == "VALUE = 'new'\n"
    assert config.current_build_file.read_text().strip() == candidate.candidate_commit
    assert config.current_link.resolve() == result.release_path.resolve()
    assert not pending.exists()
    assert (config.control_root / "approvals" / "consumed" / pending.name).is_file()
    assert ("launchctl", "kickstart", "-k", "system/com.example.minillm.production") in runner.calls
    records = adapter.journal.read_all()
    assert {record["record_type"] for record in records} >= {
        "promotion_plan",
        "authorization_consumed",
        "promotion_outcome",
        "command_evidence",
    }


def test_dirty_worktree_blocks_before_approval_consumption_or_release_write(tmp_path: Path) -> None:
    repo, candidate, config, adapter, _runner, verification = _fixture(tmp_path)
    approval = _authorization(candidate, verification.manifest_sha256, "promote-dirty")
    pending = _write_approval(config, approval)
    (repo / "untracked.txt").write_text("dirty\n", encoding="utf-8")

    result = adapter.execute_promotion(candidate, authorization_id=approval.authorization_id)

    assert result.plan.status == "blocked"
    assert "dirty_worktree" in {reason.code for reason in result.plan.reasons}
    assert result.approval_consumed is False
    assert pending.exists()
    assert not (config.release_root / candidate.candidate_commit).exists()


def test_insecure_or_reused_approval_is_rejected(tmp_path: Path) -> None:
    _repo, candidate, config, adapter, _runner, verification = _fixture(tmp_path)
    approval = _authorization(candidate, verification.manifest_sha256, "promote-private")
    path = _write_approval(config, approval)
    path.chmod(0o644)

    with pytest.raises(TrustedPromotionError, match="permissions"):
        adapter.execute_promotion(candidate, authorization_id=approval.authorization_id)

    path.chmod(0o600)
    first = adapter.execute_promotion(candidate, authorization_id=approval.authorization_id)
    assert first.outcome is not None
    with pytest.raises(TrustedPromotionError, match="missing"):
        adapter.execute_promotion(candidate, authorization_id=approval.authorization_id)


def test_release_symlink_is_rejected_before_activation(tmp_path: Path) -> None:
    _repo, candidate, config, adapter, _runner, verification = _fixture(tmp_path)
    assert candidate.rollback is not None
    bad = config.release_root / candidate.rollback.previous_build / "src" / "redirect"
    bad.symlink_to("/tmp")
    approval = _authorization(candidate, verification.manifest_sha256, "promote-symlink")
    pending = _write_approval(config, approval)

    result = adapter.execute_promotion(candidate, authorization_id=approval.authorization_id)

    assert result.plan.status == "blocked"
    assert "rollback_test_failed" in {reason.code for reason in result.plan.reasons}
    assert pending.exists()


def test_failed_smoke_stops_and_separately_authorized_rollback_restores_previous(tmp_path: Path) -> None:
    _repo, candidate, config, adapter, runner, verification = _fixture(tmp_path, failures={"memory-status"})
    promotion_auth = _authorization(candidate, verification.manifest_sha256, "promote-smoke-fail")
    _write_approval(config, promotion_auth)

    promotion = adapter.execute_promotion(candidate, authorization_id=promotion_auth.authorization_id)

    assert promotion.outcome is not None
    assert promotion.outcome.status == "stopped"
    assert promotion.outcome.rollback_required is True
    assert ("fixture", "home-mcp-smoke") not in runner.calls
    assert config.current_build_file.read_text().strip() == candidate.candidate_commit

    runner.failures.clear()
    rollback_auth = _authorization(candidate, verification.manifest_sha256, "rollback-001", action="rollback")
    _write_approval(config, rollback_auth)
    persisted = adapter.load_promotion_result(promotion.operation_id)
    rollback = adapter.execute_rollback(persisted, authorization_id=rollback_auth.authorization_id)

    assert rollback.assessment.status == "ready"
    assert rollback.completed is True
    assert candidate.rollback is not None
    assert config.current_build_file.read_text().strip() == candidate.rollback.previous_build
    assert config.current_link.resolve() == (config.release_root / candidate.rollback.previous_build).resolve()


def test_compare_and_swap_drift_blocks_plan_and_preserves_approval(tmp_path: Path) -> None:
    _repo, candidate, config, adapter, _runner, verification = _fixture(tmp_path)
    drift = "f" * 40
    (config.release_root / drift).mkdir()
    config.current_build_file.write_text(drift + "\n", encoding="ascii")
    config.current_build_file.chmod(0o600)
    approval = _authorization(candidate, verification.manifest_sha256, "promote-drift")
    pending = _write_approval(config, approval)

    with pytest.raises(TrustedPromotionError, match="marker and release pointer disagree"):
        adapter.execute_promotion(candidate, authorization_id=approval.authorization_id)

    assert pending.exists()


def test_group_writable_previous_release_blocks_before_approval_consumption(tmp_path: Path) -> None:
    _repo, candidate, config, adapter, _runner, verification = _fixture(tmp_path)
    assert candidate.rollback is not None
    exposed = config.release_root / candidate.rollback.previous_build / "src" / "local_agent_lab" / "feature.py"
    exposed.chmod(0o664)
    approval = _authorization(candidate, verification.manifest_sha256, "promote-writable-release")
    pending = _write_approval(config, approval)

    result = adapter.execute_promotion(candidate, authorization_id=approval.authorization_id)

    assert result.plan.status == "blocked"
    assert "rollback_test_failed" in {reason.code for reason in result.plan.reasons}
    assert pending.exists()


def test_audit_chain_detects_tampering(tmp_path: Path) -> None:
    audit_root = tmp_path / "audit"
    audit_root.mkdir(mode=0o700)
    journal = HashChainAuditJournal(audit_root, expected_owner_uid=os.geteuid())
    journal.append("fixture", {"value": 1})
    journal.append("fixture", {"value": 2})
    lines = journal.path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[0])
    record["payload"]["value"] = 9
    lines[0] = json.dumps(record, sort_keys=True, separators=(",", ":"))
    journal.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    journal.path.chmod(0o600)

    with pytest.raises(TrustedPromotionError, match="chain verification failed"):
        journal.read_all()


def test_persisted_operation_state_detects_tampering(tmp_path: Path) -> None:
    _repo, candidate, config, adapter, _runner, verification = _fixture(tmp_path)
    approval = _authorization(candidate, verification.manifest_sha256, "promote-state-tamper")
    _write_approval(config, approval)
    result = adapter.execute_promotion(candidate, authorization_id=approval.authorization_id)
    path = config.control_root / "operations" / f"{result.operation_id}.json"
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["result"]["plan"]["candidate_build"] = "f" * 40
    path.write_text(json.dumps(envelope), encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(TrustedPromotionError, match="digest is invalid"):
        adapter.load_promotion_result(result.operation_id)


def test_authorization_scope_mismatch_never_consumes_approval(tmp_path: Path) -> None:
    _repo, candidate, config, adapter, _runner, verification = _fixture(tmp_path)
    approval = replace(
        _authorization(candidate, verification.manifest_sha256, "promote-wrong-manifest"),
        manifest_sha256="e" * 64,
    )
    pending = _write_approval(config, approval)

    result = adapter.execute_promotion(candidate, authorization_id=approval.authorization_id)

    assert result.plan.status == "blocked"
    assert "authorization_scope_mismatch" in {reason.code for reason in result.plan.reasons}
    assert pending.exists()


def test_root_configuration_refuses_candidate_commands_without_identity_demotion(tmp_path: Path, monkeypatch) -> None:
    _repo, candidate, config, _adapter, _runner, verification = _fixture(tmp_path)
    root_config = replace(config, require_executor_uid=0)
    adapter = TrustedPromotionAdapter(root_config, runner=FixtureRunner())
    approval = _authorization(candidate, verification.manifest_sha256, "promote-root-command")
    _write_approval(config, approval)
    monkeypatch.setattr(os, "geteuid", lambda: 0)

    with pytest.raises(TrustedPromotionError, match="must demote from root"):
        adapter.execute_promotion(candidate, authorization_id=approval.authorization_id)


def test_dedicated_cli_keeps_candidate_and_smoke_code_out_of_root_identity(tmp_path: Path) -> None:
    _repo, candidate, _config, _adapter, _runner, _verification = _fixture(tmp_path)
    config = production_config(tmp_path)

    assert config.preflight_commands[0].argv[:5] == (
        "/usr/bin/sudo",
        "-n",
        "-u",
        candidate.builder_identity,
        "--",
    )
    assert config.post_smoke_commands[0].argv[:5] == (
        "/usr/bin/sudo",
        "-n",
        "-u",
        candidate.production_identity,
        "--",
    )
    assert config.preflight_commands[0].argv[5:7] == (
        "/usr/bin/env",
        f"PYTHONPATH={tmp_path.resolve() / 'src'}",
    )
    assert config.post_smoke_commands[0].argv[5:8] == (
        "/usr/bin/env",
        "PYTHONPATH=/Library/Application Support/MiniLLM/releases/current/src",
        "LAGENT_CONFIG=/private/var/db/minillm-production/config/agent.yaml",
    )
    assert "/Library/Application Support/MiniLLM/python-runtime/bin/python" in config.post_smoke_commands[0].argv
    result = CliRunner().invoke(promotion_cli, ["--help"])
    assert result.exit_code == 0
    plain_help = re.sub(r"\x1b\[[0-9;]*m", "", result.stdout)
    assert {"verify", "authorize", "apply", "rollback", "audit-verify"} <= set(plain_help.split())
