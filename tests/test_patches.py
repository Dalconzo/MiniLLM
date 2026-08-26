import subprocess
import stat

import pytest

from local_agent_lab.cli import _persist_generated_patch_artifact, _validate_generated_files
from local_agent_lab.indexing.visibility import VisibilityPolicyError
from local_agent_lab.security.paths import PathPolicyError, safe_append_text, safe_read_text, safe_write_text
from local_agent_lab.tools.patches import PatchFile, apply_files, build_unified_patch


def test_build_unified_patch_handles_new_file(tmp_path) -> None:
    repo = tmp_path / "demo"
    repo.mkdir()
    (repo / "src").mkdir()
    patch = build_unified_patch(
        repo,
        [PatchFile(relative_path="src/app.py", content="print('hi')\n")],
        authorized_relative_paths={"src/app.py"},
    )
    assert "--- a/src/app.py" in patch
    assert "+++ b/src/app.py" in patch
    assert "+print('hi')" in patch


def test_apply_files_writes_content(tmp_path) -> None:
    repo = tmp_path / "demo"
    repo.mkdir()
    (repo / "tests").mkdir()
    written = apply_files(
        repo,
        [PatchFile(relative_path="tests/test_app.py", content="def test_x():\n    pass\n")],
        authorized_relative_paths={"tests/test_app.py"},
    )
    assert written == ["tests/test_app.py"]
    assert (repo / "tests/test_app.py").read_text(encoding="utf-8").startswith("def test_x")


@pytest.mark.parametrize(
    "relative_path",
    [
        "/tmp/owned.py",
        "../owned.py",
        "a/../../owned.py",
        "a\\..\\owned.py",
        "C:/owned.py",
        "%2e%2e/owned.py",
        "a/%2E%2E/owned.py",
        "a//owned.py",
    ],
)
def test_patch_paths_reject_ambiguous_or_escaping_destinations(tmp_path, relative_path) -> None:
    repo = tmp_path / "demo"
    repo.mkdir()
    with pytest.raises(PathPolicyError):
        build_unified_patch(repo, [PatchFile(relative_path=relative_path, content="owned")])


@pytest.mark.parametrize("relative_path", ["/tmp/owned.py", "a/../../owned.py", "%2e%2e/owned.py"])
def test_model_generated_path_validation_rejects_escape_syntax(relative_path) -> None:
    with pytest.raises(PathPolicyError):
        _validate_generated_files([PatchFile(relative_path=relative_path, content="owned")])


def test_nested_generated_destination_requires_exact_caller_authorization(tmp_path) -> None:
    repo = tmp_path / "demo"
    repo.mkdir()
    (repo / "src").mkdir()
    generated = [PatchFile(relative_path="src/app.py", content="print('safe')\n")]

    with pytest.raises(PathPolicyError, match="not explicitly authorized"):
        _validate_generated_files(generated)
    with pytest.raises(PathPolicyError, match="not explicitly authorized"):
        build_unified_patch(repo, generated)
    with pytest.raises(PathPolicyError, match="not explicitly authorized"):
        apply_files(repo, generated)

    authorized = frozenset({"src/app.py"})
    validated = _validate_generated_files(generated, authorized_relative_paths=authorized)
    patch = build_unified_patch(repo, validated, authorized_relative_paths=authorized)
    written = apply_files(repo, validated, authorized_relative_paths=authorized)

    assert "+++ b/src/app.py" in patch
    assert written == ["src/app.py"]


def test_nested_generated_destination_requires_exact_not_parent_authorization(tmp_path) -> None:
    repo = tmp_path / "demo"
    repo.mkdir()
    (repo / "src").mkdir()
    generated = [PatchFile(relative_path="src/app.py", content="owned")]

    with pytest.raises(PathPolicyError, match="not explicitly authorized"):
        build_unified_patch(repo, generated, authorized_relative_paths={"src/other.py"})
    with pytest.raises(PathPolicyError, match="not explicitly authorized"):
        apply_files(repo, generated, authorized_relative_paths={"src/other.py"})
    assert not (repo / "src/app.py").exists()


def test_apply_files_rejects_model_generated_parent_directories(tmp_path) -> None:
    repo = tmp_path / "demo"
    repo.mkdir()
    with pytest.raises(PathPolicyError, match="must already exist"):
        apply_files(
            repo,
            [PatchFile(relative_path="new/nested/file.py", content="owned")],
            authorized_relative_paths={"new/nested/file.py"},
        )
    assert not (repo / "new").exists()


