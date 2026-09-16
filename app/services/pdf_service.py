from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import pymupdf


DEFAULT_HASH_CHUNK_SIZE = 1024 * 1024

# PDF dates commonly continue immediately after the year (for example
# ``D:20240807120000+07'00'``), so only the leading boundary is required.
_YEAR_PATTERN = re.compile(r"(?<!\d)((?:18|19|20|21)\d{2})")
_DOI_PATTERN = re.compile(
    r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+",
    flags=re.IGNORECASE,
)


class PdfServiceError(RuntimeError):
    """Base class for PDF inspection failures suitable for showing in the UI."""

    def __init__(self, path: Path, message: str) -> None:
        self.path = path
        super().__init__(message)


class PdfFileNotFoundError(PdfServiceError):
    """The requested PDF path does not exist."""


class PdfCorruptError(PdfServiceError):
    """The file is empty, corrupt, or is not a PDF document."""


class PdfEncryptedError(PdfServiceError):
    """The PDF requires a password before its contents can be inspected."""


class PdfReadError(PdfServiceError):
    """The PDF exists but could not be read completely."""


@dataclass(frozen=True, slots=True)
class PdfPageText:
    """Extracted text for one PDF page, numbered from one."""

    page_number: int
    text: str


@dataclass(frozen=True, slots=True)
class PdfInspectionResult:
    """Stable, typed output used by import and future text-indexing flows."""

    path: Path
    file_hash: str
    title: str
    authors: str
    year: int | None
    doi: str | None
    total_pages: int
    pages: tuple[PdfPageText, ...]

    @property
    def text(self) -> str:
        """Return all extracted page text while retaining page boundaries."""
        return "\n\n".join(page.text for page in self.pages)


def calculate_sha256(
    file_path: str | Path,
    *,
    chunk_size: int = DEFAULT_HASH_CHUNK_SIZE,
) -> str:
    """Calculate a file hash without loading the complete PDF into memory."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be greater than zero")

    path = _resolve_file(file_path)
    digest = hashlib.sha256()

    try:
        with path.open("rb") as pdf_file:
            while chunk := pdf_file.read(chunk_size):
                digest.update(chunk)
    except FileNotFoundError as error:
        raise PdfFileNotFoundError(
            path,
            f"The PDF file no longer exists: {path}",
        ) from error
    except OSError as error:
        raise PdfReadError(
            path,
            f"Could not read the PDF file: {path}",
        ) from error

    return digest.hexdigest()


def inspect_pdf(
    file_path: str | Path,
    *,
    hash_chunk_size: int = DEFAULT_HASH_CHUNK_SIZE,
) -> PdfInspectionResult:
    """Hash a PDF, read conservative metadata, and extract text page by page."""
    path = _resolve_file(file_path)
    file_hash = calculate_sha256(path, chunk_size=hash_chunk_size)

    try:
        document = pymupdf.open(path)
    except FileNotFoundError as error:
        raise PdfFileNotFoundError(
            path,
            f"The PDF file no longer exists: {path}",
        ) from error
    except (pymupdf.EmptyFileError, pymupdf.FileDataError) as error:
        raise PdfCorruptError(
            path,
            f"The file is empty, corrupt, or is not a valid PDF: {path}",
        ) from error
    except (OSError, RuntimeError, ValueError) as error:
        raise PdfReadError(path, f"Could not open the PDF: {path}") from error

    try:
        if not document.is_pdf:
            raise PdfCorruptError(path, f"The selected file is not a PDF: {path}")
        if document.needs_pass:
            raise PdfEncryptedError(
                path,
                f"The PDF is encrypted and requires a password: {path}",
            )

        metadata: Mapping[str, str] = document.metadata or {}
        pages = _extract_pages(document, path)
        title = _clean_metadata_text(metadata.get("title"))
        authors = _clean_metadata_text(metadata.get("author"))

        return PdfInspectionResult(
            path=path,
            file_hash=file_hash,
            title=title or html.unescape(path.stem).strip(),
            authors=authors,
            year=_extract_year(metadata),
            doi=_extract_doi(metadata, pages),
            total_pages=document.page_count,
            pages=pages,
        )
    except PdfServiceError:
        raise
    except (OSError, RuntimeError, ValueError) as error:
        raise PdfReadError(
            path,
            f"Could not inspect the PDF completely: {path}",
        ) from error
    finally:
        document.close()


def _resolve_file(file_path: str | Path) -> Path:
    candidate = Path(file_path).expanduser()
    try:
        path = candidate.resolve(strict=True)
    except FileNotFoundError as error:
        raise PdfFileNotFoundError(
            candidate,
            f"The PDF file does not exist: {candidate}",
        ) from error
    except OSError as error:
        raise PdfReadError(
            candidate,
            f"Could not access the PDF path: {candidate}",
        ) from error

    if not path.is_file():
        raise PdfReadError(path, f"The PDF path is not a file: {path}")
    return path


def _extract_pages(document: pymupdf.Document, path: Path) -> tuple[PdfPageText, ...]:
    pages: list[PdfPageText] = []
    for page_index in range(document.page_count):
        try:
            page = document.load_page(page_index)
            text = page.get_text("text", sort=True)
        except (OSError, RuntimeError, ValueError) as error:
            page_number = page_index + 1
            raise PdfReadError(
                path,
                f"Could not extract text from page {page_number}: {path}",
            ) from error
        pages.append(PdfPageText(page_number=page_index + 1, text=text))
    return tuple(pages)


def _clean_metadata_text(value: str | None) -> str:
    return html.unescape(value or "").strip()


def _extract_year(metadata: Mapping[str, str]) -> int | None:
    for key in ("creationDate", "modDate"):
        match = _YEAR_PATTERN.search(metadata.get(key) or "")
        if match:
            return int(match.group(1))
    return None


def _extract_doi(
    metadata: Mapping[str, str],
    pages: tuple[PdfPageText, ...],
) -> str | None:
    candidates = [
        metadata.get("doi") or "",
        metadata.get("subject") or "",
        metadata.get("keywords") or "",
        metadata.get("title") or "",
    ]
    candidates.extend(page.text for page in pages[:2])

    for candidate in candidates:
        match = _DOI_PATTERN.search(html.unescape(candidate))
        if match:
            return _trim_doi(match.group(0))
    return None


def _trim_doi(value: str) -> str:
    doi = value.rstrip(".,;:")
    while doi.endswith(")") and doi.count("(") < doi.count(")"):
        doi = doi[:-1]
    while doi.endswith("]") and doi.count("[") < doi.count("]"):
        doi = doi[:-1]
    return doi
