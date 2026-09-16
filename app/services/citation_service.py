from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Mapping

import pymupdf

from app.ai.base import AISummaryFormatError


LOGGER = logging.getLogger(__name__)

SUPPORT_STATUSES = frozenset({"supported", "partially_supported", "not_found"})
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

_SUMMARY_FIELD_ALIASES = {
    "problem": {"problem", "researchproblem", "vande", "baitoan"},
    "contribution": {"contribution", "contributions", "donggop", "donggopchinh"},
    "method": {"method", "methods", "methodology", "approach", "phuongphap"},
    "dataset": {"dataset", "datasets", "data", "dulieu", "botulieu"},
    "baseline": {"baseline", "baselines", "comparison", "comparisons", "doisanh"},
    "results": {"result", "results", "findings", "ketqua"},
    "limitations": {"limitation", "limitations", "constraints", "hanche"},
    "unclear_points": {
        "unclearpoint", "unclearpoints", "openquestion", "openquestions",
        "unknowns", "diemchuaro", "vandechuaro",
    },
    "ideas": {"idea", "ideas", "futuredirection", "futuredirections", "ytuong"},
}
_SUMMARY_WRAPPERS = ("fields", "summary", "result", "data", "output", "response")
_RESULT_PATTERN = re.compile(
    r"<!--\s*RA_RESULT\s*(\{.*?\})\s*RA_RESULT\s*-->",
    re.DOTALL,
)
_VISIBLE_PAPER_CITATION_PATTERN = re.compile(
    r"\[\s*P\d+(?:\s*(?:[·.,;/]|\band\b)\s*(?:P\d+|p\.?\s*\d+))*\s*\]",
    re.IGNORECASE,
)
_PAPER_ALIAS_PATTERN = re.compile(
    r"\b(P\d+)\b(?:\s*[·:]?\s*p\.?\s*(\d+))?",
    re.IGNORECASE,
)
_JSON_LATEX_BACKSLASH_PATTERN = re.compile(
    r"(?<!\\)\\(?=(?:"
    r"hat|bar|vec|tilde|dot|ddot|sqrt|frac|text|mathrm|mathbf|operatorname|"
    r"sum|prod|int|alpha|beta|gamma|delta|epsilon|varepsilon|theta|lambda|mu|pi|rho|"
    r"sigma|tau|phi|omega|Gamma|Delta|Theta|Lambda|Sigma|Phi|Omega|"
    r"times|cdot|odot|pm|le|leq|ge|geq|neq|approx|infty|to|left|right"
    r")\b)"
)


def _json_value_end(text: str, start: int) -> int | None:
    """Locate one JSON value even when a string contains bare LaTeX escapes."""
    if start >= len(text) or text[start] not in "{[":
        return None
    stack = [text[start]]
    in_string = False
    escaped = False
    for position in range(start + 1, len(text)):
        character = text[position]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "{[":
            stack.append(character)
        elif character in "}]":
            expected = "{" if character == "}" else "["
            if not stack or stack[-1] != expected:
                return None
            stack.pop()
            if not stack:
                return position + 1
    return None


def _decode_result_json(raw_json: str) -> Any:
    """Decode metadata after escaping only recognized bare LaTeX commands."""
    safe_json = _JSON_LATEX_BACKSLASH_PATTERN.sub(r"\\\\", raw_json)
    return json.loads(safe_json)


