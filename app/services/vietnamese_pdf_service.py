from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import QFile

from app.database.document_version_repository import DocumentVersionRepository
from app.services.library_path_service import (
    MANAGED_PAPERS_ROOT,
    resolve_paper_path,
    to_stored_paper_path,
)
from app.services.pdf_service import PdfServiceError, calculate_sha256, inspect_pdf


class VietnamesePdfError(RuntimeError):
    """A recoverable import, validation, or managed-file failure."""


@dataclass(frozen=True, slots=True)
class VietnamesePdfResult:
    path: Path
    replaced: bool
    warning: str = ""


class VietnamesePdfService:
    def __init__(
        self,
        *,
        repository: Any = DocumentVersionRepository,
        managed_root: Path = MANAGED_PAPERS_ROOT / "translations",
        trash_function: Any = QFile.moveToTrash,
    ) -> None:
        self.repository = repository
        self.managed_root = Path(managed_root)
        self.trash_function = trash_function

    def import_pdf(
        self,
        paper_id: int,
        source_path: str | Path,
        source_file_hash: str | None,
    ) -> VietnamesePdfResult:
        source = Path(source_path).expanduser()
        if source.suffix.lower() != ".pdf":
            raise VietnamesePdfError("Please choose a Vietnamese PDF file.")
        try:
            inspection = inspect_pdf(source)
        except PdfServiceError as error:
            raise VietnamesePdfError(str(error)) from error
        if (
            source_file_hash
            and inspection.file_hash.casefold() == str(source_file_hash).casefold()
        ):
            raise VietnamesePdfError(
                "The selected PDF is identical to the English original."
            )

        self.managed_root.mkdir(parents=True, exist_ok=True)
        destination = (
            self.managed_root
            / f"paper_{int(paper_id)}_{inspection.file_hash[:12]}_vi.pdf"
        )
        previous = self.repository.get(int(paper_id), "vi")
        previous_path = (
            resolve_paper_path(str(previous["file_path"]))
            if previous is not None
            else None
        )
        if (
            previous is not None
            and str(previous["file_hash"] or "") == inspection.file_hash
            and previous_path is not None
            and previous_path.is_file()
        ):
            return VietnamesePdfResult(previous_path, replaced=False)
        created_destination = False
        temporary_path: Path | None = None
        try:
            if source.resolve() != destination.resolve():
                descriptor, temporary_name = tempfile.mkstemp(
                    prefix=".vi-import-", suffix=".pdf", dir=self.managed_root
                )
                os.close(descriptor)
                temporary_path = Path(temporary_name)
                shutil.copy2(source, temporary_path)
                if calculate_sha256(temporary_path) != inspection.file_hash:
                    raise VietnamesePdfError(
                        "The Vietnamese PDF changed while it was being copied."
                    )
                os.replace(temporary_path, destination)
                temporary_path = None
                created_destination = True
            elif not destination.is_file():
                raise VietnamesePdfError("The selected Vietnamese PDF no longer exists.")

            self.repository.replace_vietnamese(
                int(paper_id),
                file_path=to_stored_paper_path(destination),
                file_hash=inspection.file_hash,
                source_file_hash=source_file_hash,
            )
        except VietnamesePdfError:
            if created_destination:
                destination.unlink(missing_ok=True)
            raise
        except (OSError, ValueError, sqlite3.Error) as error:
            if created_destination:
                destination.unlink(missing_ok=True)
            raise VietnamesePdfError(
                f"Could not add the Vietnamese PDF: {error}"
            ) from error
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

        warning = self._trash_replaced(previous_path, destination)
        return VietnamesePdfResult(
            destination,
            replaced=previous is not None,
            warning=warning,
        )

    def remove_pdf(self, paper_id: int) -> VietnamesePdfResult | None:
        previous = self.repository.remove_vietnamese(int(paper_id))
        if previous is None:
            return None
        path = resolve_paper_path(str(previous["file_path"]))
        warning = ""
        if path.is_file() and self._is_managed_translation(path):
            try:
                if not self.trash_function(str(path)):
                    warning = f"The VI record was removed, but the file was retained: {path}"
            except (OSError, RuntimeError):
                warning = f"The VI record was removed, but the file was retained: {path}"
        elif path.exists():
            warning = f"The external VI file was not deleted: {path}"
        return VietnamesePdfResult(path, replaced=True, warning=warning)

    def _trash_replaced(self, previous: Path | None, current: Path) -> str:
        if (
            previous is None
            or previous == current
            or not previous.is_file()
            or not self._is_managed_translation(previous)
        ):
            return ""
        try:
            if self.trash_function(str(previous)):
                return ""
        except (OSError, RuntimeError):
            pass
        return f"The previous VI PDF was retained: {previous}"

    def _is_managed_translation(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.managed_root.resolve())
        except ValueError:
            return False
        return True
