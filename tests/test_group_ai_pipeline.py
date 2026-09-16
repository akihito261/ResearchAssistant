from __future__ import annotations

import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

import pymupdf
from PySide6.QtWidgets import QApplication

import app.database.database as database
from app.ai.base import AIReply, RemoteDocument
from app.ai.deepseek_provider import DeepSeekProvider
from app.ai.openai_provider import OpenAIProvider
from app.database.ai_repository import AIRepository
from app.database.paper_repository import PaperRepository
from app.services.ai_chat_service import AIChatService
from app.services.citation_service import CitationResolver
from app.services.pdf_service import calculate_sha256
from app.ui import citation_widgets
from app.ui.ai_chat_panel import AIChatPanel, MarkdownMessage
from app.ui.citation_widgets import citation_value, insert_inline_citations
from PySide6.QtGui import QTextDocument


CLAIM = "Paper A uses Alpha, while Paper B uses Beta."
EVIDENCE_A = "method Alpha improves sample efficiency substantially"
EVIDENCE_B = "method Beta improves final accuracy substantially"
EVIDENCE_C = "method Gamma reduces evaluation latency substantially"


def _pdf(path: Path, text: str) -> None:
    document = pymupdf.open()
    document.new_page().insert_text((72, 72), text)
    document.save(path)
    document.close()


def _pdf_pages(path: Path, pages: list[str]) -> None:
    document = pymupdf.open()
    for text in pages:
        document.new_page().insert_text((72, 72), text)
    document.save(path)
    document.close()


class _Provider:
    reusable_document_reference = False

    def __init__(self, response: str) -> None:
        self.response = response
        self.last_message = ""
        self.document_count = 0
        self.documents: list[RemoteDocument] = []

    def prepare_document(self, path: Path) -> RemoteDocument:
        return RemoteDocument(file_id=path.name, local_path=str(path))

    def validate_document(self, _document: RemoteDocument) -> bool:
        return True

    def send_message(self, *, message: str, document, **_kwargs) -> AIReply:
        self.last_message = message
        self.documents = document if isinstance(document, list) else [document]
        self.document_count = len(self.documents)
        return AIReply(self.response, "")


class _Service(AIChatService):
    def __init__(self, provider: _Provider) -> None:
        super().__init__(repository=AIRepository)
        self.provider = provider

    def _provider(self, _provider: str, api_key: str | None = None):
        return self.provider


class GroupAIPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_real_group_service_path_resolves_persists_and_renders_both_sources(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        old_data_dir = database.DATA_DIR
        old_database_path = database.DATABASE_PATH
        database.DATA_DIR = root / "data"
        database.DATABASE_PATH = database.DATA_DIR / "research.db"
        try:
            database.init_database()
            path_a = root / "a.pdf"
            path_b = root / "b.pdf"
            path_c = root / "c.pdf"
            _pdf(path_a, f"The proposed {EVIDENCE_A}.")
            _pdf(path_b, f"The proposed {EVIDENCE_B}.")
            _pdf(path_c, "Paper C is not a member of this comparison.")
            paper_a = PaperRepository.add_paper(
                "Paper A", "Author A", 2025, None, str(path_a),
                calculate_sha256(path_a), 1,
            )
            paper_b = PaperRepository.add_paper(
                "Paper B", "Author B", 2025, None, str(path_b),
                calculate_sha256(path_b), 1,
            )
            paper_c = PaperRepository.add_paper(
                "Paper C", "Author C", 2025, None, str(path_c),
                calculate_sha256(path_c), 1,
            )
            conversation = AIRepository.create_group_conversation(
                [paper_a, paper_b]
            )
            payload = {
                "support_status": "supported",
                "citations": [
                    {
                        "claim": CLAIM,
                        "paper_alias": "P1",
                        "page_hint": 1,
                        "evidence": EVIDENCE_A,
                    },
                    {
                        "claim": CLAIM,
                        "paper_alias": "P2",
                        "page_hint": 1,
                        "evidence": EVIDENCE_B,
                    },
                ],
            }
            response = (
                f"{CLAIM}\n<!--RA_RESULT\n"
                f"{json.dumps(payload)}\nRA_RESULT-->"
            )
            provider = _Provider(response)
            result = _Service(provider).send_message(
                paper_id=paper_a,
                conversation_id=int(conversation["id"]),
                provider="gemini",
                model="test-model",
                pdf_path=path_a,
                file_hash=calculate_sha256(path_a),
                question="Compare the methods.",
                workspace_papers=[
                    {
                        "id": paper_a,
                        "title": "Paper A",
                        "pdf_path": path_a,
                        "file_hash": calculate_sha256(path_a),
                        "alias_index": 1,
                    },
                    {
                        # Deliberately contaminate the tab snapshot. A restored
                        # group must still use only its persisted P2 member.
                        "id": paper_c,
                        "title": "Paper C",
                        "pdf_path": path_c,
                        "file_hash": calculate_sha256(path_c),
                        "alias_index": 2,
                    },
                ],
            )

            self.assertEqual(provider.document_count, 2)
            self.assertEqual(
                [document.file_id for document in provider.documents],
                [path_a.name, path_b.name],
            )
            self.assertEqual(
                [document.source_alias for document in provider.documents],
                ["P1", "P2"],
            )
            self.assertEqual(
                [document.source_title for document in provider.documents],
                ["Paper A", "Paper B"],
            )
            self.assertIn("paper_alias", provider.last_message)
            self.assertIn(f"P1: paper_id={paper_a}", provider.last_message)
            self.assertIn(f"P2: paper_id={paper_b}", provider.last_message)
            self.assertNotIn(f"paper_id={paper_c}", provider.last_message)
            self.assertEqual(len(result.citations), 2)
            self.assertTrue(all(citation.verified for citation in result.citations))
            self.assertEqual(
                [(citation.paper_id, citation.alias) for citation in result.citations],
                [(paper_a, "P1"), (paper_b, "P2")],
            )
            self.assertTrue(
                all(
                    citation.conversation_id == int(conversation["id"])
                    for citation in result.citations
                )
            )

            restored = AIRepository.list_messages(int(conversation["id"]))[-1]
            self.assertEqual(
                [(row["paper_id"], row["alias"]) for row in restored["citations"]],
                [(paper_a, "P1"), (paper_b, "P2")],
            )
            document = QTextDocument()
            document.setPlainText(result.reply)
            rendered = insert_inline_citations(
                document,
                result.citations,
                paper_labels={paper_a: "P1", paper_b: "P2"},
            )
            self.assertEqual(len(rendered), 2)
        finally:
            database.DATA_DIR = old_data_dir
            database.DATABASE_PATH = old_database_path
            temporary.cleanup()

    def test_three_paper_citations_reach_message_badges_and_restore(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        old_data_dir = database.DATA_DIR
        old_database_path = database.DATABASE_PATH
        database.DATA_DIR = root / "data"
        database.DATABASE_PATH = database.DATA_DIR / "research.db"
        panels: list[AIChatPanel] = []
        try:
            database.init_database()
            for index in range(1, 15):
                PaperRepository.add_paper(
                    f"Dummy {index}",
                    "Test",
                    2025,
                    None,
                    str(root / f"dummy-{index}.pdf"),
                    f"{index:064x}",
                    1,
                )

            p3_path = root / "p3.pdf"
            p2_path = root / "p2.pdf"
            p1_path = root / "p1.pdf"
            p1_evidence = "P1 optimizes rate distortion for ROI image compression"
            p2_page1_evidence = (
                "P2 achieves over a 32 percent bit rate saving compared with H.265"
            )
            # The same evidence can legitimately occur on two hinted pages;
            # page remains part of citation identity and neither is collapsed.
            p2_page4_evidence = p2_page1_evidence
            p3_evidence = "P3 reduces transmission energy for visual monitoring"
            _pdf_pages(
                p3_path,
                ["P3 filler"] * 4 + [p3_evidence],
            )
            _pdf_pages(
                p2_path,
                [p2_page1_evidence, "P2 filler", "P2 filler", p2_page4_evidence],
            )
            _pdf_pages(p1_path, [p1_evidence])

            paper_3 = PaperRepository.add_paper(
                "Paper Three", "Author", 2025, None, str(p3_path),
                calculate_sha256(p3_path), 5,
            )
            paper_2 = PaperRepository.add_paper(
                "Paper Two", "Author", 2025, None, str(p2_path),
                calculate_sha256(p2_path), 4,
            )
            paper_1 = PaperRepository.add_paper(
                "Paper One", "Author", 2025, None, str(p1_path),
                calculate_sha256(p1_path), 1,
            )
            self.assertEqual((paper_1, paper_2, paper_3), (17, 16, 15))
            conversation = AIRepository.create_group_conversation(
                [paper_1, paper_2, paper_3]
            )

            comparison_claim = (
                "P1 và P3 bổ sung cho nhau giữa nén ROI và tiết kiệm truyền tải."
            )
            p2_claim = (
                "P2: Đạt mức tiết kiệm tỉ lệ bit trung bình trên 32% so với H.265."
            )
            answer = (
                "P1 tối ưu nén ảnh ROI, trong khi P3 giảm năng lượng truyền dữ liệu; "
                "hai hướng tiếp cận bổ sung cho nhau.\n\n"
                "P2 đạt mức tiết kiệm bitrate trung bình trên 32% so với H.265 và "
                "còn báo cáo thêm kết quả ở phần đánh giá sau."
            )
            payload = {
                "support_status": "supported",
                "citations": [
                    {
                        "claim": comparison_claim,
                        "paper_alias": "P1",
                        "paper_id": 17,
                        "page_hint": 1,
                        "evidence": p1_evidence,
                    },
                    {
                        "claim": p2_claim,
                        "paper_alias": "P2",
                        "paper_id": 16,
                        "page_hint": 1,
                        "evidence": p2_page1_evidence,
                    },
                    {
                        "claim": p2_claim,
                        "paper_alias": "P2",
                        "paper_id": 16,
                        "page_hint": 4,
                        "evidence": p2_page4_evidence,
                    },
                    {
                        "claim": comparison_claim,
                        "paper_alias": "P3",
                        "paper_id": 15,
                        "page_hint": 5,
                        "evidence": p3_evidence,
                    },
                ],
            }
            # This duplicate-comment closer is emitted by real providers and
            # previously caused the complete structured result to be dropped.
            response = (
                f"{answer}\n<!--RA_RESULT\n"
                f"{json.dumps(payload, ensure_ascii=False)}\n"
                "<!--RA_RESULT-->"
            )
            result = _Service(_Provider(response)).send_message(
                paper_id=paper_1,
                conversation_id=int(conversation["id"]),
                provider="gemini",
                model="test-model",
                pdf_path=p1_path,
                file_hash=calculate_sha256(p1_path),
                question="So sánh kết quả của các bài báo.",
                workspace_papers=[],
            )
            self.assertNotIn("RA_RESULT", result.reply)
            self.assertNotIn('"support_status"', result.reply)
            self.assertEqual(len(result.citations), 4)
            self.assertTrue(all(item.verified for item in result.citations))
            self.assertEqual(
                [(item.paper_id, item.resolved_page) for item in result.citations],
                [(17, 1), (16, 1), (16, 4), (15, 5)],
            )

            restored = AIRepository.list_messages(int(conversation["id"]))[-1]
            self.assertEqual(
                [
                    (item["paper_id"], item["alias"], item["resolved_page"])
                    for item in restored["citations"]
                ],
                [(17, "P1", 1), (16, "P2", 1), (16, "P2", 4), (15, "P3", 5)],
            )

            original_badge = citation_widgets._citation_badge

            def render_from_history() -> tuple[list[str], MarkdownMessage]:
                labels: list[str] = []

                def capture(label: str):
                    labels.append(label)
                    return original_badge(label)

                panel = AIChatPanel(paper_1)
                panels.append(panel)
                with patch(
                    "app.ui.citation_widgets._citation_badge",
                    side_effect=capture,
                ):
                    panel.load_group_chat(
                        AIRepository.get_conversation(int(conversation["id"]))
                    )
                    self.app.processEvents()
                messages = panel.findChildren(MarkdownMessage)
                self.assertTrue(messages)
                return labels, messages[-1]

            expected_labels = [
                "[P1 · p.1]",
                "[P2 · p.1]",
                "[P2 · p.4]",
                "[P3 · p.5]",
            ]
            raw_labels: list[str] = []

            def capture_raw(label: str):
                raw_labels.append(label)
                return original_badge(label)

            raw_message = MarkdownMessage(answer)
            with patch(
                "app.ui.citation_widgets._citation_badge",
                side_effect=capture_raw,
            ):
                raw_message.set_citations(
                    [dict(item, verified=True) for item in payload["citations"]],
                )
            self.assertEqual(raw_labels, expected_labels)
            self.assertEqual(len(raw_message._citation_groups), 4)
            raw_message.deleteLater()

            first_labels, first_message = render_from_history()
            self.assertEqual(first_labels, expected_labels)
            self.assertEqual(len(first_message._citation_groups), 4)
            self.assertEqual(
                [
                    (
                        int(citation_value(group[0], "paper_id")),
                        int(citation_value(group[0], "resolved_page")),
                    )
                    for group in first_message._citation_groups.values()
                ],
                [(17, 1), (16, 1), (16, 4), (15, 5)],
            )

            restored_labels, restored_message = render_from_history()
            self.assertEqual(restored_labels, expected_labels)
            self.assertEqual(len(restored_message._citation_groups), 4)
        finally:
            for panel in panels:
                panel.deleteLater()
            self.app.processEvents()
            database.DATA_DIR = old_data_dir
            database.DATABASE_PATH = old_database_path
            temporary.cleanup()

    def test_persisted_alias_gap_and_per_paper_resolution_are_isolated(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        old_data_dir = database.DATA_DIR
        old_database_path = database.DATABASE_PATH
        database.DATA_DIR = root / "data"
        database.DATABASE_PATH = database.DATA_DIR / "research.db"
        try:
            database.init_database()
            paths = [root / name for name in ("a.pdf", "b.pdf", "c.pdf")]
            _pdf(paths[0], f"The proposed {EVIDENCE_A}.")
            _pdf(paths[1], "This paper is removed from the active comparison.")
            _pdf(paths[2], f"The proposed {EVIDENCE_C}.")
            paper_ids = [
                PaperRepository.add_paper(
                    f"Paper {label}", f"Author {label}", 2025, None,
                    str(path), calculate_sha256(path), 1,
                )
                for label, path in zip(("A", "B", "C"), paths, strict=True)
            ]
            conversation = AIRepository.create_group_conversation(paper_ids)
            conversation_id = int(conversation["id"])
            AIRepository.remove_group_member(conversation_id, paper_ids[1])
            claim = "Paper A uses Alpha, while Paper C uses Gamma."
            payload = {
                "support_status": "supported",
                "citations": [
                    {
                        "claim": claim,
                        "paper_alias": "P1",
                        "page_hint": 1,
                        "evidence": EVIDENCE_A,
                    },
                    {
                        "claim": claim,
                        "paper_alias": "P3",
                        "page_hint": 1,
                        "evidence": EVIDENCE_C,
                    },
                ],
            }
            provider = _Provider(
                f"{claim}\n<!--RA_RESULT\n{json.dumps(payload)}\nRA_RESULT-->"
            )
            service = _Service(provider)

            def resolver_factory(path: Path):
                if path.name == paths[2].name:
                    raise OSError("isolated test failure")
                return CitationResolver(path)

            service.citation_resolver_factory = resolver_factory
            result = service.send_message(
                paper_id=paper_ids[0],
                conversation_id=conversation_id,
                provider="gemini",
                model="test-model",
                pdf_path=paths[0],
                file_hash=calculate_sha256(paths[0]),
                question="Compare the methods.",
                # The removed P2 must not re-enter through current tab state.
                workspace_papers=[
                    {
                        "id": paper_ids[0], "title": "Paper A",
                        "pdf_path": paths[0], "alias_index": 1,
                    },
                    {
                        "id": paper_ids[1], "title": "Paper B",
                        "pdf_path": paths[1], "alias_index": 2,
                    },
                ],
            )
            self.assertEqual(
                [document.source_alias for document in provider.documents],
                ["P1", "P3"],
            )
            self.assertEqual(
                [(item.paper_id, item.alias) for item in result.citations],
                [(paper_ids[0], "P1"), (paper_ids[2], "P3")],
            )
            self.assertTrue(result.citations[0].verified)
            self.assertEqual(
                result.citations[1].verification_status,
                "verification_error",
            )
            restored = AIRepository.list_messages(conversation_id)[-1]
            self.assertEqual(
                [item["alias"] for item in restored["citations"]],
                ["P1", "P3"],
            )
        finally:
            database.DATA_DIR = old_data_dir
            database.DATABASE_PATH = old_database_path
            temporary.cleanup()

    def test_provider_payloads_keep_stable_source_labels(self) -> None:
        documents = [
            RemoteDocument(
                file_id="file-a",
                context_text="Alpha context",
                source_alias="P1",
                source_title="Paper A",
            ),
            RemoteDocument(
                file_id="file-c",
                context_text="Gamma context",
                source_alias="P3",
                source_title="Paper C",
            ),
        ]
        openai_body = OpenAIProvider._response_body(
            "test-model", "Compare.", documents, None, []
        )
        openai_content = openai_body["input"][0]["content"]
        self.assertEqual(
            [
                item.get("text")
                for item in openai_content
                if item["type"] == "input_text"
            ][:2],
            ["Source P1: Paper A", "Source P3: Paper C"],
        )
        deepseek_messages = DeepSeekProvider._messages(
            "Compare.", documents, []
        )
        system = deepseek_messages[0]["content"]
        self.assertIn("Source P1: Paper A", system)
        self.assertIn("Source P3: Paper C", system)


if __name__ == "__main__":
    unittest.main()
