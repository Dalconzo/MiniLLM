from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal


ROLLBACK_METADATA_REQUIREMENTS = (
    "previous_build",
    "candidate_build",
    "migration_version",
    "rollback_reference",
    "data_compatibility_assumptions",
)


@dataclass(frozen=True)
class DeploymentArtifact:
    """A candidate artifact and the path resolved by a trusted inventory step."""

    path: str
    resolved_path: str | None


@dataclass(frozen=True)
class RollbackMetadata:
    previous_build: str = ""
    candidate_build: str = ""
    migration_version: str = ""
    rollback_reference: str = ""
    data_compatibility_assumptions: str = ""


@dataclass(frozen=True)
class DeploymentCandidate:
    repository: str
    branch: str
    candidate_commit: str
    builder_identity: str
    production_identity: str
    staging_runtime_root: str
    production_runtime_root: str
    artifacts: tuple[DeploymentArtifact, ...]
    rollback: RollbackMetadata | None
    tests_passed: bool
    evals_passed: bool
    security_checks_passed: bool
    migration_compatible: bool


@dataclass(frozen=True)
class DeploymentGatePolicy:
    repository: str = "MiniLLM"
    allowed_branches: tuple[str, ...] = ("main",)
    staging_runtime_root: str = "data/local_staging"
    production_runtime_root: str = "data"


@dataclass(frozen=True)
class GateReason:
    code: str
    message: str
    field: str | None = None
    artifact_path: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": self.message,
            "field": self.field,
            "artifact_path": self.artifact_path,
        }


@dataclass(frozen=True)
class ApprovedArtifact:
    path: str
    category: Literal["source", "test", "config", "documentation"]

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "category": self.category}


@dataclass(frozen=True)
class DeploymentGateDecision:
    promotable: bool
    reasons: tuple[GateReason, ...]
    approved_artifacts: tuple[ApprovedArtifact, ...]
    rollback_requirements: tuple[str, ...]
    missing_rollback_fields: tuple[str, ...]

    @property
    def status(self) -> Literal["allow", "deny"]:
        return "allow" if self.promotable else "deny"

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "promotable": self.promotable,
            "check_only": True,
            "deployment_performed": False,
            "reasons": [reason.to_dict() for reason in self.reasons],
            "approved_artifacts": [artifact.to_dict() for artifact in self.approved_artifacts],
            "rollback_requirements": list(self.rollback_requirements),
            "missing_rollback_fields": list(self.missing_rollback_fields),
        }


V1_DEPLOYMENT_POLICY = DeploymentGatePolicy()

_ALLOWED_PATH_RULES: tuple[
    tuple[str, tuple[str, ...], Literal["source", "test", "config", "documentation"]], ...
] = (
    ("src/local_agent_lab/", (".py",), "source"),
    ("tests/", (".py",), "test"),
    ("config/", (".json", ".toml", ".yaml", ".yml", ".md"), "config"),
    ("docs/", (".md",), "documentation"),
)

_DENIED_RUNTIME_PREFIXES = (
    "data/",
    ".git/",
    ".venv/",
    ".pytest_cache/",
    "__pycache__/",
)

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
    "src/local_agent_lab/services/home_mcp_launchd.py",
    "src/local_agent_lab/trusted_promotion.py",
    "src/local_agent_lab/trusted_promotion_cli.py",
}

_DENIED_SECRET_COMPONENTS = {
    ".ssh",
    "credential",
    "credentials",
    "private_key",
    "private_keys",
    "secret",
    "secrets",
    "tokens",
}

_DENIED_SECRET_SUFFIXES = (".key", ".p12", ".pem", ".pfx")


