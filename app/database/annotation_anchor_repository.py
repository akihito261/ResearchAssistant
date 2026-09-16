from __future__ import annotations

import sqlite3

from app.database.database import connection_scope, transaction
from app.database.document_version_repository import _validate_version


class AnnotationAnchorRepository:
    @staticmethod
    def list_for_paper(
        paper_id: int, document_version: str
    ) -> list[sqlite3.Row]:
        version = _validate_version(document_version)
        with connection_scope() as connection:
            return list(
                connection.execute(
                    """
                    SELECT * FROM annotation_anchors
                    WHERE paper_id = ? AND document_version = ?
                    ORDER BY id
                    """,
                    (int(paper_id), version),
                )
            )

    @staticmethod
    def get_for_note(note_id: int, document_version: str) -> sqlite3.Row | None:
        version = _validate_version(document_version)
        with connection_scope() as connection:
            return connection.execute(
                """
                SELECT * FROM annotation_anchors
                WHERE note_id = ? AND document_version = ?
                """,
                (int(note_id), version),
            ).fetchone()

    @staticmethod
    def get_for_highlight(
        highlight_id: int, document_version: str
    ) -> sqlite3.Row | None:
        version = _validate_version(document_version)
        with connection_scope() as connection:
            return connection.execute(
                """
                SELECT * FROM annotation_anchors
                WHERE highlight_id = ? AND document_version = ?
                """,
                (int(highlight_id), version),
            ).fetchone()

    @staticmethod
    def delete_for_version(paper_id: int, document_version: str) -> int:
        version = _validate_version(document_version)
        with transaction() as connection:
            cursor = connection.execute(
                """
                DELETE FROM annotation_anchors
                WHERE paper_id = ? AND document_version = ?
                """,
                (int(paper_id), version),
            )
            return int(cursor.rowcount)

    @staticmethod
    def upsert_note(
        paper_id: int,
        note_id: int,
        document_version: str,
        *,
        selected_text: str,
        page_number: int,
        location_data: str,
    ) -> None:
        AnnotationAnchorRepository._upsert(
            paper_id, document_version, selected_text, page_number,
            location_data, note_id=int(note_id), highlight_id=None,
        )

    @staticmethod
    def upsert_highlight(
        paper_id: int,
        highlight_id: int,
        document_version: str,
        *,
        selected_text: str,
        page_number: int,
        location_data: str,
    ) -> None:
        AnnotationAnchorRepository._upsert(
            paper_id, document_version, selected_text, page_number,
            location_data, note_id=None, highlight_id=int(highlight_id),
        )

    @staticmethod
    def _upsert(
        paper_id: int,
        document_version: str,
        selected_text: str,
        page_number: int,
        location_data: str,
        *,
        note_id: int | None,
        highlight_id: int | None,
    ) -> None:
        version = _validate_version(document_version)
        if page_number < 1 or not location_data:
            raise ValueError("An annotation anchor requires a page and location.")
        conflict_column = "note_id" if note_id is not None else "highlight_id"
        target_id = note_id if note_id is not None else highlight_id
        with transaction() as connection:
            owner = connection.execute(
                f"SELECT paper_id, document_version "
                f"FROM {'notes' if note_id is not None else 'highlights'} WHERE id = ?",
                (target_id,),
            ).fetchone()
            if owner is None or int(owner["paper_id"]) != int(paper_id):
                raise ValueError("The annotation does not belong to this paper.")
            if str(owner["document_version"]) != version:
                raise ValueError(
                    "An annotation anchor must use its owning document version."
                )
            connection.execute(
                f"""
                INSERT INTO annotation_anchors (
                    paper_id, note_id, highlight_id, document_version,
                    selected_text, page_number, location_data
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT({conflict_column}, document_version)
                WHERE {conflict_column} IS NOT NULL DO UPDATE SET
                    selected_text = excluded.selected_text,
                    page_number = excluded.page_number,
                    location_data = excluded.location_data,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    int(paper_id), note_id, highlight_id, version,
                    selected_text, int(page_number), location_data,
                ),
            )
