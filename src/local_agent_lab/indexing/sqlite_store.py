from __future__ import annotations

import sqlite3
from pathlib import Path


def connect_store(path: str | Path) -> sqlite3.Connection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    return connection


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
