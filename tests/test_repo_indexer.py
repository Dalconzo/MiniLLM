import subprocess

import pytest

from local_agent_lab.cli import _read_visible_repo_text
from local_agent_lab.indexing.repo_indexer import default_db_path, index_repo
from local_agent_lab.indexing.visibility import RepositoryVisibilityPolicy, VisibilityPolicyError
from local_agent_lab.sqlite_utils import sqlite_connection


def _init_git(repo) -> None:
    subprocess.run(["git", "init", "-q", str(repo)], check=True)


def _indexed_paths(db_path) -> set[str]:
    with sqlite_connection(db_path) as connection:
        return {row[0] for row in connection.execute("SELECT relative_path FROM indexed_files")}


def _indexed_content(db_path) -> str:
    with sqlite_connection(db_path) as connection:
        return "\n".join(str(row[0]) for row in connection.execute("SELECT content FROM indexed_chunks"))


def test_index_repo_builds_sqlite_index(tmp_path) -> None:
    repo = tmp_path / "demo"
    repo.mkdir()
    (repo / "main.py").write_text("print('hello world')\n", encoding="utf-8")
    (repo / "README.md").write_text("demo repository\n", encoding="utf-8")
    db_path = default_db_path(tmp_path, repo)
    summary = index_repo(repo, db_path)
    assert summary.indexed_files == 2
    assert summary.indexed_chunks >= 2
    assert db_path.exists()


def test_index_repo_skips_binary_and_replaces_existing_rows(tmp_path) -> None:
    repo = tmp_path / "demo"
    repo.mkdir()
    (repo / "binary.bin").write_bytes(b"\x00\x01\x02")
    source = repo / "file.txt"
    source.write_text("first version", encoding="utf-8")
    db_path = tmp_path / "index.sqlite3"
    first = index_repo(repo, db_path)
    source.write_text("second version with more text", encoding="utf-8")
    second = index_repo(repo, db_path)
    assert first.skipped_files == 1
    assert second.indexed_files == 1


def test_index_repo_honors_gitignore_and_negation(tmp_path) -> None:
    repo = tmp_path / "demo"
    repo.mkdir()
    _init_git(repo)
    (repo / ".gitignore").write_text("*.log\n!important.log\nignored/\n", encoding="utf-8")
    (repo / "debug.log").write_text("private diagnostic", encoding="utf-8")
    (repo / "important.log").write_text("explicitly retained diagnostic", encoding="utf-8")
    ignored = repo / "ignored"
    ignored.mkdir()
    (ignored / "notes.txt").write_text("ignored notes", encoding="utf-8")
    (repo / "main.py").write_text("print('safe')\n", encoding="utf-8")
    db_path = tmp_path / "index.sqlite3"

    index_repo(repo, db_path)

    assert _indexed_paths(db_path) == {".gitignore", "important.log", "main.py"}


def test_index_repo_secret_denylist_is_fail_closed(tmp_path) -> None:
    repo = tmp_path / "demo"
    repo.mkdir()
    _init_git(repo)
    config = repo / "config"
    config.mkdir()
    files = {
        ".env": "PASSWORD=value",
        ".env.example": "PASSWORD=example",
        ".envrc": "export PASSWORD=value",
        "id_ed25519": "private key material",
        "private.pem": "private key material",
        "config/service-credentials.json": '{"token": "value"}',
        "config/service-credentials.yaml": "token: value",
        "safe.py": "SAFE = True\n",
    }
    for relative_path, content in files.items():
        path = repo / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    db_path = tmp_path / "index.sqlite3"

    index_repo(repo, db_path)

    assert _indexed_paths(db_path) == {"safe.py"}


def test_index_repo_blocks_common_secret_config_and_backup_names(tmp_path) -> None:
    repo = tmp_path / "demo"
    repo.mkdir()
    _init_git(repo)
    blocked = {
        "secrets.txt",
        "credentials.ini",
        "id_rsa.backup",
        "private_key.txt",
        "access-token.txt",
        "credentials.conf",
        "service-credentials.ini",
        "secrets.properties",
    }
    for relative_path in blocked:
        (repo / relative_path).write_text("private material", encoding="utf-8")
    (repo / "credential_parser.py").write_text("def parse(value):\n    return value\n", encoding="utf-8")
    (repo / "safe-config.ini").write_text("[application]\nmode = test\n", encoding="utf-8")
    db_path = tmp_path / "index.sqlite3"

    index_repo(repo, db_path)

    assert _indexed_paths(db_path) == {"credential_parser.py", "safe-config.ini"}


def test_index_repo_redacts_secret_content_in_ordinary_tracked_file(tmp_path) -> None:
    repo = tmp_path / "demo"
    repo.mkdir()
    _init_git(repo)
    secret = "sk-" + "abcdefghijklmnopqrstuvwxyz123456"
    (repo / "settings.py").write_text(f'API_KEY = "{secret}"\n', encoding="utf-8")
    db_path = tmp_path / "index.sqlite3"

    index_repo(repo, db_path)

    indexed = _indexed_content(db_path)
    assert secret not in indexed
    assert "REDACTED" in indexed


