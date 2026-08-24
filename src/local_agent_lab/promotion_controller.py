from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import PurePosixPath
import re
from typing import Literal

from .deployment_gate import (
    ApprovedArtifact,
    DeploymentCandidate,
    DeploymentGateDecision,
    RollbackMetadata,
    check_deployment_candidate,
)


Action = Literal["promote", "rollback", "rollback_test"]
PlanStatus = Literal["ready", "blocked", "pending_test"]

_COMMIT_PATTERN = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_DENIED_RUNTIME_PREFIXES = (
    "data/",
    ".git/",
    ".venv/",
    ".pytest_cache/",
    "__pycache__/",
)
_DENIED_RUNTIME_COMPONENTS = {".git", ".pytest_cache", ".venv", "__pycache__"}
_DENIED_CONTROL_PATHS = {
    "config/agent.yaml",
    "scripts/bootstrap_minillm_production.sh",
    "scripts/cutover_minillm_production.sh",
    "scripts/home_mcp_tunnel.sh",
    "scripts/install_minillm_admin_helper.sh",
    "scripts/minillm_admin.py",
    "scripts/provision_minillm_service_identities.sh",
    "scripts/verify_service_identity_boundaries.sh",
    "src/local_agent_lab/deployment_gate.py",
    "src/local_agent_lab/home_mcp.py",
    "src/local_agent_lab/memory/audit.py",
    "src/local_agent_lab/promotion_controller.py",
    "src/local_agent_lab/services/home_mcp_launchd.py",
    "src/local_agent_lab/trusted_promotion.py",
    "src/local_agent_lab/trusted_promotion_cli.py",
}
_SECRET_COMPONENTS = {
    ".ssh",
    "credential",
    "credentials",
    "private_key",
    "private_keys",
    "secret",
    "secrets",
    "token",
    "tokens",
}
_SECRET_SUFFIXES = (".key", ".p12", ".pem", ".pfx")
_UNSAFE_REFERENCE_TOKENS = (";", "&&", "||", "|", ">", "<", "`", "$(", "\n", "\r")


@dataclass(frozen=True)
class ControllerReason:
    code: str
    message: str
    field: str | None = None
    artifact_path: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "code": self.code,
            "message": self.message,
            "field": self.field,
            "artifact_path": self.artifact_path,
        }


@dataclass(frozen=True)
class ManifestArtifact:
    """Content identity produced by a trusted inventory of a committed tree."""

    path: str
    resolved_path: str
    content_sha256: str
    size_bytes: int

    def to_dict(self) -> dict[str, str | int]:
        return {
            "path": self.path,
            "resolved_path": self.resolved_path,
            "content_sha256": self.content_sha256,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True)
class CandidateVerification:
    repository: str
    branch: str
    candidate_commit: str
    checked_out_commit: str
    manifest_commit: str
    worktree_clean: bool
    commit_object_verified: bool
    manifest_sha256: str
    artifacts: tuple[ManifestArtifact, ...]
    verified_by: str
    verified_at: str


@dataclass(frozen=True)
class HumanAuthorization:
    authorization_id: str
    actor: str
    actor_type: Literal["human", "service"]
    decision: Literal["approve", "deny"]
    action: Literal["promote", "rollback"]
    candidate_commit: str
    previous_build: str
    manifest_sha256: str
    authorized_at: str
    expires_at: str
    reason: str
    approval_reference: str


@dataclass(frozen=True)
class CheckEvidence:
    name: str
    passed: bool
    checked_at: str
    evidence_reference: str
    candidate_build: str
    manifest_sha256: str
    details: str = ""

    def to_dict(self) -> dict[str, str | bool]:
        return {
            "name": self.name,
            "passed": self.passed,
            "checked_at": self.checked_at,
            "evidence_reference": self.evidence_reference,
            "candidate_build": self.candidate_build,
            "manifest_sha256": self.manifest_sha256,
            "details": self.details,
        }


@dataclass(frozen=True)
class RollbackTestEvidence:
    rollback_reference: str
    target_build: str
    reference_resolved: bool
    target_build_available: bool
    data_compatibility_verified: bool
    restore_procedure_tested: bool
    passed: bool
    tested_at: str
    evidence_reference: str

    def to_dict(self) -> dict[str, str | bool]:
        return {
            "rollback_reference": self.rollback_reference,
            "target_build": self.target_build,
            "reference_resolved": self.reference_resolved,
            "target_build_available": self.target_build_available,
            "data_compatibility_verified": self.data_compatibility_verified,
            "restore_procedure_tested": self.restore_procedure_tested,
            "passed": self.passed,
            "tested_at": self.tested_at,
            "evidence_reference": self.evidence_reference,
        }


@dataclass(frozen=True)
class AuditCheck:
    name: str
    passed: bool
    evidence_reference: str
    details: str = ""

    def to_dict(self) -> dict[str, str | bool]:
        return {
            "name": self.name,
            "passed": self.passed,
            "evidence_reference": self.evidence_reference,
            "details": self.details,
        }


