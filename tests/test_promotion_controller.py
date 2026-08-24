from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest

from local_agent_lab.deployment_gate import (
    DeploymentArtifact,
    DeploymentCandidate,
    RollbackMetadata,
    check_deployment_candidate,
)
from local_agent_lab.promotion_controller import (
    CandidateVerification,
    CheckEvidence,
    HumanAuthorization,
    ManifestArtifact,
    PromotionExecutionEvidence,
    RollbackTestEvidence,
    assess_promotion_execution,
    audit_metadata_is_valid,
    build_promotion_plan,
    build_rollback_plan,
    compute_manifest_sha256,
    test_rollback_plan as assess_rollback_plan,
)


CANDIDATE_COMMIT = "a" * 40
PREVIOUS_BUILD = "b" * 40
PREPARED_AT = "2026-08-20T12:00:00-07:00"


def _candidate(
    *,
    artifacts: tuple[DeploymentArtifact, ...] | None = None,
    candidate_commit: str = CANDIDATE_COMMIT,
) -> DeploymentCandidate:
    return DeploymentCandidate(
        repository="MiniLLM",
        branch="main",
        candidate_commit=candidate_commit,
        builder_identity="minillm-builder-staging",
        production_identity="minillm-home-mcp-production",
        staging_runtime_root="data/local_staging",
        production_runtime_root="data",
        artifacts=artifacts
        or (
            DeploymentArtifact(
                "src/local_agent_lab/memory/search.py",
                "src/local_agent_lab/memory/search.py",
            ),
            DeploymentArtifact("tests/test_memory_search.py", "tests/test_memory_search.py"),
        ),
        rollback=RollbackMetadata(
            previous_build=PREVIOUS_BUILD,
            candidate_build=candidate_commit,
            migration_version="memory-v1",
            rollback_reference=f"git-ref:{PREVIOUS_BUILD}",
            data_compatibility_assumptions="Schema remains backward compatible.",
        ),
        tests_passed=True,
        evals_passed=True,
        security_checks_passed=True,
        migration_compatible=True,
    )


def _manifest(candidate: DeploymentCandidate) -> tuple[ManifestArtifact, ...]:
    return tuple(
        ManifestArtifact(
            path=artifact.path,
            resolved_path=artifact.resolved_path or "",
            content_sha256=f"{index:064x}",
            size_bytes=100 + index,
        )
        for index, artifact in enumerate(candidate.artifacts, start=1)
    )


def _verification(
    candidate: DeploymentCandidate,
    *,
    artifacts: tuple[ManifestArtifact, ...] | None = None,
    **overrides: object,
) -> CandidateVerification:
    manifest = artifacts or _manifest(candidate)
    values: dict[str, object] = {
        "repository": candidate.repository,
        "branch": candidate.branch,
        "candidate_commit": candidate.candidate_commit,
        "checked_out_commit": candidate.candidate_commit,
        "manifest_commit": candidate.candidate_commit,
        "worktree_clean": True,
        "commit_object_verified": True,
        "manifest_sha256": compute_manifest_sha256(manifest),
        "artifacts": manifest,
        "verified_by": "trusted-release-verifier",
        "verified_at": "2026-08-20T11:45:00-07:00",
    }
    values.update(overrides)
    return CandidateVerification(**values)  # type: ignore[arg-type]


def _authorization(
    verification: CandidateVerification,
    *,
    action: str = "promote",
    **overrides: object,
) -> HumanAuthorization:
    values: dict[str, object] = {
        "authorization_id": f"human-{action}-approval-001",
        "actor": "release-reviewer",
        "actor_type": "human",
        "decision": "approve",
        "action": action,
        "candidate_commit": verification.candidate_commit,
        "previous_build": PREVIOUS_BUILD,
        "manifest_sha256": verification.manifest_sha256,
        "authorized_at": "2026-08-20T11:50:00-07:00",
        "expires_at": "2026-08-20T13:00:00-07:00",
        "reason": "Reviewed candidate evidence and explicitly authorize this operation.",
        "approval_reference": "ticket:synthetic-human-approval",
    }
    values.update(overrides)
    return HumanAuthorization(**values)  # type: ignore[arg-type]


def _check(
    name: str = "home-mcp-smoke",
    *,
    passed: bool = True,
    candidate_build: str = CANDIDATE_COMMIT,
    manifest_sha256: str | None = None,
) -> CheckEvidence:
    if manifest_sha256 is None:
        manifest_sha256 = _verification(_candidate()).manifest_sha256
    return CheckEvidence(
        name=name,
        passed=passed,
        checked_at="2026-08-20T11:55:00-07:00",
        evidence_reference=f"artifact:test-results/{name}.json",
        candidate_build=candidate_build,
        manifest_sha256=manifest_sha256,
        details="Focused smoke evidence.",
    )