def _chat_result_block(
    text: str,
) -> tuple[int, int, Mapping[str, Any]] | None:
    """Decode the canonical trailer plus harmless provider-added wrappers."""
    exact_matches = list(_RESULT_PATTERN.finditer(text))
    for match in reversed(exact_matches):
        try:
            payload = _decode_result_json(match.group(1))
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(payload, Mapping):
            return match.start(), match.end(), payload

    sentinels = list(re.finditer(r"\bRA_RESULT\b", text, re.IGNORECASE))
    for sentinel in reversed(sentinels):
        object_start = text.find("{", sentinel.end(), sentinel.end() + 100)
        array_start = text.find("[", sentinel.end(), sentinel.end() + 100)
        json_start = min(
            (position for position in (object_start, array_start) if position >= 0),
            default=-1,
        )
        if json_start < 0:
            continue
        prefix = text[sentinel.end() : json_start]
        if not re.fullmatch(
            r"[\s:=-]*(?:```(?:json)?\s*)?", prefix, re.IGNORECASE
        ):
            continue
        payload_end = _json_value_end(text, json_start)
        if payload_end is None:
            continue
        try:
            payload = _decode_result_json(text[json_start:payload_end])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, list):
            payload = {
                "support_status": "supported",
                "citations": payload,
            }
        if not isinstance(payload, Mapping):
            continue
        tail = text[payload_end:]
        closing = re.match(
            r"\s*(?:(?:```\s*)?(?:<!--\s*)?RA_RESULT\s*(?:-->)?"
            r"|```\s*(?:-->)?|-->)",
            tail,
            re.IGNORECASE,
        )
        if closing is not None:
            block_end = payload_end + closing.end()
        elif tail.strip():
            continue
        else:
            block_end = len(text)

        block_start = sentinel.start()
        comment_start = text.rfind(
            "<!--", max(0, sentinel.start() - 12), sentinel.start()
        )
        if (
            comment_start >= 0
            and not text[comment_start + 4 : sentinel.start()].strip()
        ):
            block_start = comment_start
        else:
            fence_start = text.rfind(
                "```", max(0, sentinel.start() - 12), sentinel.start()
            )
            if (
                fence_start >= 0
                and not text[fence_start + 3 : sentinel.start()].strip()
            ):
                block_start = fence_start
        return block_start, block_end, payload
    return None


@dataclass(frozen=True)
class Citation:
    page_hint: int | None
    resolved_page: int | None
    section: str | None
    evidence: str
    claim_text: str = ""
    verified: bool = False
    verification_status: str = "unverified"
    paper_id: int | None = None
    conversation_id: int | None = None
    alias: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "page_hint": self.page_hint,
            "resolved_page": self.resolved_page,
            "section": self.section,
            "evidence": self.evidence,
            "claim_text": self.claim_text,
            "verified": self.verified,
            "verification_status": self.verification_status,
            "paper_id": self.paper_id,
            "conversation_id": self.conversation_id,
            "alias": self.alias,
        }


@dataclass(frozen=True)
class NormalizedAIResult:
    content: str
    support_status: str
    citations: tuple[Citation, ...]
    metadata_valid: bool = True
    metadata_present: bool = True
    metadata_claim_count: int = 0
    invalid_citation_count: int = 0
    duplicate_citation_count: int = 0


@dataclass(frozen=True)
class SummaryFieldResult:
    content: str
    support_status: str
    citations: tuple[Citation, ...]


@dataclass(frozen=True)
class AISummaryResult:
    fields: Mapping[str, SummaryFieldResult]
    provider: str
    model: str


def _support_status(value: Any) -> str:
    candidate = str(value or "").strip().lower()
    return candidate if candidate in SUPPORT_STATUSES else "not_found"


def _page_hint(value: Any) -> int | None:
    try:
        page = int(value)
    except (TypeError, ValueError):
        return None
    return page if page >= 1 else None


def _resolved_paper_id(
    value: Any, paper_aliases: Mapping[str, int]
) -> int | None:
    aliases = {
        str(alias).strip().upper(): int(paper_id)
        for alias, paper_id in paper_aliases.items()
    }
    if isinstance(value, str):
        alias = value.strip().upper()
        if alias in aliases:
            return aliases[alias]
    paper_id = _page_hint(value)
    if aliases and paper_id not in set(aliases.values()):
        return None
    return paper_id


