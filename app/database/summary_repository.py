from __future__ import annotations

import sqlite3

from app.database.database import connection_scope, transaction
from app.database.search_repository import SearchRepository


SUMMARY_FIELDS = (
    "problem",
    "contribution",
    "method",
    "dataset",
    "baseline",
    "results",
    "limitations",
    "unclear_points",
    "ideas",
)


class SummaryRepository:
    @staticmethod
    def _save_fields_on_connection(
        connection: sqlite3.Connection,
        paper_id: int,
        fields: dict[str, str],
    ) -> None:
        field_names = tuple(fields)
        values = tuple(str(fields[name] or "") for name in field_names)
        columns = ", ".join(("paper_id", *field_names))
        placeholders = ", ".join("?" for _ in range(len(values) + 1))
        assignments = ", ".join(
            f"{name} = excluded.{name}" for name in field_names
        )
        assignments += ", updated_at = CURRENT_TIMESTAMP"
        connection.execute(
            f"""
            INSERT INTO paper_summaries ({columns})
            VALUES ({placeholders})
            ON CONFLICT(paper_id) DO UPDATE SET {assignments}
            """,
            (paper_id, *values),
        )

    @staticmethod
    def _sync_search_document(
        connection: sqlite3.Connection,
        paper_id: int,
    ) -> None:
        row = connection.execute(
            """
            SELECT paper_summaries.*, papers.title AS paper_title
            FROM paper_summaries
            JOIN papers ON papers.id = paper_summaries.paper_id
            WHERE paper_summaries.paper_id = ?
            """,
            (paper_id,),
        ).fetchone()
        if row is None:
            SearchRepository.delete_document_on_connection(
                connection,
                f"summary:{paper_id}",
            )
            return

        labels = {
            "problem": "Problem",
            "contribution": "Contribution",
            "method": "Method",
            "dataset": "Dataset",
            "baseline": "Baseline",
            "results": "Results",
            "limitations": "Limitations",
            "unclear_points": "Unclear points",
            "ideas": "Ideas",
        }
        content = "\n\n".join(
            f"{labels[field]}:\n{row[field]}"
            for field in SUMMARY_FIELDS
            if row[field]
        )
        SearchRepository.upsert_document_on_connection(
            connection,
            source_key=f"summary:{paper_id}",
            paper_id=paper_id,
            source_type="summary",
            source_id=paper_id,
            title=f"{row['paper_title']} — Summary",
            content=content,
        )

    @staticmethod
    def get_for_paper(paper_id: int) -> sqlite3.Row | None:
        with connection_scope() as connection:
            return connection.execute(
                """
                SELECT *
                FROM paper_summaries
                WHERE paper_id = ?
                """,
                (paper_id,),
            ).fetchone()

    @staticmethod
    def get_citations_for_paper(paper_id: int) -> dict[str, list[dict[str, object]]]:
        with connection_scope() as connection:
            result = {field: [] for field in SUMMARY_FIELDS}
            for row in connection.execute(
                """
                SELECT * FROM summary_citations
                WHERE paper_id = ?
                ORDER BY field_name, id
                """,
                (int(paper_id),),
            ):
                value = dict(row)
                value["verified"] = bool(value["verified"])
                result[str(value["field_name"])].append(value)
            return result

    @staticmethod
    def get_support_statuses(paper_id: int) -> dict[str, str]:
        with connection_scope() as connection:
            return {
                str(row["field_name"]): str(row["support_status"])
                for row in connection.execute(
                    """
                    SELECT field_name, support_status
                    FROM summary_field_statuses
                    WHERE paper_id = ?
                    """,
                    (int(paper_id),),
                )
            }

    @staticmethod
    def save_for_paper(paper_id: int, **fields: str) -> None:
        if not fields:
            raise ValueError("At least one summary field is required")
        unknown = set(fields).difference(SUMMARY_FIELDS)
        if unknown:
            names = ", ".join(sorted(unknown))
            raise ValueError(f"Unsupported summary fields: {names}")

        with transaction() as connection:
            current = connection.execute(
                "SELECT * FROM paper_summaries WHERE paper_id = ?",
                (int(paper_id),),
            ).fetchone()
            changed = [
                name
                for name, value in fields.items()
                if current is None or str(current[name] or "") != str(value or "")
            ]
            SummaryRepository._save_fields_on_connection(
                connection,
                int(paper_id),
                {name: str(value or "") for name, value in fields.items()},
            )
            if changed:
                placeholders = ",".join("?" for _ in changed)
                parameters = (int(paper_id), *changed)
                connection.execute(
                    f"""
                    DELETE FROM summary_citations
                    WHERE paper_id = ? AND field_name IN ({placeholders})
                    """,
                    parameters,
                )
                connection.execute(
                    f"""
                    DELETE FROM summary_field_statuses
                    WHERE paper_id = ? AND field_name IN ({placeholders})
                    """,
                    parameters,
                )
            SummaryRepository._sync_search_document(connection, paper_id)

    @staticmethod
    def save_with_citations(
        paper_id: int,
        fields: dict[str, str],
        field_results: dict[str, dict[str, object]],
    ) -> None:
        if set(fields) != set(SUMMARY_FIELDS):
            raise ValueError("AI Summary must contain all nine fields.")
        if set(field_results) != set(SUMMARY_FIELDS):
            raise ValueError("AI Summary sources must contain all nine fields.")
        with transaction() as connection:
            SummaryRepository._save_fields_on_connection(
                connection,
                int(paper_id),
                {name: str(fields[name] or "") for name in SUMMARY_FIELDS},
            )
            connection.execute(
                "DELETE FROM summary_citations WHERE paper_id = ?",
                (int(paper_id),),
            )
            connection.execute(
                "DELETE FROM summary_field_statuses WHERE paper_id = ?",
                (int(paper_id),),
            )
            for field in SUMMARY_FIELDS:
                result = field_results[field]
                status = str(result.get("support_status") or "not_found")
                if status not in {"supported", "partially_supported", "not_found"}:
                    raise ValueError("Unsupported Summary support status.")
                connection.execute(
                    """
                    INSERT INTO summary_field_statuses (
                        paper_id, field_name, support_status
                    ) VALUES (?, ?, ?)
                    """,
                    (int(paper_id), field, status),
                )
                raw_citations = result.get("citations")
                citations = raw_citations if isinstance(raw_citations, list) else []
                for citation in citations:
                    if not isinstance(citation, dict):
                        continue
                    evidence = str(citation.get("evidence") or "").strip()
                    if not evidence:
                        continue
                    connection.execute(
                        """
                        INSERT INTO summary_citations (
                            paper_id, field_name, page_hint, resolved_page,
                            section, evidence, verified, verification_status,
                            claim_text
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            int(paper_id),
                            field,
                            citation.get("page_hint"),
                            citation.get("resolved_page"),
                            str(citation.get("section") or "").strip() or None,
                            evidence,
                            int(bool(citation.get("verified"))),
                            str(citation.get("verification_status") or "unverified"),
                            str(citation.get("claim_text") or "").strip(),
                        ),
                    )
            SummaryRepository._sync_search_document(connection, int(paper_id))

    @staticmethod
    def delete_for_paper(paper_id: int) -> bool:
        with transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM paper_summaries WHERE paper_id = ?",
                (paper_id,),
            )
            connection.execute(
                "DELETE FROM summary_citations WHERE paper_id = ?",
                (paper_id,),
            )
            connection.execute(
                "DELETE FROM summary_field_statuses WHERE paper_id = ?",
                (paper_id,),
            )
            SearchRepository.delete_document_on_connection(
                connection,
                f"summary:{paper_id}",
            )
            return cursor.rowcount > 0