def _rollback_test(candidate: DeploymentCandidate, *, passed: bool = True) -> RollbackTestEvidence:
    assert candidate.rollback is not None
    return RollbackTestEvidence(
        rollback_reference=candidate.rollback.rollback_reference,
        target_build=candidate.rollback.previous_build,
        reference_resolved=passed,
        target_build_available=passed,
        data_compatibility_verified=passed,
        restore_procedure_tested=passed,
        passed=passed,
        tested_at="2026-08-20T11:40:00-07:00",
        evidence_reference="artifact:test-results/rollback-drill.json",
    )


def _plan(
    *,
    candidate: DeploymentCandidate | None = None,
    verification: CandidateVerification | None = None,
    authorization: HumanAuthorization | None = None,
    smoke: tuple[CheckEvidence, ...] | None = None,
    rollback_test: RollbackTestEvidence | None = None,
):
    candidate = candidate or _candidate()
    verification = verification or _verification(candidate)
    if authorization is None:
        authorization = _authorization(verification)
    return build_promotion_plan(
        candidate,
        check_deployment_candidate(candidate),
        verification,
        authorization,
        preflight_smoke_results=smoke
        or (
            _check(
                candidate_build=candidate.candidate_commit,
                manifest_sha256=verification.manifest_sha256,
            ),
        ),
        rollback_test=rollback_test or _rollback_test(candidate),
        prepared_at=PREPARED_AT,
    )


def _reason_codes(result) -> set[str]:
    return {reason.code for reason in result.reasons}


def test_ready_plan_binds_gate_commit_manifest_authorization_and_audit() -> None:
    plan = _plan()

    assert plan.ready is True
    assert plan.status == "ready"
    assert plan.promotion_performed is False
    assert plan.requires_trusted_executor is True
    assert plan.audit.actor == "release-reviewer"
    assert plan.audit.verifier_identity == "trusted-release-verifier"
    assert plan.audit.verified_at == "2026-08-20T11:45:00-07:00"
    assert plan.audit.executor_identity == ""
    assert plan.audit.candidate_build == CANDIDATE_COMMIT
    assert plan.audit.previous_build == PREVIOUS_BUILD
    assert plan.audit.manifest_sha256 == plan.manifest_sha256
    assert plan.audit.migration_version == "memory-v1"
    assert plan.audit.rollback_reference == f"git-ref:{PREVIOUS_BUILD}"
    assert plan.audit.controller_host_changes_performed is False
    assert len(plan.audit.record_sha256) == 64
    assert audit_metadata_is_valid(plan.audit) is True
    assert {check.name for check in plan.audit.checks} >= {
        "fresh_gate_match",
        "immutable_commit_identity",
        "clean_committed_candidate",
        "manifest_digest",
        "manifest_inventory",
        "human_authorization",
        "preflight_smoke:home-mcp-smoke",
        "rollback_readiness",
    }


@pytest.mark.parametrize(
    ("overrides", "expected_code"),
    (
        ({"worktree_clean": False}, "dirty_worktree"),
        ({"commit_object_verified": False}, "unverified_commit_object"),
        ({"checked_out_commit": "c" * 40}, "candidate_identity_mismatch"),
        ({"manifest_commit": "c" * 40}, "candidate_identity_mismatch"),
    ),
)
def test_candidate_identity_must_be_committed_clean_and_exact(overrides: dict[str, object], expected_code: str) -> None:
    candidate = _candidate()
    verification = _verification(candidate, **overrides)

    plan = _plan(candidate=candidate, verification=verification)

    assert plan.status == "blocked"
    assert expected_code in _reason_codes(plan)


def test_short_branch_like_commit_is_not_an_immutable_candidate_identity() -> None:
    candidate = _candidate(candidate_commit="main")
    verification = _verification(candidate)

    plan = _plan(candidate=candidate, verification=verification)

    assert "nonimmutable_candidate_commit" in _reason_codes(plan)


def test_manifest_digest_and_inventory_are_independently_recomputed() -> None:
    candidate = _candidate()
    verification = _verification(candidate, manifest_sha256="f" * 64)

    digest_plan = _plan(candidate=candidate, verification=verification)

    assert "manifest_digest_mismatch" in _reason_codes(digest_plan)

    partial = _manifest(candidate)[:1]
    partial_verification = _verification(candidate, artifacts=partial)
    inventory_plan = _plan(candidate=candidate, verification=partial_verification)
    assert "manifest_inventory_mismatch" in _reason_codes(inventory_plan)


