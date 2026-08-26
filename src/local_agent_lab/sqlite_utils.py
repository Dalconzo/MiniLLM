from __future__ import annotations

from contextlib import contextmanager
import os
import sqlite3
import stat
from pathlib import Path
from typing import Any, Iterator


class SQLitePathError(ValueError):
    """Raised before SQLite can follow an unsafe database path."""


def validate_sqlite_path(database: str | Path, *, uri: bool = False) -> str | Path:
    """Validate a filesystem-backed SQLite path without following symlinks."""
    if isinstance(database, str) and database == ":memory:":
        if uri:
            raise SQLitePathError("SQLite in-memory databases must not use URI mode")
        return database
    if uri or (isinstance(database, str) and database.startswith("file:")):
        raise SQLitePathError("SQLite URI database paths are not allowed")

    raw = os.fspath(database)
    if not raw or "\x00" in raw:
        raise SQLitePathError("SQLite database path must be a non-empty filesystem path")
    candidate = Path(raw).expanduser()
    if ".." in candidate.parts:
        raise SQLitePathError("SQLite database path cannot contain parent traversal")
    absolute = candidate.absolute()

    current = Path(absolute.anchor)
    for part in absolute.parts[1:-1]:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise SQLitePathError(f"cannot inspect SQLite parent path: {current}") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise SQLitePathError(f"SQLite parent path cannot be a symlink: {current}")
        if not stat.S_ISDIR(metadata.st_mode):
            raise SQLitePathError(f"SQLite parent path is not a directory: {current}")

    try:
        metadata = absolute.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise SQLitePathError(f"cannot inspect SQLite database path: {absolute}") from exc
    else:
        if stat.S_ISLNK(metadata.st_mode):
            raise SQLitePathError(f"SQLite database path cannot be a symlink: {absolute}")
        if not stat.S_ISREG(metadata.st_mode):
            raise SQLitePathError(f"SQLite database path is not a regular file: {absolute}")
        if metadata.st_nlink > 1:
            raise SQLitePathError(f"SQLite database path cannot have multiple hard links: {absolute}")
    return absolute


@contextmanager
def sqlite_connection(
    database: str | Path,
    *args: Any,
    **kwargs: Any,
) -> Iterator[sqlite3.Connection]:
    """Provide transaction semantics and always close the SQLite handle."""

    validated_database = validate_sqlite_path(database, uri=bool(kwargs.get("uri", False)))
    connection = sqlite3.connect(validated_database, *args, **kwargs)
    try:
        with connection:
            yield connection
    finally:
        connection.close()
