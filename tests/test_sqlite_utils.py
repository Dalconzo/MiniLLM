from __future__ import annotations

from contextlib import closing
import sqlite3

import pytest

from local_agent_lab.sqlite_utils import SQLitePathError, sqlite_connection


def test_sqlite_connection_commits_and_closes(tmp_path) -> None:
    path = tmp_path / "fixture.sqlite3"

    with sqlite_connection(path) as connection:
        connection.execute("CREATE TABLE records (value TEXT NOT NULL)")
        connection.execute("INSERT INTO records (value) VALUES ('saved')")

    with closing(sqlite3.connect(path)) as verification:
        assert verification.execute("SELECT value FROM records").fetchone() == ("saved",)
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connection.execute("SELECT 1")


def test_sqlite_connection_rolls_back_and_closes_on_error(tmp_path) -> None:
    path = tmp_path / "fixture.sqlite3"
    with closing(sqlite3.connect(path)) as setup:
        setup.execute("CREATE TABLE records (value TEXT NOT NULL)")

    with pytest.raises(RuntimeError, match="stop"):
        with sqlite_connection(path) as connection:
            connection.execute("INSERT INTO records (value) VALUES ('discarded')")
            raise RuntimeError("stop")

    with closing(sqlite3.connect(path)) as verification:
        assert verification.execute("SELECT COUNT(*) FROM records").fetchone() == (0,)
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connection.execute("SELECT 1")


def test_sqlite_connection_rejects_symlinked_database(tmp_path) -> None:
    outside = tmp_path / "outside.sqlite3"
    with closing(sqlite3.connect(outside)) as connection:
        connection.execute("CREATE TABLE sentinel (value TEXT NOT NULL)")
        connection.execute("INSERT INTO sentinel VALUES ('untouched')")
        connection.commit()
    link = tmp_path / "database.sqlite3"
    link.symlink_to(outside)

    with pytest.raises(SQLitePathError, match="cannot be a symlink"):
        with sqlite_connection(link):
            pass


def test_sqlite_connection_rejects_hard_linked_database(tmp_path) -> None:
    outside = tmp_path / "outside.sqlite3"
    with sqlite_connection(outside) as connection:
        connection.execute("CREATE TABLE protected (value TEXT NOT NULL)")
        connection.execute("INSERT INTO protected VALUES ('untouched')")
    inside = tmp_path / "inside.sqlite3"
    inside.hardlink_to(outside)

    with pytest.raises(SQLitePathError, match="multiple hard links"):
        with sqlite_connection(inside):
            pass

    with closing(sqlite3.connect(outside)) as connection:
        assert connection.execute("SELECT value FROM protected").fetchone() == ("untouched",)


def test_sqlite_connection_rejects_symlinked_parent(tmp_path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(outside, target_is_directory=True)

    with pytest.raises(SQLitePathError, match="parent path cannot be a symlink"):
        with sqlite_connection(linked_parent / "database.sqlite3"):
            pass

    assert not (outside / "database.sqlite3").exists()


def test_sqlite_connection_rejects_parent_traversal_and_uri_paths(tmp_path) -> None:
    with pytest.raises(SQLitePathError, match="parent traversal"):
        with sqlite_connection(tmp_path / "child" / ".." / "database.sqlite3"):
            pass
    with pytest.raises(SQLitePathError, match="URI"):
        with sqlite_connection(f"file:{tmp_path / 'database.sqlite3'}", uri=True):
            pass