def test_stale_or_tampered_gate_result_is_rejected() -> None:
    candidate = _candidate()
    verification = _verification(candidate)
    supplied = replace(check_deployment_candidate(candidate), approved_artifacts=())

    plan = build_promotion_plan(
        candidate,
        supplied,
        verification,
        _authorization(verification),
        preflight_smoke_results=(_check(),),
        rollback_test=_rollback_test(candidate),
        prepared_at=PREPARED_AT,
    )

    assert plan.status == "blocked"
    assert {"gate_decision_mismatch", "manifest_inventory_mismatch"} <= _reason_codes(plan)


def test_controller_code_requires_separate_review_even_when_check_only_gate_allows_it() -> None:
    path = "src/local_agent_lab/promotion_controller.py"
    candidate = _candidate(artifacts=(DeploymentArtifact(path, path),))
    gate = check_deployment_candidate(candidate)
    assert gate.promotable is True
    verification = _verification(candidate)

    plan = _plan(candidate=candidate, verification=verification)

    assert plan.status == "blocked"
    assert "control_plane_path_denied" in _reason_codes(plan)


@pytest.mark.parametrize(
    "path",
    (
        "scripts/bootstrap_minillm_production.sh",
        "scripts/cutover_minillm_production.sh",
        "scripts/install_minillm_admin_helper.sh",
        "scripts/minillm_admin.py",
        "scripts/provision_minillm_service_identities.sh",
        "scripts/verify_service_identity_boundaries.sh",
    ),
)
def test_host_identity_and_cutover_scripts_require_separate_security_review(path: str) -> None:
    candidate = _candidate(artifacts=(DeploymentArtifact(path, path),))
    verification = _verification(candidate)

    plan = _plan(candidate=candidate, verification=verification)

    assert plan.status == "blocked"
    assert "control_plane_path_denied" in _reason_codes(plan)


@pytest.mark.parametrize(
    "path",
    (
        "data/home_mcp/private.sqlite3",
        "config/service-token.json",
        "src/local_agent_lab/private_keys/service.pem",
        "src/local_agent_lab/__pycache__/generated.py",
    ),
)
def test_runtime_data_and_secret_artifacts_are_denied_by_controller_defense_in_depth(path: str) -> None:
    candidate = _candidate(artifacts=(DeploymentArtifact(path, path),))
    verification = _verification(candidate)

    plan = _plan(candidate=candidate, verification=verification)

    assert plan.status == "blocked"
    assert _reason_codes(plan) & {"runtime_data_path_denied", "credential_path_denied"}


def test_explicit_current_human_authorization_is_required_and_bound_to_manifest() -> None:
    candidate = _candidate()
    verification = _verification(candidate)
    no_auth = build_promotion_plan(
        candidate,
        check_deployment_candidate(candidate),
        verification,
        None,
        preflight_smoke_results=(_check(),),
        rollback_test=_rollback_test(candidate),
        prepared_at=PREPARED_AT,
    )
    service_auth = _authorization(verification, actor_type="service")
    wrong_manifest = _authorization(verification, manifest_sha256="e" * 64)
    expired = _authorization(verification, expires_at="2026-08-20T11:59:00-07:00")

    assert "human_authorization_required" in _reason_codes(no_auth)
    assert "human_approval_missing" in _reason_codes(_plan(candidate=candidate, verification=verification, authorization=service_auth))
    assert "authorization_scope_mismatch" in _reason_codes(_plan(candidate=candidate, verification=verification, authorization=wrong_manifest))
    assert "authorization_not_current" in _reason_codes(_plan(candidate=candidate, verification=verification, authorization=expired))


def test_failed_preflight_smoke_and_rollback_drill_block_promotion() -> None:
    candidate = _candidate()

    failed_smoke = _plan(candidate=candidate, smoke=(_check(passed=False),))
    failed_rollback = _plan(candidate=candidate, rollback_test=_rollback_test(candidate, passed=False))

    assert "preflight_smoke_failed" in _reason_codes(failed_smoke)
    assert "rollback_test_failed" in _reason_codes(failed_rollback)
    assert failed_smoke.promotion_performed is False
    assert failed_rollback.promotion_performed is False


def test_smoke_evidence_must_be_bound_to_the_candidate_manifest() -> None:
    plan = _plan(smoke=(_check(manifest_sha256="e" * 64),))

    assert plan.status == "blocked"
    assert "preflight_smoke_failed" in _reason_codes(plan)


