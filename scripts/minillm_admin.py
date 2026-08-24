#!/usr/bin/env python3
"""Root-owned, argument-free administration surface for MiniLLM production."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from typing import Callable, Mapping, Sequence


CONFIG_PATH = Path("/Library/Application Support/MiniLLM/deployment-control/admin/config.json")
ALLOWED_PASSWORDLESS_OPERATIONS = {
    "status",
    "verify",
    "deploy-reviewed",
    "cutover-reviewed",
    "rollback",
    "audit-verify",
}
REVIEWED_OPERATIONS = {"deploy-reviewed", "cutover-reviewed", "rollback"}
ALL_OPERATIONS = ALLOWED_PASSWORDLESS_OPERATIONS | {"review", "revoke"}
COMMIT_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
FILENAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\.(?:json|bundle)\Z")
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
APPROVAL_REFERENCE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._/-]{2,127}\Z")
FIXED_ENVIRONMENT = {
    "HOME": "/var/root",
    "LANG": "C",
    "LC_ALL": "C",
    "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
}
INJECTABLE_ENVIRONMENT_PREFIXES = (
    "BASH_ENV",
    "CDPATH",
    "ENV",
    "GIT_CONFIG",
    "HOME_MCP_",
    "LAGENT_",
    "LD_",
    "DYLD_",
    "MINILLM_ADMIN_",
    "PIP_CONFIG",
    "PYTHONHOME",
    "PYTHONPATH",
    "VIRTUAL_ENV",
)


class AdminError(RuntimeError):
    """A fail-closed administration error safe to return to the operator."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class CommandRunner:
    def __init__(self, environment: Mapping[str, str] | None = None) -> None:
        self.environment = dict(environment or FIXED_ENVIRONMENT)

    def run(self, argv: Sequence[str], *, cwd: Path | None = None) -> CommandResult:
        result = subprocess.run(
            list(argv),
            cwd=cwd,
            env=self.environment,
            text=True,
            capture_output=True,
            check=False,
            shell=False,
        )
        return CommandResult(result.returncode, result.stdout, result.stderr)


