from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile


BACKUP_FORMAT = "research-assistant-backup"
BACKUP_FORMAT_VERSION = 1

_EXCLUDED_DIRECTORY_NAMES = {
    ".cache",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "backups",
    "cache",
    "logs",
    "pdfjs",
}


class BackupError(RuntimeError):
    """Base class for recoverable backup failures."""


class BackupSourceError(BackupError):
    """A requested database or library source is invalid."""


class BackupDatabaseError(BackupError):
    """SQLite could not produce or verify a consistent snapshot."""


class BackupDestinationExistsError(BackupError):
    """The timestamped destination already exists and was not overwritten."""


class BackupWriteError(BackupError):
    """The archive could not be written or published."""


@dataclass(frozen=True, slots=True)
class BackupResult:
    archive_path: Path
    created_at: datetime
    paper_file_count: int


def create_backup(
    database_path: str | Path,
    library_papers_path: str | Path,
    output_directory: str | Path,
    *,
    timestamp: datetime | None = None,
) -> BackupResult:
    """Create a timestamped ZIP from a SQLite snapshot and managed papers."""
    database = _require_database(database_path)
    library = _resolve_library_directory(library_papers_path)
    output = Path(output_directory).expanduser().resolve()
    created_at = _as_utc(timestamp or datetime.now(timezone.utc))
    archive_name = created_at.strftime(
        "research_assistant_backup_%Y%m%dT%H%M%SZ.zip"
    )
    archive_path = output / archive_name

    try:
        output.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise BackupWriteError(
            f"Could not create the backup directory: {output}"
        ) from error
    if not output.is_dir():
        raise BackupWriteError(f"The backup destination is not a directory: {output}")
    if archive_path.exists():
        raise BackupDestinationExistsError(
            f"A backup with this timestamp already exists: {archive_path}"
        )

    paper_files = _collect_library_files(library)

    try:
        with tempfile.TemporaryDirectory(
            prefix=".research-assistant-backup-",
            dir=output,
        ) as temporary_directory:
            temporary_path = Path(temporary_directory)
            snapshot_path = temporary_path / "research.db"
            temporary_archive = temporary_path / "backup.zip"

            _snapshot_database(database, snapshot_path)
            manifest = _build_manifest(
                database=database,
                snapshot=snapshot_path,
                library=library,
                paper_files=paper_files,
                created_at=created_at,
            )
            _write_archive(
                temporary_archive,
                snapshot_path,
                library,
                paper_files,
                manifest,
            )
            _verify_archive(temporary_archive)
            _publish_without_overwrite(temporary_archive, archive_path)
    except BackupError:
        raise
    except (OSError, BadZipFile) as error:
        raise BackupWriteError(
            f"Could not create the backup archive: {archive_path}"
        ) from error

    return BackupResult(
        archive_path=archive_path,
        created_at=created_at,
        paper_file_count=len(paper_files),
    )


def _require_database(database_path: str | Path) -> Path:
    candidate = Path(database_path).expanduser()
    try:
        database = candidate.resolve(strict=True)
    except (FileNotFoundError, OSError) as error:
        raise BackupSourceError(f"The database does not exist: {candidate}") from error
    if not database.is_file():
        raise BackupSourceError(f"The database path is not a file: {database}")
    return database


def _resolve_library_directory(library_path: str | Path) -> Path:
    library = Path(library_path).expanduser().resolve()
    if library.exists() and not library.is_dir():
        raise BackupSourceError(
            f"The paper library path is not a directory: {library}"
        )
    return library


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _collect_library_files(library: Path) -> tuple[Path, ...]:
    if not library.exists():
        return ()

    files: list[Path] = []
    try:
        for directory, directory_names, file_names in os.walk(
            library,
            topdown=True,
            onerror=_raise_walk_error,
            followlinks=False,
        ):
            directory_path = Path(directory)
            directory_names[:] = [
                name
                for name in directory_names
                if name.casefold() not in _EXCLUDED_DIRECTORY_NAMES
                and not (directory_path / name).is_symlink()
            ]
            for file_name in file_names:
                candidate = directory_path / file_name
                if candidate.is_symlink() or not candidate.is_file():
                    continue
                if (
                    candidate.suffix.casefold() == ".zip"
                    and candidate.name.casefold().startswith(
                        "research_assistant_backup_"
                    )
                ):
                    continue
                files.append(candidate)
    except OSError as error:
        raise BackupSourceError(
            f"Could not read the paper library: {library}"
        ) from error

    return tuple(
        sorted(
            files,
            key=lambda path: path.relative_to(library).as_posix().casefold(),
        )
    )