def test_failed_post_promotion_smoke_stops_and_requires_rollback() -> None:
    plan = _plan()
    outcome = assess_promotion_execution(
        plan,
        PromotionExecutionEvidence(
            executor_identity="trusted-production-runner",
            promotion_applied=True,
            restart_result=_check("production-restart"),
            smoke_results=(_check("memory-status"), _check("home-mcp-smoke", passed=False)),
            completed_at="2026-08-20T12:10:00-07:00",
        ),
    )

    assert outcome.status == "stopped"
    assert outcome.rollback_required is True
    assert outcome.further_promotion_allowed is False
    assert "post_promotion_smoke_failed" in _reason_codes(outcome)
    assert outcome.audit.external_host_changes_reported is True
    assert outcome.audit.controller_host_changes_performed is False
    assert outcome.audit.executor_identity == "trusted-production-runner"
    assert outcome.audit.parent_operation_id == plan.audit.operation_id
    assert audit_metadata_is_valid(outcome.audit) is True


def test_successful_external_smoke_can_complete_without_library_host_changes() -> None:
    plan = _plan()
    outcome = assess_promotion_execution(
        plan,
        PromotionExecutionEvidence(
            executor_identity="trusted-production-runner",
            promotion_applied=True,
            restart_result=_check("production-restart"),
            smoke_results=(_check("memory-status"), _check("home-mcp-smoke")),
            completed_at="2026-08-20T12:10:00-07:00",
        ),
    )

    assert outcome.status == "completed"
    assert outcome.rollback_required is False
    assert outcome.further_promotion_allowed is True
    assert outcome.controller_host_changes_performed is False


def test_external_application_of_a_blocked_plan_stops_and_requires_rollback() -> None:
    blocked = _plan(smoke=(_check(passed=False),))

    outcome = assess_promotion_execution(
        blocked,
        PromotionExecutionEvidence(
            executor_identity="trusted-production-runner",
            promotion_applied=True,
            restart_result=_check("production-restart"),
            smoke_results=(_check("home-mcp-smoke"),),
            completed_at="2026-08-20T12:10:00-07:00",
        ),
    )

    assert outcome.status == "stopped"
    assert outcome.rollback_required is True
    assert "blocked_plan_was_applied" in _reason_codes(outcome)

    verification = _verification(_candidate())
    emergency_rollback = build_rollback_plan(
        blocked,
        outcome,
        _authorization(verification, action="rollback"),
        prepared_at="2026-08-20T12:15:00-07:00",
    )
    assert emergency_rollback.status == "pending_test"


def test_rollback_plan_requires_separate_human_authorization_and_readiness_test() -> None:
    promotion = _plan()
    stopped = assess_promotion_execution(
        promotion,
        PromotionExecutionEvidence(
            executor_identity="trusted-production-runner",
            promotion_applied=True,
            restart_result=_check("production-restart"),
            smoke_results=(_check("home-mcp-smoke", passed=False),),
            completed_at="2026-08-20T12:10:00-07:00",
        ),
    )
    verification = _verification(_candidate())
    rollback_auth = _authorization(verification, action="rollback")

    rollback_plan = build_rollback_plan(
        promotion,
        stopped,
        rollback_auth,
        prepared_at="2026-08-20T12:15:00-07:00",
    )

    assert rollback_plan.status == "pending_test"
    assert rollback_plan.rollback_performed is False
    assert rollback_plan.requires_trusted_executor is True
    assert all("rm " not in step and "ssh " not in step for step in rollback_plan.steps)

    assessment = assess_rollback_plan(
        rollback_plan,
        _rollback_test(_candidate()),
        evaluated_at="2026-08-20T12:16:00-07:00",
    )
    assert assessment.status == "ready"
    assert assessment.rollback_performed is False
    assert assessment.audit.action == "rollback_test"
    assert assessment.audit.parent_operation_id == rollback_plan.audit.operation_id
    assert audit_metadata_is_valid(assessment.audit) is True


def test_rollback_plan_is_blocked_without_rollback_specific_human_approval() -> None:
    promotion = _plan()
    outcome = assess_promotion_execution(
        promotion,
        PromotionExecutionEvidence(
            executor_identity="trusted-production-runner",
            promotion_applied=True,
            restart_result=_check("production-restart"),
            smoke_results=(_check("home-mcp-smoke", passed=False),),
            completed_at="2026-08-20T12:10:00-07:00",
        ),
    )

    rollback = build_rollback_plan(promotion, outcome, None, prepared_at="2026-08-20T12:15:00-07:00")

    assert rollback.status == "blocked"
    assert "human_authorization_required" in _reason_codes(rollback)


def test_audit_metadata_and_nested_evidence_are_frozen() -> None:
    plan = _plan()

    with pytest.raises(FrozenInstanceError):
        plan.audit.status = "tampered"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        plan.audit.manifest_artifacts[0].path = "data/private.sqlite3"  # type: ignore[misc]