def test_index_repo_rejects_file_and_parent_symlinks(tmp_path) -> None:
    repo = tmp_path / "demo"
    repo.mkdir()
    _init_git(repo)
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / "secret.txt"
    outside_file.write_text("must not be indexed", encoding="utf-8")
    (repo / "file-link.txt").symlink_to(outside_file)
    (repo / "directory-link").symlink_to(outside, target_is_directory=True)
    (repo / "safe.txt").write_text("safe content", encoding="utf-8")
    db_path = tmp_path / "index.sqlite3"

    index_repo(repo, db_path)

    assert _indexed_paths(db_path) == {"safe.txt"}
    policy = RepositoryVisibilityPolicy(repo)
    assert policy.evaluate(repo / "file-link.txt").reason == "symlink"
    assert policy.evaluate(outside_file).reason == "outside_root"


def test_index_repo_excludes_private_export_directories(tmp_path) -> None:
    repo = tmp_path / "demo"
    repo.mkdir()
    _init_git(repo)
    export = repo / "private-export"
    export.mkdir()
    (export / "conversations-001.json").write_text("private conversation", encoding="utf-8")
    (export / "library_files.json").write_text("{}", encoding="utf-8")
    (export / "attachment.dat").write_text("private attachment", encoding="utf-8")
    (repo / "safe.txt").write_text("safe content", encoding="utf-8")
    db_path = tmp_path / "index.sqlite3"

    index_repo(repo, db_path)

    assert _indexed_paths(db_path) == {"safe.txt"}


def test_index_repo_detects_export_with_user_manifest(tmp_path) -> None:
    repo = tmp_path / "demo"
    repo.mkdir()
    _init_git(repo)
    export = repo / "chatgpt-export"
    export.mkdir()
    (export / "conversations.json").write_text('[{"private":"do not index"}]', encoding="utf-8")
    (export / "user.json").write_text('{"email":"private@example.com"}', encoding="utf-8")
    (repo / "safe.md").write_text("public fixture", encoding="utf-8")
    db_path = tmp_path / "index.sqlite3"

    index_repo(repo, db_path)

    assert _indexed_paths(db_path) == {"safe.md"}


def test_index_repo_rejects_private_export_as_scan_root(tmp_path) -> None:
    export = tmp_path / "chatgpt-export"
    export.mkdir()
    (export / "conversations.json").write_text('[{"private":"do not index"}]', encoding="utf-8")
    (export / "user.json").write_text('{"email":"private@example.com"}', encoding="utf-8")

    with pytest.raises(VisibilityPolicyError, match="private ChatGPT export"):
        index_repo(export, tmp_path / "index.sqlite3")


def test_ignore_rules_without_git_fail_closed(tmp_path) -> None:
    repo = tmp_path / "demo"
    repo.mkdir()
    (repo / ".gitignore").write_text("private.txt\n", encoding="utf-8")
    (repo / "private.txt").write_text("private", encoding="utf-8")

    with pytest.raises(VisibilityPolicyError, match="cannot safely evaluate"):
        index_repo(repo, tmp_path / "index.sqlite3")


def test_explicit_ignored_allowance_cannot_override_secret_denylist(tmp_path) -> None:
    repo = tmp_path / "demo"
    repo.mkdir()
    _init_git(repo)
    fixtures = repo / "fixtures"
    fixtures.mkdir()
    (repo / ".gitignore").write_text("fixtures/\n", encoding="utf-8")
    allowed = fixtures / "sample.txt"
    allowed.write_text("synthetic fixture", encoding="utf-8")
    denied = fixtures / "private.pem"
    denied.write_text("private key material", encoding="utf-8")
    policy = RepositoryVisibilityPolicy(repo, allow_ignored_patterns=("fixtures/*",))

    scan = policy.scan()

    assert allowed in scan.visible_files
    assert denied not in scan.visible_files
    assert next(item for item in scan.decisions if item.path == denied).reason == "secret_denylist"


def test_logical_path_policy_blocks_ignored_secret_and_deleted_paths(tmp_path) -> None:
    repo = tmp_path / "demo"
    repo.mkdir()
    _init_git(repo)
    (repo / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    policy = RepositoryVisibilityPolicy(repo)

    assert policy.evaluate_logical_path("src/deleted.py").visible
    assert policy.evaluate_logical_path("ignored.txt").reason == "gitignored"
    assert policy.evaluate_logical_path("config/service-credentials.json").reason == "secret_denylist"
    assert policy.evaluate_logical_path("../outside.txt").reason == "invalid_path"


def test_direct_repository_prompt_read_uses_visibility_policy(tmp_path) -> None:
    repo = tmp_path / "demo"
    repo.mkdir()
    _init_git(repo)
    (repo / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    (repo / "safe.txt").write_text("review me", encoding="utf-8")
    (repo / "ignored.txt").write_text("private context", encoding="utf-8")
    (repo / "id_ed25519").write_text("private key", encoding="utf-8")

    assert _read_visible_repo_text(repo, "safe.txt") == "review me"
    with pytest.raises(VisibilityPolicyError, match="gitignored"):
        _read_visible_repo_text(repo, "ignored.txt")
    with pytest.raises(VisibilityPolicyError, match="secret_denylist"):
        _read_visible_repo_text(repo, "id_ed25519")
