import subprocess

from local_agent_lab.cli import _collect_generation_context
from local_agent_lab.indexing.repo_indexer import index_repo
from local_agent_lab.sqlite_utils import sqlite_connection
from local_agent_lab.tools.search import fetch_file_chunks, search_index


def _indexed_content(db_path) -> str:
    with sqlite_connection(db_path) as connection:
        return "\n".join(str(row[0]) for row in connection.execute("SELECT content FROM indexed_chunks"))


def test_search_returns_relevant_hits(tmp_path) -> None:
    repo = tmp_path / "demo"
    repo.mkdir()
    (repo / "app.py").write_text("def route_task(query):\n    return query.lower()\n", encoding="utf-8")
    (repo / "notes.md").write_text("search indexing design notes\n", encoding="utf-8")
    db_path = tmp_path / "index.sqlite3"
    index_repo(repo, db_path)
    result = search_index(repo, "route_task", db_path=db_path)
    assert result["count"] == 1
    assert result["hits"][0]["relative_path"] == "app.py"


def test_fetch_file_chunks_returns_exact_file_chunks(tmp_path) -> None:
    repo = tmp_path / "demo"
    repo.mkdir()
    (repo / "app.py").write_text("def route_task(query):\n    return query.lower()\n", encoding="utf-8")
    db_path = tmp_path / "index.sqlite3"
    index_repo(repo, db_path)
    chunks = fetch_file_chunks(repo, "app.py", db_path=db_path)
    assert len(chunks) >= 1
    assert chunks[0]["relative_path"] == "app.py"
    assert "route_task" in chunks[0]["content"]


def test_existing_index_prunes_file_newly_ignored_before_search(tmp_path) -> None:
    repo = tmp_path / "demo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".gitignore").write_text("", encoding="utf-8")
    private_file = repo / "notes.txt"
    canary = "STALE_INDEX_PRIVATE_CANARY"
    private_file.write_text(canary, encoding="utf-8")
    db_path = tmp_path / "index.sqlite3"
    index_repo(repo, db_path)
    assert canary in _indexed_content(db_path)

    (repo / ".gitignore").write_text("notes.txt\n", encoding="utf-8")
    result = search_index(repo, canary, db_path=db_path)

    assert result["hits"] == []
    assert result["visibility_pruned_files"] == 1
    assert canary not in _indexed_content(db_path)


def test_prompt_context_cannot_reuse_newly_ignored_index_content(tmp_path) -> None:
    repo = tmp_path / "demo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".gitignore").write_text("", encoding="utf-8")
    canary = "PROMPT_CONTEXT_PRIVATE_CANARY"
    (repo / "private-notes.txt").write_text(canary, encoding="utf-8")
    db_path = tmp_path / "index.sqlite3"
    index_repo(repo, db_path)

    (repo / ".gitignore").write_text("private-notes.txt\n", encoding="utf-8")
    context = _collect_generation_context(repo, db_path, canary)

    assert all(canary not in str(item) for item in context)
    assert canary not in _indexed_content(db_path)


def test_existing_index_prunes_newly_denied_file_before_chunk_fetch(tmp_path) -> None:
    repo = tmp_path / "demo"
    repo.mkdir()
    source = repo / "notes.txt"
    source.write_text("indexed before rename", encoding="utf-8")
    db_path = tmp_path / "index.sqlite3"
    index_repo(repo, db_path)

    denied = repo / "secrets.txt"
    source.rename(denied)
    with sqlite_connection(db_path) as connection:
        connection.execute(
            "UPDATE indexed_files SET relative_path = ? WHERE relative_path = ?",
            ("secrets.txt", "notes.txt"),
        )
        connection.execute(
            "UPDATE indexed_chunks SET relative_path = ? WHERE relative_path = ?",
            ("secrets.txt", "notes.txt"),
        )
        connection.execute(
            "UPDATE indexed_chunks_fts SET relative_path = ? WHERE relative_path = ?",
            ("secrets.txt", "notes.txt"),
        )

    assert fetch_file_chunks(repo, "secrets.txt", db_path=db_path) == []
    assert "indexed before rename" not in _indexed_content(db_path)
