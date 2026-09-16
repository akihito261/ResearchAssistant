from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import pymupdf

from app.ai.base import (
    AIDocumentUnavailableError,
    AIProvider,
    AIReply,
    RemoteDocument,
    SYSTEM_INSTRUCTION,
    classify_provider_error,
    document_source_heading,
)


class DeepSeekProvider(AIProvider):
    name = "deepseek"
    display_name = "DeepSeek"
    base_url = "https://api.deepseek.com"
    reusable_document_reference = False
    _MAX_TOKENS = 4096

    def __init__(self, api_key: str) -> None:
        from openai import OpenAI

        self.client = OpenAI(
            api_key=api_key,
            base_url=self.base_url,
            max_retries=0,
            timeout=90.0,
        )

    def _classify_error(
        self,
        error: Exception,
        *,
        operation: str,
        model: str = "",
    ):
        return classify_provider_error(error)

    def list_models(self) -> list[str]:
        try:
            return sorted(
                {
                    str(model.id)
                    for model in self.client.models.list().data
                    if getattr(model, "id", None)
                }
            )
        except Exception as error:
            raise self._classify_error(error, operation="list_models") from None

    def test_connection(self) -> tuple[str, list[str]]:
        models = self.list_models()
        return (
            f"Connected to {self.display_name} "
            f"({len(models)} chat models available).",
            models,
        )

    def prepare_document(self, pdf_path: Path) -> RemoteDocument:
        try:
            with pymupdf.open(pdf_path) as document:
                if document.needs_pass:
                    raise AIDocumentUnavailableError(
                        "The English PDF is encrypted and cannot be prepared for "
                        f"{self.display_name}."
                    )
                pages = [page.get_text("text").strip() for page in document]
        except AIDocumentUnavailableError:
            raise
        except Exception as error:
            raise AIDocumentUnavailableError(
                f"Could not extract text from the English PDF: {error}"
            ) from None
        context = "\n\n".join(
            f"[PDF page {index}]\n{text}"
            for index, text in enumerate(pages, start=1)
            if text
        )
        if not context:
            raise AIDocumentUnavailableError(
                "The English PDF has no extractable text for "
                f"{self.display_name}."
            )
        return RemoteDocument(
            file_id=str(pdf_path),
            local_path=str(pdf_path),
            context_text=context,
        )

    def validate_document(self, document: RemoteDocument) -> bool:
        return bool(document.context_text)

    def send_message(
        self,
        *,
        model: str,
        message: str,
        document: RemoteDocument | list[RemoteDocument],
        previous_state_id: str | None,
        local_history: list[dict[str, Any]],
    ) -> AIReply:
        messages = self._messages(message, document, local_history)
        try:
            response = self.client.chat.completions.create(
                model=model,
                messages=messages,
                stream=False,
                max_tokens=self._MAX_TOKENS,
            )
            text = str(response.choices[0].message.content or "").strip()
            if not text:
                raise RuntimeError(
                    f"{self.display_name} returned an empty response."
                )
            return AIReply(text=text, remote_state_id="")
        except Exception as error:
            raise self._classify_error(
                error,
                operation="send_message",
                model=model,
            ) from None

    def send_text_message(
        self,
        *,
        model: str,
        message: str,
        local_history: list[dict[str, Any]],
        system_instruction: str,
    ) -> AIReply:
        messages = [{"role": "system", "content": system_instruction}]
        messages.extend(
            {
                "role": "assistant" if item.get("role") == "assistant" else "user",
                "content": str(item.get("content") or ""),
            }
            for item in local_history
            if str(item.get("content") or "").strip()
        )
        messages.append({"role": "user", "content": message})
        try:
            response = self.client.chat.completions.create(
                model=model,
                messages=messages,
                stream=False,
                max_tokens=self._MAX_TOKENS,
            )
            text = str(response.choices[0].message.content or "").strip()
            if not text:
                raise RuntimeError(f"{self.display_name} returned an empty response.")
            return AIReply(text=text, remote_state_id="")
        except Exception as error:
            raise self._classify_error(
                error, operation="send_text_message", model=model
            ) from None

    @staticmethod
    def _messages(
        message: str,
        document: RemoteDocument | list[RemoteDocument],
        local_history: list[dict[str, Any]],
    ) -> list[dict[str, str]]:
        documents = document if isinstance(document, list) else [document]
        if any(not item.context_text for item in documents):
            raise AIDocumentUnavailableError("The paper text context is unavailable.")
        document_context = "\n\n".join(
            f"--- {document_source_heading(item, index)} ---\n{item.context_text}"
            for index, item in enumerate(documents, start=1)
        )
        messages: list[dict[str, str]] = [
            {
                "role": "system",
                "content": (
                    f"{SYSTEM_INSTRUCTION}\n\n"
                    "Original English PDF text follows:\n"
                    f"{document_context}"
                ),
            }
        ]
        for item in local_history:
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            role = "assistant" if item.get("role") == "assistant" else "user"
            if len(messages) > 1 and messages[-1]["role"] == role:
                messages[-1]["content"] += f"\n\n{content}"
            else:
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": message})
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
            stream = self.client.chat.completions.create(
                model=model,
                messages=self._messages(message, document, local_history),
                stream=True,
                max_tokens=self._MAX_TOKENS,
            )
            if hasattr(cancel_event, "register_closer") and hasattr(stream, "close"):
                cancel_event.register_closer(stream.close)
            try:
                for event in stream:
                    if cancel_event.is_set():
                        break
                    choices = getattr(event, "choices", None) or []
                    delta = (
                        str(getattr(choices[0].delta, "content", "") or "")
                        if choices
                        else ""
                    )
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
                raise RuntimeError(
                    f"{self.display_name} returned an empty response."
                )
            return AIReply(text=text, remote_state_id="")
        except Exception as error:
            if cancel_event.is_set():
                return AIReply(text="".join(chunks).strip(), remote_state_id="")
            raise self._classify_error(
                error,
                operation="stream_message",
                model=model,
            ) from None
