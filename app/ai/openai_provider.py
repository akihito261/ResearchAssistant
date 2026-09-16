from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
    history_context,
)


class OpenAIProvider(AIProvider):
    name = "openai"

    def __init__(self, api_key: str) -> None:
        from openai import OpenAI

        self.client = OpenAI(api_key=api_key, max_retries=0, timeout=90.0)

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
            raise classify_provider_error(error) from None

    def test_connection(self) -> tuple[str, list[str]]:
        models = self.list_models()
        return f"Connected to OpenAI ({len(models)} models available).", models

    def prepare_document(self, pdf_path: Path) -> RemoteDocument:
        try:
            with pdf_path.open("rb") as source:
                uploaded = self.client.files.create(
                    file=source,
                    purpose="user_data",
                    expires_after={"anchor": "created_at", "seconds": 2_592_000},
                )
            file_id = str(uploaded.id or "")
            if not file_id:
                raise AIDocumentUnavailableError(
                    "OpenAI did not return a usable PDF reference."
                )
            expires = datetime.now(timezone.utc) + timedelta(days=30)
            return RemoteDocument(file_id=file_id, expires_at=expires.isoformat())
        except AIDocumentUnavailableError:
            raise
        except Exception as error:
            raise classify_provider_error(error) from None

    def validate_document(self, document: RemoteDocument) -> bool:
        try:
            remote = self.client.files.retrieve(document.file_id)
            return bool(getattr(remote, "id", None))
        except Exception as error:
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
            response = self.client.responses.create(
                **self._response_body(
                    model,
                    message,
                    document,
                    previous_state_id,
                    local_history,
                )
            )
            text = str(response.output_text or "").strip()
            state_id = str(response.id or "")
            if not text or not state_id:
                raise RuntimeError("OpenAI returned an incomplete response.")
            return AIReply(text=text, remote_state_id=state_id)
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
        recovered = history_context(local_history)
        prompt = f"{recovered}\n\n{message}" if recovered else message
        try:
            response = self.client.responses.create(
                model=model,
                instructions=system_instruction,
                input=prompt,
                store=False,
                max_output_tokens=8192,
            )
            text = str(response.output_text or "").strip()
            if not text:
                raise RuntimeError("OpenAI returned an empty response.")
            return AIReply(text=text, remote_state_id="")
        except Exception as error:
            raise classify_provider_error(error) from None

    @staticmethod
    def _response_body(
        model: str,
        message: str,
        document: RemoteDocument | list[RemoteDocument],
        previous_state_id: str | None,
        local_history: list[dict[str, Any]],
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "instructions": SYSTEM_INSTRUCTION,
            "store": True,
            "max_output_tokens": 8192,
        }
        if previous_state_id:
            body["previous_response_id"] = previous_state_id
            body["input"] = message
        else:
            recovered = history_context(local_history)
            prompt = (
                f"{recovered}\n\nCurrent user message:\n{message}"
                if recovered
                else message
            )
            documents = document if isinstance(document, list) else [document]
            document_content: list[dict[str, str]] = []
            for index, item in enumerate(documents, start=1):
                document_content.extend(
                    (
                        {
                            "type": "input_text",
                            "text": document_source_heading(item, index),
                        },
                        {"type": "input_file", "file_id": item.file_id},
                    )
                )
            body["input"] = [
                {
                    "role": "user",
                    "content": [
                        *document_content,
                        {"type": "input_text", "text": prompt},
                    ],
                }
            ]
        return body

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
            with self.client.responses.stream(
                **self._response_body(
                    model,
                    message,
                    document,
                    previous_state_id,
                    local_history,
                )
            ) as stream:
                if hasattr(cancel_event, "register_closer"):
                    cancel_event.register_closer(stream.close)
                for event in stream:
                    if cancel_event.is_set():
                        break
                    if getattr(event, "type", "") != "response.output_text.delta":
                        continue
                    delta = str(getattr(event, "delta", "") or "")
                    if delta:
                        chunks.append(delta)
                        on_chunk(delta)
                if cancel_event.is_set():
                    return AIReply(text="".join(chunks).strip(), remote_state_id="")
                response = stream.get_final_response()
                if hasattr(cancel_event, "clear_closer"):
                    cancel_event.clear_closer()
            text = "".join(chunks).strip() or str(response.output_text or "").strip()
            state_id = str(response.id or "")
            if not text or not state_id:
                raise RuntimeError("OpenAI returned an incomplete response.")
            return AIReply(text=text, remote_state_id=state_id)
        except Exception as error:
            if cancel_event.is_set():
                return AIReply(text="".join(chunks).strip(), remote_state_id="")
            raise classify_provider_error(error) from None
