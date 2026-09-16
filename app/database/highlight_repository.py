from __future__ import annotations

import sqlite3

from app.database.database import connection_scope, transaction
from app.database.search_repository import SearchRepository


HIGHLIGHT_COLORS = frozenset(
    {"yellow", "blue", "green", "red", "orange", "purple"}
)


class HighlightRepository:
    @staticmethod
    def _validate_color(color: str) -> None:
        if color not in HIGHLIGHT_COLORS:
            raise ValueError(f"Unsupported highlight color: {color}")

    @staticmethod
    def _sync_search_document(
        connection: sqlite3.Connection,
        highlight_id: int,
    ) -> None:
        row = connection.execute(
            """
            SELECT highlights.*, papers.title AS paper_title
            FROM highlights
            JOIN papers ON papers.id = highlights.paper_id
            WHERE highlights.id = ?
            """,
            (highlight_id,),
        ).fetchone()
        if row is None:
            SearchRepository.delete_document_on_connection(
                connection,
                f"highlight:{highlight_id}",
            )
            return
        SearchRepository.upsert_document_on_connection(
            connection,
            source_key=f"highlight:{highlight_id}",
            paper_id=int(row["paper_id"]),
            source_type="highlight",
            source_id=highlight_id,
            page_number=int(row["page_number"]),
            title=f"{row['paper_title']} — Highlight",
            content=str(row["selected_text"]),
        )

    @staticmethod
    def create(
        paper_id: int,
        selected_text: str,
        page_number: int,
        location_data: str,
        color: str = "yellow",
        *,
        note_id: int | None = None,
        anchor_version: int = 1,
        document_version: str | None = None,
    ) -> int:
        HighlightRepository._validate_color(color)
        if not selected_text:
            raise ValueError("selected_text cannot be empty")
        if page_number < 1:
            raise ValueError("page_number must be at least 1")
        if anchor_version < 1:
            raise ValueError("anchor_version must be at least 1")
        if document_version is not None and document_version not in {"en", "vi"}:
            raise ValueError(f"Unsupported document version: {document_version}")
        effective_version = document_version or "en"

        with transaction() as connection:
            if note_id is not None:
                note = connection.execute(
                    "SELECT paper_id, kind, document_version FROM notes WHERE id = ?",
                    (note_id,),
                ).fetchone()
                if note is None or int(note["paper_id"]) != paper_id:
                    raise ValueError("A highlight note must belong to the same paper")
                if (
                    str(note["kind"]) != "scratchpad"
                    and str(note["document_version"]) != effective_version
                ):
                    raise ValueError(
                        "A highlight note must belong to the same document version"
                    )
            cursor = connection.execute(
                """
                INSERT INTO highlights (
                    paper_id,
                    selected_text,
                    page_number,
                    location_data,
                    anchor_version,
                    color,
                    note_id,
                    document_version
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    paper_id,
                    selected_text,
                    page_number,
                    location_data,
                    anchor_version,
                    color,
                    note_id,
                    effective_version,
                ),
            )
            highlight_id = int(cursor.lastrowid)
            connection.execute(
                """
                INSERT INTO annotation_anchors (
                    paper_id, highlight_id, document_version, selected_text,
                    page_number, location_data
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    paper_id,
                    highlight_id,
                    effective_version,
                    selected_text,
                    page_number,
                    location_data,
                ),
            )
            HighlightRepository._sync_search_document(
                connection,
                highlight_id,
            )
            return highlight_id

    @staticmethod
    def get(highlight_id: int) -> sqlite3.Row | None:
        with connection_scope() as connection:
            return connection.execute(
                "SELECT * FROM highlights WHERE id = ?",
                (highlight_id,),
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
                        FROM highlights
                        WHERE paper_id = ? AND document_version = ?
                        ORDER BY page_number, id
                        """,
                        (paper_id, document_version),
                    )
                )
            return list(
                connection.execute(
                    """
                    SELECT *
                    FROM highlights
                    WHERE paper_id = ?
                    ORDER BY page_number, id
                    """,
                    (paper_id,),
                )
            )

    @staticmethod
    def update_color(highlight_id: int, color: str) -> bool:
        HighlightRepository._validate_color(color)
        with transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE highlights
                SET color = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (color, highlight_id),
            )
            return cursor.rowcount > 0

    @staticmethod
    def attach_note(
        highlight_id: int,
        note_id: int | None,
    ) -> bool:
        with transaction() as connection:
            highlight = connection.execute(
                "SELECT paper_id, document_version FROM highlights WHERE id = ?",
                (highlight_id,),
            ).fetchone()
            if highlight is None:
                return False
            if note_id is not None:
                note = connection.execute(
                    "SELECT paper_id, kind, document_version FROM notes WHERE id = ?",
                    (note_id,),
                ).fetchone()
                if note is None or note["paper_id"] != highlight["paper_id"]:
                    raise ValueError("A highlight note must belong to the same paper")
                if (
                    str(note["kind"]) != "scratchpad"
                    and str(note["document_version"])
                    != str(highlight["document_version"])
                ):
                    raise ValueError(
                        "A highlight note must belong to the same document version"
                    )
            connection.execute(
                """
                UPDATE highlights
                SET note_id = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (note_id, highlight_id),
            )
            return True

    @staticmethod
    def update_anchor(
        highlight_id: int,
        *,
        page_number: int,
        location_data: str,
        selected_text: str | None = None,
        anchor_version: int = 1,
    ) -> bool:
        if page_number < 1 or anchor_version < 1:
            raise ValueError("Page and anchor version must be at least 1")
        with transaction() as connection:
            if selected_text is None:
                cursor = connection.execute(
                    """
                    UPDATE highlights
                    SET page_number = ?,
                        location_data = ?,
                        anchor_version = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (page_number, location_data, anchor_version, highlight_id),
                )
            else:
                if not selected_text:
                    raise ValueError("selected_text cannot be empty")
                cursor = connection.execute(
                    """
                    UPDATE highlights
                    SET selected_text = ?,
                        page_number = ?,
                        location_data = ?,
                        anchor_version = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        selected_text,
                        page_number,
                        location_data,
                        anchor_version,
                        highlight_id,
                    ),
                )
            if cursor.rowcount == 0:
                return False
            HighlightRepository._sync_search_document(
                connection,
                highlight_id,
            )
            return True

    @staticmethod
    def delete(highlight_id: int) -> bool:
        with transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM highlights WHERE id = ?",
                (highlight_id,),
            )
            SearchRepository.delete_document_on_connection(
                connection,
                f"highlight:{highlight_id}",
            )
            return cursor.rowcount > 0
