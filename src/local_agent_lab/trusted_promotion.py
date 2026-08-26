from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import uuid
from typing import Iterator, Protocol

from .deployment_gate import (
    ApprovedArtifact,
    DeploymentArtifact,
    DeploymentCandidate,
    RollbackMetadata,
    check_deployment_candidate,
)
from .promotion_controller import (
    AuditCheck,
    AuditMetadata,
    CandidateVerification,
    CheckEvidence,
    ControllerReason,
    HumanAuthorization,
    ManifestArtifact,
    PromotionExecutionEvidence,
    PromotionOutcome,
    PromotionPlan,
    RollbackPlanAssessment,
    RollbackTestEvidence,
    assess_promotion_execution,
    audit_metadata_is_valid,
    build_promotion_plan,
    build_rollback_plan,
    compute_manifest_sha256,
    test_rollback_plan,
)


_COMMIT_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_DENIED_RELEASE_PARTS = {
    ".git",
    ".pytest_cache",
    ".ssh",
    ".venv",
    "__pycache__",
    "credential",
    "credentials",
    "data",
    "private_key",
    "private_keys",
    "secret",
    "secrets",
    "token",
    "tokens",
}
_DENIED_RELEASE_SUFFIXES = (".key", ".p12", ".pem", ".pfx")


class TrustedPromotionError(RuntimeError):
    """A fail-closed trusted-adapter error."""


@dataclass(frozen=True)
class CommandSpec:
    name: str
    argv: tuple[str, ...]


@dataclass(frozen=True)
class TrustedPromotionConfig:
    repository_dir: Path
    release_root: Path
    control_root: Path
    audit_root: Path
    service_label: str
    verifier_identity: str
    executor_identity: str
    preflight_commands: tuple[CommandSpec, ...]
    post_smoke_commands: tuple[CommandSpec, ...]
    expected_owner_uid: int = 0
    require_executor_uid: int | None = 0

    @property
    def current_link(self) -> Path:
        return self.release_root / "current"

    @property
    def current_build_file(self) -> Path:
        return self.control_root / "current_build"


@dataclass(frozen=True)
class TrustedPromotionResult:
    plan: PromotionPlan
    outcome: PromotionOutcome | None
    release_path: Path | None
    approval_consumed: bool

    @property
    def operation_id(self) -> str:
        return self.plan.audit.operation_id


@dataclass(frozen=True)
class TrustedRollbackResult:
    assessment: RollbackPlanAssessment
    target_build: str
    completed: bool
    restart_result: CheckEvidence | None
    smoke_results: tuple[CheckEvidence, ...]


class CommandRunner(Protocol):
    def run(self, argv: tuple[str, ...], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]: ...


