from __future__ import annotations

from pathlib import Path

from app.runtime_paths import writable_path


PROJECT_ROOT = writable_path()
LIBRARY_ROOT = PROJECT_ROOT / "library"
MANAGED_PAPERS_ROOT = LIBRARY_ROOT / "papers"


def resolve_paper_path(
    stored_path: str | Path,
    *,
    project_root: Path = PROJECT_ROOT,
) -> Path:
    """Resolve new relative paths and legacy absolute managed-library paths."""
    value = str(stored_path).strip()
    if not value:
        raise ValueError("Paper path is empty")

    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        return (project_root / candidate).resolve(strict=False)

    resolved = candidate.resolve(strict=False)
    if resolved.exists():
        return resolved

    # Backups may be restored under another project directory while old rows
    # still contain an absolute path. Rebase only a managed library filename.
    normalized_parts = [part.casefold() for part in candidate.parts]
    try:
        library_index = normalized_parts.index("library")
    except ValueError:
        return resolved

    if (
        library_index + 2 >= len(candidate.parts)
        or normalized_parts[library_index + 1] != "papers"
    ):
        return resolved

    relative_managed_path = Path(*candidate.parts[library_index:])
    rebased = (project_root / relative_managed_path).resolve(strict=False)
    return rebased if rebased.exists() else resolved


def to_stored_paper_path(
    path: str | Path,
    *,
    project_root: Path = PROJECT_ROOT,
) -> str:
    """Store managed project files relatively while preserving external paths."""
    resolved = Path(path).expanduser().resolve(strict=False)
    root = project_root.resolve(strict=False)
    try:
        relative = resolved.relative_to(root)
    except ValueError:
        return str(resolved)
    return relative.as_posix()


def is_managed_paper_path(
    path: str | Path,
    *,
    managed_root: Path = MANAGED_PAPERS_ROOT,
) -> bool:
    candidate = Path(path).expanduser().resolve(strict=False)
    root = managed_root.resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def managed_paper_destination(
    file_hash: str,
    source_name: str,
    *,
    managed_root: Path = MANAGED_PAPERS_ROOT,
) -> Path:
    safe_name = Path(source_name).name
    if not safe_name or safe_name in {".", ".."}:
        raise ValueError("Source filename is invalid")
    normalized_hash = file_hash.strip().lower()
    if len(normalized_hash) < 12 or any(
        character not in "0123456789abcdef" for character in normalized_hash
    ):
        raise ValueError("File hash is invalid")
    return managed_root / f"{normalized_hash[:12]}_{safe_name}"
