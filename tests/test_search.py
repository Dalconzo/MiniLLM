from local_agent_lab.indexing.repo_indexer import index_repo
from local_agent_lab.tools.search import fetch_file_chunks, search_index


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