class SubprocessRunner:
    def run(self, argv: tuple[str, ...], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        if not argv or any(not isinstance(value, str) or not value for value in argv):
            raise TrustedPromotionError("command argv must contain nonempty strings")
        return subprocess.run(
            list(argv),
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
            shell=False,
        )


def load_deployment_candidate(manifest: Path) -> DeploymentCandidate:
    try:
        raw = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TrustedPromotionError("deployment manifest is not readable JSON") from exc
    if not isinstance(raw, dict):
        raise TrustedPromotionError("deployment manifest must be an object")
    rollback_payload = raw.get("rollback")
    if rollback_payload is not None and not isinstance(rollback_payload, dict):
        raise TrustedPromotionError("deployment manifest rollback must be an object")
    artifacts_payload = raw.get("artifacts")
    if not isinstance(artifacts_payload, list) or not all(isinstance(item, dict) for item in artifacts_payload):
        raise TrustedPromotionError("deployment manifest artifacts must be a list of objects")
    try:
        rollback = RollbackMetadata(**rollback_payload) if rollback_payload is not None else None
        return DeploymentCandidate(
            repository=str(raw.get("repository") or ""),
            branch=str(raw.get("branch") or ""),
            candidate_commit=str(raw.get("candidate_commit") or ""),
            builder_identity=str(raw.get("builder_identity") or ""),
            production_identity=str(raw.get("production_identity") or ""),
            staging_runtime_root=str(raw.get("staging_runtime_root") or ""),
            production_runtime_root=str(raw.get("production_runtime_root") or ""),
            artifacts=tuple(
                DeploymentArtifact(
                    path=str(item.get("path") or ""),
                    resolved_path=str(item["resolved_path"]) if item.get("resolved_path") is not None else None,
                )
                for item in artifacts_payload
            ),
            rollback=rollback,
            tests_passed=raw.get("tests_passed") is True,
            evals_passed=raw.get("evals_passed") is True,
            security_checks_passed=raw.get("security_checks_passed") is True,
            migration_compatible=raw.get("migration_compatible") is True,
        )
    except TypeError as exc:
        raise TrustedPromotionError("deployment manifest fields are invalid") from exc


class GitCommittedTree:
    def __init__(self, repository_dir: Path, runner: CommandRunner | None = None) -> None:
        self.repository_dir = repository_dir.resolve()
        self.runner = runner or SubprocessRunner()

    def verify(self, candidate: DeploymentCandidate, *, verified_by: str, verified_at: str) -> CandidateVerification:
        commit = candidate.candidate_commit.casefold()
        if _COMMIT_RE.fullmatch(commit) is None:
            raise TrustedPromotionError("candidate commit must be a full immutable Git object ID")
        head = self._git("rev-parse", "HEAD").stdout.strip().casefold()
        branch = self._git("branch", "--show-current").stdout.strip()
        status = self._git("status", "--porcelain=v1", "--untracked-files=all").stdout
        object_check = self._git("cat-file", "-e", f"{commit}^{{commit}}", check=False)
        artifacts: list[ManifestArtifact] = []
        for declared in candidate.artifacts:
            if declared.resolved_path != declared.path:
                raise TrustedPromotionError(f"artifact path does not resolve exactly: {declared.path}")
            mode, blob = self.read_blob(commit, declared.path)
            if mode not in {"100644", "100755"}:
                raise TrustedPromotionError(f"artifact is not a regular committed file: {declared.path} ({mode})")
            artifacts.append(
                ManifestArtifact(
                    path=declared.path,
                    resolved_path=declared.path,
                    content_sha256=hashlib.sha256(blob).hexdigest(),
                    size_bytes=len(blob),
                )
            )
        manifest = tuple(artifacts)
        return CandidateVerification(
            repository=candidate.repository,
            branch=branch,
            candidate_commit=commit,
            checked_out_commit=head,
            manifest_commit=commit,
            worktree_clean=not status.strip(),
            commit_object_verified=object_check.returncode == 0,
            manifest_sha256=compute_manifest_sha256(manifest),
            artifacts=manifest,
            verified_by=verified_by,
            verified_at=verified_at,
        )

    def read_blob(self, commit: str, path: str) -> tuple[str, bytes]:
        _validate_repo_path(path)
        tree = self._git("ls-tree", "-z", commit, "--", path).stdout
        records = [record for record in tree.split("\0") if record]
        if len(records) != 1:
            raise TrustedPromotionError(f"artifact is absent or ambiguous in committed tree: {path}")
        metadata, found_path = records[0].split("\t", 1)
        mode, object_type, _object_id = metadata.split(" ", 2)
        if found_path != path or object_type != "blob":
            raise TrustedPromotionError(f"artifact is not an exact committed blob: {path}")
        result = subprocess.run(
            [
                "git",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.untrackedCache=false",
                "-c",
                f"safe.directory={self.repository_dir}",
                "show",
                f"{commit}:{path}",
            ],
            cwd=self.repository_dir,
            capture_output=True,
            check=False,
            shell=False,
        )
        if result.returncode != 0:
            raise TrustedPromotionError(
                f"could not read committed blob {path}: {result.stderr.decode(errors='replace').strip()}"
            )
        return mode, result.stdout

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        command = (
            "git",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.untrackedCache=false",
            "-c",
            f"safe.directory={self.repository_dir}",
            *args,
        )
        result = self.runner.run(command, cwd=self.repository_dir)
        if check and result.returncode != 0:
            raise TrustedPromotionError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
        return result


class HashChainAuditJournal:
    """Root-owned append-only JSONL with a verifiable SHA-256 chain."""

    def __init__(self, audit_root: Path, *, expected_owner_uid: int) -> None:
        self.audit_root = audit_root
        self.expected_owner_uid = expected_owner_uid
        self.path = audit_root / "promotion-audit.jsonl"

    def append(self, record_type: str, payload: dict[str, object]) -> dict[str, object]:
        _assert_secure_directory(self.audit_root, self.expected_owner_uid)
        records = self.read_all()
        previous = str(records[-1]["record_sha256"]) if records else "0" * 64
        body: dict[str, object] = {
            "sequence": len(records) + 1,
            "previous_record_sha256": previous,
            "record_type": record_type,
            "recorded_at": _now(),
            "payload": payload,
        }
        body["record_sha256"] = _sha256_json(body)
        encoded = (_canonical_json(body) + "\n").encode("utf-8")
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(self.path, flags, 0o600)
        try:
            metadata = os.fstat(fd)
            if metadata.st_uid != self.expected_owner_uid or stat.S_IMODE(metadata.st_mode) & 0o077:
                raise TrustedPromotionError("audit journal ownership or permissions are not private")
            _write_all(fd, encoded)
            os.fsync(fd)
        finally:
            os.close(fd)
        _fsync_directory(self.audit_root)
        return body

    def read_all(self) -> list[dict[str, object]]:
        if not self.path.exists():
            return []
        _assert_secure_regular_file(self.path, self.expected_owner_uid)
        records: list[dict[str, object]] = []
        previous = "0" * 64
        for index, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), start=1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise TrustedPromotionError(f"audit journal contains invalid JSON at line {index}") from exc
            if not isinstance(record, dict):
                raise TrustedPromotionError(f"audit journal line {index} is not an object")
            claimed = record.get("record_sha256")
            unsigned = dict(record)
            unsigned.pop("record_sha256", None)
            if (
                record.get("sequence") != index
                or record.get("previous_record_sha256") != previous
                or claimed != _sha256_json(unsigned)
            ):
                raise TrustedPromotionError(f"audit journal chain verification failed at line {index}")
            previous = str(claimed)
            records.append(record)
        return records


