from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
HELPER_PATH = ROOT / "scripts" / "minillm_admin.py"
INSTALLER_PATH = ROOT / "scripts" / "install_minillm_admin_helper.sh"
SPEC = importlib.util.spec_from_file_location("minillm_admin_test_module", HELPER_PATH)
assert SPEC is not None and SPEC.loader is not None
admin_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = admin_module
SPEC.loader.exec_module(admin_module)

AdminConfig = admin_module.AdminConfig
AdminError = admin_module.AdminError
CommandResult = admin_module.CommandResult
MiniLLMAdmin = admin_module.MiniLLMAdmin

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
CURRENT = "a" * 40
CANDIDATE = "b" * 40


class FixtureRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.git_head = CURRENT
        self.fail_boundary = False

    def run(self, argv, *, cwd=None):
        command = tuple(argv)
        self.calls.append(command)
        if command[0].endswith("verify-service-identity-boundaries"):
            failures = 1 if self.fail_boundary else 0
            return CommandResult(0, f"SUMMARY pass=41 fail={failures} read_only=true\n", "")
        if command[:2] == ("/bin/launchctl", "print"):
            return CommandResult(0, "pid = 123\n", "")
        if command[0] == "/usr/bin/curl":
            return CommandResult(0, "ok\n", "")
        if command[0] == "/usr/bin/git":
            if "status" in command:
                return CommandResult(0, "", "")
            if "checkout" in command:
                self.git_head = command[-1]
                return CommandResult(0, "", "")
            if "rev-parse" in command:
                return CommandResult(0, self.git_head + "\n", "")
            return CommandResult(0, "verified\n", "")
        if command[0].endswith("minillm-promote"):
            action = command[1]
            if action == "verify":
                return CommandResult(0, json.dumps({"status": "ok"}), "")
            if action == "authorize":
                authorization_action = command[command.index("--action") + 1]
                return CommandResult(
                    0,
                    json.dumps({"status": "ok", "authorization_id": f"human-{authorization_action}-fixture"}),
                    "",
                )
            if action == "apply":
                return CommandResult(
                    0,
                    json.dumps({"status": "completed", "operation_id": "promote-fixture", "rollback_required": False}),
                    "",
                )
            if action == "rollback":
                return CommandResult(0, json.dumps({"status": "completed", "completed": True}), "")
        if command[0].endswith("cutover-minillm-production"):
            return CommandResult(0, "APPLIED production_cutover=true\n", "")
        return CommandResult(1, "", "unexpected fixture command")


def _write(path: Path, content: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(mode)


def _fixture(tmp_path: Path):
    owner = os.geteuid()
    control = tmp_path / "control"
    admin = control / "admin"
    pending = admin / "requests" / "pending"
    consumed = admin / "requests" / "consumed"
    material = admin / "requests" / "material"
    results = admin / "results"
    audit = admin / "audit"
    source = tmp_path / "source"
    repository = tmp_path / "trusted-repository"
    releases = tmp_path / "releases"
    runtime = tmp_path / "runtime"
    for path in (control, admin, pending, consumed, material, results, audit, source, repository):
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o700)
    (releases / CURRENT).mkdir(parents=True)
    (releases / "current").symlink_to(CURRENT)

    helper = tmp_path / "installed" / "minillm-admin"
    sudoers = tmp_path / "installed" / "sudoers"
    verifier = tmp_path / "installed" / "verify-service-identity-boundaries"
    cutover = tmp_path / "installed" / "cutover-minillm-production"
    promote = runtime / "bin" / "minillm-promote"
    for path, content, mode in (
        (helper, b"helper fixture\n", 0o500),
        (sudoers, b"sudoers fixture\n", 0o440),
        (verifier, b"verifier fixture\n", 0o500),
        (cutover, b"cutover fixture\n", 0o500),
        (promote, b"promote fixture\n", 0o500),
    ):
        _write(path, content, mode)
    artifacts = {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (helper, sudoers, verifier, cutover, promote)
    }
    config = AdminConfig(
        operator_name="fixture-user",
        operator_uid=owner,
        request_source_root=source,
        control_root=control,
        trusted_repository=repository,
        release_root=releases,
        python_runtime=runtime,
        production_root=tmp_path / "production",
        production_audit=tmp_path / "production-audit",
        production_tunnel=tmp_path / "production-tunnel",
        builder_root=tmp_path / "builder",
        builder_credentials=tmp_path / "builder-credentials",
        production_credentials=tmp_path / "production-credentials",
        production_home_plist=tmp_path / "production.plist",
        production_tunnel_plist=tmp_path / "production-tunnel.plist",
        helper_path=helper,
        sudoers_path=sudoers,
        verifier_path=verifier,
        cutover_path=cutover,
        promote_path=promote,
        artifact_sha256=artifacts,
    )
    runner = FixtureRunner()
    helper_instance = MiniLLMAdmin(config, runner=runner, clock=lambda: NOW, expected_owner_uid=owner)
    environment = {"SUDO_USER": "fixture-user", "SUDO_UID": str(owner)}
    return config, helper_instance, runner, environment


