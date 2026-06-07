from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..indexing.repo_indexer import default_db_path
from ..indexing.sqlite_store import connect_store, init_schema


@dataclass(frozen=True)
class SearchHit:
    relative_path: str
    chunk_index: int
    score: float
    snippet: str

    def to_dict(self) -> dict[str, str | int | float]:
        return {
            "relative_path": self.relative_path,
            "chunk_index": self.chunk_index,
            "score": self.score,
            "snippet": self.snippet,
        }


@dataclass(frozen=True)
class FileChunk:
    relative_path: str
    chunk_index: int
    content: str

    def to_dict(self) -> dict[str, str | int]:
        return {
            "relative_path": self.relative_path,
            "chunk_index": self.chunk_index,
            "content": self.content,
        }


def search_index(
    repo: str | Path,
    query: str,
    *,
    db_path: str | Path | None = None,
    indexes_dir: str | Path | None = None,
    limit: int = 8,
) -> dict[str, object]:
    repo_path = Path(repo).resolve()
    if db_path is None:
        if indexes_dir is None:
            raise ValueError("either db_path or indexes_dir must be provided")
        db_path = default_db_path(indexes_dir, repo_path)
    connection = connect_store(db_path)
    init_schema(connection)
    rows = connection.execute(
        """
        SELECT
            indexed_chunks.relative_path,
            indexed_chunks.chunk_index,
            bm25(indexed_chunks_fts) AS score,
            snippet(indexed_chunks_fts, 2, '[', ']', '...', 18) AS snippet
        FROM indexed_chunks_fts
        JOIN indexed_chunks ON indexed_chunks.id = CAST(indexed_chunks_fts.chunk_ref AS INTEGER)
        WHERE indexed_chunks_fts.repo_path = ?
          AND indexed_chunks_fts MATCH ?
        ORDER BY score
        LIMIT ?
        """,
        (str(repo_path), query, limit),
    ).fetchall()
    if not rows:
        like_query = f"%{query.lower()}%"
        rows = connection.execute(
            """
            SELECT
                relative_path,
                chunk_index,
                0.0 AS score,
                substr(content, 1, 240) AS snippet
            FROM indexed_chunks
            WHERE repo_path = ?
              AND (lower(relative_path) LIKE ? OR lower(content) LIKE ?)
            ORDER BY relative_path, chunk_index
            LIMIT ?
            """,
            (str(repo_path), like_query, like_query, limit),
        ).fetchall()
    hits = [
        SearchHit(
            relative_path=row["relative_path"],
            chunk_index=row["chunk_index"],
            score=float(row["score"]),
            snippet=row["snippet"],
        ).to_dict()
        for row in rows
    ]
    return {
        "repo": str(repo_path),
        "db_path": str(Path(db_path).resolve()),
        "query": query,
        "hits": hits,
        "count": len(hits),
        "status": "ok",
    }


def fetch_file_chunks(
    repo: str | Path,
    relative_path: str,
    *,
    db_path: str | Path | None = None,
    indexes_dir: str | Path | None = None,
    limit: int = 8,
) -> list[dict[str, str | int]]:
    repo_path = Path(repo).resolve()
    if db_path is None:
        if indexes_dir is None:
            raise ValueError("either db_path or indexes_dir must be provided")
        db_path = default_db_path(indexes_dir, repo_path)
    connection = connect_store(db_path)
    init_schema(connection)
    rows = connection.execute(
        """
        SELECT relative_path, chunk_index, content
        FROM indexed_chunks
        WHERE repo_path = ?
          AND relative_path = ?
        ORDER BY chunk_index
        LIMIT ?
        """,
        (str(repo_path), relative_path, limit),
    ).fetchall()
    return [
        FileChunk(
            relative_path=row["relative_path"],
            chunk_index=row["chunk_index"],
            content=row["content"],
        ).to_dict()
        for row in rows
    ]