class AuthorizationStore:
    def __init__(self, control_root: Path, *, expected_owner_uid: int) -> None:
        self.control_root = control_root
        self.pending = control_root / "approvals" / "pending"
        self.consumed = control_root / "approvals" / "consumed"
        self.expected_owner_uid = expected_owner_uid

    def load(self, authorization_id: str) -> HumanAuthorization:
        path = self._path(self.pending, authorization_id)
        _assert_secure_directory(self.pending, self.expected_owner_uid)
        _assert_secure_regular_file(path, self.expected_owner_uid)
        if path.stat().st_size > 64 * 1024:
            raise TrustedPromotionError("authorization file is unexpectedly large")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise TypeError("authorization must be an object")
            return HumanAuthorization(**payload)
        except (json.JSONDecodeError, TypeError) as exc:
            raise TrustedPromotionError("authorization file is invalid") from exc

    def consume(self, authorization_id: str) -> Path:
        source = self._path(self.pending, authorization_id)
        destination = self._path(self.consumed, authorization_id)
        _assert_secure_directory(self.consumed, self.expected_owner_uid)
        _assert_secure_regular_file(source, self.expected_owner_uid)
        if destination.exists() or destination.is_symlink():
            raise TrustedPromotionError("authorization has already been consumed")
        os.replace(source, destination)
        _fsync_directory(self.pending)
        _fsync_directory(self.consumed)
        return destination

    @staticmethod
    def _path(root: Path, authorization_id: str) -> Path:
        if _IDENTIFIER_RE.fullmatch(authorization_id) is None:
            raise TrustedPromotionError("authorization ID contains unsafe characters")
        return root / f"{authorization_id}.json"