def test_safe_read_and_write_reject_symlinked_file(tmp_path) -> None:
    repo = tmp_path / "demo"
    repo.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    (repo / "link.txt").symlink_to(outside)

    with pytest.raises((PathPolicyError, OSError)):
        safe_read_text(repo, "link.txt")
    with pytest.raises((PathPolicyError, OSError)):
        safe_write_text(repo, "link.txt", "owned")
    assert outside.read_text(encoding="utf-8") == "private"


def test_safe_write_rejects_symlinked_parent(tmp_path) -> None:
    repo = tmp_path / "demo"
    outside = tmp_path / "outside"
    repo.mkdir()
    outside.mkdir()
    (repo / "nested").symlink_to(outside, target_is_directory=True)

    with pytest.raises((PathPolicyError, OSError)):
        safe_write_text(repo, "nested/owned.txt", "owned")
    assert not (outside / "owned.txt").exists()


def test_generated_patch_rejects_secret_destination_at_preview_and_write(tmp_path) -> None:
    repo = tmp_path / "demo"
    repo.mkdir()
    (repo / ".env").write_text("TOKEN=existing-secret", encoding="utf-8")
    generated = [PatchFile(relative_path=".env", content="TOKEN=model-output")]

    with pytest.raises(VisibilityPolicyError, match="secret_denylist"):
        build_unified_patch(repo, generated)
    with pytest.raises(VisibilityPolicyError, match="secret_denylist"):
        apply_files(repo, generated)

    assert (repo / ".env").read_text(encoding="utf-8") == "TOKEN=existing-secret"


def test_generated_patch_rejects_gitignored_nested_destination(tmp_path) -> None:
    repo = tmp_path / "demo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    ignored = repo / "private"
    ignored.mkdir()
    (repo / ".gitignore").write_text("private/\n", encoding="utf-8")
    (ignored / "note.md").write_text("private", encoding="utf-8")
    generated = [PatchFile(relative_path="private/note.md", content="model-output")]

    with pytest.raises(VisibilityPolicyError, match="gitignored"):
        build_unified_patch(repo, generated, authorized_relative_paths={"private/note.md"})
    with pytest.raises(VisibilityPolicyError, match="gitignored"):
        apply_files(repo, generated, authorized_relative_paths={"private/note.md"})


@pytest.mark.parametrize("operation", ["write", "append"])
def test_safe_writes_reject_hard_link_to_outside_file(tmp_path, operation) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside remains unchanged\n", encoding="utf-8")
    (repo / "linked.txt").hardlink_to(outside)

    with pytest.raises(PathPolicyError, match="multiple hard links"):
        if operation == "write":
            safe_write_text(repo, "linked.txt", "model output\n")
        else:
            safe_append_text(repo, "linked.txt", "model output\n")

    assert outside.read_text(encoding="utf-8") == "outside remains unchanged\n"
    assert (repo / "linked.txt").read_text(encoding="utf-8") == "outside remains unchanged\n"


def test_generated_patch_artifact_is_private_redacted_and_no_follow(tmp_path) -> None:
    patches_dir = tmp_path / "patches"
    patches_dir.mkdir()
    outside = tmp_path / "outside.patch"
    outside.write_text("outside remains unchanged\n", encoding="utf-8")
    artifact_name = "write-function-test.patch"
    artifact_path = patches_dir / artifact_name

    artifact_path.symlink_to(outside)
    with pytest.raises((PathPolicyError, OSError)):
        _persist_generated_patch_artifact(patches_dir, artifact_name, "model output\n")
    assert outside.read_text(encoding="utf-8") == "outside remains unchanged\n"

    artifact_path.unlink()
    artifact_path.hardlink_to(outside)
    with pytest.raises(PathPolicyError, match="multiple hard links"):
        _persist_generated_patch_artifact(patches_dir, artifact_name, "model output\n")
    assert outside.read_text(encoding="utf-8") == "outside remains unchanged\n"

    artifact_path.unlink()
    written = _persist_generated_patch_artifact(
        patches_dir,
        artifact_name,
        "+authorization: Bearer sk-" + "evalabcdefghijklmnopqrstuvwxyz123456\n",
    )
    persisted = written.read_text(encoding="utf-8")
    assert "sk-" + "evalabcdefghijklmnopqrstuvwxyz123456" not in persisted
    assert "[REDACTED]" in persisted
    assert stat.S_IMODE(written.stat().st_mode) == 0o600

    with pytest.raises(PathPolicyError, match="nested destinations are not allowed"):
        _persist_generated_patch_artifact(patches_dir, "nested/generated.patch", "model output\n")