@dataclass(frozen=True)
class AuditMetadata:
    """Frozen, content-addressed audit payload for persistence by a trusted adapter."""

    operation_id: str
    parent_operation_id: str | None
    action: Action
    status: str
    actor: str
    authorization_id: str
    verifier_identity: str
    verified_at: str
    executor_identity: str
    repository: str
    branch: str
    candidate_build: str
    previous_build: str
    manifest_sha256: str
    manifest_artifacts: tuple[ManifestArtifact, ...]
    gate_decision_sha256: str
    checks: tuple[AuditCheck, ...]
    requested_at: str
    recorded_at: str
    migration_version: str
    data_compatibility_assumptions: str
    rollback_reference: str
    restart_result: CheckEvidence | None
    smoke_results: tuple[CheckEvidence, ...]
    external_host_changes_reported: bool
    controller_host_changes_performed: bool
    record_sha256: str

    def to_dict(self) -> dict[str, object]:
        return {
            "operation_id": self.operation_id,
            "parent_operation_id": self.parent_operation_id,
            "action": self.action,
            "status": self.status,
            "actor": self.actor,
            "authorization_id": self.authorization_id,
            "verifier_identity": self.verifier_identity,
            "verified_at": self.verified_at,
            "executor_identity": self.executor_identity,
            "repository": self.repository,
            "branch": self.branch,
            "candidate_build": self.candidate_build,
            "previous_build": self.previous_build,
            "manifest_sha256": self.manifest_sha256,
            "manifest_artifacts": [item.to_dict() for item in self.manifest_artifacts],
            "gate_decision_sha256": self.gate_decision_sha256,
            "checks": [item.to_dict() for item in self.checks],
            "requested_at": self.requested_at,
            "recorded_at": self.recorded_at,
            "migration_version": self.migration_version,
            "data_compatibility_assumptions": self.data_compatibility_assumptions,
            "rollback_reference": self.rollback_reference,
            "restart_result": self.restart_result.to_dict() if self.restart_result else None,
            "smoke_results": [item.to_dict() for item in self.smoke_results],
            "external_host_changes_reported": self.external_host_changes_reported,
            "controller_host_changes_performed": self.controller_host_changes_performed,
            "record_sha256": self.record_sha256,
        }


@dataclass(frozen=True)
class PromotionPlan:
    status: Literal["ready", "blocked"]
    reasons: tuple[ControllerReason, ...]
    candidate_build: str
    previous_build: str
    builder_identity: str
    production_identity: str
    manifest_sha256: str
    approved_artifacts: tuple[ApprovedArtifact, ...]
    rollback: RollbackMetadata | None
    rollback_test: RollbackTestEvidence
    preflight_smoke_results: tuple[CheckEvidence, ...]
    audit: AuditMetadata
    requires_trusted_executor: bool = True
    promotion_performed: bool = False

    @property
    def ready(self) -> bool:
        return self.status == "ready"

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "ready": self.ready,
            "reasons": [reason.to_dict() for reason in self.reasons],
            "candidate_build": self.candidate_build,
            "previous_build": self.previous_build,
            "manifest_sha256": self.manifest_sha256,
            "approved_artifacts": [artifact.to_dict() for artifact in self.approved_artifacts],
            "rollback": _rollback_to_dict(self.rollback),
            "rollback_test": self.rollback_test.to_dict(),
            "preflight_smoke_results": [result.to_dict() for result in self.preflight_smoke_results],
            "audit": self.audit.to_dict(),
            "requires_trusted_executor": self.requires_trusted_executor,
            "promotion_performed": self.promotion_performed,
        }


@dataclass(frozen=True)
class PromotionExecutionEvidence:
    executor_identity: str
    promotion_applied: bool
    restart_result: CheckEvidence
    smoke_results: tuple[CheckEvidence, ...]
    completed_at: str


@dataclass(frozen=True)
class PromotionOutcome:
    status: Literal["completed", "stopped", "blocked", "not_executed"]
    reasons: tuple[ControllerReason, ...]
    rollback_required: bool
    further_promotion_allowed: bool
    audit: AuditMetadata
    controller_host_changes_performed: bool = False


@dataclass(frozen=True)
class RollbackPlan:
    status: PlanStatus
    reasons: tuple[ControllerReason, ...]
    candidate_build: str
    target_build: str
    rollback_reference: str
    data_compatibility_assumptions: str
    steps: tuple[str, ...]
    audit: AuditMetadata
    requires_trusted_executor: bool = True
    rollback_performed: bool = False


@dataclass(frozen=True)
class RollbackPlanAssessment:
    status: Literal["ready", "blocked"]
    reasons: tuple[ControllerReason, ...]
    rollback_plan: RollbackPlan
    test_evidence: RollbackTestEvidence
    audit: AuditMetadata
    rollback_performed: bool = False


def compute_manifest_sha256(artifacts: tuple[ManifestArtifact, ...]) -> str:
    """Return a stable digest over sorted paths and their content identities."""

    payload = [artifact.to_dict() for artifact in sorted(artifacts, key=lambda item: item.path)]
    return _sha256_json(payload)


def audit_metadata_is_valid(audit: AuditMetadata) -> bool:
    """Verify the tamper-evident digest on a frozen or deserialized audit record."""

    payload = audit.to_dict()
    claimed = payload.pop("record_sha256")
    return isinstance(claimed, str) and _SHA256_PATTERN.fullmatch(claimed) is not None and _sha256_json(payload) == claimed


