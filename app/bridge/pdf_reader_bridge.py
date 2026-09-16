from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from typing import Any

from PySide6.QtCore import QObject, Signal, Slot


HIGHLIGHT_COLORS = frozenset(
    {"yellow", "blue", "green", "red", "orange", "purple"}
)
MAX_BRIDGE_PAYLOAD_BYTES = 256_000
MAX_SELECTED_TEXT_LENGTH = 50_000
MAX_SEGMENTS = 16
MAX_RECTS_PER_SEGMENT = 512


class BridgePayloadError(ValueError):
    """Raised when untrusted viewer data does not match the bridge contract."""


def _default_highlight_repository() -> Any:
    from app.database.highlight_repository import HighlightRepository

    return HighlightRepository


def _default_note_repository() -> Any:
    from app.database.note_repository import NoteRepository

    return NoteRepository


def _default_anchor_repository() -> Any:
    from app.database.annotation_anchor_repository import AnnotationAnchorRepository

    return AnnotationAnchorRepository


def _row_mapping(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, Mapping):
        return dict(row)
    if is_dataclass(row):
        return asdict(row)
    keys = getattr(row, "keys", None)
    if callable(keys):
        return {key: row[key] for key in keys()}
    values = getattr(row, "__dict__", None)
    return dict(values) if isinstance(values, dict) else {}


def _json_message(**values: Any) -> str:
    return json.dumps(values, ensure_ascii=True, separators=(",", ":"))


def _parse_object(raw_payload: str) -> dict[str, Any]:
    if not isinstance(raw_payload, str):
        raise BridgePayloadError("The bridge payload must be JSON text.")
    if len(raw_payload.encode("utf-8")) > MAX_BRIDGE_PAYLOAD_BYTES:
        raise BridgePayloadError("The bridge payload is too large.")
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError as error:
        raise BridgePayloadError("The bridge payload is not valid JSON.") from error
    if not isinstance(payload, dict):
        raise BridgePayloadError("The bridge payload must be an object.")
    return payload


def _safe_request_id(payload: Mapping[str, Any]) -> str:
    value = payload.get("requestId", "")
    return value if isinstance(value, str) and len(value) <= 128 else ""


def _request_id_from_raw(raw_payload: str) -> str:
    """Recover a safe request id even when the rest of a payload is invalid."""
    try:
        return _safe_request_id(_parse_object(raw_payload))
    except BridgePayloadError:
        return ""


def _finite_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BridgePayloadError(f"{field} must be numeric.")
    number = float(value)
    if not math.isfinite(number) or abs(number) > 10_000_000:
        raise BridgePayloadError(f"{field} is outside the allowed range.")
    return number


def _anchor_endpoint(value: Any, field: str) -> dict[str, int]:
    if not isinstance(value, dict):
        raise BridgePayloadError(f"{field} must be an object.")
    item = value.get("item")
    offset = value.get("offset")
    if (
        isinstance(item, bool)
        or not isinstance(item, int)
        or item < 0
        or item > 10_000_000
        or isinstance(offset, bool)
        or not isinstance(offset, int)
        or offset < 0
        or offset > 10_000_000
    ):
        raise BridgePayloadError(f"{field} has an invalid item or offset.")
    return {"item": item, "offset": offset}


