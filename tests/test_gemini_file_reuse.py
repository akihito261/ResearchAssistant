from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

import pymupdf
from google.genai.errors import ClientError

import app.database.database as database
from app.ai.base import AIDocumentUnavailableError, AIReply, RemoteDocument
from app.ai.gemini_provider import (
    GeminiProvider,
    _is_inaccessible_gemini_file_error,
)
from app.database.ai_repository import AIRepository
from app.database.paper_repository import PaperRepository
from app.services.ai_chat_service import AIChatService
from app.services.pdf_service import calculate_sha256


class _GeminiProvider:
    reusable_document_reference = True

    def __init__(self, api_key: str, *, stale_generate_once: bool = False) -> None:
        self.document_cache_identity = hashlib.sha256(api_key.encode()).hexdigest()
        self.upload_count = 0
        self.validate_count = 0
        self.send_count = 0
        self.stale_generate_once = stale_generate_once
        self.sent_documents: list[str] = []

    def prepare_document(self, _path: Path) -> RemoteDocument:
        self.upload_count += 1
        suffix = self.document_cache_identity[:8]
        return RemoteDocument(
            file_id=f"files/{suffix}-{self.upload_count}",
            uri=f"https://example.invalid/{suffix}-{self.upload_count}",
        )

    def validate_document(self, _document: RemoteDocument) -> bool:
        self.validate_count += 1
        return True

    def send_message(self, *, document, **_kwargs) -> AIReply:
        self.send_count += 1
        documents = document if isinstance(document, list) else [document]
        self.sent_documents = [item.file_id for item in documents]
        if self.stale_generate_once and self.send_count == 1:
            raise AIDocumentUnavailableError("stale Gemini file")
        return AIReply("Grounded answer.", "")


class _Service(AIChatService):
    def __init__(self, provider: _GeminiProvider) -> None:
        super().__init__(repository=AIRepository)
        self.provider = provider

    def _provider(self, _provider: str, api_key: str | None = None):
        return self.provider


class GeminiFileReuseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.old_data_dir = database.DATA_DIR
        self.old_database_path = database.DATABASE_PATH
        database.DATA_DIR = self.root / "data"
        database.DATABASE_PATH = database.DATA_DIR / "research.db"
        database.init_database()
        self.pdf_path = self.root / "paper.pdf"
        document = pymupdf.open()
        document.new_page().insert_text((72, 72), "Research paper content")
        document.save(self.pdf_path)
        document.close()
        self.file_hash = calculate_sha256(self.pdf_path)
        self.paper_id = PaperRepository.add_paper(
            "Paper",
            "Author",
            2026,
            None,
            str(self.pdf_path),
            self.file_hash,
            1,
        )
        self.conversation = AIRepository.create_conversation(self.paper_id)

    def tearDown(self) -> None:
        database.DATA_DIR = self.old_data_dir
        database.DATABASE_PATH = self.old_database_path
        self.temporary.cleanup()

    def _chat(self, service: AIChatService, question: str) -> None:
        service.send_message(
            paper_id=self.paper_id,
            conversation_id=int(self.conversation["id"]),
            provider="gemini",
            model="gemini-test",
            pdf_path=self.pdf_path,
            file_hash=self.file_hash,
            question=question,
        )

    def test_changed_api_key_reuploads_then_reuses_only_current_file(self) -> None:
        first = _GeminiProvider("key-for-project-a")
        self._chat(_Service(first), "First question")
        self.assertEqual(first.upload_count, 1)
        old_file = first.sent_documents[0]

        second = _GeminiProvider("key-for-project-b")
        second_service = _Service(second)
        with self.assertLogs("app.services.ai_chat_service", level="INFO") as logs:
            self._chat(second_service, "Question after changing API key")
        combined_log = "\n".join(logs.output)
        self.assertIn(
            "Cached Gemini file inaccessible with current credentials; "
            "re-uploading paper.",
            combined_log,
        )
        self.assertIn("Gemini file re-uploaded successfully.", combined_log)
        self.assertEqual(second.upload_count, 1)
        self.assertNotEqual(second.sent_documents[0], old_file)
        stored = AIRepository.get_document_ref(self.paper_id, "gemini")
        self.assertIsNotNone(stored)
        self.assertNotIn("key-for-project-b", str(stored["source_file_hash"]))
        self.assertTrue(
            str(stored["source_file_hash"]).endswith(
                second.document_cache_identity
            )
        )

        self._chat(second_service, "Reuse with the same API key")
        self.assertEqual(second.upload_count, 1)
        self.assertEqual(second.validate_count, 1)

    def test_generate_file_error_reuploads_and_retries_exactly_once(self) -> None:
        provider = _GeminiProvider("current-key", stale_generate_once=True)
        AIRepository.upsert_document_ref(
            self.paper_id,
            "gemini",
            f"{self.file_hash}:{provider.document_cache_identity}",
            "files/stale",
            remote_uri="https://example.invalid/files/stale",
            mime_type="application/pdf",
            expires_at=None,
        )
        with self.assertLogs("app.services.ai_chat_service", level="INFO") as logs:
            self._chat(_Service(provider), "Retry stale file")
        self.assertEqual(provider.validate_count, 1)
        self.assertEqual(provider.upload_count, 1)
        self.assertEqual(provider.send_count, 2)
        combined_log = "\n".join(logs.output)
        self.assertIn(
            "Cached Gemini file inaccessible with current credentials; "
            "re-uploading paper.",
            combined_log,
        )
        self.assertIn("Gemini file re-uploaded successfully.", combined_log)

    def test_file_permission_error_is_document_stale_not_authentication(self) -> None:
        error = ClientError(
            403,
            {
                "error": {
                    "code": 403,
                    "status": "PERMISSION_DENIED",
                    "message": (
                        "You do not have permission to access the File files/old "
                        "or it may not exist."
                    ),
                }
            },
        )
        self.assertTrue(_is_inaccessible_gemini_file_error(error))
        provider = GeminiProvider.__new__(GeminiProvider)

        class _Models:
            @staticmethod
            def generate_content(**_kwargs):
                raise error

        class _Client:
            models = _Models()

        provider.client = _Client()
        with self.assertRaises(AIDocumentUnavailableError):
            provider.send_message(
                model="gemini-test",
                message="Question",
                document=RemoteDocument(
                    file_id="files/old",
                    uri="https://example.invalid/files/old",
                ),
                previous_state_id=None,
                local_history=[],
            )


if __name__ == "__main__":
    unittest.main()