class TrustedPromotionAdapter:
    def __init__(self, config: TrustedPromotionConfig, *, runner: CommandRunner | None = None) -> None:
        self.config = config
        self.runner = runner or SubprocessRunner()
        self.git = GitCommittedTree(config.repository_dir, self.runner)
        self.journal = HashChainAuditJournal(config.audit_root, expected_owner_uid=config.expected_owner_uid)
        self.authorizations = AuthorizationStore(config.control_root, expected_owner_uid=config.expected_owner_uid)

    def execute_promotion(
        self,
        candidate: DeploymentCandidate,
        *,
        authorization_id: str,
        prepared_at: str | None = None,
    ) -> TrustedPromotionResult:
        self._assert_executor()
        self._assert_commands_demote_privilege(candidate)
        with self._lock():
            verified_at = _now()
            verification = self.git.verify(
                candidate,
                verified_by=self.config.verifier_identity,
                verified_at=verified_at,
            )
            preflight = self._run_checks(
                self.config.preflight_commands,
                build=candidate.candidate_commit,
                manifest=verification.manifest_sha256,
                cwd=self.config.repository_dir,
            )
            rollback_test = self._rollback_readiness(candidate, _now())
            authorization = self.authorizations.load(authorization_id)
            operation_time = prepared_at or _now()
            plan = build_promotion_plan(
                candidate,
                check_deployment_candidate(candidate),
                verification,
                authorization,
                preflight_smoke_results=preflight,
                rollback_test=rollback_test,
                prepared_at=operation_time,
            )
            self._append_controller_audit("promotion_plan", plan.audit)
            if not plan.ready:
                return TrustedPromotionResult(plan=plan, outcome=None, release_path=None, approval_consumed=False)

            self.authorizations.consume(authorization_id)
            self.journal.append(
                "authorization_consumed",
                {
                    "authorization_id": authorization_id,
                    "action": "promote",
                    "candidate_build": plan.candidate_build,
                    "manifest_sha256": plan.manifest_sha256,
                },
            )
            release_path = self._stage_release(plan, verification)
            self._activate_release(expected_current=plan.previous_build, target_build=plan.candidate_build)
            restart = self._restart(build=plan.candidate_build, manifest=plan.manifest_sha256)
            smoke = ()
            if restart.passed:
                smoke = self._run_checks(
                    self.config.post_smoke_commands,
                    build=plan.candidate_build,
                    manifest=plan.manifest_sha256,
                    cwd=self.config.current_link,
                    stop_on_failure=True,
                )
            evidence = PromotionExecutionEvidence(
                executor_identity=self.config.executor_identity,
                promotion_applied=True,
                restart_result=restart,
                smoke_results=smoke,
                completed_at=_now(),
            )
            outcome = assess_promotion_execution(plan, evidence)
            self._append_controller_audit("promotion_outcome", outcome.audit)
            trusted_result = TrustedPromotionResult(
                plan=plan,
                outcome=outcome,
                release_path=release_path,
                approval_consumed=True,
            )
            self._persist_promotion_result(trusted_result)
            return trusted_result

    def execute_rollback(
        self,
        promotion: TrustedPromotionResult,
        *,
        authorization_id: str,
        prepared_at: str | None = None,
    ) -> TrustedRollbackResult:
        self._assert_executor()
        if self.config.require_executor_uid == 0:
            for command in self.config.post_smoke_commands:
                _assert_demoted_command(command, promotion.plan.production_identity)
        operation_time = prepared_at or _now()
        if promotion.outcome is None:
            raise TrustedPromotionError("rollback requires an attempted promotion outcome")
        with self._lock():
            authorization = self.authorizations.load(authorization_id)
            plan = build_rollback_plan(promotion.plan, promotion.outcome, authorization, prepared_at=operation_time)
            readiness = self._rollback_readiness_for_values(
                plan.rollback_reference,
                plan.target_build,
                operation_time,
                restore_procedure_tested=True,
                acceptable_current=promotion.plan.candidate_build,
            )
            assessment = test_rollback_plan(plan, readiness, evaluated_at=operation_time)
            self._append_controller_audit("rollback_plan", assessment.audit)
            if assessment.status != "ready":
                return TrustedRollbackResult(assessment, plan.target_build, False, None, ())

            self.authorizations.consume(authorization_id)
            self.journal.append(
                "authorization_consumed",
                {
                    "authorization_id": authorization_id,
                    "action": "rollback",
                    "candidate_build": promotion.plan.candidate_build,
                    "target_build": plan.target_build,
                },
            )
            self._activate_release(expected_current=promotion.plan.candidate_build, target_build=plan.target_build)
            restart = self._restart(build=plan.target_build, manifest=promotion.plan.manifest_sha256)
            smoke = ()
            if restart.passed:
                smoke = self._run_checks(
                    self.config.post_smoke_commands,
                    build=plan.target_build,
                    manifest=promotion.plan.manifest_sha256,
                    cwd=self.config.current_link,
                    stop_on_failure=True,
                )
            completed = restart.passed and bool(smoke) and all(item.passed for item in smoke)
            self.journal.append(
                "rollback_outcome",
                {
                    "parent_operation_id": assessment.audit.operation_id,
                    "candidate_build": promotion.plan.candidate_build,
                    "target_build": plan.target_build,
                    "completed": completed,
                    "restart_result": restart.to_dict(),
                    "smoke_results": [item.to_dict() for item in smoke],
                },
            )
            return TrustedRollbackResult(assessment, plan.target_build, completed, restart, smoke)

    def load_promotion_result(self, operation_id: str) -> TrustedPromotionResult:
        if _IDENTIFIER_RE.fullmatch(operation_id) is None:
            raise TrustedPromotionError("operation ID contains unsafe characters")
        operations = self.config.control_root / "operations"
        _assert_secure_directory(operations, self.config.expected_owner_uid)
        path = operations / f"{operation_id}.json"
        _assert_secure_regular_file(path, self.config.expected_owner_uid)
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise TrustedPromotionError("promotion operation state is invalid JSON") from exc
        if not isinstance(envelope, dict):
            raise TrustedPromotionError("promotion operation state must be an object")
        claimed = envelope.get("record_sha256")
        unsigned = dict(envelope)
        unsigned.pop("record_sha256", None)
        if claimed != _sha256_json(unsigned):
            raise TrustedPromotionError("promotion operation state digest is invalid")
        result = _promotion_result_from_dict(envelope.get("result"))
        if result.operation_id != operation_id:
            raise TrustedPromotionError("promotion operation state identity mismatch")
        return result

    def _assert_executor(self) -> None:
        expected = self.config.require_executor_uid
        if expected is not None and os.geteuid() != expected:
            raise TrustedPromotionError(f"trusted promotion requires executor UID {expected}")
        for path in (self.config.release_root, self.config.control_root, self.config.audit_root):
            if path == self.config.release_root:
                _assert_release_directory(path, self.config.expected_owner_uid)
            else:
                _assert_secure_directory(path, self.config.expected_owner_uid)

    def _assert_commands_demote_privilege(self, candidate: DeploymentCandidate) -> None:
        if self.config.require_executor_uid != 0:
            return
        for command in self.config.preflight_commands:
            _assert_demoted_command(command, candidate.builder_identity)
        for command in self.config.post_smoke_commands:
            _assert_demoted_command(command, candidate.production_identity)

    @contextmanager
    def _lock(self) -> Iterator[None]:
        lock_path = self.config.control_root / "promotion.lock"
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(lock_path, flags, 0o600)
        try:
            metadata = os.fstat(fd)
            if metadata.st_uid != self.config.expected_owner_uid or stat.S_IMODE(metadata.st_mode) & 0o077:
                raise TrustedPromotionError("promotion lock is not private to the control identity")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise TrustedPromotionError("another production operation is already running") from exc
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _run_checks(
        self,
        commands: tuple[CommandSpec, ...],
        *,
        build: str,
        manifest: str,
        cwd: Path,
        stop_on_failure: bool = False,
    ) -> tuple[CheckEvidence, ...]:
        if not commands:
            return ()
        results: list[CheckEvidence] = []
        for command in commands:
            result = self.runner.run(command.argv, cwd=cwd)
            output_digest = hashlib.sha256((result.stdout + "\n" + result.stderr).encode("utf-8")).hexdigest()
            evidence = CheckEvidence(
                name=command.name,
                passed=result.returncode == 0,
                checked_at=_now(),
                evidence_reference=f"sha256:{output_digest}",
                candidate_build=build,
                manifest_sha256=manifest,
                details=f"exit_code={result.returncode}",
            )
            results.append(evidence)
            self.journal.append("command_evidence", evidence.to_dict())
            if stop_on_failure and not evidence.passed:
                break
        return tuple(results)

    def _restart(self, *, build: str, manifest: str) -> CheckEvidence:
        argv = ("launchctl", "kickstart", "-k", f"system/{self.config.service_label}")
        result = self.runner.run(argv)
        output_digest = hashlib.sha256((result.stdout + "\n" + result.stderr).encode("utf-8")).hexdigest()
        evidence = CheckEvidence(
            name="production-service-restart",
            passed=result.returncode == 0,
            checked_at=_now(),
            evidence_reference=f"sha256:{output_digest}",
            candidate_build=build,
            manifest_sha256=manifest,
            details=f"service={self.config.service_label};exit_code={result.returncode}",
        )
        self.journal.append("command_evidence", evidence.to_dict())
        return evidence

    def _rollback_readiness(self, candidate: DeploymentCandidate, tested_at: str) -> RollbackTestEvidence:
        rollback = candidate.rollback
        if rollback is None:
            return self._rollback_readiness_for_values("", "", tested_at, restore_procedure_tested=False)
        return self._rollback_readiness_for_values(
            rollback.rollback_reference,
            rollback.previous_build,
            tested_at,
            restore_procedure_tested=True,
        )

    def _rollback_readiness_for_values(
        self,
        reference: str,
        target: str,
        tested_at: str,
        *,
        restore_procedure_tested: bool,
        acceptable_current: str | None = None,
    ) -> RollbackTestEvidence:
        target_path = self.config.release_root / target
        target_ok = _COMMIT_RE.fullmatch(target.casefold()) is not None
        if target_ok:
            try:
                _assert_release_tree(target_path, self.config.expected_owner_uid)
            except TrustedPromotionError:
                target_ok = False
        reference_ok = reference == f"git-ref:{target}"
        current_ok = self._read_current_build() in {acceptable_current or target, ""}
        return RollbackTestEvidence(
            rollback_reference=reference,
            target_build=target,
            reference_resolved=reference_ok,
            target_build_available=target_ok,
            data_compatibility_verified=True,
            restore_procedure_tested=restore_procedure_tested and current_ok,
            passed=reference_ok and target_ok and current_ok and restore_procedure_tested,
            tested_at=tested_at,
            evidence_reference=f"release:{target}",
        )

    def _stage_release(self, plan: PromotionPlan, verification: CandidateVerification) -> Path:
        previous = self.config.release_root / plan.previous_build
        target = self.config.release_root / plan.candidate_build
        if target.exists() or target.is_symlink():
            raise TrustedPromotionError("candidate release already exists")
        _assert_release_tree(previous, self.config.expected_owner_uid)
        temporary = self.config.release_root / f".{plan.candidate_build}.staging-{uuid.uuid4().hex}"
        try:
            shutil.copytree(previous, temporary, symlinks=False)
            for artifact in verification.artifacts:
                mode, content = self.git.read_blob(plan.candidate_build, artifact.path)
                if hashlib.sha256(content).hexdigest() != artifact.content_sha256:
                    raise TrustedPromotionError(f"committed blob changed after inventory: {artifact.path}")
                destination = temporary.joinpath(*PurePosixPath(artifact.path).parts)
                _write_release_file(temporary, destination, content, executable=mode == "100755")
            _assert_release_tree(temporary, self.config.expected_owner_uid)
            os.replace(temporary, target)
            _fsync_directory(self.config.release_root)
        except Exception:
            if temporary.exists():
                shutil.rmtree(temporary)
            raise
        return target

    def _activate_release(self, *, expected_current: str, target_build: str) -> None:
        current = self._read_current_build()
        if current != expected_current:
            raise TrustedPromotionError(
                f"production compare-and-swap failed: expected {expected_current}, found {current or '<none>'}"
            )
        target = self.config.release_root / target_build
        _assert_release_tree(target, self.config.expected_owner_uid)
        temporary_link = self.config.release_root / f".current-{uuid.uuid4().hex}"
        os.symlink(target_build, temporary_link)
        os.replace(temporary_link, self.config.current_link)
        _atomic_write(self.config.current_build_file, f"{target_build}\n".encode("ascii"), 0o600)
        _fsync_directory(self.config.release_root)
        _fsync_directory(self.config.control_root)

    def _read_current_build(self) -> str:
        path = self.config.current_build_file
        if not path.exists():
            return ""
        _assert_secure_regular_file(path, self.config.expected_owner_uid)
        value = path.read_text(encoding="ascii").strip().casefold()
        if value and _COMMIT_RE.fullmatch(value) is None:
            raise TrustedPromotionError("current build marker is invalid")
        link = self.config.current_link
        if value:
            try:
                metadata = link.lstat()
            except FileNotFoundError as exc:
                raise TrustedPromotionError("current release link is missing") from exc
            if not stat.S_ISLNK(metadata.st_mode):
                raise TrustedPromotionError("current release pointer is not an atomic symlink")
            link_value = os.readlink(link)
            if link_value != value:
                raise TrustedPromotionError("current build marker and release pointer disagree")
        return value

    def _append_controller_audit(self, record_type: str, audit: object) -> None:
        if not hasattr(audit, "to_dict") or not audit_metadata_is_valid(audit):
            raise TrustedPromotionError("controller audit metadata failed digest validation")
        self.journal.append(record_type, audit.to_dict())

    def _persist_promotion_result(self, result: TrustedPromotionResult) -> Path:
        operations = self.config.control_root / "operations"
        _assert_secure_directory(operations, self.config.expected_owner_uid)
        path = operations / f"{result.operation_id}.json"
        if path.exists() or path.is_symlink():
            raise TrustedPromotionError("promotion operation state already exists")
        envelope: dict[str, object] = {
            "schema_version": 1,
            "operation_id": result.operation_id,
            "recorded_at": _now(),
            "result": _promotion_result_to_dict(result),
        }
        envelope["record_sha256"] = _sha256_json(envelope)
        _atomic_write(path, (_canonical_json(envelope) + "\n").encode("utf-8"), 0o600)
        _fsync_directory(operations)
        return path