def _request(operation: str, *, candidate: str = CURRENT, request_id: str = "request-001", **extra):
    return {
        "schema_version": 1,
        "request_id": request_id,
        "operation": operation,
        "created_at": (NOW - timedelta(minutes=1)).isoformat(),
        "expires_at": (NOW + timedelta(hours=1)).isoformat(),
        "candidate_commit": candidate,
        "reason": "Human reviewed the exact candidate and attached evidence.",
        "approval_reference": "change:fixture-001",
        **extra,
    }


def _write_request(config: AdminConfig, request: dict[str, object]) -> None:
    path = config.request_source_root / "request.json"
    path.write_text(json.dumps(request), encoding="utf-8")
    path.chmod(0o600)


def _write_material(config: AdminConfig, filename: str, payload: bytes) -> str:
    path = config.request_source_root / filename
    path.write_bytes(payload)
    path.chmod(0o600)
    return hashlib.sha256(payload).hexdigest()


def test_installer_and_helper_are_syntactically_valid_and_document_dry_run() -> None:
    shell = subprocess.run(["/bin/bash", "-n", str(INSTALLER_PATH)], text=True, capture_output=True, check=False)
    python = subprocess.run([sys.executable, "-m", "py_compile", str(HELPER_PATH)], text=True, capture_output=True, check=False)
    help_result = subprocess.run([str(INSTALLER_PATH), "--help"], text=True, capture_output=True, check=False)

    assert shell.returncode == 0, shell.stderr
    assert python.returncode == 0, python.stderr
    assert help_result.returncode == 0
    assert "Dry-run is the default" in help_result.stdout
    assert "never starts, stops, or reloads a service" in help_result.stdout


def test_sudoers_policy_is_exact_and_omits_review_revoke_and_wildcards() -> None:
    source = INSTALLER_PATH.read_text(encoding="utf-8")

    for operation in sorted(admin_module.ALLOWED_PASSWORDLESS_OPERATIONS):
        assert f'$HELPER_PATH {operation}"' in source
    assert '$HELPER_PATH review"' not in source
    assert '$HELPER_PATH revoke"' not in source
    assert "NOPASSWD:NOSETENV:" in source
    sudoers_lines = [line for line in source.splitlines() if "NOPASSWD:NOSETENV:" in line]
    assert len(sudoers_lines) == len(admin_module.ALLOWED_PASSWORDLESS_OPERATIONS)
    assert all("*" not in line and "?" not in line for line in sudoers_lines)


def test_status_and_verify_are_audited_with_fixed_artifact_integrity(tmp_path: Path) -> None:
    config, helper, _runner, environment = _fixture(tmp_path)

    status = helper.invoke("status", environment)
    verification = helper.invoke("verify", environment)

    assert status["status"] == "ok"
    assert status["current_commit"] == CURRENT
    assert status["production_system_services_ready"] is True
    assert verification["boundary_summary"] == "SUMMARY pass=41 fail=0 read_only=true"
    records = helper.audit.read_all()
    assert [record["operation"] for record in records] == ["status", "verify"]
    assert all(record["operator"] == "fixture-user" for record in records)
    assert all(record["secrets_recorded"] is False for record in records)


def test_real_command_runner_preserves_only_root_configured_operator_metadata(tmp_path: Path) -> None:
    config, _helper, _runner, _environment = _fixture(tmp_path)

    instance = MiniLLMAdmin(config, clock=lambda: NOW, expected_owner_uid=os.geteuid())

    assert instance.runner.environment == {
        **admin_module.FIXED_ENVIRONMENT,
        "SUDO_USER": "fixture-user",
        "SUDO_UID": str(os.geteuid()),
    }


