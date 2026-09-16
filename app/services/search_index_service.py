from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from app.database.database import transaction
from app.database.paper_repository import PaperRepository
from app.database.search_repository import SearchRepository
from app.services.library_path_service import resolve_paper_path
from app.services.pdf_service import PdfInspectionResult, PdfServiceError, inspect_pdf


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class IndexingOutcome:
    paper_id: int
    status: str
    pages_indexed: int = 0
    error: str = ""


class SearchIndexService:
    """Incrementally index PDF pages while keeping structured sources current."""

    def index_inspection(
        self,
        paper_id: int,
        inspection: PdfInspectionResult,
    ) -> IndexingOutcome:
        paper = PaperRepository.get_paper_by_id(paper_id)
        if paper is None:
            raise ValueError(f"Paper {paper_id} does not exist")

        SearchRepository.replace_pdf_pages(
            paper_id,
            inspection.file_hash,
            ((page.page_number, page.text) for page in inspection.pages),
            title=str(paper["title"] or inspection.title),
        )
        self.refresh_metadata(paper_id)
        return IndexingOutcome(
            paper_id=paper_id,
            status="ready",
            pages_indexed=len(inspection.pages),
        )

    def index_paper(
        self,
        paper_id: int,
        *,
        force: bool = False,
    ) -> IndexingOutcome:
        paper = PaperRepository.get_paper_by_id(paper_id)
        if paper is None:
            return IndexingOutcome(paper_id, "error", error="Paper not found")

        state = SearchRepository.get_index_state(paper_id)
        expected_hash = str(paper["file_hash"] or "")
        if (
            not force
            and state is not None
            and state["status"] == "ready"
            and str(state["indexed_file_hash"] or "") == expected_hash
        ):
            self.refresh_metadata(paper_id)
            return IndexingOutcome(paper_id, "ready")

        SearchRepository.set_index_state(
            paper_id,
            "indexing",
            indexed_file_hash=expected_hash or None,
        )
        try:
            inspection = inspect_pdf(resolve_paper_path(paper["file_path"]))
            return self.index_inspection(paper_id, inspection)
        except (PdfServiceError, sqlite3.Error, OSError, ValueError) as error:
            message = str(error)
            LOGGER.warning("Could not index paper %s: %s", paper_id, message)
            try:
                SearchRepository.set_index_state(
                    paper_id,
                    "error",
                    indexed_file_hash=expected_hash or None,
                    error=message,
                )
            except sqlite3.Error:
                LOGGER.exception("Could not persist index error for paper %s", paper_id)
            return IndexingOutcome(paper_id, "error", error=message)

    def index_missing_papers(
        self,
        *,
        retry_errors: bool = False,
        progress: Callable[[int, int, IndexingOutcome], None] | None = None,
    ) -> list[IndexingOutcome]:
        papers = list(PaperRepository.get_all_papers())
        candidates = []
        for paper in papers:
            state = SearchRepository.get_index_state(int(paper["id"]))
            if state is None or state["status"] in {"pending", "indexing"}:
                candidates.append(paper)
            elif retry_errors and state["status"] == "error":
                candidates.append(paper)
            elif (
                state["status"] == "ready"
                and str(state["indexed_file_hash"] or "")
                != str(paper["file_hash"] or "")
            ):
                candidates.append(paper)

        outcomes: list[IndexingOutcome] = []
        total = len(candidates)
        for index, paper in enumerate(candidates, start=1):
            outcome = self.index_paper(int(paper["id"]))
            outcomes.append(outcome)
            if progress is not None:
                progress(index, total, outcome)
        return outcomes

    @staticmethod
    def refresh_metadata(paper_id: int) -> None:
        with transaction() as connection:
            SearchRepository.refresh_paper_metadata_on_connection(
                connection,
                paper_id,
            )

    def ensure_structured_documents(self) -> None:
        for paper in PaperRepository.get_all_papers():
            self.refresh_metadata(int(paper["id"]))
