from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from difflib import SequenceMatcher
import logging
import re
import unicodedata

from PySide6.QtCore import QPoint, QRect, Qt, QUrl
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QPainter,
    QPixmap,
    QTextCursor,
    QTextDocument,
    QTextImageFormat,
)
from PySide6.QtWidgets import QWidget


LOGGER = logging.getLogger(__name__)


def citation_value(citation: object, name: str, default: object = None) -> object:
    if isinstance(citation, Mapping):
        return citation.get(name, default)
    return getattr(citation, name, default)


def _positive_int(value: object) -> int:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _citation_page(citation: object) -> int:
    for field in ("resolved_page", "page", "page_hint"):
        page = _positive_int(citation_value(citation, field, 0))
        if page:
            return page
    return 0


def _citation_alias(citation: object) -> str:
    for field in ("alias", "paper_alias"):
        alias = str(citation_value(citation, field, "") or "").strip().upper()
        if re.fullmatch(r"P\d+", alias):
            return alias
    return ""


def _is_renderable_citation(citation: object) -> bool:
    return (
        bool(citation_value(citation, "verified", False))
        and bool(_citation_page(citation))
        and bool(str(citation_value(citation, "evidence", "") or "").strip())
    )


def _source_key(citation: object) -> tuple[int, int, str]:
    try:
        paper_id = int(citation_value(citation, "paper_id", 0) or 0)
    except (TypeError, ValueError):
        paper_id = 0
    page = _citation_page(citation)
    evidence = " ".join(
        str(citation_value(citation, "evidence", "") or "").casefold().split()
    )
    return paper_id, page, evidence


def _claim_text(citation: object) -> str:
    claim = citation_value(citation, "claim_text", "")
    if not claim:
        claim = citation_value(citation, "claim", "")
    return " ".join(str(claim or "").split())


def _normalized_render_citation(citation: object) -> dict[str, object]:
    """Create one UI citation shape without mutating persisted/service data."""
    value = dict(citation) if isinstance(citation, Mapping) else {}
    page = _citation_page(citation)
    alias = _citation_alias(citation)
    value.update(
        {
            "paper_id": citation_value(citation, "paper_id"),
            "page_hint": _positive_int(
                citation_value(citation, "page_hint", page)
            ) or page,
            "resolved_page": page,
            "section": citation_value(citation, "section"),
            "evidence": str(citation_value(citation, "evidence", "") or "").strip(),
            "claim_text": _claim_text(citation),
            "verified": True,
            "verification_status": citation_value(
                citation, "verification_status", "verified"
            ),
            "conversation_id": citation_value(citation, "conversation_id"),
            "alias": alias or None,
            "paper_alias": alias or None,
        }
    )
    return value


def _fold_text(value: str) -> str:
    text = str(value or "").replace("Đ", "D").replace("đ", "d")
    text = unicodedata.normalize("NFKD", text.casefold())
    result: list[str] = []
    pending_space = False
    for character in text:
        if unicodedata.combining(character):
            continue
        if character.isalnum():
            if pending_space and result:
                result.append(" ")
            result.append(character)
            pending_space = False
        else:
            pending_space = True
    return "".join(result).strip()


def _fold_text_with_positions(value: str) -> tuple[str, list[int]]:
    """Return folded text plus original character ends for attachment cursors."""
    result: list[str] = []
    positions: list[int] = []
    pending_space = False
    pending_position = 0
    for index, original in enumerate(str(value or "")):
        character_value = original.replace("Đ", "D").replace("đ", "d")
        normalized = unicodedata.normalize("NFKD", character_value.casefold())
        emitted = False
        for character in normalized:
            if unicodedata.combining(character):
                continue
            if character.isalnum():
                if pending_space and result:
                    result.append(" ")
                    positions.append(max(pending_position, index))
                result.append(character)
                positions.append(index + 1)
                pending_space = False
                emitted = True
            else:
                pending_space = True
                pending_position = index + 1
        if not emitted and normalized:
            pending_space = True
            pending_position = index + 1
    while result and result[-1] == " ":
        result.pop()
        positions.pop()
    return "".join(result), positions


