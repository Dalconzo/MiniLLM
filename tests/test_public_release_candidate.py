from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "create_public_release_candidate.py"


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return result.stdout.strip()


def _git_env() -> dict[str, str]:
    return {
        **os.environ,
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_AUTHOR_NAME": "Fixture Author",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "Fixture Author",
    }


def _commit(repo: Path, message: str) -> str:
    env = _git_env()
    _git(repo, "add", "-A", env=env)
    _git(repo, "commit", "-q", "-m", message, env=env)
    return _git(repo, "rev-parse", "HEAD")


def _source_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "-q", "--initial-branch=main")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "README.md").write_text("# Public fixture\n", encoding="utf-8")
    (repo / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    _commit(repo, "initial public tree")
    return repo


def _literal_file(tmp_path: Path, value: str | None = None) -> Path:
    path = tmp_path / "private-literals.txt"
    if value is None:
        value = "fixture-" + "private-marker"
    path.write_text(f"{value}\n", encoding="utf-8")
    return path


def _run(
    repo: Path,
    output: Path,
    literals: Path,
    *,
    check_only: bool = False,
    expected_code: int = 0,
) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        str(SCRIPT),
        "--source-repo",
        str(repo),
        "--source-commit",
        "HEAD",
        "--output-dir",
        str(output),
        "--forbidden-literals-file",
        str(literals),
    ]
    if check_only:
        command.append("--check-only")
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == expected_code, result.stderr
    return result


def test_export_is_parentless_and_does_not_copy_private_source_history(tmp_path: Path) -> None:
    private_value = "private-" + "history-canary"
    current_value = "current-" + "browser-canary"
    repo = _source_repo(tmp_path)
    private_report = repo / "test-results" / "browser-report.md"
    private_report.parent.mkdir()
    private_report.write_text(private_value, encoding="utf-8")
    _commit(repo, "private development report")
    private_report.unlink()
    private_report.write_text(current_value, encoding="utf-8")
    source_commit = _commit(repo, "remove private report")

    output = tmp_path / "public"
    literals = _literal_file(tmp_path, f"{private_value}\n{current_value}")
    result = _run(repo, output, literals)
    response = json.loads(result.stdout)
    manifest = json.loads((tmp_path / "public.release-manifest.json").read_text(encoding="utf-8"))

    assert response["source_commit"] == source_commit
    assert response["output_dir"] == "<public-candidate>"
    assert response["manifest"] == "<public-candidate-manifest>"
    assert str(tmp_path) not in result.stdout
    assert len(_git(output, "rev-list", "--all").splitlines()) == 1
    assert _git(output, "show", "-s", "--format=%P", "HEAD") == ""
    assert _git(output, "rev-list", "--all") not in _git(repo, "rev-list", "--all")
    assert not (output / "test-results").exists()
    assert private_value.encode() not in _all_git_object_bytes(output)
    assert current_value.encode() not in _all_git_object_bytes(output)
    assert manifest["source"]["commit"] == source_commit
    assert manifest["output"]["commit"] == _git(output, "rev-parse", "HEAD")
    assert manifest["lock"]["sha256"]
    assert manifest["scan"]["passed"] is True
    _run(repo, output, literals, check_only=True)


def _all_git_object_bytes(repo: Path) -> bytes:
    inventory = _git(repo, "cat-file", "--batch-all-objects", "--batch-check=%(objectname) %(objecttype)")
    contents = bytearray()
    for line in inventory.splitlines():
        object_id, object_type = line.split()
        if object_type in {"blob", "commit", "tag"}:
            contents.extend(subprocess.check_output(["git", "-C", str(repo), "cat-file", object_type, object_id]))
    return bytes(contents)


def test_creation_fails_closed_on_dirty_source_but_check_only_is_allowed(tmp_path: Path) -> None:
    repo = _source_repo(tmp_path)
    output = tmp_path / "public"
    literals = _literal_file(tmp_path)
    _run(repo, output, literals)
    (repo / "README.md").write_text("dirty\n", encoding="utf-8")

    failed = _run(repo, tmp_path / "second-public", literals, expected_code=1)
    assert "source repository is dirty" in failed.stderr
    _run(repo, output, literals, check_only=True)


@pytest.mark.parametrize(
    "relative_path",
    [
        "docs/browser_chatgpt_acceptance_results.md",
        "docs/evaluation/evidence/release-result.json",
        "docs/mac_mini_private_memory_smoke_checklist.md",
        "examples/human_memory_cognition_extensions_spec.md",
    ],
)
def test_internal_documents_are_excluded_from_public_candidate(tmp_path: Path, relative_path: str) -> None:
    repo = _source_repo(tmp_path)
    path = repo / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("internal development material\n", encoding="utf-8")
    _commit(repo, "add internal development document")

    output = tmp_path / "public"
    _run(repo, output, _literal_file(tmp_path))

    assert not (output / relative_path).exists()


@pytest.mark.parametrize(
    "relative_path",
    [
        ".env",
        "src/id_rsa.backup",
        "config/service-account-prod.json",
    ],
)
def test_secret_like_tracked_paths_reject_instead_of_silent_exclusion(tmp_path: Path, relative_path: str) -> None:
    repo = _source_repo(tmp_path)
    path = repo / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not-a-real-secret\n", encoding="utf-8")
    _commit(repo, "add forbidden path")

    failed = _run(repo, tmp_path / "public", _literal_file(tmp_path), expected_code=1)
    assert "secret-like tracked path" in failed.stderr
    assert not (tmp_path / "public").exists()


