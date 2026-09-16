from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.database.paper_repository import PaperRepository
from app.services.library_path_service import (
    MANAGED_PAPERS_ROOT,
    PROJECT_ROOT,
    managed_paper_destination,
    to_stored_paper_path,
)
from app.services.pdf_service import (
    PdfInspectionResult,
    calculate_sha256,
    inspect_pdf,
)
from app.services.search_index_service import SearchIndexService


LOGGER = logging.getLogger(__name__)


class PaperImportError(RuntimeError):
    pass


class ExactDuplicateError(PaperImportError):
    def __init__(self, paper: Any) -> None:
        self.paper = paper
        title = paper["title"] if paper is not None else "Unknown paper"
        super().__init__(f'This PDF is already in the library as "{title}".')


@dataclass(frozen=True, slots=True)
class DuplicateCandidate:
    paper_id: int
    title: str
    reason: str


@dataclass(frozen=True, slots=True)
class PaperImportMetadata:
    title: str
    authors: str = ""
    year: int | None = None
    doi: str | None = None


class PaperImportService:
    def __init__(
        self,
        *,
        repository: type[PaperRepository] = PaperRepository,
        managed_root: Path = MANAGED_PAPERS_ROOT,
        project_root: Path = PROJECT_ROOT,
        index_service: SearchIndexService | None = None,
    ) -> None:
        self.repository = repository
        self.managed_root = managed_root
        self.project_root = project_root
        self.index_service = index_service or SearchIndexService()

    @staticmethod
    def inspect(source_path: str | Path) -> PdfInspectionResult:
        return inspect_pdf(source_path)

    def exact_duplicate(self, file_hash: str) -> Any | None:
        return self.repository.get_paper_by_hash(file_hash)

    def potential_duplicates(
        self,
        metadata: PaperImportMetadata,
        *,
        title_similarity_threshold: float = 0.9,
    ) -> list[DuplicateCandidate]:
        candidates: dict[int, DuplicateCandidate] = {}
        if metadata.doi:
            for paper in self.repository.find_by_normalized_doi(metadata.doi):
                paper_id = int(paper["id"])
                candidates[paper_id] = DuplicateCandidate(
                    paper_id,
                    str(paper["title"]),
                    "Same DOI",
                )

        normalized_title = " ".join(metadata.title.casefold().split())
        if normalized_title:
            for paper in self.repository.get_all_papers():
                existing_title = " ".join(str(paper["title"]).casefold().split())
                similarity = SequenceMatcher(
                    None,
                    normalized_title,
                    existing_title,
                ).ratio()
                if similarity >= title_similarity_threshold:
                    paper_id = int(paper["id"])
                    candidates.setdefault(
                        paper_id,
                        DuplicateCandidate(
                            paper_id,
                            str(paper["title"]),
                            f"Similar title ({similarity:.0%})",
                        ),
                    )
        return list(candidates.values())

    def commit(
        self,
        inspection: PdfInspectionResult,
        metadata: PaperImportMetadata,
        *,
        tags: list[str] | tuple[str, ...] = (),
        collection_ids: list[int] | tuple[int, ...] = (),
        status: str = "Unread",
        is_important: bool = False,
        project_id: int | None = None,
    ) -> int:
        duplicate = self.exact_duplicate(inspection.file_hash)
        if duplicate is not None:
            raise ExactDuplicateError(duplicate)
        if not metadata.title.strip():
            raise ValueError("Paper title cannot be empty")

        self.managed_root.mkdir(parents=True, exist_ok=True)
        destination = managed_paper_destination(
            inspection.file_hash,
            inspection.path.name,
            managed_root=self.managed_root,
        )
        staging = destination.with_name(
            f".{destination.name}.{uuid4().hex}.importing"
        )
        created_destination = False

        try:
            if destination.exists():
                if calculate_sha256(destination) != inspection.file_hash:
                    raise PaperImportError(
                        f"A different file already occupies {destination.name}."
                    )
            else:
                shutil.copy2(inspection.path, staging)
                if calculate_sha256(staging) != inspection.file_hash:
                    raise PaperImportError("The copied PDF failed hash verification.")
                os.replace(staging, destination)
                created_destination = True

            stored_path = to_stored_paper_path(
                destination,
                project_root=self.project_root,
            )
            paper_id = self.repository.add_paper_with_relationships(
                title=metadata.title,
                authors=metadata.authors or None,
                year=metadata.year,
                doi=metadata.doi,
                file_path=stored_path,
                file_hash=inspection.file_hash,
                total_pages=inspection.total_pages,
                status=status,
                is_important=is_important,
                tags=tags,
                collection_ids=collection_ids,
                project_id=project_id,
            )
        except Exception:
            if created_destination:
                try:
                    destination.unlink(missing_ok=True)
                except OSError:
                    LOGGER.exception(
                        "Could not remove rolled-back import file: %s",
                        destination,
                    )
            raise
        finally:
            try:
                staging.unlink(missing_ok=True)
            except OSError:
                LOGGER.warning("Could not remove import staging file: %s", staging)

        try:
            self.index_service.index_inspection(paper_id, inspection)
        except Exception:
            LOGGER.exception("Paper %s was imported but could not be indexed", paper_id)
        return paper_id
