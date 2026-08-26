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


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER_SCRIPT = REPO_ROOT / "scripts" / "run_clean_clone_evidence.py"
CREATOR_SCRIPT = REPO_ROOT / "scripts" / "create_public_release_candidate.py"
MANIFEST_SCRIPT = REPO_ROOT / "scripts" / "build_release_evidence_manifest.py"


def _load_runner() -> ModuleType:
    spec = importlib.util.spec_from_file_location("minillm_clean_clone_evidence_runner", RUNNER_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


RUNNER = _load_runner()


def _git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_EMAIL": "clean-clone@example.invalid",
        "GIT_AUTHOR_NAME": "Clean Clone Test",
        "GIT_COMMITTER_EMAIL": "clean-clone@example.invalid",
        "GIT_COMMITTER_NAME": "Clean Clone Test",
    }
    completed = subprocess.run(
        ("git", "-C", str(repo), *args),
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return completed.stdout.strip()


def _source_repo(tmp_path: Path, *, verify_body: str | None = None) -> Path:
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "-q", "--initial-branch=main")
    if verify_body is None:
        verify_body = """#!/usr/bin/env bash
set -euo pipefail
test ! -e .venv
mkdir .venv
printf 'verified fixture\\n'
"""
    files = {
        ".gitignore": ".venv/\n",
        "README.md": "# Public clean-clone fixture\n",
        "pyproject.toml": "[project]\nname='minillm'\nversion='0.0.0'\n",
        "scripts/create_public_release_candidate.py": CREATOR_SCRIPT.read_text(encoding="utf-8"),
        "scripts/verify.sh": verify_body,
        "src/minillm_fixture.py": "VALUE = 1\n",
        "uv.lock": "version = 1\n",
    }
    for relative, content in files.items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    (repo / "scripts" / "create_public_release_candidate.py").chmod(0o755)
    (repo / "scripts" / "verify.sh").chmod(0o755)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "clean release source")
    return repo


def _literal_file(tmp_path: Path) -> Path:
    path = tmp_path / "external-private-literals.txt"
    path.write_text("synthetic-overlap-canary\n", encoding="utf-8")
    return path


def _run(
    tmp_path: Path,
    *,
    repo: Path | None = None,
    verify_command: tuple[str, ...] = ("./scripts/verify.sh",),
    candidate_mutator: object | None = None,
) -> tuple[Path, Path, Path, dict[str, object]]:
    source = repo or _source_repo(tmp_path)
    candidate = tmp_path / "public-candidate"
    manifest = tmp_path / "public-candidate.release-manifest.json"
    artifact = tmp_path / "clean-clone.json"
    evidence = RUNNER.run_clean_clone_evidence(
        source_repo=source,
        source_commit="HEAD",
        candidate_dir=candidate,
        candidate_manifest=manifest,
        artifact_path=artifact,
        forbidden_literals_files=[_literal_file(tmp_path)],
        verify_command=verify_command,
        candidate_mutator=candidate_mutator,
    )
    return candidate, manifest, artifact, evidence


def test_runner_creates_real_parentless_candidate_and_private_artifact(tmp_path: Path) -> None:
    candidate, manifest_path, artifact_path, evidence = _run(tmp_path)
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    public_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert artifact == evidence
    assert stat.S_IMODE(artifact_path.stat().st_mode) == 0o600
    assert artifact["schema_version"] == "minillm_release_evidence_artifact_v1"
    assert artifact["artifact_type"] == "clean_clone"
    assert artifact["status"] == "pass"
    assert artifact["metrics"] == {
        "demo_passed": True,
        "fresh_clone": True,
        "full_tests_passed": True,
        "locked_install_passed": True,
        "privacy_scan_passed": True,
        "single_parentless_public_commit": True,
    }
    assert artifact["repository"]["git_commit"] == public_manifest["source"]["commit"]
    assert artifact["repository"]["git_tree"] == public_manifest["source"]["tree"]
    assert artifact["repository"]["uv_lock_sha256"] == hashlib.sha256((candidate / "uv.lock").read_bytes()).hexdigest()
    assert _git(candidate, "show", "-s", "--format=%P", "HEAD") == ""
    assert len(_git(candidate, "rev-list", "--all").splitlines()) == 1
    assert _git(candidate, "remote") == ""
    assert _git(candidate, "status", "--porcelain=v1", "--untracked-files=all") == ""
    assert (candidate / ".venv").is_dir()
    assert [item["stage"] for item in artifact["details"]["commands"]] == [
        "create_candidate",
        "candidate_verify",
        "check_only",
    ]
    assert all("stdout" not in item and "stderr" not in item for item in artifact["details"]["commands"])
    assert "verified fixture" not in artifact_path.read_text(encoding="utf-8")
    serialized = artifact_path.read_text(encoding="utf-8")
    assert str(tmp_path) not in serialized
    assert "/Users/" not in serialized
    assert "/private/" + "tmp/" not in serialized
    assert artifact["details"]["commands"][0]["argv"][:5] == [
        "python",
        "scripts/create_public_release_candidate.py",
        "--source-repo",
        ".",
        "--source-commit",
    ]
    assert "<public-candidate>" in artifact["details"]["commands"][0]["argv"]
    assert "<private-overlap-canaries:1>" in artifact["details"]["commands"][0]["argv"]