def build_promotion_plan(
    candidate: DeploymentCandidate,
    gate_decision: DeploymentGateDecision,
    verification: CandidateVerification,
    authorization: HumanAuthorization | None,
    *,
    preflight_smoke_results: tuple[CheckEvidence, ...],
    rollback_test: RollbackTestEvidence,
    prepared_at: str,
) -> PromotionPlan:
    """Build an authorized plan without copying files, restarting services, or deploying."""

    reasons: list[ControllerReason] = []
    checks: list[AuditCheck] = []
    prepared_timestamp = _timestamp(prepared_at)
    if prepared_timestamp is None:
        reasons.append(ControllerReason("invalid_prepared_at", "prepared_at must be timezone-aware ISO-8601.", "prepared_at"))

    fresh_gate = check_deployment_candidate(candidate)
    gate_matches = gate_decision.to_dict() == fresh_gate.to_dict()
    checks.append(AuditCheck("fresh_gate_match", gate_matches, "deployment_gate", "Controller reran the gate."))
    if not gate_matches:
        reasons.append(
            ControllerReason(
                "gate_decision_mismatch",
                "Supplied gate result does not match a fresh check of the candidate.",
                "gate_decision",
            )
        )
    if not gate_decision.promotable or not fresh_gate.promotable:
        reasons.append(ControllerReason("gate_denied", "Both supplied and fresh check-only gate results must allow promotion.", "gate_decision"))
    gate_payload = gate_decision.to_dict()
    if gate_payload.get("check_only") is not True or gate_payload.get("deployment_performed") is not False:
        reasons.append(ControllerReason("invalid_gate_contract", "Controller accepts only a check-only, non-deploying gate result.", "gate_decision"))

    _verify_candidate_identity(candidate, verification, reasons, checks)
    _verify_manifest(candidate, gate_decision, verification, reasons, checks)
    _verify_authorization(candidate, verification, authorization, "promote", prepared_timestamp, reasons, checks)
    _verify_smoke_results(
        preflight_smoke_results,
        candidate_build=candidate.candidate_commit,
        manifest_sha256=verification.manifest_sha256,
        not_after=prepared_timestamp,
        reasons=reasons,
        checks=checks,
    )
    _verify_rollback_test(candidate.rollback, rollback_test, prepared_timestamp, reasons, checks)

    status: Literal["ready", "blocked"] = "ready" if not reasons else "blocked"
    rollback = candidate.rollback
    previous_build = rollback.previous_build if rollback else ""
    audit = _build_audit(
        action="promote",
        status=status,
        candidate=candidate,
        verification=verification,
        authorization=authorization,
        recorded_at=prepared_at,
        gate_decision=gate_decision,
        checks=tuple(checks),
        restart_result=None,
        smoke_results=preflight_smoke_results,
        external_host_changes_reported=False,
        parent_operation_id=None,
    )
    return PromotionPlan(
        status=status,
        reasons=tuple(reasons),
        candidate_build=candidate.candidate_commit,
        previous_build=previous_build,
        builder_identity=candidate.builder_identity,
        production_identity=candidate.production_identity,
        manifest_sha256=verification.manifest_sha256,
        approved_artifacts=gate_decision.approved_artifacts,
        rollback=rollback,
        rollback_test=rollback_test,
        preflight_smoke_results=preflight_smoke_results,
        audit=audit,
    )


def assess_promotion_execution(
    plan: PromotionPlan,
    evidence: PromotionExecutionEvidence,
) -> PromotionOutcome:
    """Evaluate external execution evidence; this function performs no host action."""

    reasons: list[ControllerReason] = []
    checks = list(plan.audit.checks)
    if not plan.ready:
        reasons.append(ControllerReason("promotion_plan_blocked", "A blocked promotion plan cannot be executed.", "plan.status"))
    if not evidence.executor_identity.strip():
        reasons.append(ControllerReason("missing_executor_identity", "Trusted executor identity is required.", "executor_identity"))
    if evidence.executor_identity.strip().casefold() == plan.builder_identity.strip().casefold():
        reasons.append(ControllerReason("builder_cannot_execute", "Builder identity cannot execute production promotion.", "executor_identity"))
    if _timestamp(evidence.completed_at) is None:
        reasons.append(ControllerReason("invalid_completed_at", "completed_at must be timezone-aware ISO-8601.", "completed_at"))

    checks.append(AuditCheck("external_promotion_applied", evidence.promotion_applied, evidence.executor_identity))
    completed_at = _timestamp(evidence.completed_at)
    restart_ok = _check_evidence_valid(
        evidence.restart_result,
        expected_build=plan.candidate_build,
        expected_manifest=plan.manifest_sha256,
        not_after=completed_at,
    )
    checks.append(AuditCheck("production_restart", restart_ok and evidence.restart_result.passed, evidence.restart_result.evidence_reference))
    if evidence.promotion_applied and (not restart_ok or not evidence.restart_result.passed):
        reasons.append(ControllerReason("restart_failed", "Promotion stopped because the production restart failed.", "restart_result"))

    if not evidence.smoke_results:
        reasons.append(ControllerReason("missing_post_promotion_smoke", "At least one post-promotion smoke result is required.", "smoke_results"))
    failed_smoke = False
    for result in evidence.smoke_results:
        valid = _check_evidence_valid(
            result,
            expected_build=plan.candidate_build,
            expected_manifest=plan.manifest_sha256,
            not_after=completed_at,
        )
        passed = valid and result.passed
        checks.append(AuditCheck(f"post_smoke:{result.name or 'unnamed'}", passed, result.evidence_reference, result.details))
        if not passed:
            failed_smoke = True
    if evidence.promotion_applied and failed_smoke:
        reasons.append(ControllerReason("post_promotion_smoke_failed", "Promotion stopped at the first failed smoke boundary; rollback is required.", "smoke_results"))

    if not plan.ready and evidence.promotion_applied:
        reasons.append(ControllerReason("blocked_plan_was_applied", "External changes from a blocked plan must stop and roll back.", "plan.status"))
        status: Literal["completed", "stopped", "blocked", "not_executed"] = "stopped"
    elif not plan.ready:
        status = "blocked"
    elif not evidence.promotion_applied:
        status = "not_executed"
    elif reasons:
        status = "stopped"
    else:
        status = "completed"
    rollback_required = evidence.promotion_applied and status == "stopped"
    further_allowed = status == "completed"
    audit = _clone_audit(
        plan.audit,
        action="promote",
        status=status,
        checks=tuple(checks),
        recorded_at=evidence.completed_at,
        restart_result=evidence.restart_result,
        smoke_results=evidence.smoke_results,
        external_host_changes_reported=evidence.promotion_applied,
        parent_operation_id=plan.audit.operation_id,
        executor_identity=evidence.executor_identity,
    )
    return PromotionOutcome(
        status=status,
        reasons=tuple(reasons),
        rollback_required=rollback_required,
        further_promotion_allowed=further_allowed,
        audit=audit,
    )