def check_deployment_candidate(
    candidate: DeploymentCandidate,
    *,
    policy: DeploymentGatePolicy = V1_DEPLOYMENT_POLICY,
) -> DeploymentGateDecision:
    """Classify a candidate without deploying, executing commands, or writing files."""

    reasons: list[GateReason] = []
    approved: list[ApprovedArtifact] = []

    _check_environment(candidate, policy, reasons)
    _check_build_evidence(candidate, policy, reasons)

    for artifact in candidate.artifacts:
        artifact_reasons, approved_artifact = _check_artifact(artifact)
        reasons.extend(artifact_reasons)
        if approved_artifact is not None:
            approved.append(approved_artifact)

    if not candidate.artifacts:
        reasons.append(GateReason("empty_artifact_set", "At least one candidate artifact is required.", "artifacts"))

    missing_rollback = _missing_rollback_fields(candidate.rollback)
    if missing_rollback:
        reasons.append(
            GateReason(
                "missing_rollback_metadata",
                "Rollback metadata is incomplete: " + ", ".join(missing_rollback),
                "rollback",
            )
        )
    elif candidate.rollback is not None:
        if candidate.rollback.candidate_build != candidate.candidate_commit:
            reasons.append(
                GateReason(
                    "rollback_candidate_mismatch",
                    "Rollback candidate_build must match the checked candidate commit.",
                    "rollback.candidate_build",
                )
            )
        if candidate.rollback.previous_build == candidate.rollback.candidate_build:
            reasons.append(
                GateReason(
                    "rollback_has_no_previous_build",
                    "Rollback previous_build must differ from candidate_build.",
                    "rollback.previous_build",
                )
            )
        if not _valid_rollback_reference(candidate.rollback.rollback_reference):
            reasons.append(
                GateReason(
                    "unsafe_rollback_reference",
                    "Rollback must be an opaque git-ref, artifact, or procedure reference; shell text is not accepted.",
                    "rollback.rollback_reference",
                )
            )

    promotable = not reasons
    if promotable:
        reasons.append(
            GateReason(
                "candidate_checks_passed",
                "Candidate satisfies the bounded v1 deployment policy; human-triggered promotion is still required.",
            )
        )

    return DeploymentGateDecision(
        promotable=promotable,
        reasons=tuple(reasons),
        approved_artifacts=tuple(approved),
        rollback_requirements=ROLLBACK_METADATA_REQUIREMENTS,
        missing_rollback_fields=missing_rollback,
    )


def _check_environment(
    candidate: DeploymentCandidate,
    policy: DeploymentGatePolicy,
    reasons: list[GateReason],
) -> None:
    builder = candidate.builder_identity.strip().casefold()
    production = candidate.production_identity.strip().casefold()
    if not builder:
        reasons.append(GateReason("missing_builder_identity", "Builder identity is required.", "builder_identity"))
    if not production:
        reasons.append(GateReason("missing_production_identity", "Production identity is required.", "production_identity"))
    if builder and builder == production:
        reasons.append(
            GateReason(
                "identity_boundary_violation",
                "Builder and production identities must be distinct.",
                "builder_identity",
            )
        )

    _check_exact_root(
        candidate.staging_runtime_root,
        policy.staging_runtime_root,
        "staging_runtime_root",
        "unexpected_staging_root",
        reasons,
    )
    _check_exact_root(
        candidate.production_runtime_root,
        policy.production_runtime_root,
        "production_runtime_root",
        "unexpected_production_root",
        reasons,
    )
    if _normalized_root(candidate.staging_runtime_root) == _normalized_root(candidate.production_runtime_root):
        reasons.append(
            GateReason(
                "runtime_boundary_violation",
                "Staging and production runtime roots must be distinct.",
                "staging_runtime_root",
            )
        )


def _check_build_evidence(
    candidate: DeploymentCandidate,
    policy: DeploymentGatePolicy,
    reasons: list[GateReason],
) -> None:
    if candidate.repository != policy.repository:
        reasons.append(GateReason("unknown_repository", "Candidate repository is not allowlisted.", "repository"))
    if candidate.branch not in policy.allowed_branches:
        reasons.append(GateReason("unexpected_branch", "Candidate branch is not allowlisted.", "branch"))
    if not candidate.candidate_commit.strip():
        reasons.append(GateReason("missing_candidate_commit", "Candidate commit is required.", "candidate_commit"))
    for field_name, passed in (
        ("tests_passed", candidate.tests_passed),
        ("evals_passed", candidate.evals_passed),
        ("security_checks_passed", candidate.security_checks_passed),
        ("migration_compatible", candidate.migration_compatible),
    ):
        if not passed:
            reasons.append(
                GateReason(
                    "required_check_failed",
                    f"Required deployment evidence is false: {field_name}.",
                    field_name,
                )
            )


