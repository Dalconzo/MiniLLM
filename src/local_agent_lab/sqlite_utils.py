from __future__ import annotations

from contextlib import contextmanager
import sqlite3
from pathlib import Path
from typing import Any, Iterator


@contextmanager
def sqlite_connection(
    database: str | Path,
    *args: Any,
    **kwargs: Any,
) -> Iterator[sqlite3.Connection]:
    """Provide transaction semantics and always close the SQLite handle."""

    connection = sqlite3.connect(database, *args, **kwargs)
    try:
        with connection:
            yield connection
    finally:
        connection.close()