def test_reviewed_cutover_is_one_time_and_replay_fails_closed(tmp_path: Path) -> None:
    config, helper, runner, environment = _fixture(tmp_path)
    _write_request(config, _request("cutover-reviewed"))

    reviewed = helper.invoke("review", environment)
    completed = helper.invoke("cutover-reviewed", environment)
    replay = helper.invoke("cutover-reviewed", environment)

    assert reviewed["status"] == "reviewed"
    assert completed["status"] == "completed"
    assert replay["status"] == "error"
    assert "unavailable" in replay["error"]
    assert (config.consumed_root / "request-001.json").is_file()
    assert (config.result_root / "request-001.json").is_file()
    cutover_calls = [call for call in runner.calls if call[0].endswith("cutover-minillm-production")]
    assert cutover_calls == [
        (str(config.cutover_path), "--apply", "--confirm", "CUTOVER_MINILLM_PRODUCTION")
    ]


def test_stale_extra_field_and_path_injected_requests_are_denied(tmp_path: Path) -> None:
    config, helper, _runner, environment = _fixture(tmp_path)
    stale = _request("cutover-reviewed")
    stale["expires_at"] = (NOW - timedelta(seconds=1)).isoformat()
    _write_request(config, stale)
    assert helper.invoke("review", environment)["status"] == "error"

    unexpected = _request("cutover-reviewed", request_id="request-002")
    unexpected["shell"] = "id"
    _write_request(config, unexpected)
    assert "unexpected fields" in helper.invoke("review", environment)["error"]

    manifest = json.dumps({"candidate_commit": CANDIDATE}).encode()
    digest = _write_material(config, "candidate.json", manifest)
    bundle_digest = _write_material(config, "candidate.bundle", b"fixture bundle")
    injected = _request(
        "deploy-reviewed",
        candidate=CANDIDATE,
        request_id="request-003",
        manifest_file="../candidate.json",
        manifest_sha256=digest,
        bundle_file="candidate.bundle",
        bundle_sha256=bundle_digest,
    )
    _write_request(config, injected)
    assert "filename is invalid" in helper.invoke("review", environment)["error"]


def test_environment_operator_and_extra_argument_injection_are_rejected(tmp_path: Path) -> None:
    _config, helper, _runner, environment = _fixture(tmp_path)

    with pytest.raises(AdminError, match="installed sudo operator"):
        helper.invoke("status", {"SUDO_USER": "attacker", "SUDO_UID": environment["SUDO_UID"]})
    with pytest.raises(AdminError, match="environment override"):
        admin_module._reject_environment_injection({"PYTHONPATH": "/tmp/attacker"})
    assert admin_module.main(["status", "--command=id"], {}) == 1


def test_artifact_and_audit_tampering_are_detected(tmp_path: Path) -> None:
    config, helper, _runner, environment = _fixture(tmp_path)
    assert helper.invoke("status", environment)["status"] == "ok"

    config.verifier_path.chmod(0o700)
    config.verifier_path.write_text("substituted\n", encoding="utf-8")
    config.verifier_path.chmod(0o500)
    denied = helper.invoke("verify", environment)
    assert denied["status"] == "error"
    assert "digest mismatch" in denied["error"]

    lines = helper.audit.path.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    first["result"] = "forged"
    lines[0] = json.dumps(first, sort_keys=True, separators=(",", ":"))
    helper.audit.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    helper.audit.path.chmod(0o600)
    with pytest.raises(AdminError, match="chain verification failed"):
        helper.audit.read_all()


def test_review_copies_bundle_and_manifest_before_passwordless_deploy(tmp_path: Path) -> None:
    config, helper, runner, environment = _fixture(tmp_path)
    manifest = json.dumps({"candidate_commit": CANDIDATE, "artifacts": []}).encode()
    bundle = b"reviewed git bundle bytes"
    manifest_digest = _write_material(config, "candidate.json", manifest)
    bundle_digest = _write_material(config, "candidate.bundle", bundle)
    _write_request(
        config,
        _request(
            "deploy-reviewed",
            candidate=CANDIDATE,
            manifest_file="candidate.json",
            manifest_sha256=manifest_digest,
            bundle_file="candidate.bundle",
            bundle_sha256=bundle_digest,
        ),
    )

    reviewed = helper.invoke("review", environment)
    assert reviewed["status"] == "reviewed"
    copied = config.material_root / "request-001"
    assert (copied / "candidate.json").read_bytes() == manifest
    assert (copied / "candidate.bundle").read_bytes() == bundle

    # User-writable sources can change after review without changing the bound root-private request.
    (config.request_source_root / "candidate.json").write_text("attacker replacement", encoding="utf-8")
    (config.request_source_root / "candidate.bundle").write_text("attacker replacement", encoding="utf-8")
    deployed = helper.invoke("deploy-reviewed", environment)
    assert deployed["status"] == "completed"
    apply_calls = [call for call in runner.calls if len(call) > 1 and call[1] == "apply"]
    assert len(apply_calls) == 1
    assert str(copied / "candidate.json") in apply_calls[0]


