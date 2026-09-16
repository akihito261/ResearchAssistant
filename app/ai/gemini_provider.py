from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Callable

from app.ai.base import (
    AIDocumentUnavailableError,
    AIError,
    AIProvider,
    AIReply,
    AISummaryFormatError,
    RemoteDocument,
    SYSTEM_INSTRUCTION,
    classify_provider_error,
    document_source_heading,
)


LOGGER = logging.getLogger(__name__)


_SUMMARY_FIELDS = (
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

_SUMMARY_SYSTEM_INSTRUCTION = """Summarize only the attached research paper.
Return the requested structured result, grounded in the paper. Do not use chat
history or outside knowledge. Do not invent facts or evidence."""


def _summary_response_schema() -> dict[str, Any]:
    citation = {
        "type": "object",
        "properties": {
            "claim": {"type": "string"},
            "page_hint": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
            "section": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "evidence": {"type": "string"},
        },
        "required": ["claim", "page_hint", "section", "evidence"],
        "additionalProperties": False,
    }
    metadata_properties = {
        field: {
            "type": "object",
            "properties": {
                "support_status": {
                    "type": "string",
                    "enum": ["supported", "partially_supported", "not_found"],
                },
                "citations": {
                    "type": "array",
                    "items": citation,
                    "maxItems": 1,
                },
            },
            "required": ["support_status", "citations"],
            "additionalProperties": False,
        }
        for field in _SUMMARY_FIELDS
    }
    properties: dict[str, Any] = {
        field: {"type": "string"} for field in _SUMMARY_FIELDS
    }
    properties["_citation_metadata"] = {
        "type": "object",
        "properties": metadata_properties,
        "required": list(_SUMMARY_FIELDS),
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": properties,
        # The canonical contract has exactly nine required string fields.
        # Citation metadata remains an optional auxiliary object so an absent
        # citation never invalidates an otherwise complete Summary.
        "required": list(_SUMMARY_FIELDS),
        "additionalProperties": False,
    }


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    text = str(value).strip()
    return text or None


def _log_gemini_api_error(operation: str, error: Exception) -> None:
    """Record the original Gemini SDK error before user-facing classification."""
    response = getattr(error, "response", None)
    http_status = getattr(response, "status_code", None)
    details = getattr(error, "details", None)
    error_details = details.get("error", details) if isinstance(details, Mapping) else {}

    google_code = getattr(error, "code", None)
    google_status = getattr(error, "status", None)
    google_message = getattr(error, "message", None)
    if isinstance(error_details, Mapping):
        google_code = error_details.get("code", google_code)
        google_status = error_details.get("status", google_status)
        google_message = error_details.get("message", google_message)
    if http_status is None:
        # google.genai APIError.code is the HTTP status when the SDK response
        # object is unavailable (for example after exception serialization).
        http_status = getattr(error, "code", None)
    if not google_message:
        google_message = str(error)

    LOGGER.error(
        "Gemini API error during %s\n"
        "HTTP status code: %s\n"
        "Google error status: %s\n"
        "Google error code: %s\n"
        "Google error message: %s\n"
        "Exception repr: %r",
        operation,
        http_status,
        google_status,
        google_code,
        google_message,
        error,
        exc_info=(type(error), error, error.__traceback__),
    )


def _is_inaccessible_gemini_file_error(error: Exception) -> bool:
    """Identify a stale File handle without treating all Gemini 403s as stale."""
    code = getattr(error, "code", None)
    if code is None:
        code = getattr(getattr(error, "response", None), "status_code", None)
    try:
        code = int(code) if code is not None else None
    except (TypeError, ValueError):
        code = None
    details = getattr(error, "details", None)
    message = " ".join(
        str(value or "")
        for value in (
            getattr(error, "status", None),
            getattr(error, "message", None),
            details,
            error,
        )
    ).casefold()
    mentions_file = "file" in message or "files/" in message
    inaccessible = any(
        phrase in message
        for phrase in (
            "do not have permission to access the file",
            "permission to access the file",
            "file may not exist",
            "file might not exist",
            "file does not exist",
            "file not found",
        )
    )
    return inaccessible or (code in {403, 404} and mentions_file)


class GeminiProvider(AIProvider):
    name = "gemini"

    def __init__(self, api_key: str) -> None:
        from google import genai

        self.document_cache_identity = hashlib.sha256(
            api_key.encode("utf-8")
        ).hexdigest()
        self.client = genai.Client(api_key=api_key)

    def list_models(self) -> list[str]:
        try:
            names = {
                str(model.name).removeprefix("models/")
                for model in self.client.models.list()
                if getattr(model, "name", None) and self._is_chat_model(model)
            }
            return sorted(names)
        except Exception as error:
            _log_gemini_api_error("list_models", error)
            raise classify_provider_error(error) from None

    @staticmethod
    def _is_chat_model(model: Any) -> bool:
        actions = {
            str(action).lower().replace("_", "").replace("-", "")
            for action in (getattr(model, "supported_actions", None) or ())
        }
        if not any("generatecontent" in action for action in actions):
            return False

        # supported_actions is the source of truth. Metadata text only removes
        # specialized generateContent endpoints that are not normal text chat.
        labels = getattr(model, "labels", None) or {}
        metadata = " ".join(
            (
                str(getattr(model, "name", "") or ""),
                str(getattr(model, "display_name", "") or ""),
                str(getattr(model, "description", "") or ""),
                " ".join(f"{key} {value}" for key, value in labels.items()),
            )
        ).lower()
        excluded = (
            "antigravity",
            "embedding",
            "image generation",
            "image-only",
            "image-preview",
            "imagen",
            "nano banana",
            "text-to-speech",
            "text to speech",
            "tts",
            "video generation",
            "veo",
            "robotics",
            "computer use",
            "live api",
            "deep-research",
            "native-audio",
            "live-preview",
            "live-translate",
            "gemini-live",
            "lyria",
            "gemma",
            "omni",
        )
        return not any(term in metadata for term in excluded)

    def test_connection(self) -> tuple[str, list[str]]:
        models = self.list_models()
        return f"Connected to Gemini ({len(models)} models available).", models

    def prepare_document(self, pdf_path: Path) -> RemoteDocument:
        try:
            uploaded = self.client.files.upload(
                file=pdf_path,
                config={"mime_type": "application/pdf", "display_name": pdf_path.name},
            )
            file_id = str(uploaded.name or "")
            uri = str(uploaded.uri or "")
            if not file_id or not uri:
                raise AIDocumentUnavailableError(
                    "Gemini did not return a usable PDF reference."
                )
            return RemoteDocument(
                file_id=file_id,
                uri=uri,
                mime_type=str(uploaded.mime_type or "application/pdf"),
                expires_at=_iso(getattr(uploaded, "expiration_time", None)),
            )
        except AIDocumentUnavailableError:
            raise
        except Exception as error:
            _log_gemini_api_error("prepare_document", error)
            raise classify_provider_error(error) from None

    def validate_document(self, document: RemoteDocument) -> bool:
        try:
            remote = self.client.files.get(name=document.file_id)
            state = str(getattr(remote, "state", "") or "").upper()
            return bool(getattr(remote, "uri", None)) and "FAILED" not in state
        except Exception as error:
            _log_gemini_api_error("validate_document", error)
            if _is_inaccessible_gemini_file_error(error):
                return False
            normalized = classify_provider_error(error)
            if isinstance(normalized, AIDocumentUnavailableError):
                return False
            raise normalized from None

    def send_message(
        self,
        *,
        model: str,
        message: str,
        document: RemoteDocument | list[RemoteDocument],
        previous_state_id: str | None,
        local_history: list[dict[str, Any]],
    ) -> AIReply:
        try:
            from google.genai import types

            response = self.client.models.generate_content(
                model=model,
                contents=self._contents(message, document, local_history),
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_INSTRUCTION,
                    max_output_tokens=8192,
                ),
            )
            text = str(getattr(response, "text", "") or "").strip()
            if not text:
                raise RuntimeError("Gemini returned an empty response.")
            return AIReply(text=text, remote_state_id="")
        except Exception as error:
            _log_gemini_api_error("send_message", error)
            if _is_inaccessible_gemini_file_error(error):
                raise AIDocumentUnavailableError(
                    "The cached Gemini PDF is not accessible with the current credentials."
                ) from None
            raise classify_provider_error(error) from None

    def send_text_message(
        self,
        *,
        model: str,
        message: str,
        local_history: list[dict[str, Any]],
        system_instruction: str,
    ) -> AIReply:
        try:
            from google.genai import types

            contents: list[Any] = []
            for item in local_history:
                text = str(item.get("content") or "").strip()
                if text:
                    contents.append(
                        types.Content(
                            role="model" if item.get("role") == "assistant" else "user",
                            parts=[types.Part.from_text(text=text)],
                        )
                    )
            contents.append(
                types.Content(role="user", parts=[types.Part.from_text(text=message)])
            )
            response = self.client.models.generate_content(
                model=model,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    max_output_tokens=8192,
                ),
            )
            text = str(getattr(response, "text", "") or "").strip()
            if not text:
                raise RuntimeError("Gemini returned an empty response.")
            return AIReply(text=text, remote_state_id="")
        except Exception as error:
            _log_gemini_api_error("send_text_message", error)
            raise classify_provider_error(error) from None

    @staticmethod
    def _contents(
        message: str,
        document: RemoteDocument | list[RemoteDocument],
        local_history: list[dict[str, Any]],
    ) -> list[Any]:
        from google.genai import types

        contents: list[Any] = []
        for item in local_history:
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            role = "model" if item.get("role") == "assistant" else "user"
            contents.append(
                types.Content(role=role, parts=[types.Part.from_text(text=content)])
            )
        documents = document if isinstance(document, list) else [document]
        document_parts: list[Any] = []
        for index, item in enumerate(documents, start=1):
            document_parts.extend(
                (
                    types.Part.from_text(
                        text=document_source_heading(item, index)
                    ),
                    types.Part.from_uri(
                        file_uri=str(item.uri or ""),
                        mime_type=item.mime_type,
                    ),
                )
            )
        contents.append(
            types.Content(
                role="user",
                parts=[*document_parts, types.Part.from_text(text=message)],
            )
        )
        return contents

    def stream_message(
        self,
        *,
        model: str,
        message: str,
        document: RemoteDocument | list[RemoteDocument],
        previous_state_id: str | None,
        local_history: list[dict[str, Any]],
        on_chunk: Callable[[str], None],
        cancel_event: Any,
    ) -> AIReply:
        try:
            from google.genai import types

            stream = self.client.models.generate_content_stream(
                model=model,
                contents=self._contents(message, document, local_history),
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_INSTRUCTION,
                    max_output_tokens=8192,
                ),
            )
            if hasattr(cancel_event, "register_closer") and hasattr(stream, "close"):
                cancel_event.register_closer(stream.close)
            chunks: list[str] = []
            try:
                for response in stream:
                    if cancel_event.is_set():
                        break
                    delta = str(getattr(response, "text", "") or "")
                    if delta:
                        chunks.append(delta)
                        on_chunk(delta)
            finally:
                if cancel_event.is_set() and hasattr(stream, "close"):
                    stream.close()
                if hasattr(cancel_event, "clear_closer"):
                    cancel_event.clear_closer()
            text = "".join(chunks).strip()
            if not text and not cancel_event.is_set():
                raise RuntimeError("Gemini returned an empty response.")
            return AIReply(text=text, remote_state_id="")
        except Exception as error:
            if cancel_event.is_set():
                return AIReply(text="", remote_state_id="")
            _log_gemini_api_error("stream_message", error)
            if _is_inaccessible_gemini_file_error(error):
                raise AIDocumentUnavailableError(
                    "The cached Gemini PDF is not accessible with the current credentials."
                ) from None
            raise classify_provider_error(error) from None

    def generate_summary(
        self,
        *,
        model: str,
        message: str,
        document: RemoteDocument,
        cancel_event: Any,
    ) -> Mapping[str, Any] | AIReply:
        """Generate one standalone non-streaming Summary using JSON Schema."""
        try:
            from google.genai import types

            if cancel_event.is_set():
                return AIReply(text="", remote_state_id="")
            LOGGER.info(
                "[SUMMARY] provider=gemini model=%s api_path=generateContent "
                "structured_output=response_mime_type:application/json+response_json_schema",
                model,
            )
            response = self.client.models.generate_content(
                model=model,
                contents=self._contents(message, document, []),
                config=types.GenerateContentConfig(
                    system_instruction=_SUMMARY_SYSTEM_INSTRUCTION,
                    response_mime_type="application/json",
                    response_json_schema=_summary_response_schema(),
                    temperature=0.1,
                    max_output_tokens=8192,
                    thinking_config=types.ThinkingConfig(
                        thinking_level=types.ThinkingLevel.LOW
                    ),
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(
                        disable=True
                    ),
                ),
            )
            candidates = list(getattr(response, "candidates", None) or ())
            finish_reason = (
                str(getattr(candidates[0], "finish_reason", "") or "")
                if candidates
                else ""
            )
            LOGGER.info(
                "[SUMMARY] response_type=%s finish_reason=%s",
                type(response).__name__,
                finish_reason or "unavailable",
            )
            normalized_finish = finish_reason.upper()
            if "MAX_TOKENS" in normalized_finish:
                raise AISummaryFormatError(
                    "truncated_response",
                    detail="Gemini stopped because the output token limit was reached.",
                    retryable=False,
                )
            if any(
                reason in normalized_finish
                for reason in ("SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED")
            ):
                raise AIError("Gemini could not complete the Summary request.")

            parsed = getattr(response, "parsed", None)
            if isinstance(parsed, Mapping):
                output: Mapping[str, Any] | AIReply = dict(parsed)
                output_type = type(parsed).__name__
                output_length = len(json.dumps(output, ensure_ascii=False))
            elif parsed is not None and callable(getattr(parsed, "model_dump", None)):
                dumped = parsed.model_dump()
                if not isinstance(dumped, Mapping):
                    raise AISummaryFormatError(
                        "unexpected_provider_response_shape",
                        detail="Gemini parsed output was not an object.",
                        retryable=False,
                    )
                output = dict(dumped)
                output_type = type(parsed).__name__
                output_length = len(json.dumps(output, ensure_ascii=False))
            elif parsed is not None:
                raise AISummaryFormatError(
                    "unexpected_provider_response_shape",
                    detail=f"Gemini parsed output type was {type(parsed).__name__}.",
                    retryable=False,
                )
            else:
                try:
                    text = str(getattr(response, "text", "") or "").strip()
                except Exception as error:
                    raise AISummaryFormatError(
                        "unexpected_provider_response_shape",
                        detail=f"Gemini text extraction failed: {type(error).__name__}.",
                        retryable=False,
                    ) from None
                output = AIReply(text=text, remote_state_id="")
                output_type = "text"
                output_length = len(text)
            LOGGER.info(
                "[SUMMARY] extracted_output_type=%s extracted_output_length=%d",
                output_type,
                output_length,
            )
            if cancel_event.is_set():
                return AIReply(text="", remote_state_id="")
            return output
        except (AIError, AISummaryFormatError):
            raise
        except Exception as error:
            if cancel_event.is_set():
                return AIReply(text="", remote_state_id="")
            _log_gemini_api_error("generate_summary", error)
            if _is_inaccessible_gemini_file_error(error):
                raise AIDocumentUnavailableError(
                    "The cached Gemini PDF is not accessible with the current credentials."
                ) from None
            raise classify_provider_error(error) from None