def build_rollback_plan(
    promotion_plan: PromotionPlan,
    outcome: PromotionOutcome,
    authorization: HumanAuthorization | None,
    *,
    prepared_at: str,
) -> RollbackPlan:
    """Create an opaque rollback plan; execution remains the trusted adapter's job."""

    reasons: list[ControllerReason] = []
    checks: list[AuditCheck] = []
    prepared_timestamp = _timestamp(prepared_at)
    if prepared_timestamp is None:
        reasons.append(ControllerReason("invalid_prepared_at", "prepared_at must be timezone-aware ISO-8601.", "prepared_at"))
    if not promotion_plan.ready and not outcome.rollback_required:
        reasons.append(ControllerReason("promotion_plan_blocked", "Rollback needs either an approved promotion or reported external changes requiring recovery.", "promotion_plan.status"))
    if outcome.status not in {"completed", "stopped"}:
        reasons.append(ControllerReason("rollback_not_applicable", "Rollback requires a completed or stopped external promotion attempt.", "outcome.status"))
    verification = _verification_from_plan(promotion_plan)
    candidate = _candidate_from_plan(promotion_plan)
    _verify_authorization(candidate, verification, authorization, "rollback", prepared_timestamp, reasons, checks)

    rollback = promotion_plan.rollback
    target = rollback.previous_build if rollback else ""
    reference = rollback.rollback_reference if rollback else ""
    steps = (
        f"Verify production currently identifies candidate build {promotion_plan.candidate_build}.",
        f"Resolve the opaque rollback reference {reference} in the trusted executor.",
        f"Restore code/config artifact identity for previous build {target}; do not copy runtime data or secrets.",
        "Restart the production service through its trusted service manager.",
        "Run the required post-rollback smoke suite and persist its evidence.",
    )
    status: PlanStatus = "pending_test" if not reasons else "blocked"
    audit = _build_audit(
        action="rollback",
        status=status,
        candidate=candidate,
        verification=verification,
        authorization=authorization,
        recorded_at=prepared_at,
        gate_decision=_gate_from_plan(promotion_plan),
        checks=tuple(checks),
        restart_result=None,
        smoke_results=(),
        external_host_changes_reported=False,
        parent_operation_id=outcome.audit.operation_id,
    )
    return RollbackPlan(
        status=status,
        reasons=tuple(reasons),
        candidate_build=promotion_plan.candidate_build,
        target_build=target,
        rollback_reference=reference,
        data_compatibility_assumptions=rollback.data_compatibility_assumptions if rollback else "",
        steps=steps,
        audit=audit,
    )


def test_rollback_plan(
    plan: RollbackPlan,
    evidence: RollbackTestEvidence,
    *,
    evaluated_at: str,
) -> RollbackPlanAssessment:
    """Verify rollback readiness without resolving references or changing the host."""

    reasons = list(plan.reasons)
    checks = list(plan.audit.checks)
    if plan.status == "blocked":
        reasons.append(ControllerReason("rollback_plan_blocked", "A blocked rollback plan cannot pass readiness testing.", "plan.status"))
    _verify_rollback_test_values(plan.rollback_reference, plan.target_build, evidence, reasons, checks)
    if _timestamp(evaluated_at) is None:
        reasons.append(ControllerReason("invalid_evaluated_at", "evaluated_at must be timezone-aware ISO-8601.", "evaluated_at"))
    status: Literal["ready", "blocked"] = "ready" if not reasons else "blocked"
    audit = _clone_audit(
        plan.audit,
        action="rollback_test",
        status=status,
        checks=tuple(checks),
        recorded_at=evaluated_at,
        restart_result=None,
        smoke_results=(),
        external_host_changes_reported=False,
        parent_operation_id=plan.audit.operation_id,
        executor_identity=plan.audit.executor_identity,
    )
    return RollbackPlanAssessment(
        status=status,
        reasons=tuple(reasons),
        rollback_plan=plan,
        test_evidence=evidence,
        audit=audit,
    )