def _citation(
    raw: Any, paper_aliases: Mapping[str, int] | None = None
) -> Citation | None:
    if not isinstance(raw, Mapping):
        return None
    evidence = " ".join(str(raw.get("evidence") or "").split()).strip()
    if not evidence:
        return None
    section = " ".join(str(raw.get("section") or "").split()).strip() or None
    aliases = {
        str(alias).strip().upper(): int(paper_id)
        for alias, paper_id in (paper_aliases or {}).items()
    }
    raw_paper = raw.get(
        "paper_id",
        raw.get("paper_alias", raw.get("alias")),
    )
    explicit_alias = str(
        raw.get("paper_alias") or raw.get("alias") or ""
    ).strip().upper()
    if not explicit_alias and isinstance(raw_paper, str):
        candidate = raw_paper.strip().upper()
        if candidate in aliases:
            explicit_alias = candidate
    paper_id = (
        aliases[explicit_alias]
        if explicit_alias in aliases
        else _resolved_paper_id(raw_paper, aliases)
    )
    if aliases and paper_id is None:
        return None
    canonical_alias = ""
    if paper_id is not None:
        canonical_alias = next(
            (
                name
                for name, value in aliases.items()
                if int(value) == int(paper_id)
            ),
            "",
        )
    return Citation(
        page_hint=_page_hint(
            raw.get("page_hint", raw.get("page", raw.get("resolved_page")))
        ),
        resolved_page=None,
        section=section[:100] if section else None,
        evidence=evidence[:700],
        claim_text=" ".join(str(raw.get("claim") or raw.get("claim_text") or "").split())[:1200],
        paper_id=paper_id,
        conversation_id=_page_hint(raw.get("conversation_id")),
        alias=canonical_alias or None,
    )


def _citation_source_list(
    value: Any,
    paper_aliases: Mapping[str, int],
) -> list[Any] | None:
    if isinstance(value, list):
        return value
    if not isinstance(value, Mapping):
        return None
    if any(
        name in value
        for name in (
            "evidence",
            "evidences",
            "paper_id",
            "paper_ids",
            "paper_alias",
            "paper_aliases",
            "alias",
            "page_hint",
            "page",
        )
    ):
        return [value]
    result: list[Any] = []
    for raw_alias, raw_sources in value.items():
        alias = str(raw_alias).strip().upper()
        sources = raw_sources if isinstance(raw_sources, list) else [raw_sources]
        for source in sources:
            if not isinstance(source, Mapping):
                continue
            normalized = dict(source)
            if alias in paper_aliases:
                normalized.setdefault("paper_alias", alias)
            result.append(normalized)
    return result or None


