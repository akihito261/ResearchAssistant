from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from app.database.database import connection_scope, transaction
from app.database.search_repository import SearchRepository


PAPER_STATUSES = frozenset({"Unread", "Reading", "Completed"})
PAPER_SORTS = {
    "title": "papers.title COLLATE NOCASE",
    "year": "papers.year",
    "date_added": "papers.created_at",
    "updated": "papers.updated_at",
    "author": "papers.authors COLLATE NOCASE",
}


def _clean_tag_names(tags: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in tags:
        name = str(value).strip()
        key = name.casefold()
        if name and key not in seen:
            seen.add(key)
            result.append(name)
    return result


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class PaperRepository:
    @staticmethod
    def _validate_status(status: str) -> None:
        if status not in PAPER_STATUSES:
            raise ValueError(f"Unsupported paper status: {status}")

    @staticmethod
    def _project_id_on_connection(
        connection: sqlite3.Connection,
        project_id: int | None,
    ) -> int:
        if project_id is not None:
            selected = int(project_id)
        else:
            row = connection.execute(
                """
                SELECT CAST(value AS INTEGER) AS project_id
                FROM app_settings WHERE key = 'current_project_id'
                """
            ).fetchone()
            selected = int(row["project_id"]) if row is not None else 0
        if selected < 1 or connection.execute(
            "SELECT 1 FROM projects WHERE id = ?", (selected,)
        ).fetchone() is None:
            row = connection.execute(
                "SELECT id FROM projects ORDER BY id LIMIT 1"
            ).fetchone()
            if row is None:
                raise ValueError("No project is available for this paper.")
            selected = int(row["id"])
        return selected

    @staticmethod
    def _validate_project_collections(
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
    def _insert_paper(
        connection: sqlite3.Connection,
        *,
        title: str,
        authors: str | None,
        year: int | None,
        doi: str | None,
        file_path: str,
        file_hash: str | None,
        total_pages: int,
        status: str,
        is_important: bool,
    ) -> int:
        normalized_title = title.strip()
        if not normalized_title:
            raise ValueError("Paper title cannot be empty")
        PaperRepository._validate_status(status)
        cursor = connection.execute(
            """
            INSERT INTO papers (
                title,
                authors,
                year,
                doi,
                file_path,
                file_hash,
                total_pages,
                status,
                is_important
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                normalized_title,
                authors,
                year,
                doi,
                file_path,
                file_hash,
                total_pages,
                status,
                int(bool(is_important)),
            ),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def add_paper(
        title: str,
        authors: str | None,
        year: int | None,
        doi: str | None,
        file_path: str,
        file_hash: str | None,
        total_pages: int = 0,
    ) -> int:
        return PaperRepository.add_paper_with_relationships(
            title=title,
            authors=authors,
            year=year,
            doi=doi,
            file_path=file_path,
            file_hash=file_hash,
            total_pages=total_pages,
        )

    @staticmethod
    def add_paper_with_relationships(
        *,
        title: str,
        authors: str | None,
        year: int | None,
        doi: str | None,
        file_path: str,
        file_hash: str | None,
        total_pages: int = 0,
        status: str = "Unread",
        is_important: bool = False,
        tags: Iterable[str] = (),
        collection_ids: Iterable[int] = (),
        project_id: int | None = None,
    ) -> int:
        normalized_tags = _clean_tag_names(tags)
        normalized_collections = list(
            dict.fromkeys(int(value) for value in collection_ids)
        )
        with transaction() as connection:
            selected_project = PaperRepository._project_id_on_connection(
                connection, project_id
            )
            normalized_collections = PaperRepository._validate_project_collections(
                connection, selected_project, normalized_collections
            )
            paper_id = PaperRepository._insert_paper(
                connection,
                title=title,
                authors=authors,
                year=year,
                doi=doi,
                file_path=file_path,
                file_hash=file_hash,
                total_pages=total_pages,
                status=status,
                is_important=is_important,
            )
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='project_papers'"
            ).fetchone() is not None:
                connection.execute(
                    """
                    INSERT INTO project_papers (project_id, paper_id, status)
                    VALUES (?, ?, ?)
                    """,
                    (selected_project, paper_id, status),
                )
            for tag_name in normalized_tags:
                connection.execute(
                    "INSERT OR IGNORE INTO tags (name) VALUES (?)",
                    (tag_name,),
                )
                tag = connection.execute(
                    "SELECT id FROM tags WHERE name = ? COLLATE NOCASE",
                    (tag_name,),
                ).fetchone()
                if tag is not None:
                    connection.execute(
                        """
                        INSERT INTO paper_tags (paper_id, tag_id)
                        VALUES (?, ?)
                        """,
                        (paper_id, int(tag["id"])),
                    )
            connection.executemany(
                """
                INSERT INTO paper_collections (paper_id, collection_id)
                VALUES (?, ?)
                """,
                (
                    (paper_id, collection_id)
                    for collection_id in normalized_collections
                ),
            )
            connection.execute(
                """
                INSERT INTO paper_index_state (paper_id, status)
                VALUES (?, 'pending')
                """,
                (paper_id,),
            )
            SearchRepository.refresh_paper_metadata_on_connection(
                connection,
                paper_id,
            )
            return paper_id

    @staticmethod
    def list_papers(
        *,
        project_id: int | None = None,
        search: str | None = None,
        status: str | None = None,
        important: bool | None = None,
        year: int | None = None,
        collection_id: int | None = None,
        tag_id: int | None = None,
        sort_by: str = "date_added",
        descending: bool = True,
    ) -> list[sqlite3.Row]:
        if status is not None:
            PaperRepository._validate_status(status)
        if sort_by not in PAPER_SORTS:
            raise ValueError(f"Unsupported paper sort: {sort_by}")

        clauses: list[str] = []
        parameters: list[object] = []
        if search and search.strip():
            pattern = f"%{_escape_like(search.strip())}%"
            clauses.append(
                """
                (
                    papers.title LIKE ? ESCAPE '\\' COLLATE NOCASE
                    OR COALESCE(papers.authors, '') LIKE ? ESCAPE '\\' COLLATE NOCASE
                    OR CAST(COALESCE(papers.year, '') AS TEXT) LIKE ? ESCAPE '\\'
                    OR COALESCE(papers.doi, '') LIKE ? ESCAPE '\\' COLLATE NOCASE
                    OR EXISTS (
                        SELECT 1
                        FROM tags
                        JOIN paper_tags ON paper_tags.tag_id = tags.id
                        WHERE paper_tags.paper_id = papers.id
                          AND tags.name LIKE ? ESCAPE '\\' COLLATE NOCASE
                    )
                    OR EXISTS (
                        SELECT 1
                        FROM collections
                        JOIN paper_collections
                            ON paper_collections.collection_id = collections.id
                        WHERE paper_collections.paper_id = papers.id
                          AND (? < 0 OR collections.project_id = ?)
                          AND collections.name LIKE ? ESCAPE '\\' COLLATE NOCASE
                    )
                )
                """
            )
            parameters.extend(
                (pattern, pattern, pattern, pattern, pattern,
                 int(project_id or -1), int(project_id or -1), pattern)
            )
        if project_id is not None:
            clauses.append("project_membership.project_id = ?")
            parameters.append(int(project_id))
        if status is not None:
            clauses.append(
                "project_membership.status = ?"
                if project_id is not None
                else "papers.status = ?"
            )
            parameters.append(status)
        if important is not None:
            clauses.append("papers.is_important = ?")
            parameters.append(int(bool(important)))
        if year is not None:
            clauses.append("papers.year = ?")
            parameters.append(year)
        if collection_id is not None:
            clauses.append(
                """
                EXISTS (
                    SELECT 1
                    FROM paper_collections
                    JOIN collections
                      ON collections.id = paper_collections.collection_id
                    WHERE paper_collections.paper_id = papers.id
                      AND paper_collections.collection_id = ?
                      AND (? < 0 OR collections.project_id = ?)
                )
                """
            )
            parameters.extend(
                (collection_id, int(project_id or -1), int(project_id or -1))
            )
        if tag_id is not None:
            clauses.append(
                """
                EXISTS (
                    SELECT 1 FROM paper_tags
                    WHERE paper_tags.paper_id = papers.id
                      AND paper_tags.tag_id = ?
                )
                """
            )
            parameters.append(tag_id)

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        direction = "DESC" if descending else "ASC"
        order_sql = PAPER_SORTS[sort_by]
        with connection_scope() as connection:
            return list(
                connection.execute(
                    f"""
                    SELECT
                        papers.*,
                        project_membership.status AS project_status,
                        project_membership.active_reading_seconds,
                        project_membership.interaction_count,
                        project_membership.last_opened AS project_last_opened,
                        COALESCE((
                            SELECT GROUP_CONCAT(ordered_tags.name, ', ')
                            FROM (
                                SELECT paper_tags.paper_id, tags.name
                                FROM tags
                                JOIN paper_tags ON paper_tags.tag_id = tags.id
                                ORDER BY tags.name COLLATE NOCASE
                            ) AS ordered_tags
                            WHERE ordered_tags.paper_id = papers.id
                        ), '') AS tags,
                        COALESCE((
                            SELECT GROUP_CONCAT(ordered_collections.name, ', ')
                            FROM (
                                SELECT paper_collections.paper_id, collections.name
                                FROM collections
                                JOIN paper_collections
                                    ON paper_collections.collection_id = collections.id
                                WHERE (? < 0 OR collections.project_id = ?)
                                ORDER BY collections.name COLLATE NOCASE
                            ) AS ordered_collections
                            WHERE ordered_collections.paper_id = papers.id
                        ), '') AS collections
                    FROM papers
                    LEFT JOIN project_papers AS project_membership
                      ON project_membership.paper_id = papers.id
                     AND project_membership.project_id = ?
                    {where_sql}
                    ORDER BY {order_sql} {direction}, papers.id {direction}
                    """,
                    [
                        int(project_id or -1),
                        int(project_id or -1),
                        int(project_id or -1),
                        *parameters,
                    ],
                )
            )

    @staticmethod
    def get_all_papers() -> list[sqlite3.Row]:
        return PaperRepository.list_papers()

    @staticmethod
    def get_paper_by_id(paper_id: int) -> sqlite3.Row | None:
        with connection_scope() as connection:
            return connection.execute(
                "SELECT * FROM papers WHERE id = ?",
                (paper_id,),
            ).fetchone()

    @staticmethod
    def get_paper_by_hash(file_hash: str) -> sqlite3.Row | None:
        with connection_scope() as connection:
            return connection.execute(
                "SELECT * FROM papers WHERE file_hash = ?",
                (file_hash,),
            ).fetchone()

    @staticmethod
    def find_by_normalized_doi(doi: str | None) -> list[sqlite3.Row]:
        normalized = (doi or "").strip()
        if not normalized:
            return []
        with connection_scope() as connection:
            return list(
                connection.execute(
                    """
                    SELECT *
                    FROM papers
                    WHERE lower(trim(doi)) = lower(trim(?))
                    ORDER BY id
                    """,
                    (normalized,),
                )
            )

    @staticmethod
    def update_metadata(
        paper_id: int,
        *,
        title: str,
        authors: str | None,
        year: int | None,
        doi: str | None,
    ) -> bool:
        normalized_title = title.strip()
        if not normalized_title:
            raise ValueError("Paper title cannot be empty")
        with transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE papers
                SET title = ?,
                    authors = ?,
                    year = ?,
                    doi = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (normalized_title, authors, year, doi, paper_id),
            )
            if cursor.rowcount:
                SearchRepository.refresh_paper_metadata_on_connection(
                    connection,
                    paper_id,
                )
            return cursor.rowcount > 0

    @staticmethod
    def update_details(
        paper_id: int,
        *,
        title: str,
        authors: str | None,
        year: int | None,
        doi: str | None,
        status: str,
        is_important: bool,
        tags: Iterable[str],
        collection_ids: Iterable[int],
        project_id: int | None = None,
    ) -> bool:
        """Atomically update all editable paper fields and relationships."""
        normalized_title = title.strip()
        if not normalized_title:
            raise ValueError("Paper title cannot be empty")
        PaperRepository._validate_status(status)
        normalized_tags = _clean_tag_names(tags)
        normalized_collections = list(
            dict.fromkeys(int(value) for value in collection_ids)
        )

        with transaction() as connection:
            selected_project = PaperRepository._project_id_on_connection(
                connection, project_id
            )
            normalized_collections = PaperRepository._validate_project_collections(
                connection, selected_project, normalized_collections
            )
            cursor = connection.execute(
                """
                UPDATE papers
                SET title = ?,
                    authors = ?,
                    year = ?,
                    doi = ?,
                    status = ?,
                    is_important = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    normalized_title,
                    authors,
                    year,
                    doi,
                    status,
                    int(bool(is_important)),
                    paper_id,
                ),
            )
            if cursor.rowcount == 0:
                return False

            connection.execute(
                "DELETE FROM paper_tags WHERE paper_id = ?",
                (paper_id,),
            )
            for tag_name in normalized_tags:
                connection.execute(
                    "INSERT OR IGNORE INTO tags (name) VALUES (?)",
                    (tag_name,),
                )
                tag = connection.execute(
                    "SELECT id FROM tags WHERE name = ? COLLATE NOCASE",
                    (tag_name,),
                ).fetchone()
                if tag is not None:
                    connection.execute(
                        "INSERT INTO paper_tags (paper_id, tag_id) VALUES (?, ?)",
                        (paper_id, int(tag["id"])),
                    )

            connection.execute(
                """
                DELETE FROM paper_collections
                WHERE paper_id = ? AND collection_id IN (
                    SELECT id FROM collections WHERE project_id = ?
                )
                """,
                (paper_id, selected_project),
            )
            connection.executemany(
                """
                INSERT INTO paper_collections (paper_id, collection_id)
                VALUES (?, ?)
                """,
                (
                    (paper_id, collection_id)
                    for collection_id in normalized_collections
                ),
            )
            SearchRepository.refresh_paper_metadata_on_connection(
                connection,
                paper_id,
            )
            return True

    @staticmethod
    def set_status(paper_id: int, status: str) -> bool:
        PaperRepository._validate_status(status)
        with transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE papers
                SET status = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, paper_id),
            )
            if cursor.rowcount:
                SearchRepository.refresh_paper_metadata_on_connection(
                    connection,
                    paper_id,
                )
            return cursor.rowcount > 0

    @staticmethod
    def set_important(paper_id: int, important: bool) -> bool:
        with transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE papers
                SET is_important = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (int(bool(important)), paper_id),
            )
            if cursor.rowcount:
                SearchRepository.refresh_paper_metadata_on_connection(
                    connection,
                    paper_id,
                )
            return cursor.rowcount > 0

    @staticmethod
    def replace_relationships(
        paper_id: int,
        *,
        tags: Iterable[str],
        collection_ids: Iterable[int],
        project_id: int | None = None,
    ) -> None:
        normalized_tags = _clean_tag_names(tags)
        normalized_collections = list(
            dict.fromkeys(int(value) for value in collection_ids)
        )
        with transaction() as connection:
            selected_project = PaperRepository._project_id_on_connection(
                connection, project_id
            )
            normalized_collections = PaperRepository._validate_project_collections(
                connection, selected_project, normalized_collections
            )
            if connection.execute(
                "SELECT 1 FROM papers WHERE id = ?",
                (paper_id,),
            ).fetchone() is None:
                raise ValueError(f"Unknown paper id: {paper_id}")
            connection.execute("DELETE FROM paper_tags WHERE paper_id = ?", (paper_id,))
            for tag_name in normalized_tags:
                connection.execute(
                    "INSERT OR IGNORE INTO tags (name) VALUES (?)",
                    (tag_name,),
                )
                tag = connection.execute(
                    "SELECT id FROM tags WHERE name = ? COLLATE NOCASE",
                    (tag_name,),
                ).fetchone()
                if tag is not None:
                    connection.execute(
                        "INSERT INTO paper_tags (paper_id, tag_id) VALUES (?, ?)",
                        (paper_id, int(tag["id"])),
                    )
            connection.execute(
                """
                DELETE FROM paper_collections
                WHERE paper_id = ? AND collection_id IN (
                    SELECT id FROM collections WHERE project_id = ?
                )
                """,
                (paper_id, selected_project),
            )
            connection.executemany(
                """
                INSERT INTO paper_collections (paper_id, collection_id)
                VALUES (?, ?)
                """,
                (
                    (paper_id, collection_id)
                    for collection_id in normalized_collections
                ),
            )
            connection.execute(
                "UPDATE papers SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (paper_id,),
            )
            SearchRepository.refresh_paper_metadata_on_connection(
                connection,
                paper_id,
            )

    @staticmethod
    def replace_tags(paper_id: int, tags: Iterable[str]) -> None:
        from app.database.tag_repository import TagRepository

        TagRepository.replace_for_paper(paper_id, tags)

    @staticmethod
    def replace_collections(
        paper_id: int,
        collection_ids: Iterable[int],
        *,
        project_id: int | None = None,
    ) -> None:
        from app.database.collection_repository import CollectionRepository

        CollectionRepository.replace_for_paper(
            paper_id, collection_ids, project_id=project_id
        )

    @staticmethod
    def delete_paper(paper_id: int) -> bool:
        with transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM papers WHERE id = ?",
                (paper_id,),
            )
            return cursor.rowcount > 0

    # Compatibility methods used by the current UI.
    @staticmethod
    def get_all_collections(project_id: int | None = None) -> list[sqlite3.Row]:
        from app.database.collection_repository import CollectionRepository

        return CollectionRepository.list_all(project_id)

    @staticmethod
    def add_paper_to_collection(
        paper_id: int,
        collection_id: int,
        *,
        project_id: int | None = None,
    ) -> bool:
        from app.database.collection_repository import CollectionRepository

        return CollectionRepository.add_to_paper(
            paper_id, collection_id, project_id=project_id
        )

    @staticmethod
    def add_tags_to_paper(paper_id: int, tags: Iterable[str]) -> None:
        from app.database.tag_repository import TagRepository

        for tag in _clean_tag_names(tags):
            TagRepository.add_to_paper(paper_id, tag)

    @staticmethod
    def get_tags_for_paper(paper_id: int) -> list[str]:
        from app.database.tag_repository import TagRepository

        return [str(row["name"]) for row in TagRepository.get_for_paper(paper_id)]
