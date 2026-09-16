from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Callable

from app.ai.base import (
    AIDocumentUnavailableError,
    AIProvider,
    AIReply,
    RemoteDocument,
    SYSTEM_INSTRUCTION,
    classify_provider_error,
    document_source_heading,
)


class ClaudeProvider(AIProvider):
    name = "claude"
    reusable_document_reference = False
    _MAX_INLINE_PDF_BYTES = 24 * 1024 * 1024
    _CHAT_MAX_TOKENS = 4096

    def __init__(self, api_key: str) -> None:
        import anthropic

        self.client = anthropic.Anthropic(api_key=api_key, max_retries=0, timeout=90.0)

    def list_models(self) -> list[str]:
        try:
            page = self.client.models.list(limit=1000)
            models: list[Any] = list(page.data)
            while page.has_next_page():
                page = page.get_next_page()
                models.extend(page.data)
            names = []
            for model in models:
                capabilities = getattr(model, "capabilities", None)
                pdf_input = getattr(capabilities, "pdf_input", None)
                if pdf_input is not None and not bool(getattr(pdf_input, "supported", False)):
                    continue
                if getattr(model, "id", None):
                    names.append(str(model.id))
            return sorted(set(names))
        except Exception as error:
            raise classify_provider_error(error) from None

    def test_connection(self) -> tuple[str, list[str]]:
        models = self.list_models()
        return f"Connected to Claude ({len(models)} PDF-capable models available).", models

    def prepare_document(self, pdf_path: Path) -> RemoteDocument:
        try:
            size = pdf_path.stat().st_size
        except OSError as error:
            raise AIDocumentUnavailableError(f"Could not read the English PDF: {error}") from None
        if size > self._MAX_INLINE_PDF_BYTES:
            raise AIDocumentUnavailableError(
                "The PDF is too large for Claude's inline document request."
            )
        return RemoteDocument(file_id=str(pdf_path), local_path=str(pdf_path))

    def validate_document(self, document: RemoteDocument) -> bool:
        return bool(document.local_path and Path(document.local_path).is_file())

    @staticmethod
    def _history(local_history: list[dict[str, Any]]) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = []
        for item in local_history:
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            role = "assistant" if item.get("role") == "assistant" else "user"
            if messages and messages[-1]["role"] == role:
                messages[-1]["content"] += f"\n\n{content}"
            else:
                messages.append({"role": role, "content": content})
        return messages

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
            response = self.client.messages.create(
                model=model,
                system=SYSTEM_INSTRUCTION,
                messages=self._messages(message, document, local_history),
                max_tokens=self._CHAT_MAX_TOKENS,
            )
            text = "\n".join(
                str(block.text)
                for block in response.content
                if getattr(block, "type", "") == "text" and getattr(block, "text", None)
            ).strip()
            if not text:
                raise RuntimeError("Claude returned an empty response.")
            return AIReply(text=text, remote_state_id="")
        except AIDocumentUnavailableError:
            raise
        except Exception as error:
            raise classify_provider_error(error) from None

    def send_text_message(
        self,
        *,
        model: str,
        message: str,
        local_history: list[dict[str, Any]],
        system_instruction: str,
    ) -> AIReply:
        messages = self._history(local_history)
        messages.append({"role": "user", "content": message})
        try:
            response = self.client.messages.create(
                model=model,
                system=system_instruction,
                messages=messages,
                max_tokens=self._CHAT_MAX_TOKENS,
            )
            text = "\n".join(
                str(block.text) for block in response.content
                if getattr(block, "type", "") == "text" and getattr(block, "text", None)
            ).strip()
            if not text:
                raise RuntimeError("Claude returned an empty response.")
            return AIReply(text=text, remote_state_id="")
        except Exception as error:
            raise classify_provider_error(error) from None

    def _messages(
        self,
        message: str,
        document: RemoteDocument | list[RemoteDocument],
        local_history: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        documents = document if isinstance(document, list) else [document]
        document_blocks: list[dict[str, Any]] = []
        for index, item in enumerate(documents, start=1):
            path = Path(item.local_path or "")
            if not path.is_file():
                raise AIDocumentUnavailableError(
                    "The original English PDF is unavailable."
                )
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            document_blocks.extend((
                {
                    "type": "text",
                    "text": document_source_heading(item, index),
                },
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": encoded,
                    },
                },
            ))
        messages: list[dict[str, Any]] = list(self._history(local_history))
        messages.append(
            {
                "role": "user",
                "content": [*document_blocks, {"type": "text", "text": message}],
            }
        )
        return messages

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
        chunks: list[str] = []
        try:
            with self.client.messages.stream(
                model=model,
                system=SYSTEM_INSTRUCTION,
                messages=self._messages(message, document, local_history),
                max_tokens=self._CHAT_MAX_TOKENS,
            ) as stream:
                if hasattr(cancel_event, "register_closer"):
                    cancel_event.register_closer(stream.close)
                for delta in stream.text_stream:
                    if cancel_event.is_set():
                        break
                    text = str(delta or "")
                    if text:
                        chunks.append(text)
                        on_chunk(text)
                if not cancel_event.is_set():
                    stream.get_final_message()
                if hasattr(cancel_event, "clear_closer"):
                    cancel_event.clear_closer()
            text = "".join(chunks).strip()
            if not text and not cancel_event.is_set():
                raise RuntimeError("Claude returned an empty response.")
            return AIReply(text=text, remote_state_id="")
        except AIDocumentUnavailableError:
            raise
        except Exception as error:
            if cancel_event.is_set():
                return AIReply(text="".join(chunks).strip(), remote_state_id="")
            raise classify_provider_error(error) from None