def _expanded_citation_items(
    raw: list[Any],
    *,
    paper_aliases: Mapping[str, int] | None = None,
    depth: int = 0,
) -> list[Any]:
    """Flatten provider citation clusters into one mapping per destination."""
    if depth > 4:
        return list(raw)
    aliases = {
        str(alias).strip().upper(): int(paper_id)
        for alias, paper_id in (paper_aliases or {}).items()
    }
    expanded: list[Any] = []
    for item in raw:
        if isinstance(item, list):
            expanded.extend(
                _expanded_citation_items(
                    item,
                    paper_aliases=aliases,
                    depth=depth + 1,
                )
            )
            continue
        if not isinstance(item, Mapping):
            expanded.append(item)
            continue

        nested = _citation_source_list(item.get("sources"), aliases)
        if not nested:
            nested = _citation_source_list(item.get("citations"), aliases)
        if isinstance(nested, list) and nested:
            common = {
                key: value
                for key, value in item.items()
                if key not in {"sources", "citations"}
            }
            merged = [
                {**common, **dict(source)}
                for source in nested
                if isinstance(source, Mapping)
            ]
            expanded.extend(
                _expanded_citation_items(
                    merged,
                    paper_aliases=aliases,
                    depth=depth + 1,
                )
            )
            continue

        paper_ids = item.get(
            "paper_id",
            item.get(
                "paper_ids",
                item.get(
                    "paper_alias",
                    item.get("paper_aliases", item.get("alias")),
                ),
            ),
        )
        evidences = item.get("evidence", item.get("evidences"))
        marker = str(
            item.get("marker")
            or item.get("citation")
            or item.get("reference")
            or ""
        )
        marker_sources = [
            (aliases[alias.upper()], _page_hint(page))
            for alias, page in _PAPER_ALIAS_PATTERN.findall(marker)
            if alias.upper() in aliases
        ]
        if isinstance(paper_ids, str):
            parsed_ids = [
                aliases[alias.upper()]
                for alias in re.findall(
                    r"\bP\d+\b", paper_ids, re.IGNORECASE
                )
                if alias.upper() in aliases
            ] or [
                int(value)
                for value in re.findall(r"(?<![A-Za-z])\d+", paper_ids)
            ]
            if len(parsed_ids) > 1:
                paper_ids = parsed_ids
            elif len(parsed_ids) == 1:
                paper_ids = parsed_ids[0]
        elif isinstance(paper_ids, list):
            paper_ids = [
                _resolved_paper_id(value, aliases)
                for value in paper_ids
            ]
            paper_ids = [value for value in paper_ids if value is not None]
            if len(paper_ids) == 1:
                paper_ids = paper_ids[0]
        if paper_ids is None and marker_sources:
            paper_ids = [paper_id for paper_id, _page in marker_sources]
        if isinstance(paper_ids, list) and len(paper_ids) > 1:
            pages = item.get(
                "page_hint", item.get("page", item.get("pages"))
            )
            if (
                marker_sources
                and len(marker_sources) == len(paper_ids)
                and any(page is not None for _paper_id, page in marker_sources)
            ):
                pages = [page for _paper_id, page in marker_sources]
            if isinstance(pages, str):
                parsed_pages = [
                    int(value) for value in re.findall(r"\d+", pages)
                ]
                if len(parsed_pages) > 1:
                    pages = parsed_pages
            sections = item.get("section")
            split_items: list[dict[str, Any]] = []
            for index, source_paper_id in enumerate(paper_ids):
                if isinstance(evidences, list):
                    evidence = (
                        evidences[index] if index < len(evidences) else ""
                    )
                elif isinstance(evidences, Mapping):
                    alias = next(
                        (
                            name
                            for name, value in aliases.items()
                            if value == int(source_paper_id)
                        ),
                        "",
                    )
                    evidence = evidences.get(
                        alias, evidences.get(alias.lower(), "")
                    )
                else:
                    evidence = evidences
                split_item = dict(item)
                split_item["paper_id"] = source_paper_id
                split_item["evidence"] = evidence
                if isinstance(pages, list) and index < len(pages):
                    split_item["page_hint"] = pages[index]
                if isinstance(sections, list) and index < len(sections):
                    split_item["section"] = sections[index]
                split_items.append(split_item)
            expanded.extend(split_items)
            continue
        expanded.append(item)
    return expanded