@dataclass(frozen=True)
class AdminConfig:
    operator_name: str
    operator_uid: int
    request_source_root: Path
    control_root: Path
    trusted_repository: Path
    release_root: Path
    python_runtime: Path
    production_root: Path
    production_audit: Path
    production_tunnel: Path
    builder_root: Path
    builder_credentials: Path
    production_credentials: Path
    production_home_plist: Path
    production_tunnel_plist: Path
    helper_path: Path
    sudoers_path: Path
    verifier_path: Path
    cutover_path: Path
    promote_path: Path
    artifact_sha256: Mapping[str, str]

    @property
    def admin_root(self) -> Path:
        return self.control_root / "admin"

    @property
    def pending_root(self) -> Path:
        return self.admin_root / "requests" / "pending"

    @property
    def consumed_root(self) -> Path:
        return self.admin_root / "requests" / "consumed"

    @property
    def material_root(self) -> Path:
        return self.admin_root / "requests" / "material"

    @property
    def result_root(self) -> Path:
        return self.admin_root / "results"

    @property
    def audit_root(self) -> Path:
        return self.admin_root / "audit"

    @property
    def lock_path(self) -> Path:
        return self.admin_root / "admin.lock"


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise AdminError(f"{field} must be a short ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AdminError(f"{field} is not a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise AdminError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _validate_absolute(path: Path, field: str) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise AdminError(f"{field} must be an absolute normalized path")


def _assert_private_file(path: Path, owner_uid: int, *, executable: bool | None = None) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise AdminError(f"required private file is unavailable: {path}") from exc
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise AdminError(f"required private path is not a real file: {path}")
    if metadata.st_uid != owner_uid or stat.S_IMODE(metadata.st_mode) & 0o022:
        raise AdminError(f"private file ownership or permissions are unsafe: {path}")
    if executable is True and not metadata.st_mode & stat.S_IXUSR:
        raise AdminError(f"required private file is not executable: {path}")


def _assert_private_directory(path: Path, owner_uid: int) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise AdminError(f"required private directory is unavailable: {path}") from exc
    if not stat.S_ISDIR(metadata.st_mode) or path.is_symlink():
        raise AdminError(f"required private path is not a real directory: {path}")
    if metadata.st_uid != owner_uid or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise AdminError(f"private directory ownership or permissions are unsafe: {path}")


def _assert_source_directory(path: Path, owner_uid: int) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise AdminError(f"review source directory is unavailable: {path}") from exc
    if not stat.S_ISDIR(metadata.st_mode) or path.is_symlink():
        raise AdminError(f"review source root is not a real directory: {path}")
    if metadata.st_uid != owner_uid or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise AdminError(f"review source root ownership or permissions are unsafe: {path}")


def _atomic_write(path: Path, payload: bytes, mode: int = 0o600) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(temporary, flags, mode)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise AdminError(f"short write while creating {path}")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _read_json_bytes(payload: bytes, description: str) -> dict[str, object]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdminError(f"{description} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise AdminError(f"{description} must be a JSON object")
    return value


def _read_source_root_file(root: Path, filename: str, *, owner_uid: int, maximum_size: int) -> bytes:
    if FILENAME_RE.fullmatch(filename) is None:
        raise AdminError("review source filename is invalid")
    root_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        root_flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        root_flags |= os.O_NOFOLLOW
    try:
        root_fd = os.open(root, root_flags)
    except OSError as exc:
        raise AdminError(f"review source directory cannot be opened safely: {root}") from exc
    try:
        metadata = os.fstat(root_fd)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != owner_uid
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise AdminError(f"review source root ownership or permissions are unsafe: {root}")
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(filename, flags, dir_fd=root_fd)
        except OSError as exc:
            raise AdminError(f"review source cannot be opened safely: {filename}") from exc
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_uid != owner_uid:
                raise AdminError(f"review source owner or type is invalid: {filename}")
            if stat.S_IMODE(before.st_mode) & 0o022:
                raise AdminError(f"review source must not be group/other writable: {filename}")
            if before.st_size > maximum_size:
                raise AdminError(f"review source is too large: {filename}")
            chunks: list[bytes] = []
            total = 0
            while True:
                block = os.read(fd, min(1024 * 1024, maximum_size + 1 - total))
                if not block:
                    break
                chunks.append(block)
                total += len(block)
                if total > maximum_size:
                    raise AdminError(f"review source is too large: {filename}")
            after = os.fstat(fd)
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ):
                raise AdminError(f"review source changed while being copied: {filename}")
            return b"".join(chunks)
        finally:
            os.close(fd)
    finally:
        os.close(root_fd)


def load_config(path: Path = CONFIG_PATH, *, expected_owner_uid: int = 0) -> AdminConfig:
    _assert_private_file(path, expected_owner_uid)
    payload = _read_json_bytes(path.read_bytes(), "admin config")
    expected = {
        "schema_version",
        "operator_name",
        "operator_uid",
        "request_source_root",
        "control_root",
        "trusted_repository",
        "release_root",
        "python_runtime",
        "production_root",
        "production_audit",
        "production_tunnel",
        "builder_root",
        "builder_credentials",
        "production_credentials",
        "production_home_plist",
        "production_tunnel_plist",
        "helper_path",
        "sudoers_path",
        "verifier_path",
        "cutover_path",
        "promote_path",
        "artifact_sha256",
    }
    if set(payload) != expected or payload.get("schema_version") != 1:
        raise AdminError("admin config schema or fields are invalid")
    operator_name = payload["operator_name"]
    operator_uid = payload["operator_uid"]
    artifacts = payload["artifact_sha256"]
    if not isinstance(operator_name, str) or IDENTIFIER_RE.fullmatch(operator_name) is None:
        raise AdminError("configured operator name is invalid")
    if not isinstance(operator_uid, int) or operator_uid <= 0:
        raise AdminError("configured operator UID is invalid")
    if not isinstance(artifacts, dict) or not artifacts:
        raise AdminError("configured artifact digests are invalid")
    normalized_artifacts: dict[str, str] = {}
    for artifact_path, digest in artifacts.items():
        if not isinstance(artifact_path, str) or not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
            raise AdminError("configured artifact digest entry is invalid")
        _validate_absolute(Path(artifact_path), "artifact path")
        normalized_artifacts[artifact_path] = digest

    def configured_path(field: str) -> Path:
        raw = payload[field]
        if not isinstance(raw, str):
            raise AdminError(f"configured {field} path is invalid")
        result = Path(raw)
        _validate_absolute(result, field)
        return result

    return AdminConfig(
        operator_name=operator_name,
        operator_uid=operator_uid,
        request_source_root=configured_path("request_source_root"),
        control_root=configured_path("control_root"),
        trusted_repository=configured_path("trusted_repository"),
        release_root=configured_path("release_root"),
        python_runtime=configured_path("python_runtime"),
        production_root=configured_path("production_root"),
        production_audit=configured_path("production_audit"),
        production_tunnel=configured_path("production_tunnel"),
        builder_root=configured_path("builder_root"),
        builder_credentials=configured_path("builder_credentials"),
        production_credentials=configured_path("production_credentials"),
        production_home_plist=configured_path("production_home_plist"),
        production_tunnel_plist=configured_path("production_tunnel_plist"),
        helper_path=configured_path("helper_path"),
        sudoers_path=configured_path("sudoers_path"),
        verifier_path=configured_path("verifier_path"),
        cutover_path=configured_path("cutover_path"),
        promote_path=configured_path("promote_path"),
        artifact_sha256=normalized_artifacts,
    )


class AdminAuditJournal:
    def __init__(self, root: Path, *, owner_uid: int) -> None:
        self.root = root
        self.owner_uid = owner_uid
        self.path = root / "admin-audit.jsonl"

    def read_all(self) -> list[dict[str, object]]:
        _assert_private_directory(self.root, self.owner_uid)
        if not self.path.exists():
            return []
        _assert_private_file(self.path, self.owner_uid)
        records: list[dict[str, object]] = []
        previous = "0" * 64
        for line_number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), start=1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AdminError(f"admin audit JSON is invalid at line {line_number}") from exc
            if not isinstance(record, dict):
                raise AdminError(f"admin audit record {line_number} is not an object")
            claimed = record.get("record_sha256")
            unsigned = dict(record)
            unsigned.pop("record_sha256", None)
            if (
                record.get("sequence") != line_number
                or record.get("previous_record_sha256") != previous
                or claimed != _sha256_bytes(_canonical_json(unsigned).encode("utf-8"))
            ):
                raise AdminError(f"admin audit chain verification failed at line {line_number}")
            previous = str(claimed)
            records.append(record)
        return records

    def append(self, payload: dict[str, object]) -> dict[str, object]:
        records = self.read_all()
        record: dict[str, object] = {
            "sequence": len(records) + 1,
            "previous_record_sha256": records[-1]["record_sha256"] if records else "0" * 64,
            **payload,
        }
        record["record_sha256"] = _sha256_bytes(_canonical_json(record).encode("utf-8"))
        encoded = (_canonical_json(record) + "\n").encode("utf-8")
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(self.path, flags, 0o600)
        try:
            metadata = os.fstat(fd)
            if metadata.st_uid != self.owner_uid or stat.S_IMODE(metadata.st_mode) & 0o077:
                raise AdminError("admin audit journal ownership or permissions are unsafe")
            view = memoryview(encoded)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise AdminError("short write while appending the admin audit")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        return record


