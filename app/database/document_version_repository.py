from __future__ import annotations

import sqlite3

from app.database.database import connection_scope, transaction


DOCUMENT_VERSIONS = frozenset({"en", "vi"})


def _validate_version(version_code: str) -> str:
    value = str(version_code).strip().lower()
    if value not in DOCUMENT_VERSIONS:
        raise ValueError(f"Unsupported document version: {version_code}")
    return value


class DocumentVersionRepository:
    @staticmethod
    def ensure_original(paper_id: int) -> None:
        with transaction() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO paper_document_versions (
                    paper_id, version_code, language_code, file_path,
                    file_hash, source_file_hash, provider
                )
                SELECT id, 'en', 'en', file_path, file_hash, file_hash, 'original'
                FROM papers WHERE id = ?
                """,
                (int(paper_id),),
            )

    @staticmethod
    def get(paper_id: int, version_code: str) -> sqlite3.Row | None:
        version = _validate_version(version_code)
        with connection_scope() as connection:
            return connection.execute(
                """
                SELECT * FROM paper_document_versions
                WHERE paper_id = ? AND version_code = ?
                """,
                (int(paper_id), version),
            ).fetchone()

    @staticmethod
    def upsert(
        paper_id: int,
        version_code: str,
        *,
        language_code: str,
        file_path: str,
        file_hash: str,
        source_file_hash: str | None,
        provider: str,
    ) -> None:
        version = _validate_version(version_code)
        if not file_path.strip() or not file_hash.strip():
            raise ValueError("A cached document requires a path and hash.")
        with transaction() as connection:
            paper = connection.execute(
                "SELECT 1 FROM papers WHERE id = ?", (int(paper_id),)
            ).fetchone()
            if paper is None:
                raise ValueError("The paper no longer exists.")
            connection.execute(
                """
                INSERT INTO paper_document_versions (
                    paper_id, version_code, language_code, file_path,
                    file_hash, source_file_hash, provider
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(paper_id, version_code) DO UPDATE SET
                    language_code = excluded.language_code,
                    file_path = excluded.file_path,
                    file_hash = excluded.file_hash,
                    source_file_hash = excluded.source_file_hash,
                    provider = excluded.provider,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    int(paper_id), version, language_code.strip().lower(),
                    file_path, file_hash, source_file_hash, provider,
                ),
            )

    @staticmethod
    def replace_vietnamese(
        paper_id: int,
        *,
        file_path: str,
        file_hash: str,
        source_file_hash: str | None,
    ) -> dict[str, object] | None:
        """Replace only the VI document and invalidate only VI anchors."""
        if not file_path.strip() or not file_hash.strip():
            raise ValueError("A Vietnamese document requires a path and hash.")
        with transaction() as connection:
            paper = connection.execute(
                "SELECT 1 FROM papers WHERE id = ?", (int(paper_id),)
            ).fetchone()
            if paper is None:
                raise ValueError("The paper no longer exists.")
            previous_row = connection.execute(
                """
                SELECT * FROM paper_document_versions
                WHERE paper_id = ? AND version_code = 'vi'
                """,
                (int(paper_id),),
            ).fetchone()
            previous = dict(previous_row) if previous_row is not None else None
            connection.execute(
                "DELETE FROM annotation_anchors WHERE paper_id = ? AND document_version = 'vi'",
                (int(paper_id),),
            )
            connection.execute(
                """
                INSERT INTO paper_document_versions (
                    paper_id, version_code, language_code, file_path,
                    file_hash, source_file_hash, provider
                ) VALUES (?, 'vi', 'vi', ?, ?, ?, 'manual-import')
                ON CONFLICT(paper_id, version_code) DO UPDATE SET
                    language_code = 'vi',
                    file_path = excluded.file_path,
                    file_hash = excluded.file_hash,
                    source_file_hash = excluded.source_file_hash,
                    provider = 'manual-import',
                    updated_at = CURRENT_TIMESTAMP
                """,
                (int(paper_id), file_path, file_hash, source_file_hash),
            )
            return previous

    @staticmethod
    def remove_vietnamese(paper_id: int) -> dict[str, object] | None:
        """Remove the VI file record/anchors while preserving logical data."""
        with transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM paper_document_versions
                WHERE paper_id = ? AND version_code = 'vi'
                """,
                (int(paper_id),),
            ).fetchone()
            if row is None:
                return None
            previous = dict(row)
            connection.execute(
                "DELETE FROM annotation_anchors WHERE paper_id = ? AND document_version = 'vi'",
                (int(paper_id),),
            )
            connection.execute(
                """
                DELETE FROM paper_document_versions
                WHERE paper_id = ? AND version_code = 'vi'
                """,
                (int(paper_id),),
            )
            return previous