def _citations_with_diagnostics(
    raw: Any,
    *,
    limit: int = 16,
    paper_aliases: Mapping[str, int] | None = None,
    visible_markers: list[str] | None = None,
) -> tuple[tuple[Citation, ...], int, int, int]:
    aliases = {
        str(alias).strip().upper(): int(paper_id)
        for alias, paper_id in (paper_aliases or {}).items()
    }
    if isinstance(raw, Mapping):
        raw = _citation_source_list(raw, aliases) or [raw]
    if not isinstance(raw, list):
        return (), 0, 0, 0
    marker_hints = [str(value) for value in (visible_markers or []) if value]
    if marker_hints:
        marker_destinations = [
            (alias.upper(), _page_hint(page))
            for marker in marker_hints
            for alias, page in _PAPER_ALIAS_PATTERN.findall(marker)
            if alias.upper() in aliases
        ]
        identity_names = (
            "paper_id",
            "paper_ids",
            "paper_alias",
            "paper_aliases",
            "alias",
            "marker",
            "citation",
            "reference",
        )
        claimed_aliases: list[str] = []
        identityless_indexes: list[int] = []
        for index, item in enumerate(raw):
            if not isinstance(item, Mapping):
                continue
            identity_values = [item.get(name) for name in identity_names]
            if not any(identity_values):
                identityless_indexes.append(index)
                continue
            for identity in identity_values:
                if isinstance(identity, str):
                    claimed_aliases.extend(
                        alias.upper()
                        for alias in re.findall(
                            r"\bP\d+\b", identity, re.IGNORECASE
                        )
                        if alias.upper() in aliases
                    )
                elif _page_hint(identity) in set(aliases.values()):
                    paper_id = int(identity)
                    claimed_aliases.extend(
                        name for name, value in aliases.items()
                        if value == paper_id
                    )
        unclaimed_destinations = list(marker_destinations)
        for claimed in claimed_aliases:
            for destination_index, destination in enumerate(
                unclaimed_destinations
            ):
                if destination[0] == claimed:
                    unclaimed_destinations.pop(destination_index)
                    break

        prepared: list[Any] = []
        for index, item in enumerate(raw):
            if not isinstance(item, Mapping) or any(
                item.get(name) for name in identity_names
            ):
                prepared.append(item)
                continue
            value = dict(item)
            if (
                len(identityless_indexes) == len(unclaimed_destinations)
                and index in identityless_indexes
            ):
                destination_index = identityless_indexes.index(index)
                alias, page = unclaimed_destinations[destination_index]
                value["paper_alias"] = alias
                if page is not None and not _page_hint(
                    value.get("page_hint", value.get("page"))
                ):
                    value["page_hint"] = page
            elif len(raw) == 1:
                value["marker"] = " ".join(marker_hints)
            elif index < len(marker_hints):
                value["marker"] = marker_hints[index]
            prepared.append(value)
        raw = prepared
    expanded_items = _expanded_citation_items(
        raw, paper_aliases=aliases
    )
    result: list[Citation] = []
    seen: set[tuple[int | None, str, int | None, str]] = set()
    seen_evidence: dict[tuple[int | None, str, int | None], list[str]] = {}
    metadata_claim_count = 0
    invalid_count = 0
    duplicate_count = 0
    for item in expanded_items:
        if isinstance(item, Mapping):
            metadata_claim_count += 1
        value = _citation(item, aliases)
        if value is None:
            invalid_count += 1
            continue
        evidence_key = " ".join(value.evidence.casefold().split())
        claim_key = " ".join(value.claim_text.casefold().split())
        key = (value.paper_id, evidence_key, value.page_hint, claim_key)
        if key in seen:
            duplicate_count += 1
            continue
        if any(
            SequenceMatcher(None, evidence_key, previous).ratio() >= 0.94
            for previous in seen_evidence.get(
                (value.paper_id, claim_key, value.page_hint), []
            )
        ):
            duplicate_count += 1
            continue
        seen.add(key)
        seen_evidence.setdefault(
            (value.paper_id, claim_key, value.page_hint), []
        ).append(evidence_key)
        result.append(value)
        if len(result) >= limit:
            break
    return tuple(result), metadata_claim_count, invalid_count, duplicate_count


def _citations(raw: Any, *, limit: int = 16) -> tuple[Citation, ...]:
    return _citations_with_diagnostics(raw, limit=limit)[0]


def parse_chat_result(
    raw_text: str,
    *,
    paper_aliases: Mapping[str, int] | None = None,
) -> NormalizedAIResult:
    """Remove the invisible metadata trailer and normalize a provider reply."""
    text = str(raw_text or "").strip()
    result_block = _chat_result_block(text)
    if result_block is None:
        content = _VISIBLE_PAPER_CITATION_PATTERN.sub("", text).strip()
        return NormalizedAIResult(
            content,
            "not_found",
            (),
            metadata_valid=False,
            metadata_present=False,
        )
    block_start, block_end, payload = result_block
    content = (text[:block_start] + text[block_end:]).strip()
    visible_markers = [
        marker.group(0)
        for marker in _VISIBLE_PAPER_CITATION_PATTERN.finditer(content)
    ]
    content = _VISIBLE_PAPER_CITATION_PATTERN.sub("", content)
    content = re.sub(r"[ \t]+\n", "\n", content).strip()
    status = _support_status(payload.get("support_status"))
    citations, metadata_claims, invalid, duplicates = _citations_with_diagnostics(
        payload.get("citations"),
        paper_aliases=paper_aliases,
        visible_markers=visible_markers,
    )
    return NormalizedAIResult(
        content=content,
        support_status=status,
        citations=() if status == "not_found" else citations,
        metadata_valid=True,
        metadata_present=True,
        metadata_claim_count=metadata_claims,
        invalid_citation_count=invalid,
        duplicate_citation_count=duplicates,
    )


