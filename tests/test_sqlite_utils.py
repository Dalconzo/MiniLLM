from __future__ import annotations

import sqlite3

import pytest

from local_agent_lab.sqlite_utils import sqlite_connection


def test_sqlite_connection_commits_and_closes(tmp_path) -> None:
    path = tmp_path / "fixture.sqlite3"

    with sqlite_connection(path) as connection:
        connection.execute("CREATE TABLE records (value TEXT NOT NULL)")
        connection.execute("INSERT INTO records (value) VALUES ('saved')")

    with sqlite3.connect(path) as verification:
        assert verification.execute("SELECT value FROM records").fetchone() == ("saved",)
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connection.execute("SELECT 1")


def test_sqlite_connection_rolls_back_and_closes_on_error(tmp_path) -> None:
    path = tmp_path / "fixture.sqlite3"
    with sqlite3.connect(path) as setup:
        setup.execute("CREATE TABLE records (value TEXT NOT NULL)")

    with pytest.raises(RuntimeError, match="stop"):
        with sqlite_connection(path) as connection:
            connection.execute("INSERT INTO records (value) VALUES ('discarded')")
            raise RuntimeError("stop")

    with sqlite3.connect(path) as verification:
        assert verification.execute("SELECT COUNT(*) FROM records").fetchone() == (0,)
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connection.execute("SELECT 1")