def _verify_candidate_identity(
    candidate: DeploymentCandidate,
    verification: CandidateVerification,
    reasons: list[ControllerReason],
    checks: list[AuditCheck],
) -> None:
    commit = candidate.candidate_commit.casefold()
    immutable_commit = bool(_COMMIT_PATTERN.fullmatch(commit))
    checks.append(AuditCheck("immutable_commit_identity", immutable_commit, verification.verified_by))
    if not immutable_commit:
        reasons.append(ControllerReason("nonimmutable_candidate_commit", "Candidate must use a full 40- or 64-character hexadecimal commit ID.", "candidate_commit"))
    comparisons = (
        ("repository", verification.repository, candidate.repository),
        ("branch", verification.branch, candidate.branch),
        ("candidate_commit", verification.candidate_commit.casefold(), commit),
        ("checked_out_commit", verification.checked_out_commit.casefold(), commit),
        ("manifest_commit", verification.manifest_commit.casefold(), commit),
    )
    for field, actual, expected in comparisons:
        if actual != expected:
            reasons.append(ControllerReason("candidate_identity_mismatch", f"Verified {field} does not match the gated candidate.", f"verification.{field}"))
    if not verification.commit_object_verified:
        reasons.append(ControllerReason("unverified_commit_object", "Trusted inventory must verify that the candidate commit object exists.", "verification.commit_object_verified"))
    if not verification.worktree_clean:
        reasons.append(ControllerReason("dirty_worktree", "Promotion refuses dirty or uncommitted worktrees.", "verification.worktree_clean"))
    verifier = verification.verified_by.strip().casefold()
    if not verifier:
        reasons.append(ControllerReason("missing_verifier_identity", "Independent verifier identity is required.", "verification.verified_by"))
    if verifier in {candidate.builder_identity.strip().casefold(), candidate.production_identity.strip().casefold()}:
        reasons.append(ControllerReason("nonindependent_verifier", "Verifier must be distinct from builder and production service identities.", "verification.verified_by"))
    if _timestamp(verification.verified_at) is None:
        reasons.append(ControllerReason("invalid_verified_at", "verified_at must be timezone-aware ISO-8601.", "verification.verified_at"))
    checks.append(AuditCheck("clean_committed_candidate", not any(reason.code in {"candidate_identity_mismatch", "unverified_commit_object", "dirty_worktree", "nonimmutable_candidate_commit"} for reason in reasons), verification.verified_by))


def _verify_manifest(
    candidate: DeploymentCandidate,
    gate_decision: DeploymentGateDecision,
    verification: CandidateVerification,
    reasons: list[ControllerReason],
    checks: list[AuditCheck],
) -> None:
    paths = [artifact.path for artifact in verification.artifacts]
    if len(paths) != len(set(paths)):
        reasons.append(ControllerReason("duplicate_manifest_path", "Manifest paths must be unique.", "verification.artifacts"))
    for artifact in verification.artifacts:
        denial = _controller_artifact_denial(artifact.path, artifact.resolved_path)
        if denial:
            reasons.append(ControllerReason(denial, "Controller independently denies runtime, secret, or control-plane artifacts.", "verification.artifacts", artifact.path))
        if not _SHA256_PATTERN.fullmatch(artifact.content_sha256.casefold()):
            reasons.append(ControllerReason("invalid_artifact_digest", "Every artifact requires a full SHA-256 content digest.", "verification.artifacts.content_sha256", artifact.path))
        if artifact.size_bytes < 0:
            reasons.append(ControllerReason("invalid_artifact_size", "Artifact size cannot be negative.", "verification.artifacts.size_bytes", artifact.path))
    computed = compute_manifest_sha256(verification.artifacts)
    manifest_matches = _SHA256_PATTERN.fullmatch(verification.manifest_sha256.casefold()) is not None and computed == verification.manifest_sha256.casefold()
    checks.append(AuditCheck("manifest_digest", manifest_matches, f"sha256:{computed}"))
    if not manifest_matches:
        reasons.append(ControllerReason("manifest_digest_mismatch", "Manifest digest does not match its canonical artifact inventory.", "verification.manifest_sha256"))

    declared = {(artifact.path, artifact.resolved_path or "") for artifact in candidate.artifacts}
    inventoried = {(artifact.path, artifact.resolved_path) for artifact in verification.artifacts}
    approved = {artifact.path for artifact in gate_decision.approved_artifacts}
    inventory_matches = declared == inventoried and approved == {path for path, _ in inventoried}
    checks.append(AuditCheck("manifest_inventory", inventory_matches, verification.verified_by))
    if not inventory_matches:
        reasons.append(ControllerReason("manifest_inventory_mismatch", "Committed manifest must exactly match gated and approved artifact inventories.", "verification.artifacts"))