def _raise_walk_error(error: OSError) -> None:
    raise error


def _snapshot_database(database: Path, snapshot: Path) -> None:
    try:
        source_uri = f"{database.as_uri()}?mode=ro"
        with closing(
            sqlite3.connect(source_uri, uri=True, timeout=10.0)
        ) as source_connection, closing(sqlite3.connect(snapshot)) as snapshot_connection:
            source_connection.backup(snapshot_connection)
            check = snapshot_connection.execute("PRAGMA quick_check").fetchone()
            if check is None or str(check[0]).casefold() != "ok":
                detail = check[0] if check else "no result"
                raise BackupDatabaseError(
                    f"The SQLite backup failed its integrity check: {detail}"
                )
    except BackupDatabaseError:
        raise
    except sqlite3.Error as error:
        raise BackupDatabaseError(
            f"Could not create a SQLite snapshot from: {database}"
        ) from error


def _build_manifest(
    *,
    database: Path,
    snapshot: Path,
    library: Path,
    paper_files: tuple[Path, ...],
    created_at: datetime,
) -> dict[str, object]:
    return {
        "format": BACKUP_FORMAT,
        "format_version": BACKUP_FORMAT_VERSION,
        "created_at": created_at.isoformat().replace("+00:00", "Z"),
        "database": {
            "original_path": str(database),
            "stored_path": "data/research.db",
            "size": snapshot.stat().st_size,
        },
        "library": {
            "original_path": str(library),
            "stored_path": "library/papers",
        },
        "paper_files": [
            {
                "original_path": str(path),
                "stored_path": (
                    Path("library/papers") / path.relative_to(library)
                ).as_posix(),
                "size": path.stat().st_size,
            }
            for path in paper_files
        ],
    }


def _write_archive(
    archive_path: Path,
    snapshot_path: Path,
    library: Path,
    paper_files: tuple[Path, ...],
    manifest: dict[str, object],
) -> None:
    with ZipFile(
        archive_path,
        mode="w",
        compression=ZIP_DEFLATED,
        compresslevel=6,
        allowZip64=True,
    ) as archive:
        archive.write(snapshot_path, "data/research.db")
        for paper_file in paper_files:
            stored_path = (
                Path("library/papers") / paper_file.relative_to(library)
            ).as_posix()
            archive.write(paper_file, stored_path)
        archive.writestr(
            "manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        )


def _verify_archive(archive_path: Path) -> None:
    try:
        with ZipFile(archive_path, mode="r") as archive:
            bad_entry = archive.testzip()
    except (OSError, BadZipFile) as error:
        raise BackupWriteError(
            f"Could not verify the temporary backup archive: {archive_path}"
        ) from error
    if bad_entry is not None:
        raise BackupWriteError(
            f"The temporary backup archive contains a damaged entry: {bad_entry}"
        )


def _publish_without_overwrite(source: Path, destination: Path) -> None:
    created_destination = False
    try:
        with source.open("rb") as source_file:
            with destination.open("xb") as destination_file:
                created_destination = True
                shutil.copyfileobj(source_file, destination_file, length=1024 * 1024)
                destination_file.flush()
                os.fsync(destination_file.fileno())
    except FileExistsError as error:
        raise BackupDestinationExistsError(
            f"The backup destination already exists: {destination}"
        ) from error
    except OSError as error:
        if created_destination:
            destination.unlink(missing_ok=True)
        raise BackupWriteError(
            f"Could not publish the backup archive: {destination}"
        ) from error