def test_root_private_material_substitution_blocks_before_cutover(tmp_path: Path) -> None:
    config, helper, runner, environment = _fixture(tmp_path)
    _write_request(config, _request("cutover-reviewed"))
    assert helper.invoke("review", environment)["status"] == "reviewed"
    material = config.material_root / "request-001" / "request.json"
    material.chmod(0o600)
    material.write_text("{}", encoding="utf-8")
    material.chmod(0o400)

    denied = helper.invoke("cutover-reviewed", environment)

    assert denied["status"] == "error"
    assert "substituted" in denied["error"]
    assert not any(call[0].endswith("cutover-minillm-production") for call in runner.calls)


def test_rollback_requires_a_separate_reviewed_authorization(tmp_path: Path) -> None:
    config, helper, runner, environment = _fixture(tmp_path)
    manifest = json.dumps({"candidate_commit": CURRENT, "artifacts": []}).encode()
    manifest_digest = _write_material(config, "rollback.json", manifest)
    _write_request(
        config,
        _request(
            "rollback",
            manifest_file="rollback.json",
            manifest_sha256=manifest_digest,
            operation_id="promotion-operation-001",
        ),
    )

    assert helper.invoke("review", environment)["status"] == "reviewed"
    result = helper.invoke("rollback", environment)

    assert result["status"] == "completed"
    rollback_calls = [call for call in runner.calls if len(call) > 1 and call[1] == "rollback"]
    assert len(rollback_calls) == 1
    assert "promotion-operation-001" in rollback_calls[0]
    assert "human-rollback-fixture" in rollback_calls[0]
    assert helper.audit.read_all()[-1]["rollback_status"] == "completed"


def test_review_source_symlink_and_unsafe_lock_are_rejected(tmp_path: Path) -> None:
    config, helper, _runner, environment = _fixture(tmp_path)
    external = tmp_path / "external.json"
    external.write_text(json.dumps(_request("cutover-reviewed")), encoding="utf-8")
    external.chmod(0o600)
    (config.request_source_root / "request.json").symlink_to(external)
    denied = helper.invoke("review", environment)
    assert denied["status"] == "error"

    config.lock_path.unlink()
    lock_target = tmp_path / "lock-target"
    lock_target.write_text("", encoding="utf-8")
    lock_target.chmod(0o600)
    config.lock_path.symlink_to(lock_target)
    with pytest.raises(AdminError, match="lock cannot be opened safely"):
        helper.invoke("status", environment)


def test_boundary_failure_blocks_reviewed_mutation_without_consuming_request(tmp_path: Path) -> None:
    config, helper, runner, environment = _fixture(tmp_path)
    _write_request(config, _request("cutover-reviewed"))
    assert helper.invoke("review", environment)["status"] == "reviewed"
    runner.fail_boundary = True

    denied = helper.invoke("cutover-reviewed", environment)

    assert denied["status"] == "error"
    assert "fail=0" in denied["error"]
    assert (config.pending_root / "cutover-reviewed.json").is_file()
    assert not any(call[0].endswith("cutover-minillm-production") for call in runner.calls)


def test_revoke_removes_only_passwordless_policy_and_preserves_evidence(tmp_path: Path) -> None:
    config, helper, _runner, environment = _fixture(tmp_path)
    _write_request(config, _request("cutover-reviewed"))
    assert helper.invoke("review", environment)["status"] == "reviewed"

    result = helper.invoke("revoke", environment)

    assert result == {"status": "revoked", "sudoers_removed": True, "audit_preserved": True}
    assert not config.sudoers_path.exists()
    assert helper.audit.path.is_file()
    assert (config.pending_root / "cutover-reviewed.json").is_file()
