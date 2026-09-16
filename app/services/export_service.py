from __future__ import annotations

import csv
import io
import os
import re
import sqlite3
import tempfile
from collections import defaultdict
from contextlib import closing
from pathlib import Path
from typing import Any


SUMMARY_FIELDS: tuple[tuple[str, str], ...] = (
    ("problem", "Problem"),
    ("contribution", "Contribution"),
    ("method", "Method"),
    ("dataset", "Dataset"),
    ("baseline", "Baseline"),
    ("results", "Results"),
    ("limitations", "Limitations"),
    ("unclear_points", "Unclear points"),
    ("ideas", "Ideas"),
)

LIBRARY_CSV_COLUMNS: tuple[str, ...] = (
    "ID",
    "Title",
    "Authors",
    "Year",
    "DOI",
    "Status",
    "Important",
    "Tags",
    "Collections",
    "File Path",
    "Total Pages",
    "Created At",
    "Updated At",
)


class ExportError(RuntimeError):
    """Base class for recoverable export failures."""


class ExportDatabaseError(ExportError):
    """The supplied SQLite database could not be queried."""


class ExportWriteError(ExportError):
    """An export could not be written atomically."""


def export_notes_markdown(
    database_path: str | Path,
    output_path: str | Path,
) -> Path:
    database, output = _prepare_paths(database_path, output_path)
    try:
        with closing(_connect_read_only(database)) as connection:
            papers = _load_papers(connection)
            markdown = _render_notes_markdown(connection, papers)
    except sqlite3.Error as error:
        raise ExportDatabaseError(
            f"Could not read notes from the database: {database}"
        ) from error
    return _atomic_write(output, markdown, encoding="utf-8")


def export_summaries_markdown(
    database_path: str | Path,
    output_path: str | Path,
) -> Path:
    database, output = _prepare_paths(database_path, output_path)
    try:
        with closing(_connect_read_only(database)) as connection:
            papers = _load_papers(connection)
            markdown = _render_summaries_markdown(connection, papers)
    except sqlite3.Error as error:
        raise ExportDatabaseError(
            f"Could not read summaries from the database: {database}"
        ) from error
    return _atomic_write(output, markdown, encoding="utf-8")


def export_library_csv(
    database_path: str | Path,
    output_path: str | Path,
) -> Path:
    database, output = _prepare_paths(database_path, output_path)
    try:
        with closing(_connect_read_only(database)) as connection:
            papers = _load_papers(connection)
            tags = _load_related_names(
                connection,
                link_table="paper_tags",
                value_table="tags",
                value_id_column="tag_id",
            )
            collections = _load_related_names(
                connection,
                link_table="paper_collections",
                value_table="collections",
                value_id_column="collection_id",
            )
    except sqlite3.Error as error:
        raise ExportDatabaseError(
            f"Could not read the paper library from the database: {database}"
        ) from error

    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(LIBRARY_CSV_COLUMNS)
    for paper in papers:
        paper_id = _integer_value(paper.get("id"))
        writer.writerow(
            (
                paper_id,
                _text_value(paper.get("title")),
                _text_value(paper.get("authors")),
                _text_value(paper.get("year")),
                _text_value(paper.get("doi")),
                _text_value(paper.get("status")) or "Unread",
                "Yes" if bool(paper.get("is_important")) else "No",
                ", ".join(tags.get(paper_id, ())),
                ", ".join(collections.get(paper_id, ())),
                _text_value(paper.get("file_path")),
                _text_value(paper.get("total_pages")),
                _text_value(paper.get("created_at")),
                _text_value(paper.get("updated_at")),
            )
        )
    return _atomic_write(output, stream.getvalue(), encoding="utf-8-sig")


def export_bibtex(
    database_path: str | Path,
    output_path: str | Path,
) -> Path:
    database, output = _prepare_paths(database_path, output_path)
    try:
        with closing(_connect_read_only(database)) as connection:
            papers = _load_papers(connection)
    except sqlite3.Error as error:
        raise ExportDatabaseError(
            f"Could not read bibliography data from the database: {database}"
        ) from error

    lines = ["% Research Assistant BibTeX export"]
    for row_number, paper in enumerate(papers, start=1):
        paper_id = _integer_value(paper.get("id")) or row_number
        citation_key = re.sub(r"[^A-Za-z0-9_:-]", "_", f"paper_{paper_id}")
        fields = [
            ("title", _text_value(paper.get("title")) or f"Paper {paper_id}"),
            ("author", _text_value(paper.get("authors"))),
            ("year", _text_value(paper.get("year"))),
            ("doi", _text_value(paper.get("doi"))),
        ]

        lines.extend(("", f"@article{{{citation_key},"))
        populated_fields = [(name, value) for name, value in fields if value]
        for index, (name, value) in enumerate(populated_fields):
            suffix = "," if index < len(populated_fields) - 1 else ""
            lines.append(f"  {name} = {{{_escape_bibtex(value)}}}{suffix}")
        lines.append("}")

    return _atomic_write(output, "\n".join(lines) + "\n", encoding="utf-8")