def _check_artifact(artifact: DeploymentArtifact) -> tuple[list[GateReason], ApprovedArtifact | None]:
    reasons: list[GateReason] = []
    declared, declared_error = _validate_repo_relative_path(artifact.path)
    if declared_error:
        return [GateReason(declared_error, "Artifact path is not a safe repo-relative path.", "artifacts", artifact.path)], None

    if artifact.resolved_path is None:
        return [
            GateReason(
                "missing_resolved_path",
                "Trusted resolved-path metadata is required to reject symlink escapes.",
                "artifacts.resolved_path",
                declared,
            )
        ], None

    resolved, resolved_error = _validate_repo_relative_path(artifact.resolved_path)
    if resolved_error:
        return [
            GateReason(
                resolved_error,
                "Resolved artifact path is not a safe repo-relative path.",
                "artifacts.resolved_path",
                artifact.resolved_path,
            )
        ], None
    if resolved != declared:
        return [
            GateReason(
                "artifact_resolution_mismatch",
                "Declared and resolved artifact paths differ; possible symlink redirection is denied.",
                "artifacts.resolved_path",
                declared,
            )
        ], None

    denial = _denied_artifact_reason(declared)
    if denial is not None:
        return [GateReason(denial[0], denial[1], "artifacts", declared)], None

    category = _allowed_artifact_category(declared)
    if category is None:
        reasons.append(
            GateReason(
                "artifact_not_allowlisted",
                "Artifact is outside the explicit source/config/test/documentation allowlist.",
                "artifacts",
                declared,
            )
        )
        return reasons, None
    return reasons, ApprovedArtifact(path=declared, category=category)


def _denied_artifact_reason(path: str) -> tuple[str, str] | None:
    lower = path.casefold()
    if any(lower.startswith(prefix) for prefix in _DENIED_RUNTIME_PREFIXES):
        return "runtime_data_path_denied", "Runtime, production data, and audit-log paths cannot be deployed."
    if lower in _DENIED_CONTROL_PATHS:
        return "control_plane_path_denied", "Production auth, tunnel, audit, or deployment-gate code requires a separate human security review."

    parts = tuple(part.casefold() for part in PurePosixPath(path).parts)
    basename = parts[-1]
    if (
        any(part in _DENIED_SECRET_COMPONENTS for part in parts)
        or basename == ".env"
        or basename.startswith(".env.")
        or basename in {"id_dsa", "id_ed25519", "id_rsa"}
        or basename.endswith(_DENIED_SECRET_SUFFIXES)
        or any(token in basename for token in ("credential", "secret", "token"))
    ):
        return "credential_path_denied", "Credential, token, private-key, and secret paths cannot be deployed."

    if lower.startswith("config/") and any(token in basename for token in ("auth", "production", "tunnel")):
        return "control_plane_path_denied", "Production auth and tunnel configuration requires a separate human security review."
    return None


def _allowed_artifact_category(
    path: str,
) -> Literal["source", "test", "config", "documentation"] | None:
    suffix = PurePosixPath(path).suffix.casefold()
    for prefix, suffixes, category in _ALLOWED_PATH_RULES:
        if path.startswith(prefix) and suffix in suffixes:
            return category
    return None


def _validate_repo_relative_path(value: str) -> tuple[str, str | None]:
    if not value or "\x00" in value or "\\" in value:
        return value, "invalid_artifact_path"
    if value.startswith("/") or value.endswith("/"):
        return value, "invalid_artifact_path"
    raw_parts = value.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        return value, "path_traversal_denied"
    path = PurePosixPath(value)
    if path.is_absolute():
        return value, "invalid_artifact_path"
    return path.as_posix(), None


def _check_exact_root(
    actual: str,
    expected: str,
    field: str,
    code: str,
    reasons: list[GateReason],
) -> None:
    normalized = _normalized_root(actual)
    if normalized is None or normalized != expected:
        reasons.append(GateReason(code, f"{field} must be {expected!r}.", field))


def _normalized_root(value: str) -> str | None:
    normalized, error = _validate_repo_relative_path(value)
    return None if error else normalized


def _missing_rollback_fields(rollback: RollbackMetadata | None) -> tuple[str, ...]:
    if rollback is None:
        return ROLLBACK_METADATA_REQUIREMENTS
    return tuple(name for name in ROLLBACK_METADATA_REQUIREMENTS if not str(getattr(rollback, name)).strip())


def _valid_rollback_reference(value: str) -> bool:
    if any(token in value for token in (";", "&&", "||", "`", "$(", "\n", "\r")):
        return False
    return value.startswith(("artifact:", "git-ref:", "procedure:")) and len(value.split(":", 1)[1].strip()) > 0