class MiniLLMAdmin:
    def __init__(
        self,
        config: AdminConfig,
        *,
        runner: CommandRunner | None = None,
        clock: Callable[[], datetime] = _now,
        expected_owner_uid: int = 0,
    ) -> None:
        self.config = config
        self.runner = runner or CommandRunner(
            {
                **FIXED_ENVIRONMENT,
                "SUDO_USER": config.operator_name,
                "SUDO_UID": str(config.operator_uid),
            }
        )
        self.clock = clock
        self.expected_owner_uid = expected_owner_uid
        self.audit = AdminAuditJournal(config.audit_root, owner_uid=expected_owner_uid)

    def _verify_installed_artifacts(self) -> dict[str, str]:
        for path in (
            self.config.admin_root,
            self.config.pending_root,
            self.config.consumed_root,
            self.config.material_root,
            self.config.result_root,
            self.config.audit_root,
        ):
            _assert_private_directory(path, self.expected_owner_uid)
        observed: dict[str, str] = {}
        required = {
            str(self.config.helper_path),
            str(self.config.sudoers_path),
            str(self.config.verifier_path),
            str(self.config.cutover_path),
            str(self.config.promote_path),
        }
        if set(self.config.artifact_sha256) != required:
            raise AdminError("installed artifact allowlist does not match the fixed administration contract")
        for raw_path, expected_digest in self.config.artifact_sha256.items():
            path = Path(raw_path)
            _assert_private_file(path, self.expected_owner_uid, executable=path != self.config.sudoers_path)
            observed_digest = _sha256_file(path)
            if observed_digest != expected_digest:
                raise AdminError(f"installed artifact digest mismatch: {path}")
            observed[raw_path] = observed_digest
        return observed

    def _assert_operator(self, environment: Mapping[str, str]) -> str:
        operator = environment.get("SUDO_USER")
        sudo_uid = environment.get("SUDO_UID")
        if operator != self.config.operator_name or sudo_uid != str(self.config.operator_uid):
            raise AdminError("invocation does not match the installed sudo operator")
        return operator

    def _run(self, argv: Sequence[str], *, cwd: Path | None = None, description: str) -> CommandResult:
        if not argv or any(not isinstance(item, str) or not item for item in argv):
            raise AdminError(f"{description} command is invalid")
        result = self.runner.run(argv, cwd=cwd)
        if result.returncode != 0:
            detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "no stderr"
            raise AdminError(f"{description} failed with exit {result.returncode}: {detail[:300]}")
        return result

    def _run_json(self, argv: Sequence[str], *, cwd: Path | None = None, description: str) -> dict[str, object]:
        result = self._run(argv, cwd=cwd, description=description)
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise AdminError(f"{description} did not return valid JSON") from exc
        if not isinstance(payload, dict):
            raise AdminError(f"{description} did not return a JSON object")
        return payload

    def _current_commit(self) -> str:
        current = self.config.release_root / "current"
        if not current.is_symlink():
            raise AdminError("current release pointer is not a symlink")
        target = os.readlink(current)
        if COMMIT_RE.fullmatch(target) is None:
            raise AdminError("current release pointer is not an immutable commit")
        release = self.config.release_root / target
        if not release.is_dir() or release.is_symlink():
            raise AdminError("current release target is unavailable")
        return target

    def _boundary_command(self) -> tuple[str, ...]:
        release = self.config.release_root / self._current_commit()
        return (
            str(self.config.verifier_path),
            "--builder-user",
            "_minillm_builder",
            "--production-user",
            "_minillm_prod",
            "--builder-root",
            str(self.config.builder_root),
            "--production-root",
            str(self.config.production_root),
            "--builder-credentials",
            str(self.config.builder_credentials),
            "--production-credentials",
            str(self.config.production_credentials),
            "--production-audit",
            str(self.config.production_audit),
            "--production-tunnel",
            str(self.config.production_tunnel),
            "--deployment-control",
            str(self.config.control_root),
            "--production-plist",
            str(self.config.production_home_plist),
            "--production-tunnel-plist",
            str(self.config.production_tunnel_plist),
            "--trusted-repository",
            str(self.config.trusted_repository),
            "--root-python-runtime",
            str(self.config.python_runtime),
            "--production-release",
            str(release),
        )

    def status(self) -> dict[str, object]:
        current = self._current_commit()
        services = {}
        for name, label in (
            ("home_mcp", "system/com.dalconzo.minillm.production"),
            ("tunnel", "system/com.dalconzo.minillm.production-tunnel"),
        ):
            result = self.runner.run(("/bin/launchctl", "print", label), cwd=Path("/"))
            services[name] = {"loaded": result.returncode == 0}
        endpoints = {}
        for name, url in (
            ("home_mcp", "http://127.0.0.1:8765/health"),
            ("tunnel", "http://127.0.0.1:8080/readyz"),
        ):
            result = self.runner.run(("/usr/bin/curl", "-fsS", "--max-time", "3", url), cwd=Path("/"))
            endpoints[name] = {"healthy": result.returncode == 0}
        production_ready = all(item["loaded"] for item in services.values()) and all(
            item["healthy"] for item in endpoints.values()
        )
        pending = [path.stem for path in sorted(self.config.pending_root.glob("*.json")) if path.is_file()]
        return {
            "status": "ok",
            "current_commit": current,
            "services": services,
            "endpoints": endpoints,
            "production_system_services_ready": production_ready,
            "pending_reviewed_operations": pending,
            "audit_records": len(self.audit.read_all()),
        }

    def verify(self) -> dict[str, object]:
        result = self._run(self._boundary_command(), cwd=Path("/"), description="identity boundary verification")
        summary = next((line for line in reversed(result.stdout.splitlines()) if line.startswith("SUMMARY ")), None)
        if summary is None or "fail=0" not in summary:
            raise AdminError("identity boundary verifier did not report fail=0")
        return {"status": "ok", "boundary_summary": summary, "audit_chain_valid": True}

    def _validate_review_request(self, request: dict[str, object]) -> dict[str, object]:
        common = {
            "schema_version",
            "request_id",
            "operation",
            "created_at",
            "expires_at",
            "candidate_commit",
            "reason",
            "approval_reference",
        }
        operation = request.get("operation")
        if operation == "deploy-reviewed":
            expected = common | {"manifest_file", "manifest_sha256", "bundle_file", "bundle_sha256"}
        elif operation == "rollback":
            expected = common | {"manifest_file", "manifest_sha256", "operation_id"}
        elif operation == "cutover-reviewed":
            expected = common
        else:
            raise AdminError("review request operation is not allowlisted")
        if set(request) != expected or request.get("schema_version") != 1:
            raise AdminError("review request contains missing or unexpected fields")
        request_id = request.get("request_id")
        candidate = request.get("candidate_commit")
        reason = request.get("reason")
        reference = request.get("approval_reference")
        if not isinstance(request_id, str) or IDENTIFIER_RE.fullmatch(request_id) is None:
            raise AdminError("review request ID is invalid")
        if not isinstance(candidate, str) or COMMIT_RE.fullmatch(candidate) is None:
            raise AdminError("review candidate commit is invalid")
        if not isinstance(reason, str) or not 8 <= len(reason) <= 500 or any(ord(char) < 32 for char in reason):
            raise AdminError("review reason is invalid")
        if not isinstance(reference, str) or APPROVAL_REFERENCE_RE.fullmatch(reference) is None:
            raise AdminError("approval reference is not safe opaque data")
        created = _parse_timestamp(request.get("created_at"), "created_at")
        expires = _parse_timestamp(request.get("expires_at"), "expires_at")
        now = self.clock()
        if created > now + timedelta(minutes=5) or created < now - timedelta(hours=24):
            raise AdminError("review request creation time is outside the accepted window")
        if expires <= now or expires > created + timedelta(hours=4):
            raise AdminError("review request expiration is stale or exceeds four hours")
        if operation in {"deploy-reviewed", "rollback"}:
            filename = request.get("manifest_file")
            digest = request.get("manifest_sha256")
            if not isinstance(filename, str) or FILENAME_RE.fullmatch(filename) is None or not filename.endswith(".json"):
                raise AdminError("deployment manifest filename is invalid")
            if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
                raise AdminError("deployment manifest digest is invalid")
        if operation == "deploy-reviewed":
            filename = request.get("bundle_file")
            digest = request.get("bundle_sha256")
            if not isinstance(filename, str) or FILENAME_RE.fullmatch(filename) is None or not filename.endswith(".bundle"):
                raise AdminError("candidate bundle filename is invalid")
            if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
                raise AdminError("candidate bundle digest is invalid")
        if operation == "rollback":
            operation_id = request.get("operation_id")
            if not isinstance(operation_id, str) or IDENTIFIER_RE.fullmatch(operation_id) is None:
                raise AdminError("rollback operation ID is invalid")
        return request

    def _assert_request_id_unused(self, request_id: str, operation: str) -> None:
        pending = self.config.pending_root / f"{operation}.json"
        if pending.exists() or pending.is_symlink():
            raise AdminError(f"a reviewed {operation} request is already pending")
        for root in (self.config.consumed_root, self.config.result_root):
            if (root / f"{request_id}.json").exists() or (root / f"{request_id}.json").is_symlink():
                raise AdminError("review request ID has already been consumed")

    def _copy_review_material(self, filename: str, expected_digest: str, destination: Path, maximum: int) -> str:
        payload = _read_source_root_file(
            self.config.request_source_root,
            filename,
            owner_uid=self.config.operator_uid,
            maximum_size=maximum,
        )
        digest = _sha256_bytes(payload)
        if digest != expected_digest:
            raise AdminError(f"review material digest mismatch: {filename}")
        _atomic_write(destination, payload, 0o400)
        return digest

    def _prepare_repository(self, bundle: Path, candidate: str) -> None:
        git = (
            "/usr/bin/git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.untrackedCache=false",
            "-C",
            str(self.config.trusted_repository),
        )
        commands = (
            (*git, "bundle", "verify", str(bundle)),
            (*git, "status", "--porcelain=v1", "--untracked-files=all"),
        )
        for index, command in enumerate(commands):
            result = self._run(command, cwd=Path("/"), description="trusted repository preflight")
            if index == 1 and result.stdout.strip():
                raise AdminError("trusted repository is dirty")
        self._run(
            (*git, "fetch", "--no-tags", str(bundle), candidate),
            cwd=Path("/"),
            description="reviewed candidate fetch",
        )
        self._run(
            (*git, "checkout", "--detach", candidate),
            cwd=Path("/"),
            description="reviewed candidate checkout",
        )
        actual = self._run(
            (*git, "rev-parse", "HEAD"),
            cwd=Path("/"),
            description="trusted candidate identity check",
        ).stdout.strip()
        if actual != candidate:
            raise AdminError("trusted repository checkout does not match the reviewed candidate")

    def _authorize(self, request: dict[str, object], manifest: Path, action: str) -> str:
        self._run_json(
            (
                str(self.config.promote_path),
                "verify",
                "--manifest",
                str(manifest),
                "--repository-dir",
                str(self.config.trusted_repository),
            ),
            cwd=Path("/"),
            description="trusted candidate verification",
        )
        payload = self._run_json(
            (
                str(self.config.promote_path),
                "authorize",
                "--manifest",
                str(manifest),
                "--repository-dir",
                str(self.config.trusted_repository),
                "--action",
                action,
                "--reason",
                str(request["reason"]),
                "--approval-reference",
                str(request["approval_reference"]),
                "--expires-minutes",
                "240",
                "--yes",
            ),
            cwd=Path("/"),
            description="human authorization materialization",
        )
        authorization_id = payload.get("authorization_id")
        if not isinstance(authorization_id, str) or IDENTIFIER_RE.fullmatch(authorization_id) is None:
            raise AdminError("trusted authorization did not return a safe authorization ID")
        return authorization_id

    def review(self) -> dict[str, object]:
        _assert_source_directory(self.config.request_source_root, self.config.operator_uid)
        raw = _read_source_root_file(
            self.config.request_source_root,
            "request.json",
            owner_uid=self.config.operator_uid,
            maximum_size=64 * 1024,
        )
        request = self._validate_review_request(_read_json_bytes(raw, "review request"))
        request_id = str(request["request_id"])
        operation = str(request["operation"])
        candidate = str(request["candidate_commit"])
        self._assert_request_id_unused(request_id, operation)
        material = self.config.material_root / request_id
        if material.exists() or material.is_symlink():
            raise AdminError("review material directory already exists")
        material.mkdir(mode=0o700)
        manifest_path: Path | None = None
        artifact_digests: dict[str, str] = {"request.json": _sha256_bytes(raw)}
        try:
            _atomic_write(material / "request.json", raw, 0o400)
            if operation in {"deploy-reviewed", "rollback"}:
                manifest_path = material / "candidate.json"
                artifact_digests["candidate.json"] = self._copy_review_material(
                    str(request["manifest_file"]),
                    str(request["manifest_sha256"]),
                    manifest_path,
                    1024 * 1024,
                )
                manifest = _read_json_bytes(manifest_path.read_bytes(), "deployment manifest")
                if manifest.get("candidate_commit") != candidate:
                    raise AdminError("deployment manifest candidate does not match the review request")
            if operation == "deploy-reviewed":
                bundle_path = material / "candidate.bundle"
                artifact_digests["candidate.bundle"] = self._copy_review_material(
                    str(request["bundle_file"]),
                    str(request["bundle_sha256"]),
                    bundle_path,
                    256 * 1024 * 1024,
                )
                self._prepare_repository(bundle_path, candidate)
            elif self._current_commit() != candidate:
                raise AdminError("review candidate does not match the active immutable release")

            authorization_id = None
            if operation in {"deploy-reviewed", "rollback"}:
                assert manifest_path is not None
                authorization_id = self._authorize(
                    request,
                    manifest_path,
                    "promote" if operation == "deploy-reviewed" else "rollback",
                )
            reviewed = {
                "schema_version": 1,
                "request_id": request_id,
                "operation": operation,
                "candidate_commit": candidate,
                "created_at": request["created_at"],
                "expires_at": request["expires_at"],
                "reviewed_at": _timestamp(self.clock()),
                "reviewed_by": self.config.operator_name,
                "request_sha256": _sha256_bytes(raw),
                "artifact_sha256": artifact_digests,
                "material_root": str(material),
                "authorization_id": authorization_id,
                "operation_id": request.get("operation_id"),
            }
            pending = self.config.pending_root / f"{operation}.json"
            _atomic_write(pending, (_canonical_json(reviewed) + "\n").encode("utf-8"), 0o600)
            return {
                "status": "reviewed",
                "request_id": request_id,
                "operation": operation,
                "candidate_commit": candidate,
                "expires_at": request["expires_at"],
                "artifact_sha256": artifact_digests,
                "passwordless_execution_ready": True,
            }
        except Exception:
            # Preserve copied evidence for root inspection, but never create a pending operation.
            raise

    def _load_pending(self, operation: str, *, verify_artifacts: bool = True) -> dict[str, object]:
        path = self.config.pending_root / f"{operation}.json"
        _assert_private_file(path, self.expected_owner_uid)
        payload = _read_json_bytes(path.read_bytes(), "reviewed operation")
        required = {
            "schema_version",
            "request_id",
            "operation",
            "candidate_commit",
            "created_at",
            "expires_at",
            "reviewed_at",
            "reviewed_by",
            "request_sha256",
            "artifact_sha256",
            "material_root",
            "authorization_id",
            "operation_id",
        }
        if set(payload) != required or payload.get("schema_version") != 1 or payload.get("operation") != operation:
            raise AdminError("reviewed operation schema or type is invalid")
        request_id = payload.get("request_id")
        candidate = payload.get("candidate_commit")
        if not isinstance(request_id, str) or IDENTIFIER_RE.fullmatch(request_id) is None:
            raise AdminError("reviewed request ID is invalid")
        if not isinstance(candidate, str) or COMMIT_RE.fullmatch(candidate) is None:
            raise AdminError("reviewed candidate commit is invalid")
        if _parse_timestamp(payload.get("expires_at"), "expires_at") <= self.clock():
            raise AdminError("reviewed operation has expired")
        material = self.config.material_root / request_id
        if payload.get("material_root") != str(material):
            raise AdminError("reviewed material root does not match its fixed private location")
        digests = payload.get("artifact_sha256")
        if not isinstance(digests, dict) or not digests:
            raise AdminError("reviewed artifact digest inventory is invalid")
        for filename, digest in digests.items():
            if not isinstance(filename, str) or filename not in {"request.json", "candidate.json", "candidate.bundle"}:
                raise AdminError("reviewed artifact filename is not allowlisted")
            if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
                raise AdminError("reviewed artifact digest is invalid")
            if verify_artifacts:
                artifact = material / filename
                _assert_private_file(artifact, self.expected_owner_uid)
                if _sha256_file(artifact) != digest:
                    raise AdminError(f"reviewed artifact was substituted: {filename}")
        return payload

    def _consume(self, operation: str, reviewed: dict[str, object]) -> Path:
        source = self.config.pending_root / f"{operation}.json"
        destination = self.config.consumed_root / f"{reviewed['request_id']}.json"
        if destination.exists() or destination.is_symlink():
            raise AdminError("reviewed operation has already been consumed")
        os.replace(source, destination)
        return destination

    def execute_reviewed(self, operation: str) -> dict[str, object]:
        reviewed = self._load_pending(operation)
        candidate = str(reviewed["candidate_commit"])
        if operation != "deploy-reviewed" and self._current_commit() != candidate:
            raise AdminError("active release drifted from the reviewed candidate")
        self.verify()
        self._consume(operation, reviewed)
        material = self.config.material_root / str(reviewed["request_id"])
        if operation == "deploy-reviewed":
            authorization_id = reviewed.get("authorization_id")
            if not isinstance(authorization_id, str) or IDENTIFIER_RE.fullmatch(authorization_id) is None:
                raise AdminError("reviewed promotion authorization is invalid")
            payload = self._run_json(
                (
                    str(self.config.promote_path),
                    "apply",
                    "--manifest",
                    str(material / "candidate.json"),
                    "--repository-dir",
                    str(self.config.trusted_repository),
                    "--authorization-id",
                    authorization_id,
                ),
                cwd=Path("/"),
                description="reviewed production deployment",
            )
        elif operation == "cutover-reviewed":
            result = self._run(
                (str(self.config.cutover_path), "--apply", "--confirm", "CUTOVER_MINILLM_PRODUCTION"),
                cwd=Path("/"),
                description="reviewed production cutover",
            )
            payload = {"status": "completed", "output_sha256": _sha256_bytes(result.stdout.encode("utf-8"))}
        elif operation == "rollback":
            authorization_id = reviewed.get("authorization_id")
            operation_id = reviewed.get("operation_id")
            if (
                not isinstance(authorization_id, str)
                or IDENTIFIER_RE.fullmatch(authorization_id) is None
                or not isinstance(operation_id, str)
                or IDENTIFIER_RE.fullmatch(operation_id) is None
            ):
                raise AdminError("reviewed rollback identifiers are invalid")
            payload = self._run_json(
                (
                    str(self.config.promote_path),
                    "rollback",
                    "--operation-id",
                    operation_id,
                    "--authorization-id",
                    authorization_id,
                    "--repository-dir",
                    str(self.config.trusted_repository),
                ),
                cwd=Path("/"),
                description="reviewed production rollback",
            )
        else:
            raise AdminError("reviewed operation is not allowlisted")
        result = {
            "schema_version": 1,
            "request_id": reviewed["request_id"],
            "operation": operation,
            "candidate_commit": candidate,
            "completed_at": _timestamp(self.clock()),
            "result": payload,
        }
        _atomic_write(
            self.config.result_root / f"{reviewed['request_id']}.json",
            (_canonical_json(result) + "\n").encode("utf-8"),
            0o600,
        )
        return {"status": "completed", "artifact_sha256": reviewed["artifact_sha256"], **result}

    def audit_verify(self) -> dict[str, object]:
        records = self.audit.read_all()
        return {
            "status": "ok",
            "records": len(records),
            "last_record_sha256": records[-1]["record_sha256"] if records else None,
        }

    def revoke(self) -> dict[str, object]:
        if self.config.sudoers_path.exists():
            self.config.sudoers_path.unlink()
        marker = self.config.admin_root / "REVOKED"
        _atomic_write(marker, (_timestamp(self.clock()) + "\n").encode("ascii"), 0o600)
        return {"status": "revoked", "sudoers_removed": True, "audit_preserved": True}

    def dispatch(self, operation: str) -> dict[str, object]:
        self._verify_installed_artifacts()
        if operation == "status":
            return self.status()
        if operation == "verify":
            return self.verify()
        if operation == "review":
            return self.review()
        if operation in REVIEWED_OPERATIONS:
            return self.execute_reviewed(operation)
        if operation == "audit-verify":
            return self.audit_verify()
        if operation == "revoke":
            return self.revoke()
        raise AdminError("operation is not allowlisted")

    def invoke(self, operation: str, environment: Mapping[str, str]) -> dict[str, object]:
        operator = self._assert_operator(environment)
        started = self.clock()
        candidate = None
        request_id = None
        artifact_digests: dict[str, str] = {}
        stages = ["validate_operator", "verify_installed_artifacts"]
        result_status = "error"
        rollback_status = "not_requested"
        error = None
        payload: dict[str, object]
        lock_flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            lock_flags |= os.O_NOFOLLOW
        try:
            lock_fd = os.open(self.config.lock_path, lock_flags, 0o600)
        except OSError as exc:
            raise AdminError("admin operation lock cannot be opened safely") from exc
        try:
            lock_metadata = os.fstat(lock_fd)
            if (
                not stat.S_ISREG(lock_metadata.st_mode)
                or lock_metadata.st_uid != self.expected_owner_uid
                or stat.S_IMODE(lock_metadata.st_mode) & 0o077
            ):
                raise AdminError("admin operation lock ownership or permissions are unsafe")
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                if operation in REVIEWED_OPERATIONS:
                    try:
                        context = self._load_pending(operation, verify_artifacts=False)
                    except AdminError:
                        context = {}
                    context_candidate = context.get("candidate_commit")
                    context_request = context.get("request_id")
                    context_digests = context.get("artifact_sha256")
                    candidate = str(context_candidate) if context_candidate else None
                    request_id = str(context_request) if context_request else None
                    if isinstance(context_digests, dict):
                        artifact_digests = {str(key): str(value) for key, value in context_digests.items()}
                    stages.append("load_reviewed_request")
                payload = self.dispatch(operation)
                result_status = str(payload.get("status", "ok"))
                candidate_value = payload.get("candidate_commit") or payload.get("current_commit")
                candidate = str(candidate_value) if candidate_value else None
                request_value = payload.get("request_id")
                request_id = str(request_value) if request_value else None
                digest_value = payload.get("artifact_sha256")
                if isinstance(digest_value, dict):
                    artifact_digests = {str(key): str(value) for key, value in digest_value.items()}
                nested = payload.get("result")
                if isinstance(nested, dict) and nested.get("rollback_required") is True:
                    rollback_status = "required"
                elif operation == "rollback" and result_status == "completed":
                    rollback_status = "completed"
                stages.append("operation_completed")
            except AdminError as exc:
                error = str(exc)
                payload = {"status": "error", "operation": operation, "error": error}
                stages.append("operation_denied")
            finished = self.clock()
            self.audit.append(
                {
                    "recorded_at": _timestamp(finished),
                    "started_at": _timestamp(started),
                    "finished_at": _timestamp(finished),
                    "operator": operator,
                    "operator_uid": self.config.operator_uid,
                    "operation": operation,
                    "request_id": request_id,
                    "candidate_commit": candidate,
                    "artifact_sha256": artifact_digests,
                    "stages": stages,
                    "result": result_status,
                    "rollback_status": rollback_status,
                    "error": error,
                    "secrets_recorded": False,
                }
            )
            return payload
        except BlockingIOError as exc:
            raise AdminError("another MiniLLM administration operation is in progress") from exc
        finally:
            os.close(lock_fd)


def _reject_environment_injection(environment: Mapping[str, str]) -> None:
    for name in environment:
        if any(name == prefix or name.startswith(prefix) for prefix in INJECTABLE_ENVIRONMENT_PREFIXES):
            raise AdminError(f"environment override is not permitted: {name}")


def main(argv: Sequence[str] | None = None, environment: Mapping[str, str] | None = None) -> int:
    arguments = list(argv if argv is not None else sys.argv[1:])
    active_environment = dict(environment if environment is not None else os.environ)
    try:
        if len(arguments) != 1 or arguments[0] not in ALL_OPERATIONS:
            raise AdminError("usage: minillm-admin status|verify|review|deploy-reviewed|cutover-reviewed|rollback|audit-verify|revoke")
        if os.geteuid() != 0:
            raise AdminError("minillm-admin must run as root through the installed sudo policy")
        _reject_environment_injection(active_environment)
        config = load_config()
        payload = MiniLLMAdmin(config).invoke(arguments[0], active_environment)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload.get("status") not in {"error", "blocked"} else 1
    except AdminError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, indent=2, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