def validate_location(
    value: Any,
    *,
    page_count: int = 0,
    fingerprint: str = "",
    require_anchors: bool = False,
) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("version") != 1:
        raise BridgePayloadError("Unsupported highlight location format.")

    document_fingerprint = value.get("fingerprint", "")
    if not isinstance(document_fingerprint, str) or len(document_fingerprint) > 256:
        raise BridgePayloadError("Invalid document fingerprint.")
    if fingerprint and document_fingerprint != fingerprint:
        raise BridgePayloadError("The selection belongs to a different document.")

    raw_segments = value.get("segments")
    if (
        not isinstance(raw_segments, list)
        or not raw_segments
        or len(raw_segments) > MAX_SEGMENTS
    ):
        raise BridgePayloadError("A selection must contain valid page segments.")

    segments: list[dict[str, Any]] = []
    for segment_index, raw_segment in enumerate(raw_segments):
        if not isinstance(raw_segment, dict):
            raise BridgePayloadError("A location segment must be an object.")
        page = raw_segment.get("page")
        if isinstance(page, bool) or not isinstance(page, int) or page < 1:
            raise BridgePayloadError("A location segment has an invalid page.")
        if page_count and page > page_count:
            raise BridgePayloadError("A location segment is outside the document.")

        is_page_only = not any(
            key in raw_segment
            for key in ("start", "end", "exact", "prefix", "suffix", "pdfRects")
        )
        if is_page_only and not require_anchors:
            segments.append({"page": page})
            continue
        if is_page_only:
            raise BridgePayloadError("A selection location requires text anchors.")

        exact = raw_segment.get("exact", "")
        prefix = raw_segment.get("prefix", "")
        suffix = raw_segment.get("suffix", "")
        if not isinstance(exact, str) or len(exact) > MAX_SELECTED_TEXT_LENGTH:
            raise BridgePayloadError("A location segment has invalid text.")
        if not isinstance(prefix, str) or len(prefix) > 256:
            raise BridgePayloadError("A location segment has invalid prefix context.")
        if not isinstance(suffix, str) or len(suffix) > 256:
            raise BridgePayloadError("A location segment has invalid suffix context.")

        raw_rects = raw_segment.get("pdfRects")
        if (
            not isinstance(raw_rects, list)
            or not raw_rects
            or len(raw_rects) > MAX_RECTS_PER_SEGMENT
        ):
            raise BridgePayloadError("A location segment has invalid rectangles.")
        rects: list[list[float]] = []
        for rect_index, raw_rect in enumerate(raw_rects):
            if not isinstance(raw_rect, list) or len(raw_rect) != 4:
                raise BridgePayloadError("A highlight rectangle must have four values.")
            rects.append(
                [
                    _finite_number(number, f"segments[{segment_index}].pdfRects[{rect_index}]")
                    for number in raw_rect
                ]
            )

        segments.append(
            {
                "page": page,
                "start": _anchor_endpoint(raw_segment.get("start"), "segment.start"),
                "end": _anchor_endpoint(raw_segment.get("end"), "segment.end"),
                "exact": exact,
                "prefix": prefix,
                "suffix": suffix,
                "pdfRects": rects,
            }
        )

    return {
        "version": 1,
        "fingerprint": document_fingerprint,
        "segments": segments,
    }


def validate_selection_payload(
    raw_payload: str,
    *,
    page_count: int = 0,
    fingerprint: str = "",
    require_color: bool = False,
) -> dict[str, Any]:
    payload = _parse_object(raw_payload)
    selected_text = payload.get("selectedText", "")
    if not isinstance(selected_text, str):
        raise BridgePayloadError("Selected text must be a string.")
    selected_text = selected_text.strip()
    if not selected_text or len(selected_text) > MAX_SELECTED_TEXT_LENGTH:
        raise BridgePayloadError("Selected text is empty or too long.")

    color = payload.get("color", "yellow")
    if require_color and color not in HIGHLIGHT_COLORS:
        raise BridgePayloadError("Unsupported highlight color.")
    if color not in HIGHLIGHT_COLORS:
        color = "yellow"

    return {
        "requestId": _safe_request_id(payload),
        "selectedText": selected_text,
        "color": color,
        "location": validate_location(
            payload.get("location"),
            page_count=page_count,
            fingerprint=fingerprint,
            require_anchors=True,
        ),
    }