def write_human_authorization(
    control_root: Path,
    authorization: HumanAuthorization,
    *,
    expected_owner_uid: int = 0,
    require_executor_uid: int | None = 0,
) -> Path:
    """Create one root-controlled pending approval; never overwrite an approval ID."""

    if require_executor_uid is not None and os.geteuid() != require_executor_uid:
        raise TrustedPromotionError(f"authorization creation requires UID {require_executor_uid}")
    store = AuthorizationStore(control_root, expected_owner_uid=expected_owner_uid)
    _assert_secure_directory(store.pending, expected_owner_uid)
    path = store._path(store.pending, authorization.authorization_id)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        _write_all(fd, (_canonical_json(authorization.__dict__) + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    _fsync_directory(store.pending)
    return path


def _promotion_result_to_dict(result: TrustedPromotionResult) -> dict[str, object]:
    plan = result.plan.to_dict()
    plan["builder_identity"] = result.plan.builder_identity
    plan["production_identity"] = result.plan.production_identity
    outcome = None
    if result.outcome is not None:
        outcome = {
            "status": result.outcome.status,
            "reasons": [reason.to_dict() for reason in result.outcome.reasons],
            "rollback_required": result.outcome.rollback_required,
            "further_promotion_allowed": result.outcome.further_promotion_allowed,
            "audit": result.outcome.audit.to_dict(),
            "controller_host_changes_performed": result.outcome.controller_host_changes_performed,
        }
    return {
        "plan": plan,
        "outcome": outcome,
        "release_path": str(result.release_path) if result.release_path else None,
        "approval_consumed": result.approval_consumed,
    }


def _promotion_result_from_dict(raw: object) -> TrustedPromotionResult:
    if not isinstance(raw, dict):
        raise TrustedPromotionError("promotion result state must be an object")
    try:
        plan_raw = raw["plan"]
        if not isinstance(plan_raw, dict):
            raise TypeError("plan must be an object")
        rollback_raw = plan_raw.get("rollback")
        rollback = RollbackMetadata(**rollback_raw) if isinstance(rollback_raw, dict) else None
        plan = PromotionPlan(
            status=str(plan_raw["status"]),  # type: ignore[arg-type]
            reasons=_reasons_from_list(plan_raw.get("reasons")),
            candidate_build=str(plan_raw["candidate_build"]),
            previous_build=str(plan_raw["previous_build"]),
            builder_identity=str(plan_raw["builder_identity"]),
            production_identity=str(plan_raw["production_identity"]),
            manifest_sha256=str(plan_raw["manifest_sha256"]),
            approved_artifacts=tuple(
                ApprovedArtifact(path=str(item["path"]), category=str(item["category"]))  # type: ignore[arg-type]
                for item in _object_list(plan_raw.get("approved_artifacts"), "approved_artifacts")
            ),
            rollback=rollback,
            rollback_test=_rollback_test_from_dict(_object(plan_raw.get("rollback_test"), "rollback_test")),
            preflight_smoke_results=tuple(
                _check_from_dict(item)
                for item in _object_list(plan_raw.get("preflight_smoke_results"), "preflight_smoke_results")
            ),
            audit=_audit_from_dict(_object(plan_raw.get("audit"), "plan.audit")),
        )
        outcome_raw = raw.get("outcome")
        outcome = None
        if outcome_raw is not None:
            outcome_payload = _object(outcome_raw, "outcome")
            outcome = PromotionOutcome(
                status=str(outcome_payload["status"]),  # type: ignore[arg-type]
                reasons=_reasons_from_list(outcome_payload.get("reasons")),
                rollback_required=outcome_payload.get("rollback_required") is True,
                further_promotion_allowed=outcome_payload.get("further_promotion_allowed") is True,
                audit=_audit_from_dict(_object(outcome_payload.get("audit"), "outcome.audit")),
                controller_host_changes_performed=outcome_payload.get("controller_host_changes_performed") is True,
            )
        release_value = raw.get("release_path")
        result = TrustedPromotionResult(
            plan=plan,
            outcome=outcome,
            release_path=Path(str(release_value)) if release_value else None,
            approval_consumed=raw.get("approval_consumed") is True,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise TrustedPromotionError("promotion result state has invalid fields") from exc
    if not audit_metadata_is_valid(result.plan.audit) or (
        result.outcome is not None and not audit_metadata_is_valid(result.outcome.audit)
    ):
        raise TrustedPromotionError("promotion result contains invalid controller audit metadata")
    return result


def _reasons_from_list(raw: object) -> tuple[ControllerReason, ...]:
    return tuple(
        ControllerReason(
            code=str(item.get("code") or ""),
            message=str(item.get("message") or ""),
            field=str(item["field"]) if item.get("field") is not None else None,
            artifact_path=str(item["artifact_path"]) if item.get("artifact_path") is not None else None,
        )
        for item in _object_list(raw, "reasons")
    )


def _check_from_dict(raw: dict[str, object]) -> CheckEvidence:
    return CheckEvidence(
        name=str(raw.get("name") or ""),
        passed=raw.get("passed") is True,
        checked_at=str(raw.get("checked_at") or ""),
        evidence_reference=str(raw.get("evidence_reference") or ""),
        candidate_build=str(raw.get("candidate_build") or ""),
        manifest_sha256=str(raw.get("manifest_sha256") or ""),
        details=str(raw.get("details") or ""),
    )


def _rollback_test_from_dict(raw: dict[str, object]) -> RollbackTestEvidence:
    return RollbackTestEvidence(
        rollback_reference=str(raw.get("rollback_reference") or ""),
        target_build=str(raw.get("target_build") or ""),
        reference_resolved=raw.get("reference_resolved") is True,
        target_build_available=raw.get("target_build_available") is True,
        data_compatibility_verified=raw.get("data_compatibility_verified") is True,
        restore_procedure_tested=raw.get("restore_procedure_tested") is True,
        passed=raw.get("passed") is True,
        tested_at=str(raw.get("tested_at") or ""),
        evidence_reference=str(raw.get("evidence_reference") or ""),
    )


def _audit_from_dict(raw: dict[str, object]) -> AuditMetadata:
    restart_raw = raw.get("restart_result")
    return AuditMetadata(
        operation_id=str(raw.get("operation_id") or ""),
        parent_operation_id=str(raw["parent_operation_id"]) if raw.get("parent_operation_id") is not None else None,
        action=str(raw.get("action") or ""),  # type: ignore[arg-type]
        status=str(raw.get("status") or ""),
        actor=str(raw.get("actor") or ""),
        authorization_id=str(raw.get("authorization_id") or ""),
        verifier_identity=str(raw.get("verifier_identity") or ""),
        verified_at=str(raw.get("verified_at") or ""),
        executor_identity=str(raw.get("executor_identity") or ""),
        repository=str(raw.get("repository") or ""),
        branch=str(raw.get("branch") or ""),
        candidate_build=str(raw.get("candidate_build") or ""),
        previous_build=str(raw.get("previous_build") or ""),
        manifest_sha256=str(raw.get("manifest_sha256") or ""),
        manifest_artifacts=tuple(
            ManifestArtifact(
                path=str(item.get("path") or ""),
                resolved_path=str(item.get("resolved_path") or ""),
                content_sha256=str(item.get("content_sha256") or ""),
                size_bytes=int(item.get("size_bytes") or 0),
            )
            for item in _object_list(raw.get("manifest_artifacts"), "manifest_artifacts")
        ),
        gate_decision_sha256=str(raw.get("gate_decision_sha256") or ""),
        checks=tuple(
            AuditCheck(
                name=str(item.get("name") or ""),
                passed=item.get("passed") is True,
                evidence_reference=str(item.get("evidence_reference") or ""),
                details=str(item.get("details") or ""),
            )
            for item in _object_list(raw.get("checks"), "audit.checks")
        ),
        requested_at=str(raw.get("requested_at") or ""),
        recorded_at=str(raw.get("recorded_at") or ""),
        migration_version=str(raw.get("migration_version") or ""),
        data_compatibility_assumptions=str(raw.get("data_compatibility_assumptions") or ""),
        rollback_reference=str(raw.get("rollback_reference") or ""),
        restart_result=_check_from_dict(_object(restart_raw, "restart_result")) if restart_raw is not None else None,
        smoke_results=tuple(
            _check_from_dict(item) for item in _object_list(raw.get("smoke_results"), "audit.smoke_results")
        ),
        external_host_changes_reported=raw.get("external_host_changes_reported") is True,
        controller_host_changes_performed=raw.get("controller_host_changes_performed") is True,
        record_sha256=str(raw.get("record_sha256") or ""),
    )


def _object(raw: object, label: str) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise TypeError(f"{label} must be an object")
    return raw


def _object_list(raw: object, label: str) -> list[dict[str, object]]:
    if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
        raise TypeError(f"{label} must be a list of objects")
    return raw


def _validate_repo_path(path: str) -> None:
    if not path or path.startswith("/") or path.endswith("/") or "\\" in path or "\x00" in path:
        raise TrustedPromotionError(f"unsafe repository path: {path!r}")
    if any(part in {"", ".", ".."} for part in path.split("/")):
        raise TrustedPromotionError(f"unsafe repository path: {path!r}")


def _assert_demoted_command(command: CommandSpec, service_identity: str) -> None:
    prefix = ("/usr/bin/sudo", "-n", "-u", service_identity, "--")
    if command.argv[: len(prefix)] != prefix or len(command.argv) == len(prefix):
        raise TrustedPromotionError(
            f"command {command.name!r} must demote from root to {service_identity} with a fixed sudo argv"
        )


def _release_path_denied(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    parts = tuple(part.casefold() for part in relative.parts)
    name = parts[-1] if parts else ""
    return bool(
        any(part in _DENIED_RELEASE_PARTS for part in parts)
        or name == ".env"
        or name.startswith(".env.")
        or name.endswith(_DENIED_RELEASE_SUFFIXES)
        or any(token in name for token in ("credential", "secret", "token"))
    )


def _assert_release_tree(root: Path, expected_owner_uid: int) -> None:
    if root.is_symlink() or not root.is_dir():
        raise TrustedPromotionError(f"release is not a real directory: {root}")
    for path in (root, *root.rglob("*")):
        if path.is_symlink():
            raise TrustedPromotionError(f"release tree contains a symlink: {path}")
        metadata = path.lstat()
        if not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)):
            raise TrustedPromotionError(f"release tree contains a non-regular entry: {path}")
        if metadata.st_uid != expected_owner_uid or stat.S_IMODE(metadata.st_mode) & 0o022:
            raise TrustedPromotionError(f"release entry is not owner-controlled and read-only to services: {path}")
        if path != root and _release_path_denied(path, root):
            raise TrustedPromotionError(f"release tree contains denied runtime/control material: {path}")


def _write_release_file(root: Path, destination: Path, content: bytes, *, executable: bool) -> None:
    try:
        destination.relative_to(root)
    except ValueError as exc:
        raise TrustedPromotionError("release destination escaped staging root") from exc
    if _release_path_denied(destination, root):
        raise TrustedPromotionError(f"release destination is denied: {destination}")
    parent = destination.parent
    parent.mkdir(parents=True, exist_ok=True)
    cursor = parent
    while cursor != root:
        if cursor.is_symlink() or not cursor.is_dir():
            raise TrustedPromotionError(f"release parent is not a real directory: {cursor}")
        cursor = cursor.parent
    temporary = parent / f".{destination.name}.tmp-{uuid.uuid4().hex}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(temporary, flags, 0o755 if executable else 0o644)
    try:
        _write_all(fd, content)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, destination)


def _assert_secure_directory(path: Path, expected_owner_uid: int) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise TrustedPromotionError(f"required secure directory is missing: {path}") from exc
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise TrustedPromotionError(f"secure path is not a real directory: {path}")
    if metadata.st_uid != expected_owner_uid or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise TrustedPromotionError(f"secure directory ownership or permissions are invalid: {path}")


def _assert_release_directory(path: Path, expected_owner_uid: int) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise TrustedPromotionError(f"required release directory is missing: {path}") from exc
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise TrustedPromotionError(f"release root is not a real directory: {path}")
    mode = stat.S_IMODE(metadata.st_mode)
    if metadata.st_uid != expected_owner_uid or mode & 0o022:
        raise TrustedPromotionError(f"release root must be owner-controlled and not group/other writable: {path}")


def _assert_secure_regular_file(path: Path, expected_owner_uid: int) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise TrustedPromotionError(f"required secure file is missing: {path}") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise TrustedPromotionError(f"secure path is not a single regular file: {path}")
    if metadata.st_uid != expected_owner_uid or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise TrustedPromotionError(f"secure file ownership or permissions are invalid: {path}")


def _atomic_write(path: Path, content: bytes, mode: int) -> None:
    temporary = path.parent / f".{path.name}.tmp-{uuid.uuid4().hex}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(temporary, flags, mode)
    try:
        _write_all(fd, content)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, path)


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_all(fd: int, content: bytes) -> None:
    offset = 0
    while offset < len(content):
        written = os.write(fd, content[offset:])
        if written <= 0:
            raise TrustedPromotionError("short write while persisting trusted state")
        offset += written


def _canonical_json(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256_json(payload: object) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