def test_artifact_passes_real_manifest_clean_clone_contract(tmp_path: Path) -> None:
    source = _source_repo(tmp_path)
    _, _, artifact, _ = _run(tmp_path, repo=source)
    output = tmp_path / "release-evidence-manifest.json"
    completed = subprocess.run(
        (
            sys.executable,
            str(MANIFEST_SCRIPT),
            "--repo",
            str(source),
            "--mode",
            "pre-release",
            "--as-of",
            datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "--output",
            str(output),
            "--clean-clone",
            str(artifact),
        ),
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    report = json.loads(output.read_text(encoding="utf-8"))
    record = next(item for item in report["artifacts"] if item["artifact_type"] == "clean_clone")
    assert record["gate_passed"] is True


def test_dirty_source_fails_before_candidate_creation(tmp_path: Path) -> None:
    source = _source_repo(tmp_path)
    (source / "README.md").write_text("dirty\n", encoding="utf-8")

    try:
        _run(tmp_path, repo=source)
    except RUNNER.CleanCloneEvidenceError as exc:
        message = str(exc)
        assert "stage=source_validation" in message
        assert "code=source_dirty" in message
    else:
        raise AssertionError("dirty source unexpectedly produced evidence")
    assert not (tmp_path / "public-candidate").exists()


def test_preexisting_output_fails_closed(tmp_path: Path) -> None:
    source = _source_repo(tmp_path)
    (tmp_path / "public-candidate").mkdir()

    try:
        _run(tmp_path, repo=source)
    except RUNNER.CleanCloneEvidenceError as exc:
        assert "stage=output_validation" in str(exc)
        assert "code=output_preexists" in str(exc)
    else:
        raise AssertionError("preexisting output was overwritten")


def test_failed_verify_reports_only_counts_and_hashes(tmp_path: Path) -> None:
    secret = "PRIVATE VERIFY STDERR MUST NOT LEAK"
    source = _source_repo(
        tmp_path,
        verify_body=f"#!/usr/bin/env bash\nprintf '{secret}' >&2\nexit 23\n",
    )

    try:
        _run(tmp_path, repo=source)
    except RUNNER.CleanCloneEvidenceError as exc:
        message = str(exc)
        assert "stage=candidate_verify index=1 exit=23" in message
        assert "stderr_bytes=35" in message
        assert "stderr_sha256=" in message
        assert secret not in message
    else:
        raise AssertionError("failed verify unexpectedly produced evidence")
    assert not (tmp_path / "clean-clone.json").exists()


def test_non_parentless_candidate_is_rejected_by_independent_check(tmp_path: Path) -> None:
    def add_commit(candidate: Path, _manifest: Path) -> None:
        (candidate / "README.md").write_text("tampered\n", encoding="utf-8")
        _git(candidate, "add", "README.md")
        _git(candidate, "commit", "-q", "-m", "tamper candidate")

    try:
        _run(tmp_path, candidate_mutator=add_commit)
    except RUNNER.CleanCloneEvidenceError as exc:
        message = str(exc)
        assert "stage=check_only index=1 exit=1" in message
        assert "stderr_sha256=" in message
        assert "candidate history" not in message
    else:
        raise AssertionError("non-parentless candidate unexpectedly produced evidence")


def test_tampered_manifest_is_rejected_without_raw_output(tmp_path: Path) -> None:
    def tamper_manifest(_candidate: Path, manifest: Path) -> None:
        value = json.loads(manifest.read_text(encoding="utf-8"))
        value["lock"]["sha256"] = "0" * 64
        manifest.write_text(json.dumps(value), encoding="utf-8")

    try:
        _run(tmp_path, candidate_mutator=tamper_manifest)
    except RUNNER.CleanCloneEvidenceError as exc:
        message = str(exc)
        assert "stage=check_only index=1 exit=1" in message
        assert "manifest does not match" not in message
    else:
        raise AssertionError("tampered manifest unexpectedly produced evidence")
