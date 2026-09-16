from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from app.database.database import connection_scope, transaction
from app.database.search_repository import SearchRepository


class CollectionRepository:
    """Project-scoped collections over globally shared paper rows."""

    @staticmethod
    def _resolve_project_id(
        connection: sqlite3.Connection,
        project_id: int | None,
    ) -> int:
        if project_id is not None:
            value = int(project_id)
        else:
            row = connection.execute(
                """
                SELECT CAST(value AS INTEGER) AS project_id
                FROM app_settings WHERE key = 'current_project_id'
                """
            ).fetchone()
            value = int(row["project_id"]) if row is not None else 0
        if value < 1 or connection.execute(
            "SELECT 1 FROM projects WHERE id = ?", (value,)
        ).fetchone() is None:
            row = connection.execute(
                "SELECT id FROM projects ORDER BY id LIMIT 1"
            ).fetchone()
            if row is None:
                raise ValueError("No project is available for collections.")
            value = int(row["id"])
        return value

    @staticmethod
    def _require_project_paper(
        connection: sqlite3.Connection,
        project_id: int,
        paper_id: int,
    ) -> None:
        if connection.execute(
            """
            SELECT 1 FROM project_papers
            WHERE project_id = ? AND paper_id = ?
            """,
            (project_id, int(paper_id)),
        ).fetchone() is None:
            raise ValueError(
                f"Paper {paper_id} is not part of project {project_id}."
            )

    @staticmethod
    def _validate_collection_ids(
        connection: sqlite3.Connection,
        project_id: int,
        collection_ids: Iterable[int],
    ) -> list[int]:
        ids = list(dict.fromkeys(int(value) for value in collection_ids))
        if not ids:
            return []
        placeholders = ",".join("?" for _value in ids)
        valid = {
            int(row["id"])
            for row in connection.execute(
                f"""
                SELECT id FROM collections
                WHERE project_id = ? AND id IN ({placeholders})
                """,
                (project_id, *ids),
            )
        }
        if valid != set(ids):
            raise ValueError("A selected collection belongs to another project.")
        return ids

    @staticmethod
    def _touch_and_refresh(
        connection: sqlite3.Connection,
        paper_ids: Iterable[int],
    ) -> None:
        for paper_id in set(int(value) for value in paper_ids):
            connection.execute(
                """
                UPDATE papers SET updated_at = CURRENT_TIMESTAMP WHERE id = ?
                """,
                (paper_id,),
            )
            SearchRepository.refresh_paper_metadata_on_connection(
                connection, paper_id
            )

    @staticmethod
    def list_all(project_id: int | None = None) -> list[sqlite3.Row]:
        with connection_scope() as connection:
            selected_project = CollectionRepository._resolve_project_id(
                connection, project_id
            )
            return list(
                connection.execute(
                    """
                    SELECT id, project_id, name, description
                    FROM collections
                    WHERE project_id = ?
                    ORDER BY name COLLATE NOCASE
                    """,
                    (selected_project,),
                )
            )

    @staticmethod
    def get_for_paper(
        paper_id: int,
        project_id: int | None = None,
    ) -> list[sqlite3.Row]:
        with connection_scope() as connection:
            selected_project = CollectionRepository._resolve_project_id(
                connection, project_id
            )
            return list(
                connection.execute(
                    """
                    SELECT collections.id, collections.project_id,
                           collections.name, collections.description
                    FROM collections
                    JOIN paper_collections
                      ON paper_collections.collection_id = collections.id
                    WHERE paper_collections.paper_id = ?
                      AND collections.project_id = ?
                    ORDER BY collections.name COLLATE NOCASE
                    """,
                    (int(paper_id), selected_project),
                )
            )

    @staticmethod
    def create(
        name: str,
        description: str | None = None,
        *,
        project_id: int | None = None,
    ) -> int:
        normalized = name.strip()
        if not normalized:
            raise ValueError("Collection name cannot be empty")
        with transaction() as connection:
            selected_project = CollectionRepository._resolve_project_id(
                connection, project_id
            )
            cursor = connection.execute(
                """
                INSERT INTO collections (project_id, name, description)
                VALUES (?, ?, ?)
                """,
                (selected_project, normalized, description),
            )
            return int(cursor.lastrowid)

    @staticmethod
    def rename(
        collection_id: int,
        new_name: str,
        *,
        description: str | None = None,
        project_id: int | None = None,
    ) -> bool:
        normalized = new_name.strip()
        if not normalized:
            raise ValueError("Collection name cannot be empty")
        with transaction() as connection:
            selected_project = CollectionRepository._resolve_project_id(
                connection, project_id
            )
            paper_ids = [
                int(row["paper_id"])
                for row in connection.execute(
                    """
                    SELECT relation.paper_id
                    FROM paper_collections AS relation
                    JOIN collections ON collections.id = relation.collection_id
                    WHERE relation.collection_id = ?
                      AND collections.project_id = ?
                    """,
                    (int(collection_id), selected_project),
                )
            ]
            cursor = connection.execute(
                """
                UPDATE collections SET name = ?, description = ?
                WHERE id = ? AND project_id = ?
                """,
                (
                    normalized,
                    description,
                    int(collection_id),
                    selected_project,
                ),
            )
            if cursor.rowcount:
                CollectionRepository._touch_and_refresh(connection, paper_ids)
            return cursor.rowcount > 0

    @staticmethod
    def delete(
        collection_id: int,
        *,
        project_id: int | None = None,
    ) -> bool:
        with transaction() as connection:
            selected_project = CollectionRepository._resolve_project_id(
                connection, project_id
            )
            paper_ids = [
                int(row["paper_id"])
                for row in connection.execute(
                    """
                    SELECT relation.paper_id
                    FROM paper_collections AS relation
                    JOIN collections ON collections.id = relation.collection_id
                    WHERE relation.collection_id = ?
                      AND collections.project_id = ?
                    """,
                    (int(collection_id), selected_project),
                )
            ]
            cursor = connection.execute(
                "DELETE FROM collections WHERE id = ? AND project_id = ?",
                (int(collection_id), selected_project),
            )
            if cursor.rowcount:
                CollectionRepository._touch_and_refresh(connection, paper_ids)
            return cursor.rowcount > 0

    @staticmethod
    def replace_for_paper(
        paper_id: int,
        collection_ids: Iterable[int],
        *,
        project_id: int | None = None,
    ) -> None:
        with transaction() as connection:
            selected_project = CollectionRepository._resolve_project_id(
                connection, project_id
            )
            CollectionRepository._require_project_paper(
                connection, selected_project, int(paper_id)
            )
            ids = CollectionRepository._validate_collection_ids(
                connection, selected_project, collection_ids
            )
            connection.execute(
                """
                DELETE FROM paper_collections
                WHERE paper_id = ? AND collection_id IN (
                    SELECT id FROM collections WHERE project_id = ?
                )
                """,
                (int(paper_id), selected_project),
            )
            connection.executemany(
                """
                INSERT INTO paper_collections (paper_id, collection_id)
                VALUES (?, ?)
                """,
                ((int(paper_id), collection_id) for collection_id in ids),
            )
            CollectionRepository._touch_and_refresh(connection, (paper_id,))

    @staticmethod
    def add_to_paper(
        paper_id: int,
        collection_id: int,
        *,
        project_id: int | None = None,
    ) -> bool:
        with transaction() as connection:
            selected_project = CollectionRepository._resolve_project_id(
                connection, project_id
            )
            CollectionRepository._require_project_paper(
                connection, selected_project, int(paper_id)
            )
            CollectionRepository._validate_collection_ids(
                connection, selected_project, (collection_id,)
            )
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO paper_collections (paper_id, collection_id)
                VALUES (?, ?)
                """,
                (int(paper_id), int(collection_id)),
            )
            if cursor.rowcount:
                CollectionRepository._touch_and_refresh(connection, (paper_id,))
            return cursor.rowcount > 0

    @staticmethod
    def remove_from_paper(
        paper_id: int,
        collection_id: int,
        *,
        project_id: int | None = None,
    ) -> bool:
        with transaction() as connection:
            selected_project = CollectionRepository._resolve_project_id(
                connection, project_id
            )
            cursor = connection.execute(
                """
                DELETE FROM paper_collections
                WHERE paper_id = ? AND collection_id = ?
                  AND collection_id IN (
                      SELECT id FROM collections WHERE project_id = ?
                  )
                """,
                (int(paper_id), int(collection_id), selected_project),
            )
            if cursor.rowcount:
                CollectionRepository._touch_and_refresh(connection, (paper_id,))
            return cursor.rowcount > 0
