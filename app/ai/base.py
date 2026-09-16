from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Callable


SYSTEM_INSTRUCTION = """You are the AI research assistant for the specific paper or
explicitly selected set of papers attached to the current request.
Ground every answer in the attached original English PDF material and the conversation.
Answer in the same language as the user's latest question unless they request another language.
Do not invent facts. If the paper does not provide enough information, say so plainly.
Clearly label any inference as an inference rather than a statement from the paper.
Do not claim precise citations or page locations unless they are directly available to you.
Write every mathematical expression as LaTeX using $...$ for inline math or
$$...$$ for display math; never emit bare LaTeX commands or scripts."""


class AIError(RuntimeError):
    """A safe, user-facing AI operation failure."""


class AIConfigurationError(AIError):
    pass


class AIAuthenticationError(AIError):
    pass


class AIModelUnavailableError(AIError):
    pass


class AINetworkError(AIError):
    pass


class AIProviderUnavailableError(AINetworkError):
    """The provider is temporarily unavailable and a manual retry is safe."""


class AIRateLimitError(AIError):
    pass


class AIDocumentUnavailableError(AIError):
    """Remote file reference expired or no longer exists."""


class AIStateUnavailableError(AIError):
    """Provider-side conversation state expired or was removed."""


class AISummaryFormatError(ValueError):
    """A categorized structured Summary response failure."""

    def __init__(
        self,
        category: str,
        *,
        detail: str = "",
        missing_keys: tuple[str, ...] = (),
        retryable: bool = True,
    ) -> None:
        super().__init__(detail or category)
        self.category = category
        self.detail = detail
        self.missing_keys = missing_keys
        self.retryable = retryable


@dataclass(frozen=True)
class RemoteDocument:
    file_id: str
    uri: str | None = None
    mime_type: str = "application/pdf"
    expires_at: str | None = None
    local_path: str | None = None
    context_text: str | None = None
    # Request-local source identity.  These fields are deliberately not
    # persisted with the reusable remote file reference: one uploaded PDF can
    # have a different stable P alias in each comparison conversation.
    source_alias: str | None = None
    source_title: str | None = None


@dataclass(frozen=True)
class AIReply:
    text: str
    remote_state_id: str


class AIProvider(ABC):
    name: str
    reusable_document_reference = True

    @abstractmethod
    def test_connection(self) -> tuple[str, list[str]]:
        raise NotImplementedError
    @abstractmethod
    def list_models(self) -> list[str]:
        raise NotImplementedError

    @abstractmethod
    def prepare_document(self, pdf_path: Path) -> RemoteDocument:
        raise NotImplementedError

    @abstractmethod
    def validate_document(self, document: RemoteDocument) -> bool:
        raise NotImplementedError

    @abstractmethod
    def send_message(
        self,
        *,
        model: str,
        message: str,
        document: RemoteDocument | list[RemoteDocument],
        previous_state_id: str | None,
        local_history: list[dict[str, Any]],
    ) -> AIReply:
        raise NotImplementedError

    @abstractmethod
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
        """Stream real provider deltas and return the accumulated response."""
        raise NotImplementedError

    def send_text_message(
        self,
        *,
        model: str,
        message: str,
        local_history: list[dict[str, Any]],
        system_instruction: str,
    ) -> AIReply:
        """Provider-neutral text-only request used by project Search with AI."""
        raise AIConfigurationError(
            f"{self.name} does not support text-only project search."
        )


def document_source_heading(document: RemoteDocument, index: int) -> str:
    """Return a provider-neutral, request-local label for an attached PDF."""
    alias = str(document.source_alias or "").strip().upper()
    if not re.fullmatch(r"P\d+", alias):
        alias = f"Attachment {max(1, int(index))}"
    title = " ".join(str(document.source_title or "").split())[:240]
    return f"Source {alias}: {title}" if title else f"Source {alias}"


def history_context(history: list[dict[str, Any]]) -> str:
    """Recover context when a provider-side conversation has expired."""
    if not history:
        return ""
    lines = ["Local conversation history (oldest to newest):"]
    for item in history[-20:]:
        role = "User" if item.get("role") == "user" else "Assistant"
        content = str(item.get("content") or "").strip()
        if content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines)


def classify_provider_error(error: Exception) -> AIError:
    """Normalize SDK/network failures without exposing credentials."""
    status = getattr(error, "status_code", None)
    if status is None:
        status = getattr(error, "code", None)
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None
    message = _safe_error_detail(error)
    lowered = message.lower()
    if status in {401, 403} or "api key" in lowered or "unauth" in lowered:
        return AIAuthenticationError(
            "Authentication failed. Check the provider API key in Settings."
        )
    if status == 429 or "rate limit" in lowered or "quota" in lowered:
        return AIRateLimitError(
            "The provider rate limit or quota was reached. Try again later; no automatic retry was made."
        )
    if status in {502, 503, 504} or any(
        phrase in lowered
        for phrase in ("service unavailable", "temporarily unavailable", "overloaded")
    ):
        return AIProviderUnavailableError(
            "The AI provider is temporarily unavailable. Try again."
        )
    if status == 404 and ("file" in lowered or "document" in lowered):
        return AIDocumentUnavailableError("The uploaded PDF is no longer available.")
    if status in {400, 404, 410} and (
        "interaction" in lowered or "previous_response" in lowered or "response" in lowered
    ):
        return AIStateUnavailableError("The provider conversation state has expired.")
    if status == 404 or ("model" in lowered and "not found" in lowered):
        return AIModelUnavailableError(
            "The selected model is unavailable. Test the provider in Settings, "
            "then choose an available model in the AI sidebar."
        )
    if status in {400, 410} and ("file" in lowered or "document" in lowered):
        return AIDocumentUnavailableError("The uploaded PDF is no longer valid.")
    if status == 400:
        if "model" in lowered:
            return AIModelUnavailableError(
                f"The selected model rejected the request: {message}"
            )
        return AIError(f"The provider rejected the request (HTTP 400): {message}")
    if status is not None:
        return AIError(f"The AI provider returned an error (HTTP {status}).")
    return AINetworkError(
        "Could not reach the AI provider. Check the network connection and try again."
    )


def _safe_error_detail(error: Exception) -> str:
    """Keep useful provider detail while redacting common credential forms."""
    message = str(error).replace("\r", " ").replace("\n", " ").strip()
    patterns = (
        (r"(?i)([?&](?:key|api_key)=)[^&\s]+", r"\1[REDACTED]"),
        (r"(?i)((?:api[-_ ]?key|authorization)\s*[:=]\s*)[^,;\s]+", r"\1[REDACTED]"),
        (r"\bAIza[0-9A-Za-z_-]{20,}\b", "[REDACTED]"),
        (r"\bsk-[0-9A-Za-z_-]{12,}\b", "[REDACTED]"),
    )
    for pattern, replacement in patterns:
        message = re.sub(pattern, replacement, message)
    return message[:600] or "The request was rejected without additional details."
