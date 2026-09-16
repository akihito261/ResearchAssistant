from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from app.database.database import connection_scope, transaction


def _decode(row: Any) -> dict[str, Any]:
    value = dict(row)
    try:
        value["structured"] = json.loads(value["structured_json"])
    except (TypeError, ValueError):
        value["structured"] = {}
    return value


def _fts_query(question: str) -> str:
    terms = list(dict.fromkeys(re.findall(r"[^\W_]{2,}", question.casefold())))
    return " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms[:16])


class ResearchProfileRepository:
    """Global per-paper retrieval profile; unrelated to the visible Summary."""

    @staticmethod
    def get(paper_id: int) -> dict[str, Any] | None:
        with connection_scope() as connection:
            row = connection.execute(
                "SELECT * FROM research_summaries WHERE paper_id = ?",
                (int(paper_id),),
            ).fetchone()
            return _decode(row) if row is not None else None

    @staticmethod
    def mark_stale_if_source_changed(paper_id: int, source_hash: str) -> bool:
        with transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE research_summaries
                SET profile_status = 'stale', generation_status = 'stale',
                    updated_at = CURRENT_TIMESTAMP
                WHERE paper_id = ? AND source_file_hash <> ?
                  AND profile_status <> 'stale'
                """,
                (int(paper_id), str(source_hash)),
            )
            if cursor.rowcount:
                connection.execute(
                    "DELETE FROM research_profile_fts WHERE paper_id = ?",
                    (int(paper_id),),
                )
            return cursor.rowcount > 0

    @staticmethod
    def claim_generation(
        paper_id: int,
        source_hash: str,
        *,
        retry_failed: bool = False,
    ) -> bool:
        """Claim absent/stale work once; failed profiles await an explicit retry."""
        with transaction() as connection:
            row = connection.execute(
                """
                SELECT source_file_hash, profile_status
                FROM research_summaries WHERE paper_id = ?
                """,
                (int(paper_id),),
            ).fetchone()
            if row is not None:
                same_source = str(row["source_file_hash"]) == str(source_hash)
                status = str(row["profile_status"])
                if same_source and (
                    status in {"ready", "pending"}
                    or (status == "failed" and not retry_failed)
                ):
                    return False
                connection.execute(
                    """
                    UPDATE research_summaries
                    SET structured_json = '{}', search_text = '',
                        source_file_hash = ?, profile_status = 'pending',
                        generation_status = 'generating', generated_at = NULL,
                        error_message = NULL, updated_at = CURRENT_TIMESTAMP
                    WHERE paper_id = ?
                    """,
                    (str(source_hash), int(paper_id)),
                )
                connection.execute(
                    "DELETE FROM research_profile_fts WHERE paper_id = ?",
                    (int(paper_id),),
                )
                return True
            connection.execute(
                """
                INSERT INTO research_summaries (
                    paper_id, structured_json, search_text, source_file_hash,
                    generation_status, profile_status
                ) VALUES (?, '{}', '', ?, 'generating', 'pending')
                """,
                (int(paper_id), str(source_hash)),
            )
            return True

    @staticmethod
    def save(
        paper_id: int,
        structured: Mapping[str, Any],
        *,
        source_hash: str,
        provider: str,
        model: str,
    ) -> None:
        normalized = {str(key): value for key, value in structured.items()}
        search_text = "\n".join(
            str(value).strip()
            if not isinstance(value, list)
            else ", ".join(str(item).strip() for item in value)
            for value in normalized.values()
            if value
        )
        with transaction() as connection:
            connection.execute(
                """
                INSERT INTO research_summaries (
                    paper_id, structured_json, search_text, source_file_hash,
                    provider, model, generation_status, profile_status, generated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'ready', 'ready', CURRENT_TIMESTAMP)
                ON CONFLICT(paper_id) DO UPDATE SET
                    structured_json = excluded.structured_json,
                    search_text = excluded.search_text,
                    source_file_hash = excluded.source_file_hash,
                    provider = excluded.provider,
                    model = excluded.model,
                    generation_status = 'ready', profile_status = 'ready',
                    generated_at = CURRENT_TIMESTAMP, error_message = NULL,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    int(paper_id),
                    json.dumps(normalized, ensure_ascii=False),
                    search_text,
                    str(source_hash),
                    provider,
                    model,
                ),
            )
            connection.execute(
                "DELETE FROM research_profile_fts WHERE paper_id = ?",
                (int(paper_id),),
            )
            connection.execute(
                "INSERT INTO research_profile_fts (paper_id, search_text) VALUES (?, ?)",
                (int(paper_id), search_text),
            )

    @staticmethod
    def fail(paper_id: int, error: str) -> None:
        with transaction() as connection:
            connection.execute(
                """
                UPDATE research_summaries
                SET profile_status = 'failed', generation_status = 'error',
                    error_message = ?, updated_at = CURRENT_TIMESTAMP
                WHERE paper_id = ?
                """,
                (str(error)[:2000], int(paper_id)),
            )
            connection.execute(
                "DELETE FROM research_profile_fts WHERE paper_id = ?",
                (int(paper_id),),
            )

    @staticmethod
    def list_ready_for_project(project_id: int) -> list[dict[str, Any]]:
        with connection_scope() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT profile.*, paper.title, paper.authors, paper.year
                    FROM research_summaries AS profile
                    JOIN papers AS paper ON paper.id = profile.paper_id
                    JOIN project_papers AS membership
                      ON membership.paper_id = profile.paper_id
                    WHERE membership.project_id = ?
                      AND profile.profile_status = 'ready'
                      AND profile.source_file_hash = paper.file_hash
                    GROUP BY profile.paper_id
                    ORDER BY profile.updated_at DESC, profile.paper_id
                    """,
                    (int(project_id),),
                )
            ]

    @staticmethod
    def shortlist_for_project(
        project_id: int,
        question: str,
        *,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        query = _fts_query(question)
        if not query:
            return ResearchProfileRepository.list_ready_for_project(project_id)[:limit]
        with connection_scope() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT profile.*, paper.title, paper.authors, paper.year,
                           bm25(research_profile_fts) AS rank
                    FROM research_profile_fts
                    JOIN research_summaries AS profile
                      ON profile.paper_id = CAST(research_profile_fts.paper_id AS INTEGER)
                    JOIN papers AS paper ON paper.id = profile.paper_id
                    JOIN project_papers AS membership
                      ON membership.paper_id = profile.paper_id
                    WHERE research_profile_fts MATCH ?
                      AND membership.project_id = ?
                      AND profile.profile_status = 'ready'
                      AND profile.source_file_hash = paper.file_hash
                    ORDER BY rank, profile.paper_id
                    LIMIT ?
                    """,
                    (query, int(project_id), max(1, int(limit))),
                )
            ]


# Compatibility for existing call sites and older focused tests.
ResearchSummaryRepository = ResearchProfileRepository