def _verify_authorization(
    candidate: DeploymentCandidate,
    verification: CandidateVerification,
    authorization: HumanAuthorization | None,
    action: Literal["promote", "rollback"],
    prepared_at: datetime | None,
    reasons: list[ControllerReason],
    checks: list[AuditCheck],
) -> None:
    if authorization is None:
        reasons.append(ControllerReason("human_authorization_required", "Explicit human authorization data is required.", "authorization"))
        checks.append(AuditCheck("human_authorization", False, "missing"))
        return
    valid = True
    required = {
        "authorization_id": authorization.authorization_id,
        "actor": authorization.actor,
        "reason": authorization.reason,
        "approval_reference": authorization.approval_reference,
    }
    for field, value in required.items():
        if not value.strip():
            reasons.append(ControllerReason("incomplete_authorization", f"Authorization field {field} is required.", f"authorization.{field}"))
            valid = False
    if authorization.actor_type != "human" or authorization.decision != "approve":
        reasons.append(ControllerReason("human_approval_missing", "Authorization must be an affirmative human approval.", "authorization"))
        valid = False
    if authorization.action != action:
        reasons.append(ControllerReason("authorization_action_mismatch", f"Authorization must explicitly approve {action}.", "authorization.action"))
        valid = False
    rollback = candidate.rollback
    expected_previous = rollback.previous_build if rollback else ""
    for field, actual, expected in (
        ("candidate_commit", authorization.candidate_commit.casefold(), candidate.candidate_commit.casefold()),
        ("previous_build", authorization.previous_build, expected_previous),
        ("manifest_sha256", authorization.manifest_sha256.casefold(), verification.manifest_sha256.casefold()),
    ):
        if actual != expected:
            reasons.append(ControllerReason("authorization_scope_mismatch", f"Authorization {field} does not match this plan.", f"authorization.{field}"))
            valid = False
    actor = authorization.actor.strip().casefold()
    if actor in {candidate.builder_identity.strip().casefold(), candidate.production_identity.strip().casefold()}:
        reasons.append(ControllerReason("unauthorized_actor_identity", "Human actor must be distinct from builder and production service identities.", "authorization.actor"))
        valid = False
    authorized_at = _timestamp(authorization.authorized_at)
    expires_at = _timestamp(authorization.expires_at)
    if authorized_at is None or expires_at is None:
        reasons.append(ControllerReason("invalid_authorization_time", "Authorization timestamps must be timezone-aware ISO-8601.", "authorization"))
        valid = False
    elif authorized_at > expires_at:
        reasons.append(ControllerReason("invalid_authorization_window", "Authorization expires before it is granted.", "authorization.expires_at"))
        valid = False
    elif prepared_at is not None and not (authorized_at <= prepared_at <= expires_at):
        reasons.append(ControllerReason("authorization_not_current", "Authorization is not valid at plan preparation time.", "authorization.expires_at"))
        valid = False
    if not _opaque_reference(authorization.approval_reference):
        reasons.append(ControllerReason("unsafe_approval_reference", "Approval reference must be opaque data, not shell text.", "authorization.approval_reference"))
        valid = False
    checks.append(AuditCheck("human_authorization", valid, authorization.approval_reference, authorization.reason))


def _verify_smoke_results(
    results: tuple[CheckEvidence, ...],
    *,
    candidate_build: str,
    manifest_sha256: str,
    not_after: datetime | None,
    reasons: list[ControllerReason],
    checks: list[AuditCheck],
) -> None:
    if not results:
        reasons.append(ControllerReason("missing_preflight_smoke", "At least one preflight smoke result is required.", "preflight_smoke_results"))
        return
    names: set[str] = set()
    for result in results:
        valid = _check_evidence_valid(
            result,
            expected_build=candidate_build,
            expected_manifest=manifest_sha256,
            not_after=not_after,
        )
        if result.name in names:
            valid = False
            reasons.append(ControllerReason("duplicate_smoke_name", "Smoke result names must be unique.", "preflight_smoke_results", result.name))
        names.add(result.name)
        passed = valid and result.passed
        checks.append(AuditCheck(f"preflight_smoke:{result.name or 'unnamed'}", passed, result.evidence_reference, result.details))
        if not passed:
            reasons.append(ControllerReason("preflight_smoke_failed", "Promotion stops when any preflight smoke result fails or lacks valid evidence.", "preflight_smoke_results", result.name))


def _verify_rollback_test(
    rollback: RollbackMetadata | None,
    evidence: RollbackTestEvidence,
    not_after: datetime | None,
    reasons: list[ControllerReason],
    checks: list[AuditCheck],
) -> None:
    if rollback is None:
        reasons.append(ControllerReason("missing_rollback_metadata", "Rollback metadata is required.", "rollback"))
        return
    _verify_rollback_test_values(
        rollback.rollback_reference,
        rollback.previous_build,
        evidence,
        reasons,
        checks,
        not_after=not_after,
    )