def _claim_variants(claim: str) -> list[str]:
    folded = _fold_text(claim)
    if not folded:
        return []
    variants = [folded]
    without_alias = re.sub(r"^p\d+\s+", "", folded).strip()
    if without_alias and without_alias != folded:
        variants.append(without_alias)
    return variants


def _normalized_exact_end(plain_text: str, claim: str) -> int | None:
    folded_text, positions = _fold_text_with_positions(plain_text)
    for variant in _claim_variants(claim):
        start = folded_text.find(variant)
        if start >= 0:
            return positions[start + len(variant) - 1]
    return None


def _fuzzy_claim_end(plain_text: str, claim: str) -> int | None:
    variants = _claim_variants(claim)
    if not variants:
        return None
    best: tuple[float, int] | None = None
    for match in re.finditer(r"[^\n\r\u2029]+", plain_text):
        candidate = _fold_text(match.group(0))
        if not candidate:
            continue
        candidate_tokens = set(candidate.split())
        for variant in variants:
            claim_tokens = set(variant.split())
            if len(claim_tokens) < 3:
                continue
            shared = claim_tokens.intersection(candidate_tokens)
            coverage = len(shared) / len(claim_tokens)
            precision = len(shared) / max(1, len(candidate_tokens))
            ratio = SequenceMatcher(None, variant, candidate).ratio()
            if coverage < 0.42 and ratio < 0.62:
                continue
            claim_aliases = set(re.findall(r"\bp\d+\b", variant))
            candidate_aliases = set(re.findall(r"\bp\d+\b", candidate))
            alias_coverage = (
                len(claim_aliases.intersection(candidate_aliases))
                / len(claim_aliases)
                if claim_aliases
                else 0.0
            )
            score = (
                0.55 * ratio
                + 0.35 * coverage
                + 0.10 * precision
                + 0.08 * alias_coverage
            )
            if score >= 0.52 and (best is None or score > best[0]):
                best = (score, match.end())
    return best[1] if best is not None else None


def _claim_attachment_end(document: QTextDocument, claim: str) -> int | None:
    if not claim:
        return None
    cursor = document.find(claim)
    if not cursor.isNull():
        return cursor.selectionEnd()
    plain_text = document.toPlainText()
    normalized_end = _normalized_exact_end(plain_text, claim)
    if normalized_end is not None:
        return normalized_end
    return _fuzzy_claim_end(plain_text, claim)


def verified_citation_groups(citations: object) -> list[list[object]]:
    """Return verified citations grouped by source/evidence identity."""
    values = list(citations) if isinstance(citations, (list, tuple)) else []
    groups: dict[tuple[int, int, str], list[object]] = {}
    for citation in values:
        if not _is_renderable_citation(citation):
            continue
        key = _source_key(citation)
        if not key[1]:
            continue
        groups.setdefault(key, []).append(citation)
    return list(groups.values())


def citation_number_map(citations: object) -> dict[tuple[int, int, str], int]:
    """Assign contiguous numbers to final verified source identities."""
    numbers: dict[tuple[int, int, str], int] = {}
    values = list(citations) if isinstance(citations, (list, tuple)) else []
    for citation in values:
        if not _is_renderable_citation(citation):
            continue
        key = _source_key(citation)
        if key[1] and key not in numbers:
            numbers[key] = len(numbers) + 1
    return numbers


def citation_claim(group: Iterable[object]) -> str:
    for citation in group:
        claim = _claim_text(citation)
        if claim:
            return claim
    return ""


def citation_label(citation: object) -> str:
    page = _citation_page(citation)
    section = str(citation_value(citation, "section", "") or "").strip()
    return f"p. {page}{f' · {section}' if section else ''}"


def source_tooltip(group: list[object]) -> str:
    if not group:
        return "Verified source"
    alias = _citation_alias(group[0])
    prefix = f"Source {alias}" if alias else "Source"
    return f"{prefix}: {citation_label(group[0])}"


def activate_source_group(
    parent: QWidget,
    group: list[object],
    callback: Callable[[object], None],
    global_position: QPoint | None = None,
) -> None:
    if group:
        # Citations are already ordered by verification quality. One numbered
        # badge always opens the best matching source directly.
        callback(group[0])