def _normalized_key(value: object) -> str:
    text = str(value or "").replace("Đ", "D").replace("đ", "d")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(character for character in text if not unicodedata.combining(character))
    return re.sub(r"[^a-z0-9]+", "", text.casefold())


def _canonical_summary_name(value: object) -> str | None:
    normalized = _normalized_key(value)
    for canonical, aliases in _SUMMARY_FIELD_ALIASES.items():
        if normalized in aliases:
            return canonical
    return None


def _mapping_value(raw: Mapping[str, Any], *names: str) -> Any:
    wanted = {_normalized_key(name) for name in names}
    for key, value in raw.items():
        if _normalized_key(key) in wanted:
            return value
    return None


def _summary_fields_from_value(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = _extract_json_object(value)
        except ValueError:
            return {}
    if isinstance(value, list):
        mapped: dict[str, Any] = {}
        for item in value:
            if not isinstance(item, Mapping):
                continue
            name = _mapping_value(item, "name", "key", "field")
            canonical = _canonical_summary_name(name)
            if canonical is not None:
                mapped[canonical] = item
        return mapped
    if not isinstance(value, Mapping):
        return {}

    direct: dict[str, Any] = {}
    for key, field_value in value.items():
        canonical = _canonical_summary_name(key)
        if canonical is not None and canonical not in direct:
            direct[canonical] = field_value
    if len(direct) == len(SUMMARY_FIELDS):
        return direct

    normalized_wrappers = {
        _normalized_key(name) for name in _SUMMARY_WRAPPERS
    }
    for key, nested in value.items():
        if _normalized_key(key) not in normalized_wrappers:
            continue
        nested_fields = _summary_fields_from_value(nested)
        if len(nested_fields) > len(direct):
            direct.update(nested_fields)
        if len(direct) == len(SUMMARY_FIELDS):
            break
    return direct


def _extract_json_object(raw_text: str) -> Mapping[str, Any]:
    text = str(raw_text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL | re.I)
    if fenced:
        text = fenced.group(1)
    decoder = json.JSONDecoder()
    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            value, _end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, Mapping):
            return value
    LOGGER.warning(
        "Could not decode AI summary JSON (response_length=%d)",
        len(text),
    )
    raise ValueError("Could not parse the AI summary response.")


def _legacy_parse_summary_result(raw_text: str, *, provider: str, model: str) -> AISummaryResult:
    payload = _extract_json_object(raw_text)
    raw_fields = _summary_fields_from_value(payload)
    raw_metadata = payload.get("_citation_metadata")
    if not isinstance(raw_metadata, Mapping):
        raw_metadata = {}
    missing = [name for name in SUMMARY_FIELDS if name not in raw_fields]
    if missing:
        LOGGER.warning(
            "AI summary shape did not contain every canonical field "
            "(top_level_keys=%s, normalized_fields=%s, missing=%s)",
            sorted(str(key)[:80] for key in payload.keys()),
            sorted(raw_fields),
            missing,
        )
        raise ValueError("Could not parse the AI summary response.")
    fields: dict[str, SummaryFieldResult] = {}
    for name in SUMMARY_FIELDS:
        raw = raw_fields[name]
        if isinstance(raw, Mapping):
            content_value = _mapping_value(raw, "content", "text", "value", "summary")
            status_value = _mapping_value(raw, "support_status", "supportStatus", "status")
            citation_values = _mapping_value(raw, "citations", "sources", "references")
            content = str(content_value or "").strip()
            status = _support_status(status_value or "supported")
        else:
            content = str(raw or "").strip()
            metadata = raw_metadata.get(name)
            if isinstance(metadata, Mapping):
                status = _support_status(metadata.get("support_status") or "supported")
                citation_values = metadata.get("citations")
            else:
                status = "supported"
                citation_values = []
        if not content:
            LOGGER.warning("AI summary field was empty (field=%s)", name)
            raise ValueError("Could not parse the AI summary response.")
        fields[name] = SummaryFieldResult(
            content=content,
            support_status=status,
            citations=(
                ()
                if status == "not_found"
                else _citations(citation_values, limit=1 if name == "ideas" else 2)
            ),
        )
    return AISummaryResult(fields=fields, provider=provider, model=model)