def _verify_rollback_test_values(
    rollback_reference: str,
    target_build: str,
    evidence: RollbackTestEvidence,
    reasons: list[ControllerReason],
    checks: list[AuditCheck],
    *,
    not_after: datetime | None = None,
) -> None:
    valid = True
    if evidence.rollback_reference != rollback_reference or evidence.target_build != target_build:
        reasons.append(ControllerReason("rollback_test_scope_mismatch", "Rollback test must match the plan reference and target build.", "rollback_test"))
        valid = False
    if not all((evidence.reference_resolved, evidence.target_build_available, evidence.data_compatibility_verified, evidence.restore_procedure_tested, evidence.passed)):
        reasons.append(ControllerReason("rollback_test_failed", "Rollback reference, target, compatibility, and restore procedure must all pass testing.", "rollback_test"))
        valid = False
    tested_at = _timestamp(evidence.tested_at)
    if tested_at is None or not evidence.evidence_reference.strip():
        reasons.append(ControllerReason("invalid_rollback_test_evidence", "Rollback test needs a timestamp and evidence reference.", "rollback_test"))
        valid = False
    elif not_after is not None and tested_at > not_after:
        reasons.append(ControllerReason("future_rollback_test_evidence", "Rollback test cannot postdate plan preparation.", "rollback_test.tested_at"))
        valid = False
    checks.append(AuditCheck("rollback_readiness", valid, evidence.evidence_reference, f"target={target_build}"))


def _check_evidence_valid(
    evidence: CheckEvidence,
    *,
    expected_build: str,
    expected_manifest: str,
    not_after: datetime | None,
) -> bool:
    checked_at = _timestamp(evidence.checked_at)
    return bool(
        evidence.name.strip()
        and evidence.evidence_reference.strip()
        and checked_at is not None
        and (not_after is None or checked_at <= not_after)
        and evidence.candidate_build.casefold() == expected_build.casefold()
        and evidence.manifest_sha256.casefold() == expected_manifest.casefold()
    )


def _controller_artifact_denial(path: str, resolved_path: str) -> str | None:
    if path != resolved_path:
        return "artifact_resolution_mismatch"
    if not _safe_repo_path(path):
        return "invalid_artifact_path"
    lower = path.casefold()
    if any(lower.startswith(prefix) for prefix in _DENIED_RUNTIME_PREFIXES):
        return "runtime_data_path_denied"
    if lower in _DENIED_CONTROL_PATHS:
        return "control_plane_path_denied"
    parts = tuple(part.casefold() for part in PurePosixPath(path).parts)
    basename = parts[-1]
    if any(part in _DENIED_RUNTIME_COMPONENTS for part in parts):
        return "runtime_data_path_denied"
    if (
        any(part in _SECRET_COMPONENTS for part in parts)
        or basename == ".env"
        or basename.startswith(".env.")
        or basename in {"id_dsa", "id_ed25519", "id_rsa"}
        or basename.endswith(_SECRET_SUFFIXES)
        or any(token in basename for token in ("credential", "secret", "token"))
    ):
        return "credential_path_denied"
    if lower.startswith("config/") and any(token in basename for token in ("auth", "production", "tunnel")):
        return "control_plane_path_denied"
    return None


def _safe_repo_path(value: str) -> bool:
    if not value or "\x00" in value or "\\" in value or value.startswith("/") or value.endswith("/"):
        return False
    return all(part not in {"", ".", ".."} for part in value.split("/"))


def _timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def _opaque_reference(value: str) -> bool:
    return bool(value.strip()) and not any(token in value for token in _UNSAFE_REFERENCE_TOKENS)


def _rollback_to_dict(rollback: RollbackMetadata | None) -> dict[str, str] | None:
    if rollback is None:
        return None
    return {
        "previous_build": rollback.previous_build,
        "candidate_build": rollback.candidate_build,
        "migration_version": rollback.migration_version,
        "rollback_reference": rollback.rollback_reference,
        "data_compatibility_assumptions": rollback.data_compatibility_assumptions,
    }


def _build_audit(
    *,
    action: Action,
    status: str,
    candidate: DeploymentCandidate,
    verification: CandidateVerification,
    authorization: HumanAuthorization | None,
    recorded_at: str,
    gate_decision: DeploymentGateDecision,
    checks: tuple[AuditCheck, ...],
    restart_result: CheckEvidence | None,
    smoke_results: tuple[CheckEvidence, ...],
    external_host_changes_reported: bool,
    parent_operation_id: str | None,
) -> AuditMetadata:
    rollback = candidate.rollback or RollbackMetadata()
    base: dict[str, object] = {
        "parent_operation_id": parent_operation_id,
        "action": action,
        "status": status,
        "actor": authorization.actor if authorization else "",
        "authorization_id": authorization.authorization_id if authorization else "",
        "verifier_identity": verification.verified_by,
        "verified_at": verification.verified_at,
        "executor_identity": "",
        "repository": candidate.repository,
        "branch": candidate.branch,
        "candidate_build": candidate.candidate_commit,
        "previous_build": rollback.previous_build,
        "manifest_sha256": verification.manifest_sha256,
        "manifest_artifacts": [item.to_dict() for item in verification.artifacts],
        "gate_decision_sha256": _sha256_json(gate_decision.to_dict()),
        "checks": [item.to_dict() for item in checks],
        "requested_at": authorization.authorized_at if authorization else "",
        "recorded_at": recorded_at,
        "migration_version": rollback.migration_version,
        "data_compatibility_assumptions": rollback.data_compatibility_assumptions,
        "rollback_reference": rollback.rollback_reference,
        "restart_result": restart_result.to_dict() if restart_result else None,
        "smoke_results": [item.to_dict() for item in smoke_results],
        "external_host_changes_reported": external_host_changes_reported,
        "controller_host_changes_performed": False,
    }
    operation_id = f"{action}-{_sha256_json(base)[:20]}"
    payload = {"operation_id": operation_id, **base}
    return AuditMetadata(
        operation_id=operation_id,
        parent_operation_id=parent_operation_id,
        action=action,
        status=status,
        actor=str(base["actor"]),
        authorization_id=str(base["authorization_id"]),
        verifier_identity=verification.verified_by,
        verified_at=verification.verified_at,
        executor_identity="",
        repository=candidate.repository,
        branch=candidate.branch,
        candidate_build=candidate.candidate_commit,
        previous_build=rollback.previous_build,
        manifest_sha256=verification.manifest_sha256,
        manifest_artifacts=verification.artifacts,
        gate_decision_sha256=str(base["gate_decision_sha256"]),
        checks=checks,
        requested_at=str(base["requested_at"]),
        recorded_at=recorded_at,
        migration_version=rollback.migration_version,
        data_compatibility_assumptions=rollback.data_compatibility_assumptions,
        rollback_reference=rollback.rollback_reference,
        restart_result=restart_result,
        smoke_results=smoke_results,
        external_host_changes_reported=external_host_changes_reported,
        controller_host_changes_performed=False,
        record_sha256=_sha256_json(payload),
    )


