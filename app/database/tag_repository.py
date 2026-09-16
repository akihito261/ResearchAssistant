from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from app.database.database import connection_scope, transaction
from app.database.search_repository import SearchRepository


def _normalized_names(names: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in names:
        name = str(value).strip()
        key = name.casefold()
        if name and key not in seen:
            seen.add(key)
            result.append(name)
    return result


class TagRepository:
    @staticmethod
    def _require_paper(
        connection: sqlite3.Connection,
        paper_id: int,
    ) -> None:
        if connection.execute(
            "SELECT 1 FROM papers WHERE id = ?",
            (paper_id,),
        ).fetchone() is None:
            raise ValueError(f"Unknown paper id: {paper_id}")

    @staticmethod
    def _touch_and_refresh(
        connection: sqlite3.Connection,
        paper_ids: Iterable[int],
    ) -> None:
        for paper_id in set(paper_ids):
            connection.execute(
                """
                UPDATE papers
                SET updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (paper_id,),
            )
            SearchRepository.refresh_paper_metadata_on_connection(
                connection,
                paper_id,
            )

    @staticmethod
    def list_all() -> list[sqlite3.Row]:
        with connection_scope() as connection:
            return list(
                connection.execute(
                    "SELECT id, name FROM tags ORDER BY name COLLATE NOCASE"
                )
            )

    @staticmethod
    def get_for_paper(paper_id: int) -> list[sqlite3.Row]:
        with connection_scope() as connection:
            return list(
                connection.execute(
                    """
                    SELECT tags.id, tags.name
                    FROM tags
                    JOIN paper_tags ON paper_tags.tag_id = tags.id
                    WHERE paper_tags.paper_id = ?
                    ORDER BY tags.name COLLATE NOCASE
                    """,
                    (paper_id,),
                )
            )

    @staticmethod
    def create(name: str) -> int:
        normalized = name.strip()
        if not normalized:
            raise ValueError("Tag name cannot be empty")
        with transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO tags (name) VALUES (?)",
                (normalized,),
            )
            row = connection.execute(
                "SELECT id FROM tags WHERE name = ? COLLATE NOCASE",
                (normalized,),
            ).fetchone()
            if row is None:
                raise sqlite3.DatabaseError("Tag upsert returned no row")
            return int(row["id"])

    @staticmethod
    def replace_for_paper(paper_id: int, names: Iterable[str]) -> None:
        normalized = _normalized_names(names)
        with transaction() as connection:
            TagRepository._require_paper(connection, paper_id)
            tag_ids: list[int] = []
            for name in normalized:
                connection.execute(
                    "INSERT OR IGNORE INTO tags (name) VALUES (?)",
                    (name,),
                )
                row = connection.execute(
                    "SELECT id FROM tags WHERE name = ? COLLATE NOCASE",
                    (name,),
                ).fetchone()
                if row is not None:
                    tag_ids.append(int(row["id"]))
            connection.execute(
                "DELETE FROM paper_tags WHERE paper_id = ?",
                (paper_id,),
            )
            connection.executemany(
                """
                INSERT INTO paper_tags (paper_id, tag_id)
                VALUES (?, ?)
                """,
                ((paper_id, tag_id) for tag_id in tag_ids),
            )
            TagRepository._touch_and_refresh(connection, (paper_id,))

    @staticmethod
    def add_to_paper(paper_id: int, name: str) -> int:
        normalized = name.strip()
        if not normalized:
            raise ValueError("Tag name cannot be empty")
        with transaction() as connection:
            TagRepository._require_paper(connection, paper_id)
            connection.execute(
                "INSERT OR IGNORE INTO tags (name) VALUES (?)",
                (normalized,),
            )
            row = connection.execute(
                "SELECT id FROM tags WHERE name = ? COLLATE NOCASE",
                (normalized,),
            ).fetchone()
            if row is None:
                raise sqlite3.DatabaseError("Tag upsert returned no row")
            tag_id = int(row["id"])
            connection.execute(
                """
                INSERT OR IGNORE INTO paper_tags (paper_id, tag_id)
                VALUES (?, ?)
                """,
                (paper_id, tag_id),
            )
            TagRepository._touch_and_refresh(connection, (paper_id,))
            return tag_id

    @staticmethod
    def remove_from_paper(paper_id: int, tag_id: int) -> bool:
        with transaction() as connection:
            cursor = connection.execute(
                """
                DELETE FROM paper_tags
                WHERE paper_id = ? AND tag_id = ?
                """,
                (paper_id, tag_id),
            )
            if cursor.rowcount:
                TagRepository._touch_and_refresh(connection, (paper_id,))
            return cursor.rowcount > 0

    @staticmethod
    def rename(tag_id: int, new_name: str) -> int | None:
        normalized = new_name.strip()
        if not normalized:
            raise ValueError("Tag name cannot be empty")
        with transaction() as connection:
            source = connection.execute(
                "SELECT id FROM tags WHERE id = ?",
                (tag_id,),
            ).fetchone()
            if source is None:
                return None
            paper_ids = [
                int(row["paper_id"])
                for row in connection.execute(
                    "SELECT paper_id FROM paper_tags WHERE tag_id = ?",
                    (tag_id,),
                )
            ]
            target = connection.execute(
                """
                SELECT id
                FROM tags
                WHERE name = ? COLLATE NOCASE AND id <> ?
                """,
                (normalized, tag_id),
            ).fetchone()
            if target is None:
                connection.execute(
                    "UPDATE tags SET name = ? WHERE id = ?",
                    (normalized, tag_id),
                )
                result_id = tag_id
            else:
                result_id = int(target["id"])
                connection.execute(
                    """
                    INSERT OR IGNORE INTO paper_tags (paper_id, tag_id)
                    SELECT paper_id, ?
                    FROM paper_tags
                    WHERE tag_id = ?
                    """,
                    (result_id, tag_id),
                )
                connection.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
            TagRepository._touch_and_refresh(connection, paper_ids)
            return result_id

    @staticmethod
    def delete(tag_id: int) -> bool:
        with transaction() as connection:
            paper_ids = [
                int(row["paper_id"])
                for row in connection.execute(
                    "SELECT paper_id FROM paper_tags WHERE tag_id = ?",
                    (tag_id,),
                )
            ]
            cursor = connection.execute(
                "DELETE FROM tags WHERE id = ?",
                (tag_id,),
            )
            if cursor.rowcount:
                TagRepository._touch_and_refresh(connection, paper_ids)
            return cursor.rowcount > 0
