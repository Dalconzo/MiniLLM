from local_agent_lab.indexing.repo_indexer import default_db_path, index_repo


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