def _clone_audit(
    source: AuditMetadata,
    *,
    action: Action,
    status: str,
    checks: tuple[AuditCheck, ...],
    recorded_at: str,
    restart_result: CheckEvidence | None,
    smoke_results: tuple[CheckEvidence, ...],
    external_host_changes_reported: bool,
    parent_operation_id: str | None,
    executor_identity: str,
) -> AuditMetadata:
    base: dict[str, object] = {
        "parent_operation_id": parent_operation_id,
        "action": action,
        "status": status,
        "actor": source.actor,
        "authorization_id": source.authorization_id,
        "verifier_identity": source.verifier_identity,
        "verified_at": source.verified_at,
        "executor_identity": executor_identity,
        "repository": source.repository,
        "branch": source.branch,
        "candidate_build": source.candidate_build,
        "previous_build": source.previous_build,
        "manifest_sha256": source.manifest_sha256,
        "manifest_artifacts": [item.to_dict() for item in source.manifest_artifacts],
        "gate_decision_sha256": source.gate_decision_sha256,
        "checks": [item.to_dict() for item in checks],
        "requested_at": source.requested_at,
        "recorded_at": recorded_at,
        "migration_version": source.migration_version,
        "data_compatibility_assumptions": source.data_compatibility_assumptions,
        "rollback_reference": source.rollback_reference,
        "restart_result": restart_result.to_dict() if restart_result else None,
        "smoke_results": [item.to_dict() for item in smoke_results],
        "external_host_changes_reported": external_host_changes_reported,
        "controller_host_changes_performed": False,
    }
    operation_id = f"{action}-{_sha256_json(base)[:20]}"
    payload = {"operation_id": operation_id, **base}
    return AuditMetadata(
        operation_id=operation_id,
        parent_operation_id=parent_operation_id,
        action=action,
        status=status,
        actor=source.actor,
        authorization_id=source.authorization_id,
        verifier_identity=source.verifier_identity,
        verified_at=source.verified_at,
        executor_identity=executor_identity,
        repository=source.repository,
        branch=source.branch,
        candidate_build=source.candidate_build,
        previous_build=source.previous_build,
        manifest_sha256=source.manifest_sha256,
        manifest_artifacts=source.manifest_artifacts,
        gate_decision_sha256=source.gate_decision_sha256,
        checks=checks,
        requested_at=source.requested_at,
        recorded_at=recorded_at,
        migration_version=source.migration_version,
        data_compatibility_assumptions=source.data_compatibility_assumptions,
        rollback_reference=source.rollback_reference,
        restart_result=restart_result,
        smoke_results=smoke_results,
        external_host_changes_reported=external_host_changes_reported,
        controller_host_changes_performed=False,
        record_sha256=_sha256_json(payload),
    )


def _verification_from_plan(plan: PromotionPlan) -> CandidateVerification:
    return CandidateVerification(
        repository=plan.audit.repository,
        branch=plan.audit.branch,
        candidate_commit=plan.candidate_build,
        checked_out_commit=plan.candidate_build,
        manifest_commit=plan.candidate_build,
        worktree_clean=True,
        commit_object_verified=True,
        manifest_sha256=plan.manifest_sha256,
        artifacts=plan.audit.manifest_artifacts,
        verified_by="trusted-promotion-controller",
        verified_at=plan.audit.recorded_at,
    )


def _candidate_from_plan(plan: PromotionPlan) -> DeploymentCandidate:
    from .deployment_gate import DeploymentArtifact

    return DeploymentCandidate(
        repository=plan.audit.repository,
        branch=plan.audit.branch,
        candidate_commit=plan.candidate_build,
        builder_identity=plan.builder_identity,
        production_identity=plan.production_identity,
        staging_runtime_root="data/local_staging",
        production_runtime_root="data",
        artifacts=tuple(DeploymentArtifact(item.path, item.resolved_path) for item in plan.audit.manifest_artifacts),
        rollback=plan.rollback,
        tests_passed=True,
        evals_passed=True,
        security_checks_passed=True,
        migration_compatible=True,
    )


def _gate_from_plan(plan: PromotionPlan) -> DeploymentGateDecision:
    return check_deployment_candidate(_candidate_from_plan(plan))


def _sha256_json(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
