from __future__ import annotations

import json
from dataclasses import asdict

import pytest
from typer.testing import CliRunner

from local_agent_lab.cli import app
from local_agent_lab.deployment_gate import (
    DeploymentArtifact,
    DeploymentCandidate,
    RollbackMetadata,
    check_deployment_candidate,
)


def _candidate(
    *,
    artifacts: tuple[DeploymentArtifact, ...] | None = None,
    rollback: RollbackMetadata | None = None,
    **overrides: object,
) -> DeploymentCandidate:
    values: dict[str, object] = {
        "repository": "MiniLLM",
        "branch": "main",
        "candidate_commit": "candidate-abc123",
        "builder_identity": "minillm-builder-staging",
        "production_identity": "minillm-home-mcp-production",
        "staging_runtime_root": "data/local_staging",
        "production_runtime_root": "data",
        "artifacts": artifacts
        or (
            DeploymentArtifact("src/local_agent_lab/memory/search.py", "src/local_agent_lab/memory/search.py"),
            DeploymentArtifact("tests/test_memory_search.py", "tests/test_memory_search.py"),
            DeploymentArtifact("config/agent.local-staging.yaml", "config/agent.local-staging.yaml"),
        ),
        "rollback": rollback
        or RollbackMetadata(
            previous_build="production-before-abc123",
            candidate_build="candidate-abc123",
            migration_version="none",
            rollback_reference="git-ref:production-before-abc123",
            data_compatibility_assumptions="No schema or production-data changes.",
        ),
        "tests_passed": True,
        "evals_passed": True,
        "security_checks_passed": True,
        "migration_compatible": True,
    }
    values.update(overrides)
    return DeploymentCandidate(**values)  # type: ignore[arg-type]


def _reason_codes(candidate: DeploymentCandidate) -> set[str]:
    return {reason.code for reason in check_deployment_candidate(candidate).reasons}


