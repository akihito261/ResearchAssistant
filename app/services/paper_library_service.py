from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from PySide6.QtCore import QFile

from app.database.paper_repository import PaperRepository
from app.services.library_path_service import (
    MANAGED_PAPERS_ROOT,
    is_managed_paper_path,
    resolve_paper_path,
)


@dataclass(frozen=True, slots=True)
class DeleteOutcome:
    paper_id: int
    removed_from_library: bool
    file_deleted: bool
    retained_path: str = ""
    warning: str = ""


class PaperLibraryService:
    def __init__(
        self,
        *,
        repository: type[PaperRepository] = PaperRepository,
        managed_root: Path = MANAGED_PAPERS_ROOT,
        trash_function: Callable[[str], bool] = QFile.moveToTrash,
    ) -> None:
        self.repository = repository
        self.managed_root = managed_root
        self.trash_function = trash_function

    def delete_paper(
        self,
        paper_id: int,
        *,
        delete_local_file: bool,
    ) -> DeleteOutcome:
        paper: Any = self.repository.get_paper_by_id(paper_id)
        if paper is None:
            return DeleteOutcome(paper_id, False, False, warning="Paper not found")

        paper_path = resolve_paper_path(paper["file_path"])
        if not delete_local_file:
            removed = bool(self.repository.delete_paper(paper_id))
            return DeleteOutcome(
                paper_id,
                removed,
                False,
                retained_path=str(paper_path) if paper_path.exists() else "",
            )

        if not paper_path.exists():
            removed = bool(self.repository.delete_paper(paper_id))
            return DeleteOutcome(
                paper_id,
                removed,
                False,
                warning="The PDF was already missing; only the library record was removed.",
            )

        if not is_managed_paper_path(paper_path, managed_root=self.managed_root):
            removed = bool(self.repository.delete_paper(paper_id))
            return DeleteOutcome(
                paper_id,
                removed,
                False,
                retained_path=str(paper_path),
                warning=(
                    "The PDF is outside the managed library and was not deleted."
                ),
            )

        quarantine = paper_path.with_name(
            f".{paper_path.name}.{uuid4().hex}.deleting"
        )
        os.replace(paper_path, quarantine)
        try:
            removed = bool(self.repository.delete_paper(paper_id))
        except Exception:
            os.replace(quarantine, paper_path)
            raise

        try:
            deleted = bool(self.trash_function(str(quarantine)))
        except (OSError, RuntimeError):
            deleted = False

        if deleted:
            return DeleteOutcome(paper_id, removed, True)

        os.replace(quarantine, paper_path)
        return DeleteOutcome(
            paper_id,
            removed,
            False,
            retained_path=str(paper_path),
            warning="The paper was removed from Library, but its PDF could not be trashed.",
        )
