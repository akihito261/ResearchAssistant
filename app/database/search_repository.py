from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable

from app.database.database import connection_scope, transaction


SOURCE_TYPES = frozenset(
    {"metadata", "pdf", "note", "highlight", "summary"}
)
INDEX_STATUSES = frozenset({"pending", "indexing", "ready", "error"})
_SEARCH_TOKEN_PATTERN = re.compile(r"\w+", re.UNICODE)


class SearchRepository:
    @staticmethod
    def _validate_document(
        source_type: str,
        page_number: int | None,
    ) -> None:
        if source_type not in SOURCE_TYPES:
            raise ValueError(f"Unsupported search source type: {source_type}")
        if page_number is not None and page_number < 1:
            raise ValueError("page_number must be at least 1")

    @staticmethod
    def upsert_document_on_connection(
        connection: sqlite3.Connection,
        *,
        source_key: str,
        paper_id: int,
        source_type: str,
        source_id: int,
        title: str = "",
        content: str = "",
        page_number: int | None = None,
    ) -> int:
        SearchRepository._validate_document(source_type, page_number)
        connection.execute(
            """
            INSERT INTO search_documents (
                source_key,
                paper_id,
                source_type,
                source_id,
                page_number,
                title,
                content
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_key) DO UPDATE SET
                paper_id = excluded.paper_id,
                source_type = excluded.source_type,
                source_id = excluded.source_id,
                page_number = excluded.page_number,
                title = excluded.title,
                content = excluded.content,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                source_key,
                paper_id,
                source_type,
                source_id,
                page_number,
                title,
                content,
            ),
        )
        row = connection.execute(
            """
            SELECT id
            FROM search_documents
            WHERE source_key = ?
            """,
            (source_key,),
        ).fetchone()
        if row is None:
            raise sqlite3.DatabaseError("Search document upsert returned no row")
        return int(row["id"])

    @staticmethod
    def delete_document_on_connection(
        connection: sqlite3.Connection,
        source_key: str,
    ) -> bool:
        cursor = connection.execute(
            """
            DELETE FROM search_documents
            WHERE source_key = ?
            """,
            (source_key,),
        )
        return cursor.rowcount > 0

    @staticmethod
    def refresh_paper_metadata_on_connection(
        connection: sqlite3.Connection,
        paper_id: int,
    ) -> bool:
        paper = connection.execute(
            """
            SELECT id, title, authors, year, doi, status, is_important
            FROM papers
            WHERE id = ?
            """,
            (paper_id,),
        ).fetchone()
        if paper is None:
            SearchRepository.delete_document_on_connection(
                connection,
                f"metadata:{paper_id}",
            )
            return False

        tags = ", ".join(
            str(row["name"])
            for row in connection.execute(
                """
                SELECT tags.name
                FROM tags
                JOIN paper_tags ON paper_tags.tag_id = tags.id
                WHERE paper_tags.paper_id = ?
                ORDER BY tags.name COLLATE NOCASE
                """,
                (paper_id,),
            )
        )
        content = "\n".join(
            (
                f"Authors: {paper['authors'] or ''}",
                f"Year: {paper['year'] or ''}",
                f"DOI: {paper['doi'] or ''}",
                f"Tags: {tags}",
                f"Status: {paper['status'] or ''}",
                f"Important: {'yes' if paper['is_important'] else 'no'}",
            )
        )
        SearchRepository.upsert_document_on_connection(
            connection,
            source_key=f"metadata:{paper_id}",
            paper_id=paper_id,
            source_type="metadata",
            source_id=paper_id,
            title=str(paper["title"]),
            content=content,
        )
        return True

    @staticmethod
    def upsert_document(
        *,
        source_key: str,
        paper_id: int,
        source_type: str,
        source_id: int,
        title: str = "",
        content: str = "",
        page_number: int | None = None,
    ) -> int:
        with transaction() as connection:
            return SearchRepository.upsert_document_on_connection(
                connection,
                source_key=source_key,
                paper_id=paper_id,
                source_type=source_type,
                source_id=source_id,
                title=title,
                content=content,
                page_number=page_number,
            )

    @staticmethod
    def delete_document(source_key: str) -> bool:
        with transaction() as connection:
            return SearchRepository.delete_document_on_connection(
                connection,
                source_key,
            )

    @staticmethod
    def delete_for_paper(
        paper_id: int,
        source_type: str | None = None,
    ) -> int:
        if source_type is not None and source_type not in SOURCE_TYPES:
            raise ValueError(f"Unsupported search source type: {source_type}")
        with transaction() as connection:
            if source_type is None:
                cursor = connection.execute(
                    "DELETE FROM search_documents WHERE paper_id = ?",
                    (paper_id,),
                )
            else:
                cursor = connection.execute(
                    """
                    DELETE FROM search_documents
                    WHERE paper_id = ? AND source_type = ?
                    """,
                    (paper_id, source_type),
                )
            return cursor.rowcount

    @staticmethod
    def _build_match_query(query: str) -> str:
        tokens = _SEARCH_TOKEN_PATTERN.findall(query)
        return " AND ".join(f'"{token.replace(chr(34), chr(34) * 2)}"*' for token in tokens)

    @staticmethod
    def search(
        query: str,
        *,
        project_id: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[sqlite3.Row]:
        match_query = SearchRepository._build_match_query(query.strip())
        if not match_query:
            return []
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        supports_project_scope = False
        with connection_scope() as connection:
            supports_project_scope = connection.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type = 'table' AND name = 'project_papers'
                """
            ).fetchone() is not None
        effective_project_id = project_id if supports_project_scope else None
        project_join = (
            "JOIN project_papers AS membership "
            "ON membership.paper_id = documents.paper_id "
            if effective_project_id is not None
            else ""
        )
        project_where = (
            "AND membership.project_id = ?"
            if effective_project_id is not None
            else ""
        )
        parameters: list[object] = [match_query]
        if effective_project_id is not None:
            parameters.append(int(effective_project_id))
        parameters.extend((limit, offset))
        with connection_scope() as connection:
            return list(
                connection.execute(
                    f"""
                    SELECT
                        documents.id,
                        documents.source_key,
                        documents.paper_id,
                        documents.source_type,
                        documents.source_id,
                        documents.page_number,
                        documents.title,
                        snippet(search_fts, 1, '[', ']', '…', 20) AS snippet,
                        bm25(search_fts, 4.0, 1.0) AS score
                    FROM search_fts
                    JOIN search_documents AS documents
                        ON documents.id = search_fts.rowid
                    {project_join}
                    WHERE search_fts MATCH ?
                    {project_where}
                    ORDER BY score, documents.id
                    LIMIT ? OFFSET ?
                    """,
                    parameters,
                )
            )

    @staticmethod
    def rebuild() -> None:
        with transaction() as connection:
            connection.execute(
                "INSERT INTO search_fts(search_fts) VALUES ('rebuild')"
            )

    @staticmethod
    def ensure_index_state(paper_id: int) -> None:
        with transaction() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO paper_index_state (paper_id, status)
                VALUES (?, 'pending')
                """,
                (paper_id,),
            )

    @staticmethod
    def set_index_state(
        paper_id: int,
        status: str,
        *,
        indexed_file_hash: str | None = None,
        error: str | None = None,
    ) -> None:
        if status not in INDEX_STATUSES:
            raise ValueError(f"Unsupported index status: {status}")
        indexed_at_sql = "CURRENT_TIMESTAMP" if status == "ready" else "NULL"
        with transaction() as connection:
            connection.execute(
                f"""
                INSERT INTO paper_index_state (
                    paper_id,
                    indexed_file_hash,
                    status,
                    indexed_at,
                    error
                )
                VALUES (?, ?, ?, {indexed_at_sql}, ?)
                ON CONFLICT(paper_id) DO UPDATE SET
                    indexed_file_hash = excluded.indexed_file_hash,
                    status = excluded.status,
                    indexed_at = excluded.indexed_at,
                    error = excluded.error
                """,
                (paper_id, indexed_file_hash, status, error),
            )

    @staticmethod
    def get_index_state(paper_id: int) -> sqlite3.Row | None:
        with connection_scope() as connection:
            return connection.execute(
                """
                SELECT *
                FROM paper_index_state
                WHERE paper_id = ?
                """,
                (paper_id,),
            ).fetchone()

    @staticmethod
    def list_index_states(
        status: str | None = None,
        *,
        project_id: int | None = None,
    ) -> list[sqlite3.Row]:
        if status is not None and status not in INDEX_STATUSES:
            raise ValueError(f"Unsupported index status: {status}")
        with connection_scope() as connection:
            clauses: list[str] = []
            parameters: list[object] = []
            if status is not None:
                clauses.append("state.status = ?")
                parameters.append(status)
            join = ""
            has_project_membership = connection.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type = 'table' AND name = 'project_papers'
                """
            ).fetchone() is not None
            if project_id is not None and has_project_membership:
                join = (
                    "JOIN project_papers AS membership "
                    "ON membership.paper_id = state.paper_id"
                )
                clauses.append("membership.project_id = ?")
                parameters.append(int(project_id))
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            return list(
                connection.execute(
                    f"""
                    SELECT state.* FROM paper_index_state AS state
                    {join}
                    {where}
                    ORDER BY state.paper_id
                    """,
                    parameters,
                )
            )

    @staticmethod
    def replace_pdf_pages(
        paper_id: int,
        indexed_file_hash: str | None,
        pages: Iterable[tuple[int, str]],
        *,
        title: str = "",
    ) -> None:
        normalized_pages = list(pages)
        if any(page_number < 1 for page_number, _text in normalized_pages):
            raise ValueError("PDF page numbers must be at least 1")

        with transaction() as connection:
            connection.execute(
                """
                DELETE FROM search_documents
                WHERE paper_id = ? AND source_type = 'pdf'
                """,
                (paper_id,),
            )
            for page_number, text in normalized_pages:
                SearchRepository.upsert_document_on_connection(
                    connection,
                    source_key=f"pdf:{paper_id}:{page_number}",
                    paper_id=paper_id,
                    source_type="pdf",
                    source_id=paper_id,
                    title=title,
                    content=text,
                    page_number=page_number,
                )
            connection.execute(
                """
                INSERT INTO paper_index_state (
                    paper_id,
                    indexed_file_hash,
                    status,
                    indexed_at,
                    error
                )
                VALUES (?, ?, 'ready', CURRENT_TIMESTAMP, NULL)
                ON CONFLICT(paper_id) DO UPDATE SET
                    indexed_file_hash = excluded.indexed_file_hash,
                    status = 'ready',
                    indexed_at = CURRENT_TIMESTAMP,
                    error = NULL
                """,
                (paper_id, indexed_file_hash),
            )
