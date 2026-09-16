from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.database.migrations import (
    LATEST_SCHEMA_VERSION,
    UnsupportedDatabaseVersionError,
    run_migrations,
)
from app.runtime_paths import writable_path


LOGGER = logging.getLogger(__name__)

BASE_DIR = writable_path()
DATA_DIR = writable_path("data")
DATABASE_PATH = DATA_DIR / "research.db"


def get_connection() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH, timeout=10.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


@contextmanager
def connection_scope() -> Iterator[sqlite3.Connection]:
    connection = get_connection()
    try:
        yield connection
    finally:
        connection.close()


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    connection = get_connection()
    try:
        connection.execute("BEGIN")
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _create_base_schema(connection: sqlite3.Connection) -> None:
    statements = (
        """
        CREATE TABLE IF NOT EXISTS papers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            authors TEXT,
            year INTEGER,
            doi TEXT,
            file_path TEXT NOT NULL UNIQUE,
            file_hash TEXT UNIQUE,
            status TEXT NOT NULL DEFAULT 'Unread',
            is_important INTEGER NOT NULL DEFAULT 0,
            current_page INTEGER NOT NULL DEFAULT 1,
            total_pages INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE COLLATE NOCASE
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS paper_tags (
            paper_id INTEGER NOT NULL,
            tag_id INTEGER NOT NULL,
            PRIMARY KEY (paper_id, tag_id),
            FOREIGN KEY (paper_id)
                REFERENCES papers(id)
                ON DELETE CASCADE,
            FOREIGN KEY (tag_id)
                REFERENCES tags(id)
                ON DELETE CASCADE
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS collections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            name TEXT NOT NULL COLLATE NOCASE,
            description TEXT,
            FOREIGN KEY (project_id)
                REFERENCES projects(id)
                ON DELETE CASCADE,
            UNIQUE (project_id, name)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS paper_collections (
            paper_id INTEGER NOT NULL,
            collection_id INTEGER NOT NULL,
            PRIMARY KEY (paper_id, collection_id),
            FOREIGN KEY (paper_id)
                REFERENCES papers(id)
                ON DELETE CASCADE,
            FOREIGN KEY (collection_id)
                REFERENCES collections(id)
                ON DELETE CASCADE
        )
        """,
    )
    for statement in statements:
        connection.execute(statement)


def init_database() -> None:
    connection = get_connection()
    try:
        current_version = int(
            connection.execute("PRAGMA user_version").fetchone()[0]
        )
        if current_version > LATEST_SCHEMA_VERSION:
            raise UnsupportedDatabaseVersionError(
                "Database schema version "
                f"{current_version} is newer than supported version "
                f"{LATEST_SCHEMA_VERSION}."
            )
        collections_already_existed = connection.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = 'collections'
            """
        ).fetchone() is not None
        _create_base_schema(connection)
        connection.commit()
        run_migrations(connection)

        # Defaults belong to initial database creation only. Re-seeding them on
        # every launch would resurrect collections the user deliberately deleted.
        if not collections_already_existed:
            project = connection.execute(
                "SELECT id FROM projects ORDER BY id LIMIT 1"
            ).fetchone()
            if project is None:
                raise sqlite3.DatabaseError("Cannot seed collections without a project.")
            connection.executemany(
                """
                INSERT OR IGNORE INTO collections (project_id, name)
                VALUES (?, ?)
                """,
                (
                    (int(project["id"]), "Video Compression"),
                    (int(project["id"]), "ROI Compression"),
                    (int(project["id"]), "Neural Codec"),
                ),
            )
            connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()

    LOGGER.info("Database ready: %s", DATABASE_PATH)