def decode_summary_payload(raw_value: Any) -> Mapping[str, Any]:
    """Decode exactly one structured provider output without format guessing."""
    if isinstance(raw_value, Mapping):
        return raw_value
    if not isinstance(raw_value, str):
        raise AISummaryFormatError(
            "unexpected_provider_response_shape",
            detail=f"Summary output type was {type(raw_value).__name__}.",
            retryable=False,
        )
    text = raw_value.strip()
    if not text:
        raise AISummaryFormatError("empty_response")
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            text = "\n".join(lines[1:-1]).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        truncated = text.startswith("{") and not text.rstrip().endswith("}")
        raise AISummaryFormatError(
            "truncated_response" if truncated else "json_decode_failed",
            detail=f"JSON decode failed at character {error.pos}.",
            retryable=not truncated,
        ) from None
    if not isinstance(payload, Mapping):
        raise AISummaryFormatError(
            "wrong_top_level_type",
            detail=f"Summary JSON top-level type was {type(payload).__name__}.",
        )
    return payload


def parse_summary_result(raw_value: Any, *, provider: str, model: str) -> AISummaryResult:
    """Validate the strict nine-string-field Summary contract."""
    payload = decode_summary_payload(raw_value)
    field_payload: Mapping[str, Any] = payload
    missing = [name for name in SUMMARY_FIELDS if name not in field_payload]
    if missing:
        for wrapper in ("summary", "fields"):
            nested = payload.get(wrapper)
            if not isinstance(nested, Mapping):
                continue
            field_payload = nested
            missing = [name for name in SUMMARY_FIELDS if name not in field_payload]
            if not missing:
                break
    if missing:
        raise AISummaryFormatError(
            "missing_required_keys",
            detail="Summary did not contain all nine canonical keys.",
            missing_keys=tuple(missing),
        )

    raw_metadata = payload.get("_citation_metadata")
    if not isinstance(raw_metadata, Mapping):
        raw_metadata = field_payload.get("_citation_metadata")
    if not isinstance(raw_metadata, Mapping):
        raw_metadata = {}

    fields: dict[str, SummaryFieldResult] = {}
    for name in SUMMARY_FIELDS:
        raw = field_payload[name]
        if not isinstance(raw, str) or not raw.strip():
            raise AISummaryFormatError(
                "invalid_field_type",
                detail=f"Summary field {name!r} was not a non-empty string.",
            )
        metadata = raw_metadata.get(name)
        if isinstance(metadata, Mapping):
            status = _support_status(metadata.get("support_status") or "supported")
            citation_values = metadata.get("citations")
        else:
            status = "supported"
            citation_values = []
        fields[name] = SummaryFieldResult(
            content=raw.strip(),
            support_status=status,
            citations=(
                ()
                if status == "not_found"
                else _citations(citation_values, limit=1 if name == "ideas" else 2)
            ),
        )
    return AISummaryResult(fields=fields, provider=provider, model=model)