def _prepare_paths(
    database_path: str | Path,
    output_path: str | Path,
) -> tuple[Path, Path]:
    database_candidate = Path(database_path).expanduser()
    try:
        database = database_candidate.resolve(strict=True)
    except (FileNotFoundError, OSError) as error:
        raise ExportDatabaseError(
            f"The export database does not exist: {database_candidate}"
        ) from error
    if not database.is_file():
        raise ExportDatabaseError(
            f"The export database path is not a file: {database}"
        )

    output = Path(output_path).expanduser().resolve()
    if output == database:
        raise ExportWriteError("The export destination cannot replace the database.")
    return database, output


def _connect_read_only(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"{database.as_uri()}?mode=ro",
        uri=True,
        timeout=10.0,
    )
    connection.row_factory = sqlite3.Row
    return connection


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    return (
        connection.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = ?
            """,
            (table_name,),
        ).fetchone()
        is not None
    )


def _table_columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    return {
        str(row["name"])
        for row in connection.execute(f'PRAGMA table_info("{table_name}")')
    }


def _load_papers(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    if not _table_exists(connection, "papers"):
        raise ExportDatabaseError("The database does not contain a papers table.")
    columns = _table_columns(connection, "papers")
    if not {"id", "title"}.issubset(columns):
        raise ExportDatabaseError(
            "The papers table does not contain the required id and title columns."
        )
    return [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM papers ORDER BY title COLLATE NOCASE, id"
        ).fetchall()
    ]


def _render_notes_markdown(
    connection: sqlite3.Connection,
    papers: list[dict[str, Any]],
) -> str:
    paper_map = {_integer_value(paper.get("id")): paper for paper in papers}
    lines = ["# Research Notes", ""]

    if _table_exists(connection, "notes"):
        columns = _table_columns(connection, "notes")
        if not {"paper_id", "content"}.issubset(columns):
            raise ExportDatabaseError(
                "The notes table does not contain paper_id and content columns."
            )
        rows = [dict(row) for row in connection.execute("SELECT * FROM notes")]
        rows.sort(key=lambda row: _note_sort_key(row, paper_map))
        _append_modern_notes(lines, rows, paper_map)
    elif _table_exists(connection, "paper_notes"):
        columns = _table_columns(connection, "paper_notes")
        if not {"paper_id", "content"}.issubset(columns):
            raise ExportDatabaseError(
                "The paper_notes table does not contain paper_id and content columns."
            )
        rows = [dict(row) for row in connection.execute("SELECT * FROM paper_notes")]
        rows.sort(key=lambda row: _note_sort_key(row, paper_map))
        _append_legacy_notes(lines, rows, paper_map)

    if len(lines) == 2:
        lines.extend(("_No notes found._", ""))
    return "\n".join(lines)


def _append_modern_notes(
    lines: list[str],
    rows: list[dict[str, Any]],
    paper_map: dict[int, dict[str, Any]],
) -> None:
    current_paper_id: int | None = None
    for row in rows:
        paper_id = _integer_value(row.get("paper_id"))
        paper = paper_map.get(paper_id, {})
        if paper_id != current_paper_id:
            _append_paper_heading(lines, paper_id, paper)
            current_paper_id = paper_id

        note_title = _text_value(row.get("title"))
        kind = _text_value(row.get("kind"))
        lines.extend((f"### {_inline(note_title or kind or 'Note')}", ""))
        if kind:
            lines.extend((f"**Type:** {_inline(kind)}", ""))
        page_number = _text_value(row.get("page_number"))
        if page_number:
            lines.extend((f"**Page:** {_inline(page_number)}", ""))
        source_text = _text_value(row.get("source_text")).strip()
        if source_text:
            lines.extend((*(f"> {line}" for line in source_text.splitlines()), ""))
        content = _text_value(row.get("content"))
        lines.extend((content if content.strip() else "_No content._", ""))


def _append_legacy_notes(
    lines: list[str],
    rows: list[dict[str, Any]],
    paper_map: dict[int, dict[str, Any]],
) -> None:
    for row in rows:
        paper_id = _integer_value(row.get("paper_id"))
        paper = paper_map.get(paper_id, {})
        _append_paper_heading(lines, paper_id, paper)
        lines.extend(("### Paper note", ""))
        content = _text_value(row.get("content"))
        lines.extend((content if content.strip() else "_No content._", ""))


def _append_paper_heading(
    lines: list[str],
    paper_id: int,
    paper: dict[str, Any],
) -> None:
    title = _text_value(paper.get("title")) or f"Unknown paper #{paper_id}"
    lines.extend((f"## {_inline(title)}", ""))
    details = [
        _text_value(paper.get("authors")),
        _text_value(paper.get("year")),
    ]
    details = [detail for detail in details if detail]
    if details:
        lines.extend((f"*{' · '.join(_inline(value) for value in details)}*", ""))


def _note_sort_key(
    row: dict[str, Any],
    paper_map: dict[int, dict[str, Any]],
) -> tuple[str, int, str, int]:
    paper_id = _integer_value(row.get("paper_id"))
    paper_title = _text_value(paper_map.get(paper_id, {}).get("title")).casefold()
    created_at = _text_value(row.get("created_at"))
    return (paper_title, paper_id, created_at, _integer_value(row.get("id")))


def _render_summaries_markdown(
    connection: sqlite3.Connection,
    papers: list[dict[str, Any]],
) -> str:
    lines = ["# Paper Summaries", ""]
    if not _table_exists(connection, "paper_summaries"):
        return "\n".join((*lines, "_No summaries found._", ""))

    columns = _table_columns(connection, "paper_summaries")
    if "paper_id" not in columns:
        raise ExportDatabaseError(
            "The paper_summaries table does not contain a paper_id column."
        )
    paper_map = {_integer_value(paper.get("id")): paper for paper in papers}
    rows = [dict(row) for row in connection.execute("SELECT * FROM paper_summaries")]
    rows.sort(
        key=lambda row: (
            _text_value(
                paper_map.get(_integer_value(row.get("paper_id")), {}).get("title")
            ).casefold(),
            _integer_value(row.get("paper_id")),
        )
    )
    for row in rows:
        paper_id = _integer_value(row.get("paper_id"))
        paper = paper_map.get(paper_id, {})
        _append_paper_heading(lines, paper_id, paper)
        populated = False
        for field_name, label in SUMMARY_FIELDS:
            if field_name not in columns:
                continue
            value = _text_value(row.get(field_name))
            if not value.strip():
                continue
            populated = True
            lines.extend((f"### {label}", "", value, ""))
        if not populated:
            lines.extend(("_No summary fields completed._", ""))

    if not rows:
        lines.extend(("_No summaries found._", ""))
    return "\n".join(lines)


def _load_related_names(
    connection: sqlite3.Connection,
    *,
    link_table: str,
    value_table: str,
    value_id_column: str,
) -> dict[int, tuple[str, ...]]:
    if not (
        _table_exists(connection, link_table)
        and _table_exists(connection, value_table)
    ):
        return {}
    link_columns = _table_columns(connection, link_table)
    value_columns = _table_columns(connection, value_table)
    if not {"paper_id", value_id_column}.issubset(link_columns) or not {
        "id",
        "name",
    }.issubset(value_columns):
        return {}

    grouped: defaultdict[int, list[str]] = defaultdict(list)
    query = f"""
        SELECT links.paper_id, values_table.name
        FROM "{link_table}" AS links
        JOIN "{value_table}" AS values_table
          ON values_table.id = links."{value_id_column}"
        ORDER BY values_table.name COLLATE NOCASE
    """
    for row in connection.execute(query):
        grouped[_integer_value(row["paper_id"])].append(_text_value(row["name"]))
    return {paper_id: tuple(names) for paper_id, names in grouped.items()}


def _atomic_write(output: Path, contents: str, *, encoding: str) -> Path:
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise ExportWriteError(
            f"Could not create the export directory: {output.parent}"
        ) from error

    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{output.name}.",
            suffix=".tmp",
            dir=output.parent,
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding=encoding, newline="") as output_file:
            output_file.write(contents)
            output_file.flush()
            os.fsync(output_file.fileno())
        os.replace(temporary_path, output)
    except OSError as error:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise ExportWriteError(f"Could not write the export file: {output}") from error
    return output


def _inline(value: str) -> str:
    return " ".join(value.replace("\r", " ").replace("\n", " ").split())


def _text_value(value: Any) -> str:
    return "" if value is None else str(value)


def _integer_value(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _escape_bibtex(value: str) -> str:
    replacements = {
        "\\": r"\textbackslash{}",
        "{": r"\{",
        "}": r"\}",
    }
    flattened = " ".join(value.replace("\r", " ").replace("\n", " ").split())
    return "".join(replacements.get(character, character) for character in flattened)