def _citation_badge(label: str) -> tuple[QPixmap, int, int]:
    font = QFont()
    font.setPixelSize(10)
    font.setBold(True)
    metrics = QFontMetrics(font)
    width = max(20, metrics.horizontalAdvance(label) + 7)
    height = 15
    pixmap = QPixmap(width, height)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QColor("#D9E5FA"))
        painter.setBrush(QColor("#EEF4FF"))
        painter.drawRoundedRect(QRect(0, 0, width - 1, height - 1), 4, 4)
        painter.setPen(QColor("#315FBF"))
        painter.setFont(font)
        painter.drawText(
            QRect(1, 0, width - 2, height - 1),
            Qt.AlignmentFlag.AlignCenter,
            label,
        )
    finally:
        painter.end()
    return pixmap, width, height


def insert_inline_citations(
    document: QTextDocument,
    citations: object,
    numbering: Mapping[tuple[int, int, str], int] | None = None,
    paper_labels: Mapping[int, str] | None = None,
) -> dict[str, list[object]]:
    """Insert compact per-response numbered badges after verified claims."""
    values = list(citations) if isinstance(citations, (list, tuple)) else []
    verified = [
        _normalized_render_citation(citation)
        for citation in values
        if _is_renderable_citation(citation)
    ]
    if not verified:
        LOGGER.info(
            "[CITATION] verified=0 rendered=0 "
            "claim_not_located_in_rendered_text=0 duplicate=0"
        )
        return {}

    shared_numbers = dict(numbering or {})
    source_numbers: dict[tuple[int, int, str], int] = {}
    source_groups: dict[tuple[int, int, str], list[object]] = {}
    placements: list[tuple[int, int, tuple[int, int, str]]] = []
    seen_sources: set[tuple[int, int, str]] = set()
    duplicate_count = 0
    claim_not_located = 0
    fallback_position = max(0, document.characterCount() - 1)

    for citation in verified:
        source_key = _source_key(citation)
        if source_key in seen_sources:
            duplicate_count += 1
            continue
        seen_sources.add(source_key)
        if source_key not in source_numbers:
            if source_key not in shared_numbers:
                shared_numbers[source_key] = len(shared_numbers) + 1
            source_numbers[source_key] = shared_numbers[source_key]
        source_groups[source_key] = [citation]
        claim = _claim_text(citation)
        position = _claim_attachment_end(document, claim)
        if position is None:
            claim_not_located += 1
            position = fallback_position
        placements.append((position, source_numbers[source_key], source_key))

    resources: dict[int, tuple[QUrl, int, int]] = {}
    result_groups: dict[str, list[object]] = {}
    for source_key, number in source_numbers.items():
        resource_url = QUrl(f"ra-source-badge://badge-{number}")
        paper_id, page, _evidence = source_key
        paper_label = (paper_labels or {}).get(paper_id) or _citation_alias(
            source_groups[source_key][0]
        )
        badge_label = (
            f"[{paper_label} · p.{page}]" if paper_label else f"[{number}]"
        )
        pixmap, width, height = _citation_badge(badge_label)
        document.addResource(
            QTextDocument.ResourceType.ImageResource,
            resource_url,
            pixmap,
        )
        resources[number] = (resource_url, width, height)
        result_groups[f"source-{number}"] = source_groups[source_key]

    by_position: dict[int, list[tuple[int, tuple[int, int, str]]]] = {}
    for position, number, source_key in placements:
        by_position.setdefault(position, []).append((number, source_key))
    for position in sorted(by_position, reverse=True):
        cursor = QTextCursor(document)
        cursor.setPosition(position)
        for number, source_key in sorted(by_position[position], key=lambda item: item[0]):
            resource_url, width, height = resources[number]
            cursor.insertText("\u00a0")
            image = QTextImageFormat()
            image.setName(resource_url.toString())
            image.setWidth(width)
            image.setHeight(height)
            image.setAnchor(True)
            image.setAnchorHref(f"ra-source://source-{number}")
            image.setToolTip(source_tooltip(source_groups[source_key]))
            cursor.insertImage(image)

    LOGGER.info(
        "[CITATION] verified=%d rendered=%d "
        "claim_not_located_in_rendered_text=%d duplicate=%d",
        len(source_numbers),
        len(placements),
        claim_not_located,
        duplicate_count,
    )
    return result_groups