def _normalize_text(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = text.replace("\u00ad", "")
    text = re.sub(r"(?<=\w)-\s*\n\s*(?=\w)", "", text)
    return " ".join(text.casefold().split())


class CitationResolver:
    """Verify short evidence snippets against the local original English PDF."""

    def __init__(self, pdf_path: Path) -> None:
        self.pdf_path = Path(pdf_path)

    @staticmethod
    def _fuzzy_match(evidence: str, page_text: str) -> bool:
        evidence_tokens = evidence.split()
        page_tokens = page_text.split()
        count = len(evidence_tokens)
        if count < 5 or len(page_tokens) < count:
            return False
        best = 0.0
        step = max(1, count // 8)
        for start in range(0, len(page_tokens) - count + 1, step):
            window = page_tokens[start : start + count]
            common = len(set(evidence_tokens).intersection(window)) / max(
                1, len(set(evidence_tokens))
            )
            if common < 0.72:
                continue
            ratio = SequenceMatcher(None, evidence_tokens, window).ratio()
            best = max(best, ratio)
            if best >= 0.86:
                return True
        return False

    @staticmethod
    def _candidate_pages(page_hint: int | None, page_count: int) -> list[int]:
        candidates: list[int] = []
        if page_hint is not None and 1 <= page_hint <= page_count:
            hinted = page_hint - 1
            for offset in (0, -1, 1, -2, 2):
                page = hinted + offset
                if 0 <= page < page_count and page not in candidates:
                    candidates.append(page)
        candidates.extend(page for page in range(page_count) if page not in candidates)
        return candidates

    @staticmethod
    def _page_text(
        document: pymupdf.Document,
        page_index: int,
        normalized_pages: dict[int, str],
    ) -> str:
        if page_index not in normalized_pages:
            normalized_pages[page_index] = _normalize_text(
                document.load_page(page_index).get_text("text")
            )
        return normalized_pages[page_index]

    def resolve(
        self,
        citation: Citation,
        document: pymupdf.Document,
        normalized_pages: dict[int, str] | None = None,
    ) -> Citation:
        evidence = _normalize_text(citation.evidence)
        if len(evidence) < 12:
            return replace(citation, verification_status="invalid_evidence")
        pages = self._candidate_pages(citation.page_hint, document.page_count)
        page_cache = normalized_pages if normalized_pages is not None else {}
        has_valid_hint = (
            citation.page_hint is not None
            and 1 <= citation.page_hint <= document.page_count
        )
        nearby_count = min(5, len(pages)) if has_valid_hint else 0
        page_groups = (
            (pages[:nearby_count], pages[nearby_count:])
            if nearby_count
            else (pages,)
        )
        for group in page_groups:
            if not group:
                continue
            for page_index in group:
                page_text = self._page_text(document, page_index, page_cache)
                if evidence in page_text:
                    return replace(
                        citation,
                        resolved_page=page_index + 1,
                        verified=True,
                        verification_status="exact",
                    )
            for page_index in group:
                if self._fuzzy_match(evidence, page_cache[page_index]):
                    return replace(
                        citation,
                        resolved_page=page_index + 1,
                        verified=True,
                        verification_status="fuzzy",
                    )
        return replace(citation, verification_status="not_found")

    def resolve_all(self, citations: tuple[Citation, ...]) -> tuple[Citation, ...]:
        if not citations:
            return ()
        if not self.pdf_path.is_file():
            raise FileNotFoundError(f"The original English PDF no longer exists:\n{self.pdf_path}")
        with pymupdf.open(self.pdf_path) as document:
            normalized_pages: dict[int, str] = {}
            resolved_cache: dict[tuple[str, int | None], Citation] = {}
            resolved: list[Citation] = []
            for citation in citations:
                key = (_normalize_text(citation.evidence), citation.page_hint)
                cached = resolved_cache.get(key)
                if cached is None:
                    cached = self.resolve(citation, document, normalized_pages)
                    resolved_cache[key] = cached
                resolved.append(
                    replace(
                        citation,
                        resolved_page=cached.resolved_page,
                        verified=cached.verified,
                        verification_status=cached.verification_status,
                    )
                )
            return tuple(resolved)

    def resolve_result(self, result: NormalizedAIResult) -> NormalizedAIResult:
        return replace(result, citations=self.resolve_all(result.citations))

    def resolve_summary(self, result: AISummaryResult) -> AISummaryResult:
        all_citations = tuple(
            citation
            for field in result.fields.values()
            for citation in field.citations
        )
        resolved = iter(self.resolve_all(all_citations))
        fields = {
            name: replace(field, citations=tuple(next(resolved) for _ in field.citations))
            for name, field in result.fields.items()
        }
        return replace(result, fields=fields)