@pytest.mark.parametrize(
    "forbidden_value",
    [
        "/" + "Users/example/private/project",
        "/private/" + "tmp",
        "/private/" + "tmp/public-evidence.json",
        "20260815" + "T043029Z-" + "f2324ef3",
        "lagent-" + "314",
        "cand_" + "0123456789abcdef",
        "projects:" + "_acceptance" + "_tests/" + "results/report.md",
        "sk-" + "proj-abcdefghijklmnop",
        "AKIA" + "ABCDEFGHIJKLMNOP",
        "xoxb-" + "abcdefghijklmnop",
        "private-" + "corpus-canary",
    ],
)
def test_forbidden_content_rejects_candidate(tmp_path: Path, forbidden_value: str) -> None:
    repo = _source_repo(tmp_path)
    (repo / "src" / "private.py").write_text(forbidden_value, encoding="utf-8")
    _commit(repo, "add forbidden content")

    corpus_literal = forbidden_value if forbidden_value.startswith("private-") else None
    failed = _run(repo, tmp_path / "public", _literal_file(tmp_path, corpus_literal), expected_code=1)
    assert "found in src/private.py" in failed.stderr
    assert not (tmp_path / "public").exists()


def test_check_only_rejects_extra_history_and_objects(tmp_path: Path) -> None:
    repo = _source_repo(tmp_path)
    output = tmp_path / "public"
    literals = _literal_file(tmp_path)
    _run(repo, output, literals)
    (output / "src" / "extra.py").write_text("VALUE = 2\n", encoding="utf-8")
    _commit(output, "unexpected second commit")

    failed = _run(repo, output, literals, check_only=True, expected_code=1)
    assert "exactly one commit" in failed.stderr


def test_check_only_rejects_manifest_tampering(tmp_path: Path) -> None:
    repo = _source_repo(tmp_path)
    output = tmp_path / "public"
    literals = _literal_file(tmp_path)
    _run(repo, output, literals)
    manifest_path = tmp_path / "public.release-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["lock"]["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    failed = _run(repo, output, literals, check_only=True, expected_code=1)
    assert "manifest does not match" in failed.stderr


def test_check_only_rejects_candidate_missing_an_approved_source_file(tmp_path: Path) -> None:
    repo = _source_repo(tmp_path)
    output = tmp_path / "public"
    literals = _literal_file(tmp_path)
    _run(repo, output, literals)
    (output / "src" / "app.py").unlink()
    _commit(output, "replace candidate with incomplete parentless tree")
    current = _git(output, "rev-parse", "HEAD")
    tree = _git(output, "rev-parse", f"{current}^{{tree}}")
    env = _git_env()
    replacement = subprocess.run(
        ["git", "-C", str(output), "commit-tree", tree],
        input="incomplete candidate\n",
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.strip()
    _git(output, "update-ref", "refs/heads/main", replacement)
    _git(output, "reflog", "expire", "--expire=now", "--all")
    _git(output, "gc", "--prune=now")

    failed = _run(repo, output, literals, check_only=True, expected_code=1)
    assert "exactly match approved source blobs" in failed.stderr


def test_check_only_rejects_hard_linked_worktree_file(tmp_path: Path) -> None:
    repo = _source_repo(tmp_path)
    output = tmp_path / "public"
    literals = _literal_file(tmp_path)
    _run(repo, output, literals)
    source_file = output / "src" / "app.py"
    alias = tmp_path / "external-alias.py"
    data = source_file.read_bytes()
    source_file.unlink()
    alias.write_bytes(data)
    os.link(alias, source_file)

    failed = _run(repo, output, literals, check_only=True, expected_code=1)
    assert "single-link regular file" in failed.stderr


def test_same_source_commit_produces_same_output_commit(tmp_path: Path) -> None:
    repo = _source_repo(tmp_path)
    literals = _literal_file(tmp_path)
    first = tmp_path / "public-one"
    second = tmp_path / "public-two"
    _run(repo, first, literals)
    _run(repo, second, literals)

    assert _git(first, "rev-parse", "HEAD") == _git(second, "rev-parse", "HEAD")


def test_tracked_symlink_is_rejected(tmp_path: Path) -> None:
    repo = _source_repo(tmp_path)
    (repo / "src" / "link.py").symlink_to("app.py")
    _commit(repo, "add symlink")

    failed = _run(repo, tmp_path / "public", _literal_file(tmp_path), expected_code=1)
    assert "non-regular tracked entry" in failed.stderr


@pytest.mark.parametrize("relative_path", ["src/%2e%2e/private.py", "src/%252e%252e/private.py", "src\\private.py"])
def test_encoded_and_mixed_separator_paths_are_rejected(tmp_path: Path, relative_path: str) -> None:
    repo = _source_repo(tmp_path)
    path = repo / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("VALUE = 2\n", encoding="utf-8")
    _commit(repo, "add unsafe logical path")

    failed = _run(repo, tmp_path / "public", _literal_file(tmp_path), expected_code=1)
    assert "unsafe tracked path" in failed.stderr