def _write_config(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_path = config_dir / "agent.yaml"
    config_path.write_text(
        json.dumps(
            {
                "app": {"name": "deployment-gate-test", "log_level": "info"},
                "paths": {
                    "data_dir": "data",
                    "logs_dir": "data/logs",
                    "indexes_dir": "data/indexes",
                    "memory_dir": "data/memory",
                    "patches_dir": "data/patches",
                },
                "ollama": {"host": "http://127.0.0.1:11434", "request_timeout_seconds": 5},
                "runtime": {"default_task": "chat", "redact_before_model": True, "save_full_prompts": False},
                "models": {},
                "routing": {"task_map": {}},
            }
        ),
        encoding="utf-8",
    )
    return config_path


def test_gate_allows_explicit_source_test_staging_config_and_docs_paths() -> None:
    candidate = _candidate(
        artifacts=(
            DeploymentArtifact("src/local_agent_lab/memory/search.py", "src/local_agent_lab/memory/search.py"),
            DeploymentArtifact("tests/test_memory_search.py", "tests/test_memory_search.py"),
            DeploymentArtifact("config/agent.local-staging.yaml", "config/agent.local-staging.yaml"),
            DeploymentArtifact("docs/local_staging_memory.md", "docs/local_staging_memory.md"),
        )
    )

    decision = check_deployment_candidate(candidate)

    assert decision.promotable is True
    assert decision.status == "allow"
    assert [artifact.category for artifact in decision.approved_artifacts] == [
        "source",
        "test",
        "config",
        "documentation",
    ]
    assert decision.to_dict()["check_only"] is True
    assert decision.to_dict()["deployment_performed"] is False


@pytest.mark.parametrize(
    "path",
    (
        "src/local_agent_lab/../deployment_gate.py",
        "tests/../../data/memory/private.sqlite3",
        "/opt/example/MiniLLM/src/local_agent_lab/config.py",
        "src//local_agent_lab/config.py",
        r"src\local_agent_lab\config.py",
    ),
)
def test_gate_denies_traversal_and_non_repo_relative_paths(path: str) -> None:
    artifact = DeploymentArtifact(path, path)
    codes = _reason_codes(_candidate(artifacts=(artifact,)))

    assert codes & {"path_traversal_denied", "invalid_artifact_path"}


def test_gate_denies_symlink_like_resolution_outside_declared_source_path() -> None:
    artifact = DeploymentArtifact(
        "src/local_agent_lab/memory/search.py",
        "data/memory/private.sqlite3",
    )

    assert "artifact_resolution_mismatch" in _reason_codes(_candidate(artifacts=(artifact,)))


def test_gate_requires_trusted_resolved_path_metadata() -> None:
    artifact = DeploymentArtifact("src/local_agent_lab/memory/search.py", None)

    assert "missing_resolved_path" in _reason_codes(_candidate(artifacts=(artifact,)))


@pytest.mark.parametrize(
    "path",
    (
        "data/home_mcp/recipe_book/private.md",
        "data/memory/chatgpt_memory.sqlite3",
        "data/logs/home_mcp/audit.jsonl",
        "data/local_staging/memory/test.sqlite3",
        "data/chatgpt_exports/raw/conversations.json",
    ),
)
def test_gate_denies_production_and_staging_runtime_data(path: str) -> None:
    assert "runtime_data_path_denied" in _reason_codes(
        _candidate(artifacts=(DeploymentArtifact(path, path),))
    )


@pytest.mark.parametrize(
    "path",
    (
        ".env",
        "config/production-secrets.yaml",
        "config/tunnel-auth.yaml",
        "src/local_agent_lab/private_keys/service.pem",
        "config/id_ed25519",
    ),
)
def test_gate_denies_secret_tunnel_and_auth_paths(path: str) -> None:
    decision = check_deployment_candidate(_candidate(artifacts=(DeploymentArtifact(path, path),)))

    assert decision.promotable is False
    assert _reason_codes(_candidate(artifacts=(DeploymentArtifact(path, path),))) & {
        "credential_path_denied",
        "control_plane_path_denied",
    }


@pytest.mark.parametrize(
    "path",
    (
        "config/agent.yaml",
        "scripts/bootstrap_minillm_production.sh",
        "scripts/cutover_minillm_production.sh",
        "scripts/install_minillm_admin_helper.sh",
        "scripts/minillm_admin.py",
        "scripts/provision_minillm_service_identities.sh",
        "scripts/verify_service_identity_boundaries.sh",
        "src/local_agent_lab/home_mcp.py",
        "src/local_agent_lab/memory/audit.py",
        "src/local_agent_lab/services/home_mcp_launchd.py",
        "src/local_agent_lab/deployment_gate.py",
    ),
)
def test_gate_denies_v1_production_control_plane_paths(path: str) -> None:
    assert "control_plane_path_denied" in _reason_codes(
        _candidate(artifacts=(DeploymentArtifact(path, path),))
    )


def test_gate_denies_same_builder_and_production_identity() -> None:
    candidate = _candidate(production_identity="minillm-builder-staging")

    assert "identity_boundary_violation" in _reason_codes(candidate)


def test_gate_validates_expected_staging_and_production_roots() -> None:
    candidate = _candidate(staging_runtime_root="data/memory", production_runtime_root="data/local_staging")
    codes = _reason_codes(candidate)

    assert "unexpected_staging_root" in codes
    assert "unexpected_production_root" in codes


def test_gate_reports_all_missing_rollback_metadata() -> None:
    decision = check_deployment_candidate(_candidate(rollback=RollbackMetadata()))

    assert decision.promotable is False
    assert "missing_rollback_metadata" in {reason.code for reason in decision.reasons}
    assert decision.missing_rollback_fields == (
        "previous_build",
        "candidate_build",
        "migration_version",
        "rollback_reference",
        "data_compatibility_assumptions",
    )
    assert decision.to_dict()["rollback_requirements"] == list(decision.missing_rollback_fields)


def test_gate_rejects_shell_text_as_rollback_reference() -> None:
    rollback = RollbackMetadata(
        previous_build="production-before-abc123",
        candidate_build="candidate-abc123",
        migration_version="none",
        rollback_reference="procedure:restore && rm -rf data",
        data_compatibility_assumptions="No schema changes.",
    )

    assert "unsafe_rollback_reference" in _reason_codes(_candidate(rollback=rollback))


def test_gate_requires_tests_evals_security_and_migration_evidence() -> None:
    decision = check_deployment_candidate(
        _candidate(
            tests_passed=False,
            evals_passed=False,
            security_checks_passed=False,
            migration_compatible=False,
        )
    )

    failed_fields = {reason.field for reason in decision.reasons if reason.code == "required_check_failed"}
    assert failed_fields == {
        "tests_passed",
        "evals_passed",
        "security_checks_passed",
        "migration_compatible",
    }


def test_deployment_check_cli_emits_audited_allow_decision(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LAGENT_CONFIG", str(_write_config(tmp_path)))
    manifest = tmp_path / "candidate.json"
    manifest.write_text(json.dumps(asdict(_candidate())), encoding="utf-8")

    result = CliRunner().invoke(app, ["deployment-check", "--manifest", str(manifest), "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["decision"]["status"] == "allow"
    assert payload["decision"]["check_only"] is True
    assert payload["decision"]["deployment_performed"] is False
    assert (tmp_path / "data" / "logs" / payload["run_id"] / "deployment_gate.json").exists()


def test_deployment_check_cli_reports_control_plane_denial_without_deploying(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LAGENT_CONFIG", str(_write_config(tmp_path)))
    candidate = _candidate(
        artifacts=(DeploymentArtifact("src/local_agent_lab/home_mcp.py", "src/local_agent_lab/home_mcp.py"),)
    )
    manifest = tmp_path / "denied.json"
    manifest.write_text(json.dumps(asdict(candidate)), encoding="utf-8")

    result = CliRunner().invoke(app, ["deployment-check", "--manifest", str(manifest), "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["decision"]["status"] == "deny"
    assert payload["decision"]["deployment_performed"] is False
    assert "control_plane_path_denied" in {reason["code"] for reason in payload["decision"]["reasons"]}


@pytest.mark.parametrize(
    "path",
    (
        "src/local_agent_lab/trusted_promotion.py",
        "src/local_agent_lab/trusted_promotion_cli.py",
    ),
)
def test_trusted_adapter_cannot_promote_itself(path: str) -> None:
    decision = check_deployment_candidate(_candidate(artifacts=(DeploymentArtifact(path, path),)))

    assert decision.promotable is False
    assert "control_plane_path_denied" in {reason.code for reason in decision.reasons}