class PdfReaderBridge(QObject):
    """Per-reader, paper-scoped QWebChannel endpoint for PDF.js actions."""

    highlightsSnapshot = Signal(str)
    highlightUpserted = Signal(str)
    highlightDeleted = Signal(int)
    notesSnapshot = Signal(str)
    navigateRequested = Signal(str)
    operationResult = Signal(str)

    selectionNoteRequested = Signal(str)
    noteOpenRequested = Signal(int)
    translationRequested = Signal(str)
    copyRequested = Signal(str)
    askAIRequested = Signal(str)
    transientSelectionCancelled = Signal(str)
    clientConnected = Signal(str)
    readingInteraction = Signal(str)

    def __init__(
        self,
        paper_id: int,
        parent: QObject | None = None,
        *,
        highlight_repository: Any = None,
        note_repository: Any = None,
        anchor_repository: Any = None,
        document_version: str = "en",
    ) -> None:
        super().__init__(parent)
        self.paper_id = int(paper_id)
        self.highlight_repository = (
            highlight_repository
            if highlight_repository is not None
            else _default_highlight_repository()
        )
        self.note_repository = (
            note_repository if note_repository is not None else _default_note_repository()
        )
        self.anchor_repository = (
            anchor_repository
            if anchor_repository is not None
            else _default_anchor_repository()
        )
        self.document_version = "en"
        self.set_document_version(document_version)
        self.page_count = 0
        self.fingerprint = ""

    def _result(
        self,
        action: str,
        request_id: str,
        *,
        ok: bool,
        error: str = "",
        **values: Any,
    ) -> None:
        self.operationResult.emit(
            _json_message(
                action=action,
                requestId=request_id,
                ok=ok,
                error=error,
                **values,
            )
        )

    def report_action_result(
        self,
        action: str,
        request_id: str,
        *,
        ok: bool,
        error: str = "",
        **values: Any,
    ) -> None:
        """Complete an action handled by a native Reader panel."""
        self._result(
            action,
            request_id if len(request_id) <= 128 else "",
            ok=ok,
            error=error,
            **values,
        )

    def set_document_version(self, document_version: str) -> None:
        value = str(document_version).strip().lower()
        if value not in {"en", "vi"}:
            raise ValueError(f"Unsupported document version: {document_version}")
        self.document_version = value
        self.page_count = 0
        self.fingerprint = ""

    def _record(self, row: Any, anchor: Any = None) -> dict[str, Any] | None:
        record = _row_mapping(row)
        if str(record.get("document_version") or "en") != self.document_version:
            return None
        anchor_record = _row_mapping(anchor)
        if anchor_record:
            record.update(
                selected_text=anchor_record.get("selected_text", ""),
                page_number=anchor_record.get("page_number", 1),
                location_data=anchor_record.get("location_data", "{}"),
            )
        else:
            return None
        location_data = record.get("location_data", record.get("location_json", "{}"))
        if isinstance(location_data, str):
            try:
                location = json.loads(location_data)
            except json.JSONDecodeError:
                location = {}
        else:
            location = location_data if isinstance(location_data, dict) else {}
        return {
            "id": int(record.get("id", 0)),
            "selectedText": str(record.get("selected_text", "")),
            "pageNumber": int(record.get("page_number", 1)),
            "color": str(record.get("color", "yellow")),
            "location": location,
            "noteId": record.get("note_id"),
            "anchorVersion": int(record.get("anchor_version", 1)),
            "createdAt": str(record.get("created_at", "")),
            "updatedAt": str(record.get("updated_at", "")),
        }

    def _note_record(self, row: Any, anchor: Any = None) -> dict[str, Any] | None:
        record = _row_mapping(row)
        try:
            paper_id = int(record.get("paper_id", -1))
        except (TypeError, ValueError):
            return None
        if not record or paper_id != self.paper_id:
            return None
        kind = str(record.get("kind", ""))
        if kind not in {"manual", "selection", "translation"}:
            return None
        if str(record.get("document_version") or "en") != self.document_version:
            return None

        anchor_record = _row_mapping(anchor)
        if anchor_record:
            record.update(
                source_text=anchor_record.get("selected_text", ""),
                page_number=anchor_record.get("page_number"),
                location_data=anchor_record.get("location_data"),
            )
        else:
            return None
        location_data = record.get("location_data")
        if isinstance(location_data, str):
            try:
                location_value = json.loads(location_data)
            except json.JSONDecodeError:
                return None
        elif isinstance(location_data, dict):
            location_value = location_data
        else:
            return None

        try:
            location = validate_location(
                location_value,
                page_count=self.page_count,
                fingerprint=self.fingerprint,
                require_anchors=True,
            )
            note_id = int(record.get("id", 0))
        except (BridgePayloadError, TypeError, ValueError):
            return None
        if note_id < 1:
            return None

        return {
            "id": note_id,
            "kind": kind,
            "title": str(record.get("title") or ""),
            "sourceText": str(record.get("source_text") or ""),
            "pageNumber": int(location["segments"][0]["page"]),
            "location": location,
            "createdAt": str(record.get("created_at", "")),
            "updatedAt": str(record.get("updated_at", "")),
        }

    def _owned_highlight(self, highlight_id: int) -> Any:
        row = self.highlight_repository.get(int(highlight_id))
        record = _row_mapping(row)
        if not record or int(record.get("paper_id", -1)) != self.paper_id:
            raise BridgePayloadError("This highlight does not belong to the open paper.")
        if str(record.get("document_version") or "en") != self.document_version:
            raise BridgePayloadError(
                "This highlight belongs to another document version."
            )
        return row

    def _owned_note(self, note_id: int) -> Any:
        if isinstance(note_id, bool) or not isinstance(note_id, int) or note_id < 1:
            raise BridgePayloadError("Invalid note identifier.")
        row = self.note_repository.get(note_id)
        record = _row_mapping(row)
        try:
            paper_id = int(record.get("paper_id", -1))
        except (TypeError, ValueError) as error:
            raise BridgePayloadError(
                "This note does not belong to the open paper."
            ) from error
        if not record or paper_id != self.paper_id:
            raise BridgePayloadError("This note does not belong to the open paper.")
        if str(record.get("document_version") or "en") != self.document_version:
            raise BridgePayloadError("This note belongs to another document version.")
        return row

    def publish_highlights(self) -> None:
        try:
            rows = self.highlight_repository.list_for_paper(self.paper_id)
            anchors = {
                int(row["highlight_id"]): row
                for row in self.anchor_repository.list_for_paper(
                    self.paper_id, self.document_version
                )
                if row["highlight_id"] is not None
            }
            records = []
            for row in rows:
                record = self._record(row, anchors.get(int(row["id"])))
                if record is not None:
                    records.append(record)
        except (sqlite3.Error, OSError, ValueError) as error:
            self.highlightsSnapshot.emit("[]")
            self._result("loadHighlights", "", ok=False, error=str(error))
            return
        self.highlightsSnapshot.emit(
            json.dumps(records, ensure_ascii=True, separators=(",", ":"))
        )

    def publish_notes(self) -> None:
        """Publish only located notes owned by the open paper/document."""
        try:
            rows = self.note_repository.list_for_paper(self.paper_id)
            anchors = {
                int(row["note_id"]): row
                for row in self.anchor_repository.list_for_paper(
                    self.paper_id, self.document_version
                )
                if row["note_id"] is not None
            }
            records = []
            for row in rows:
                record = self._note_record(row, anchors.get(int(row["id"])))
                if record is not None:
                    records.append(record)
        except (sqlite3.Error, OSError, ValueError) as error:
            self.notesSnapshot.emit("[]")
            self._result("loadNotes", "", ok=False, error=str(error))
            return
        self.notesSnapshot.emit(
            json.dumps(records, ensure_ascii=True, separators=(",", ":"))
        )

    def annotation_row(self, row: Any, *, kind: str) -> dict[str, Any]:
        """Return a sidebar row with the anchor for the active PDF version."""
        record = _row_mapping(row)
        if str(record.get("document_version") or "en") != self.document_version:
            raise ValueError("This annotation belongs to another document version.")
        identifier = int(record.get("id", 0))
        if kind == "note":
            anchor = self.anchor_repository.get_for_note(
                identifier, self.document_version
            )
        elif kind == "highlight":
            anchor = self.anchor_repository.get_for_highlight(
                identifier, self.document_version
            )
        else:
            raise ValueError(f"Unsupported annotation kind: {kind}")
        anchor_record = _row_mapping(anchor)
        if anchor_record:
            record["source_text" if kind == "note" else "selected_text"] = (
                anchor_record.get("selected_text") or ""
            )
            record["page_number"] = anchor_record.get("page_number")
            record["location_data"] = anchor_record.get("location_data")
        else:
            record["page_number"] = None
            record["location_data"] = None
        return record

    @Slot(str)
    def clientReady(self, raw_payload: str) -> None:
        try:
            payload = _parse_object(raw_payload)
            pages = payload.get("pages", 0)
            fingerprint = payload.get("fingerprint", "")
            if isinstance(pages, bool) or not isinstance(pages, int) or pages < 1:
                raise BridgePayloadError("The viewer reported an invalid page count.")
            if not isinstance(fingerprint, str) or not fingerprint or len(fingerprint) > 256:
                raise BridgePayloadError("The viewer reported an invalid fingerprint.")
        except BridgePayloadError as error:
            self._result("clientReady", "", ok=False, error=str(error))
            return

        self.page_count = pages
        self.fingerprint = fingerprint
        self.clientConnected.emit(raw_payload)
        self.publish_highlights()
        self.publish_notes()
        self._result("clientReady", "", ok=True)

    @Slot(str)
    def createHighlight(self, raw_payload: str) -> None:
        request_id = _request_id_from_raw(raw_payload)
        try:
            payload = validate_selection_payload(
                raw_payload,
                page_count=self.page_count,
                fingerprint=self.fingerprint,
                require_color=True,
            )
            request_id = payload["requestId"]
            location_json = json.dumps(
                payload["location"], ensure_ascii=True, separators=(",", ":")
            )
            highlight_id = self.highlight_repository.create(
                self.paper_id,
                payload["selectedText"],
                payload["location"]["segments"][0]["page"],
                location_json,
                payload["color"],
                anchor_version=1,
                document_version=self.document_version,
            )
            row = self.highlight_repository.get(highlight_id)
            anchor = self.anchor_repository.get_for_highlight(
                highlight_id, self.document_version
            )
            record = self._record(row, anchor) if row is not None else {
                "id": int(highlight_id),
                "selectedText": payload["selectedText"],
                "pageNumber": payload["location"]["segments"][0]["page"],
                "color": payload["color"],
                "location": payload["location"],
                "noteId": None,
                "anchorVersion": 1,
                "createdAt": "",
                "updatedAt": "",
            }
        except (BridgePayloadError, sqlite3.Error, OSError, ValueError) as error:
            self._result("createHighlight", request_id, ok=False, error=str(error))
            return

        self.highlightUpserted.emit(
            json.dumps(record, ensure_ascii=True, separators=(",", ":"))
        )
        self._result(
            "createHighlight", request_id, ok=True, highlightId=record["id"]
        )

    @Slot(str)
    def createSelectionNote(self, raw_payload: str) -> None:
        request_id = _request_id_from_raw(raw_payload)
        try:
            payload = validate_selection_payload(
                raw_payload,
                page_count=self.page_count,
                fingerprint=self.fingerprint,
            )
        except BridgePayloadError as error:
            self._result(
                "createSelectionNote", request_id, ok=False, error=str(error)
            )
            return
        self.selectionNoteRequested.emit(
            json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
        )

    @Slot(str)
    def translateSelection(self, raw_payload: str) -> None:
        request_id = _request_id_from_raw(raw_payload)
        try:
            payload = validate_selection_payload(
                raw_payload,
                page_count=self.page_count,
                fingerprint=self.fingerprint,
            )
        except BridgePayloadError as error:
            self._result("translateSelection", request_id, ok=False, error=str(error))
            return
        self.translationRequested.emit(
            json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
        )

    @Slot(str)
    def copySelection(self, raw_payload: str) -> None:
        request_id = _request_id_from_raw(raw_payload)
        try:
            payload = validate_selection_payload(
                raw_payload,
                page_count=self.page_count,
                fingerprint=self.fingerprint,
            )
        except BridgePayloadError as error:
            self._result("copySelection", request_id, ok=False, error=str(error))
            return
        self.copyRequested.emit(
            json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
        )

    @Slot(str)
    def askAISelection(self, raw_payload: str) -> None:
        """Attach a validated selection to the native composer without sending it."""
        request_id = _request_id_from_raw(raw_payload)
        try:
            payload = validate_selection_payload(
                raw_payload,
                page_count=self.page_count,
                fingerprint=self.fingerprint,
            )
        except BridgePayloadError as error:
            self._result("askAISelection", request_id, ok=False, error=str(error))
            return
        self.askAIRequested.emit(
            json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
        )
        self._result("askAISelection", payload["requestId"], ok=True)

    @Slot(str)
    def cancelTransientSelection(self, request_id: str) -> None:
        safe_id = request_id if isinstance(request_id, str) and len(request_id) <= 128 else ""
        self.transientSelectionCancelled.emit(safe_id)

    @Slot(str)
    def recordReadingInteraction(self, kind: str) -> None:
        value = str(kind).strip().lower()
        if value in {"page", "scroll", "search", "selection", "annotation"}:
            self.readingInteraction.emit(value)

    @Slot(int)
    def openNote(self, note_id: int) -> None:
        try:
            self._owned_note(note_id)
        except (BridgePayloadError, sqlite3.Error, OSError, ValueError) as error:
            self._result("openNote", "", ok=False, error=str(error))
            return
        self.noteOpenRequested.emit(note_id)
        self._result("openNote", "", ok=True, noteId=note_id)

    def navigate_to(self, location: Mapping[str, Any]) -> None:
        validated = validate_location(
            dict(location),
            page_count=self.page_count,
            fingerprint=self.fingerprint,
        )
        self.navigateRequested.emit(
            json.dumps(validated, ensure_ascii=True, separators=(",", ":"))
        )

    def update_highlight_color(self, highlight_id: int, color: str) -> bool:
        if color not in HIGHLIGHT_COLORS:
            raise BridgePayloadError("Unsupported highlight color.")
        self._owned_highlight(highlight_id)
        changed = bool(self.highlight_repository.update_color(highlight_id, color))
        if changed:
            self.highlightUpserted.emit(
                json.dumps(
                    self._record(
                        self.highlight_repository.get(highlight_id),
                        self.anchor_repository.get_for_highlight(
                            highlight_id, self.document_version
                        ),
                    ),
                    ensure_ascii=True,
                    separators=(",", ":"),
                )
            )
        return changed

    def delete_highlight(self, highlight_id: int) -> bool:
        self._owned_highlight(highlight_id)
        deleted = bool(self.highlight_repository.delete(highlight_id))
        if deleted:
            self.highlightDeleted.emit(int(highlight_id))
        return deleted

    def attach_note(self, highlight_id: int, note_id: int | None) -> bool:
        self._owned_highlight(highlight_id)
        if note_id is not None:
            note = _row_mapping(self.note_repository.get(int(note_id)))
            if not note or int(note.get("paper_id", -1)) != self.paper_id:
                raise BridgePayloadError("This note does not belong to the open paper.")
            if str(note.get("document_version") or "en") != self.document_version:
                raise BridgePayloadError("This note belongs to another document version.")
        changed = bool(self.highlight_repository.attach_note(highlight_id, note_id))
        if changed:
            self.highlightUpserted.emit(
                json.dumps(
                    self._record(
                        self.highlight_repository.get(highlight_id),
                        self.anchor_repository.get_for_highlight(
                            highlight_id, self.document_version
                        ),
                    ),
                    ensure_ascii=True,
                    separators=(",", ":"),
                )
            )
        return changed
