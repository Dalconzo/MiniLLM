from __future__ import annotations

from contextlib import contextmanager
import sqlite3
from pathlib import Path
from typing import Iterator

from ..sqlite_utils import sqlite_connection, validate_sqlite_path


@contextmanager
def store_connection(path: str | Path) -> Iterator[sqlite3.Connection]:
    validated_path = validate_sqlite_path(path)
    if isinstance(validated_path, str):
        raise ValueError("repository indexes must use filesystem-backed SQLite databases")
    db_path = validated_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite_connection(db_path) as connection:
        connection.row_factory = sqlite3.Row
        yield connection


def init_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS indexed_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_path TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            modified_time REAL NOT NULL,
            indexed_at TEXT NOT NULL,
            UNIQUE(repo_path, relative_path)
        );

        CREATE TABLE IF NOT EXISTS indexed_chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id INTEGER NOT NULL REFERENCES indexed_files(id) ON DELETE CASCADE,
            repo_path TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            chunk_index INTEGER NOT NULL,
            content TEXT NOT NULL,
            UNIQUE(file_id, chunk_index)
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS indexed_chunks_fts USING fts5(
            repo_path UNINDEXED,
            relative_path,
            content,
            chunk_ref UNINDEXED,
            tokenize = 'unicode61'
        );
        """
    )
    connection.commit()
