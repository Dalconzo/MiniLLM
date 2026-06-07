from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .chunker import chunk_text
from .sqlite_store import connect_store, init_schema


EXCLUDED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    "dist",
    "build",
    ".mypy_cache",
    ".pytest_cache",
}
EXCLUDED_PATH_PREFIXES = {
    "data/indexes",
    "data/logs",
}
TEXT_SUFFIXES = {
    ".c",
    ".cc",
    ".cpp",
    ".css",
    ".go",
    ".h",
    ".hpp",
    ".html",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".md",
    ".py",
    ".rb",
    ".rs",
    ".sh",
    ".sql",
    ".swift",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".yaml",
    ".yml",
}
MAX_FILE_BYTES = 256_000


@dataclass(frozen=True)
class IndexSummary:
    repo: str
    db_path: str
    indexed_files: int
    indexed_chunks: int
    skipped_files: int
    status: str = "ok"

    def to_dict(self) -> dict[str, int | str]:
        return {
            "repo": self.repo,
            "db_path": self.db_path,
            "indexed_files": self.indexed_files,
            "indexed_chunks": self.indexed_chunks,
            "skipped_files": self.skipped_files,
            "status": self.status,
        }


def default_db_path(indexes_dir: str | Path, repo_path: str | Path) -> Path:
    repo = Path(repo_path).resolve()
    slug = "__".join(part for part in repo.parts if part not in (repo.anchor, "/")) or "root"
    slug = slug.replace(":", "").replace(" ", "_")
    return Path(indexes_dir) / f"{slug}.sqlite3"


def index_repo(repo_path: str | Path, db_path: str | Path) -> IndexSummary:
    repo = Path(repo_path).resolve()
    if not repo.exists() or not repo.is_dir():
        raise FileNotFoundError(f"repository path does not exist: {repo}")

    connection = connect_store(db_path)
    init_schema(connection)
    now = datetime.now(timezone.utc).isoformat()
    indexed_files = 0
    indexed_chunks = 0
    skipped_files = 0

    with connection:
        connection.execute("DELETE FROM indexed_chunks_fts WHERE repo_path = ?", (str(repo),))
        connection.execute("DELETE FROM indexed_chunks WHERE repo_path = ?", (str(repo),))
        connection.execute("DELETE FROM indexed_files WHERE repo_path = ?", (str(repo),))

        for path in iter_indexable_files(repo):
            content = read_text_file(path)
            if content is None:
                skipped_files += 1
                continue

            relative_path = path.relative_to(repo).as_posix()
            stat = path.stat()
            cursor = connection.execute(
                """
                INSERT INTO indexed_files (repo_path, relative_path, size_bytes, modified_time, indexed_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (str(repo), relative_path, stat.st_size, stat.st_mtime, now),
            )
            file_id = int(cursor.lastrowid)
            chunks = [chunk.strip() for chunk in chunk_text(content) if chunk.strip()]
            for chunk_index, chunk in enumerate(chunks):
                chunk_cursor = connection.execute(
                    """
                    INSERT INTO indexed_chunks (file_id, repo_path, relative_path, chunk_index, content)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (file_id, str(repo), relative_path, chunk_index, chunk),
                )
                connection.execute(
                    """
                    INSERT INTO indexed_chunks_fts (repo_path, relative_path, content, chunk_ref)
                    VALUES (?, ?, ?, ?)
                    """,
                    (str(repo), relative_path, chunk, str(chunk_cursor.lastrowid)),
                )
            indexed_files += 1
            indexed_chunks += len(chunks)

    return IndexSummary(
        repo=str(repo),
        db_path=str(Path(db_path).resolve()),
        indexed_files=indexed_files,
        indexed_chunks=indexed_chunks,
        skipped_files=skipped_files,
    )


def iter_indexable_files(repo: Path) -> Iterable[Path]:
    for path in repo.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(repo)
        relative_posix = relative.as_posix()
        if any(part in EXCLUDED_DIRS for part in relative.parts):
            continue
        if any(relative_posix == prefix or relative_posix.startswith(f"{prefix}/") for prefix in EXCLUDED_PATH_PREFIXES):
            continue
        yield path


def read_text_file(path: Path) -> str | None:
    if path.stat().st_size > MAX_FILE_BYTES:
        return None
    if path.suffix.lower() not in TEXT_SUFFIXES and not looks_like_text(path):
        return None
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None


def looks_like_text(path: Path) -> bool:
    try:
        sample = path.read_bytes()[:1024]
    except OSError:
        return False
    return b"\x00" not in sample
