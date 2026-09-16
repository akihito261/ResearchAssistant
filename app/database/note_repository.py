from __future__ import annotations

import sqlite3

from app.database.database import connection_scope, transaction
from app.database.search_repository import SearchRepository


NOTE_KINDS = frozenset({"scratchpad", "manual", "selection", "translation"})


class NoteRepository:
    @staticmethod
    def _validate(
        kind: str,
        page_number: int | None,
    ) -> None:
        if kind not in NOTE_KINDS:
            raise ValueError(f"Unsupported note kind: {kind}")
        if page_number is not None and page_number < 1:
            raise ValueError("page_number must be at least 1")

    @staticmethod
    def _sync_search_document(
        connection: sqlite3.Connection,
        note_id: int,
    ) -> None:
        row = connection.execute(
            """
            SELECT notes.*, papers.title AS paper_title
            FROM notes
            JOIN papers ON papers.id = notes.paper_id
            WHERE notes.id = ?
            """,
            (note_id,),
        ).fetchone()
        if row is None:
            SearchRepository.delete_document_on_connection(
                connection,
                f"note:{note_id}",
            )
            return

        parts = []
        if row["source_text"]:
            parts.append(f"Source:\n{row['source_text']}")
        if row["content"]:
            parts.append(str(row["content"]))
        SearchRepository.upsert_document_on_connection(
            connection,
            source_key=f"note:{note_id}",
            paper_id=int(row["paper_id"]),
            source_type="note",
            source_id=note_id,
            page_number=row["page_number"],
            title=str(row["title"] or f"{row['paper_title']} — Note"),
            content="\n\n".join(parts),
        )

    @staticmethod
    def create(
        paper_id: int,
        content: str = "",
        *,
        title: str | None = None,
        kind: str = "manual",
        source_text: str | None = None,
        page_number: int | None = None,
        location_data: str | None = None,
        document_version: str | None = None,
    ) -> int:
        NoteRepository._validate(kind, page_number)
        if document_version is not None and document_version not in {"en", "vi"}:
            raise ValueError(f"Unsupported document version: {document_version}")
        effective_version = document_version or "en"
        with transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO notes (
                    paper_id,
                    title,
                    content,
                    kind,
                    source_text,
                    page_number,
                    location_data,
                    document_version
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    paper_id,
                    title,
                    content,
                    kind,
                    source_text,
                    page_number,
                    location_data,
                    effective_version,
                ),
            )
            note_id = int(cursor.lastrowid)
            if page_number is not None and location_data:
                connection.execute(
                    """
                    INSERT INTO annotation_anchors (
                        paper_id, note_id, document_version, selected_text,
                        page_number, location_data
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        paper_id,
                        note_id,
                        effective_version,
                        source_text or "",
                        page_number,
                        location_data,
                    ),
                )
            NoteRepository._sync_search_document(connection, note_id)
            return note_id

    @staticmethod
    def get(note_id: int) -> sqlite3.Row | None:
        with connection_scope() as connection:
            return connection.execute(
                "SELECT * FROM notes WHERE id = ?",
                (note_id,),
            ).fetchone()

    @staticmethod
    def list_for_paper(
        paper_id: int,
        document_version: str | None = None,
    ) -> list[sqlite3.Row]:
        if document_version is not None and document_version not in {"en", "vi"}:
            raise ValueError(f"Unsupported document version: {document_version}")
        with connection_scope() as connection:
            if document_version is not None:
                return list(
                    connection.execute(
                        """
                        SELECT *
                        FROM notes
                        WHERE paper_id = ?
                          AND (kind = 'scratchpad' OR document_version = ?)
                        ORDER BY updated_at DESC, id DESC
                        """,
                        (paper_id, document_version),
                    )
                )
            return list(
                connection.execute(
                    """
                    SELECT *
                    FROM notes
                    WHERE paper_id = ?
                    ORDER BY updated_at DESC, id DESC
                    """,
                    (paper_id,),
                )
            )

    @staticmethod
    def update(
        note_id: int,
        *,
        content: str,
        title: str | None = None,
    ) -> bool:
        with transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE notes
                SET title = ?, content = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (title, content, note_id),
            )
            if cursor.rowcount == 0:
                return False
            NoteRepository._sync_search_document(connection, note_id)
            return True

    @staticmethod
    def delete(note_id: int) -> bool:
        with transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM notes WHERE id = ?",
                (note_id,),
            )
            SearchRepository.delete_document_on_connection(
                connection,
                f"note:{note_id}",
            )
            return cursor.rowcount > 0

    @staticmethod
    def get_scratchpad(paper_id: int) -> sqlite3.Row | None:
        with connection_scope() as connection:
            return connection.execute(
                """
                SELECT *
                FROM notes
                WHERE paper_id = ? AND kind = 'scratchpad'
                """,
                (paper_id,),
            ).fetchone()

    @staticmethod
    def get_for_paper(paper_id: int) -> str:
        """Compatibility API for the original one-editor NotesSidebar."""
        row = NoteRepository.get_scratchpad(paper_id)
        return str(row["content"]) if row is not None else ""

    @staticmethod
    def save_for_paper(paper_id: int, content: str) -> int:
        """Upsert the one compatibility scratchpad for a paper."""
        with transaction() as connection:
            connection.execute(
                """
                INSERT INTO notes (paper_id, content, kind)
                VALUES (?, ?, 'scratchpad')
                ON CONFLICT(paper_id) WHERE kind = 'scratchpad'
                DO UPDATE SET
                    content = excluded.content,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (paper_id, content),
            )
            # Keep the version-1 scratchpad mirror current so a rollback to an
            # older app version does not lose edits. New code reads `notes`.
            connection.execute(
                """
                INSERT INTO paper_notes (paper_id, content)
                VALUES (?, ?)
                ON CONFLICT(paper_id) DO UPDATE SET
                    content = excluded.content,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (paper_id, content),
            )
            row = connection.execute(
                """
                SELECT id
                FROM notes
                WHERE paper_id = ? AND kind = 'scratchpad'
                """,
                (paper_id,),
            ).fetchone()
            if row is None:
                raise sqlite3.DatabaseError("Scratchpad upsert returned no row")
            note_id = int(row["id"])
            NoteRepository._sync_search_document(connection, note_id)
            return note_id

    @staticmethod
    def delete_for_paper(paper_id: int) -> bool:
        with transaction() as connection:
            row = connection.execute(
                """
                SELECT id
                FROM notes
                WHERE paper_id = ? AND kind = 'scratchpad'
                """,
                (paper_id,),
            ).fetchone()
            if row is None:
                return False
            note_id = int(row["id"])
            connection.execute("DELETE FROM notes WHERE id = ?", (note_id,))
            connection.execute(
                "DELETE FROM paper_notes WHERE paper_id = ?",
                (paper_id,),
            )
            SearchRepository.delete_document_on_connection(
                connection,
                f"note:{note_id}",
            )
            return True
