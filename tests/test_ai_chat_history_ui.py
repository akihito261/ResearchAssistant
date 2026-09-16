from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import QPoint, QPointF, Qt, QUrl, Signal
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QWidget

from app.ai import AIProviderUnavailableError
from app.ai.base import AIReply, RemoteDocument, classify_provider_error
from app.ui.ai_chat_panel import AIChatPanel, ChatHistoryPopup, MarkdownMessage
from app.ui.markdown_math import _math_tokens
from app.services.citation_service import parse_chat_result
from app.services.ai_chat_service import AIChatService, AIRequestControl


class _HistoryPanel(QWidget):
    group_paper_requested = Signal(int, int)

    def __init__(self) -> None:
        super().__init__()
        self.active_conversation_id = 1
        self.selected: int | None = None
        self.setFixedSize(380, 700)
        self.anchor = QPushButton(self)
        self.anchor.setGeometry(330, 10, 32, 28)
        self.conversations = [
            {
                "id": 1,
                "title": "A deliberately long comparison conversation title",
                "conversation_type": "group",
                "all_members": [
                    {"id": 11, "alias_index": 1, "title": "Paper one"},
                    {"id": 12, "alias_index": 2, "title": "Paper two"},
                    {"id": 13, "alias_index": 3, "title": "Paper three"},
                ],
            },
            *[
                {
                    "id": index,
                    "title": f"Solo conversation {index}",
                    "conversation_type": "solo",
                }
                for index in range(2, 14)
            ],
        ]

    def available_conversations(self, *, for_history: bool = False):
        return list(self.conversations)

    def select_chat(self, conversation_id: int) -> None:
        self.selected = int(conversation_id)

    def new_chat(self) -> None:
        self.selected = 0

    def delete_chat(self, _conversation_id: int, _title: str) -> bool:
        return False


class _PanelSettings:
    values = {"ai_chat_font_size": "14"}

    @classmethod
    def get(cls, key: str, default: str | None = None):
        return cls.values.get(key, default)

    @classmethod
    def get_json(cls, _key: str, default=None):
        return default

    @classmethod
    def set(cls, key: str, value: str) -> None:
        cls.values[key] = value


class _Status503(Exception):
    status_code = 503


class AIChatHistoryUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_markdown_height_updates_are_owned_coalesced_and_hover_stable(self) -> None:
        message = MarkdownMessage("A wrapped response. " * 80)
        message.resize(280, 40)
        message.show()
        QTest.qWait(30)

        self.assertIs(message._height_timer.parent(), message)
        timer_fires = [0]
        message._height_timer.timeout.connect(
            lambda: timer_fires.__setitem__(0, timer_fires[0] + 1)
        )
        for _index in range(20):
            message._schedule_adjust_height()
        QTest.qWait(30)
        self.assertEqual(timer_fires[0], 1)

        geometry = message.geometry()
        for y in range(2, min(message.viewport().height(), 150), 12):
            QTest.mouseMove(message.viewport(), QPoint(10, y))
        QTest.qWait(20)
        self.assertEqual(message.geometry(), geometry)

        message.update_markdown("Replacement response. " * 80)
        message.deleteLater()
        QTest.qWait(30)

    def test_chat_math_renders_delimiters_bare_latex_and_streaming_tail(self) -> None:
        markdown = (
            "**Math**\n\n"
            "- $\\hat{F}_{t-1}$\n"
            "- $\\hat{v}_t$\n"
            "- $F_t - F_{t-1}$\n"
            "- $R(x)=R_f(x)+R_{res}(x)$\n"
            "- $x_t$ and $\\sum_i R_i$\n\n"
            "- $\\varepsilon(x_t)=(\\hat{x}_t-x_t)^2$\n"
            "- $p \\odot \\varepsilon(x_t)$\n"
            "- $\\frac{1}{C_{HW}}\\sum_i\\sum_j\\sum_k$\n\n"
            "Bare: \\hat{F}_{t-1}, \\hat{v}_t, x_t, R_{res}(x).\n\n"
            "Preserved: `x_t`, file_name, https://example.com/x_t"
        )
        message = MarkdownMessage(markdown)
        plain = message.document().toPlainText()
        rendered_html = message.document().toHtml()

        self.assertNotIn("\\hat", plain)
        self.assertNotIn("_{", plain)
        self.assertIn("F̂", plain)
        self.assertIn("v̂", plain)
        self.assertIn("∑", plain)
        self.assertIn("ε", plain)
        self.assertIn("⊙", plain)
        self.assertIn("⁄", plain)
        self.assertNotIn("\\varepsilon", plain)
        self.assertNotIn("\\odot", plain)
        self.assertNotIn("\\frac", plain)
        self.assertGreaterEqual(rendered_html.count("vertical-align:sub"), 11)
        self.assertIn("font-weight:700", rendered_html)
        self.assertIn("x_t", plain)  # Inline code remains literal.
        self.assertIn("file_name", plain)
        self.assertIn('href="https://example.com/x_t"', rendered_html)
        protected_source = (
            '<span data-id="x_t">label</span> '
            '[P1 · p.4] https://example.com/$x_t$ and x_t'
        )
        prepared, protected_tokens = _math_tokens(protected_source)
        self.assertIn('<span data-id="x_t">', prepared)
        self.assertIn('[P1 · p.4]', prepared)
        self.assertIn('https://example.com/$x_t$', prepared)
        self.assertEqual([token.expression for token in protected_tokens], ["x_t"])

        alternate_delimiters = MarkdownMessage(
            "Inline \\(\\hat{v}_t\\).\n\n"
            "\\[\\sum_i R_i\\]\n\n"
            "$$F_t - F_{t-1}$$"
        )
        alternate_plain = alternate_delimiters.document().toPlainText()
        self.assertNotIn("\\(", alternate_plain)
        self.assertNotIn("\\[", alternate_plain)
        self.assertNotIn("$$", alternate_plain)
        self.assertIn("v̂", alternate_plain)
        self.assertIn("∑", alternate_plain)

        partial = MarkdownMessage("")
        partial.update_markdown(r"Before $\hat{F", streaming=True)
        self.assertEqual(partial.document().toPlainText(), "Before")
        partial.update_markdown(r"Before $\hat{F}_{t-1}$", streaming=True)
        self.assertIn("F̂", partial.document().toPlainText())

    def test_math_and_clickable_citation_survive_history_render(self) -> None:
        markdown = (
            "**Kết quả:** $R(x)=R_f(x)+R_{res}(x)$\n\n"
            "- Giá trị $x_t$ ổn định."
        )
        citation = {
            "paper_id": 7,
            "resolved_page": 4,
            "evidence": "distinct source evidence",
            "verified": True,
            "claim_text": "Kết quả: R(x)=R_f(x)+R_{res}(x)",
        }
        message = MarkdownMessage(markdown)
        message.set_citations([citation], {7: "P1"})

        self.assertEqual(list(message._citation_groups), ["source-1"])
        self.assertIn("ra-source://source-1", message.document().toHtml())
        self.assertGreaterEqual(
            message.document().toHtml().count("vertical-align:sub"), 3
        )

        restored = MarkdownMessage(markdown)
        restored.set_citations([dict(citation)], {7: "P1"})
        self.assertEqual(list(restored._citation_groups), ["source-1"])
        self.assertIn("ra-source://source-1", restored.document().toHtml())

    def test_latex_metadata_keeps_group_citations_and_proportional_body_font(self) -> None:
        raw_response = r"""## Phân tích

**Kết quả:** Đoạn văn tiếng Việt trước công thức.

- P1 dùng $R(x_t)=R_v(x_t)+R_{res}(x_t)$ để tối ưu tốc độ.
- P2 dùng $\varepsilon(x_t)$ và $\hat{v}_t$ cho dự đoán chuyển động.

\[
\epsilon(x_t)=(\hat{x}_t-x_t)^2
\]

Đoạn văn tiếng Việt sau công thức vẫn dùng font giao diện bình thường.
<!--RA_RESULT
{"support_status":"supported","citations":[{"paper_alias":"P1","paper_id":17,"claim":"P1 dùng $R(x_t)=R_v(x_t)+R_{res}(x_t)$ để tối ưu tốc độ.","page_hint":4,"section":null,"evidence":"distinct evidence for source one"},{"paper_alias":"P2","paper_id":16,"claim":"P2 dùng $\varepsilon(x_t)$ và $\hat{v}_t$ cho dự đoán chuyển động.","page_hint":7,"section":null,"evidence":"distinct evidence for source two"}]}
RA_RESULT-->"""
        result = parse_chat_result(
            raw_response,
            paper_aliases={"P1": 17, "P2": 16},
        )
        self.assertTrue(result.metadata_valid)
        self.assertNotIn("RA_RESULT", result.content)
        self.assertEqual(len(result.citations), 2)
        citations = [
            replace(
                citation,
                verified=True,
                resolved_page=citation.page_hint,
                verification_status="verified",
            )
            for citation in result.citations
        ]

        message = MarkdownMessage(result.content)
        message.set_citations(citations, {17: "P1", 16: "P2"})
        self.assertEqual(len(message._citation_groups), 2)
        self.assertEqual(message.document().toPlainText().count("\ufffc"), 2)

        before = message.document().find("Đoạn văn tiếng Việt trước")
        after = message.document().find("Đoạn văn tiếng Việt sau")
        self.assertFalse(before.isNull())
        self.assertFalse(after.isNull())
        before_font = before.charFormat().font()
        after_font = after.charFormat().font()
        self.assertFalse(before_font.fixedPitch())
        self.assertFalse(after_font.fixedPitch())
        self.assertEqual(before_font.family(), after_font.family())
        self.assertEqual(before_font.italic(), after_font.italic())

        message.set_chat_font_size(18)
        self.assertEqual(len(message._citation_groups), 2)
        self.assertEqual(message.document().toPlainText().count("\ufffc"), 2)

        clicked: list[object] = []
        message.citation_requested.connect(clicked.append)
        message._anchor_clicked(QUrl("ra-source://source-1"))
        message._anchor_clicked(QUrl("ra-source://source-2"))
        self.assertEqual(
            [int(citation["paper_id"]) for citation in clicked],
            [17, 16],
        )

        restored = MarkdownMessage(result.content)
        restored.set_citations(citations, {17: "P1", 16: "P2"})
        self.assertEqual(len(restored._citation_groups), 2)
        self.assertEqual(restored.document().toPlainText().count("\ufffc"), 2)

    def test_ctrl_wheel_zooms_only_chat_content_and_persists(self) -> None:
        _PanelSettings.values = {"ai_chat_font_size": "14"}
        with (
            patch.object(AIChatPanel, "refresh_settings"),
            patch.object(AIChatPanel, "load_initial_chat"),
        ):
            panel = AIChatPanel(
                1,
                repository=object(),
                settings_repository=_PanelSettings,
                summary_repository=object(),
            )
        panel.resize(430, 520)
        panel.show()
        message = panel._append_assistant_message(
            "gemini",
            "model",
            "## Heading\n\n" + ("Body $x_t$ with `code`. " * 90),
        )
        panel._append_message({"role": "user", "content": "User question"})
        QTest.qWait(50)
        self.assertEqual(panel._chat_font_size, 14.0)

        ctrl_up = QWheelEvent(
            QPointF(5, 5),
            QPointF(5, 5),
            QPoint(),
            QPoint(0, 120),
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.ControlModifier,
            Qt.ScrollPhase.NoScrollPhase,
            False,
        )
        QApplication.sendEvent(message.viewport(), ctrl_up)
        QTest.qWait(70)
        self.assertEqual(panel._chat_font_size, 15.0)
        self.assertEqual(message._chat_font_size, 15.0)
        self.assertEqual(_PanelSettings.values["ai_chat_font_size"], "15")
        user = panel.findChild(QLabel, "aiMessageText")
        self.assertIsNotNone(user)
        self.assertIn("15px", user.styleSheet())

        normal_wheel = QWheelEvent(
            QPointF(5, 5),
            QPointF(5, 5),
            QPoint(),
            QPoint(0, -120),
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase,
            False,
        )
        QApplication.sendEvent(message.viewport(), normal_wheel)
        self.assertEqual(panel._chat_font_size, 15.0)

        with (
            patch.object(AIChatPanel, "refresh_settings"),
            patch.object(AIChatPanel, "load_initial_chat"),
        ):
            restored_panel = AIChatPanel(
                1,
                repository=object(),
                settings_repository=_PanelSettings,
                summary_repository=object(),
            )
        self.assertEqual(restored_panel._chat_font_size, 15.0)
        restored_panel._change_chat_font_size(100)
        self.assertEqual(restored_panel._chat_font_size, 22.0)
        restored_panel._change_chat_font_size(-100)
        self.assertEqual(restored_panel._chat_font_size, 11.0)
        restored_panel.close()
        panel.close()

    def test_503_retry_reuses_request_without_duplicate_user_bubble(self) -> None:
        error = classify_provider_error(_Status503("service unavailable"))
        self.assertIsInstance(error, AIProviderUnavailableError)
        _PanelSettings.values = {"ai_chat_font_size": "12.5"}
        with (
            patch.object(AIChatPanel, "refresh_settings"),
            patch.object(AIChatPanel, "load_initial_chat"),
        ):
            panel = AIChatPanel(
                1,
                repository=object(),
                settings_repository=_PanelSettings,
                summary_repository=object(),
            )
        panel.active_conversation_id = 42
        panel.model.addItem("model")
        panel.model.setCurrentText("model")
        panel.composer.setPlainText("Original question")
        emitted: list[tuple[object, ...]] = []
        panel.send_requested.connect(lambda *args: emitted.append(args))
        panel._submit()
        self.assertEqual(len(panel.findChildren(QLabel, "aiMessageText")), 1)
        self.assertFalse(bool(emitted[0][-1]))

        panel.finish_request(
            error=str(error),
            conversation_id=42,
            retryable=True,
        )
        retry = panel.findChild(QPushButton, "aiInlineRetry")
        self.assertIsNotNone(retry)
        QTest.mouseClick(retry, Qt.MouseButton.LeftButton)
        self.assertEqual(len(emitted), 2)
        self.assertTrue(bool(emitted[1][-1]))
        self.assertEqual(len(panel.findChildren(QLabel, "aiMessageText")), 1)
        self.assertEqual(emitted[0][0:7], emitted[1][0:7])
        panel.cancel_request()
        panel.close()

        class _RetryRepository:
            appended: list[tuple[object, ...]] = []

            @staticmethod
            def get_conversation(conversation_id: int, _paper_id: int):
                return {"id": conversation_id, "conversation_type": "solo"}

            @staticmethod
            def list_messages(_conversation_id: int):
                return [
                    {
                        "role": "user",
                        "content": "Original question",
                        "selected_text": None,
                        "selected_page": None,
                    }
                ]

            @classmethod
            def append_message(cls, *args, **kwargs):
                cls.appended.append((*args, kwargs))

        control = AIRequestControl()
        control.set()
        result = AIChatService(repository=_RetryRepository).send_message(
            paper_id=1,
            conversation_id=42,
            provider="gemini",
            model="model",
            pdf_path=Path("unused.pdf"),
            file_hash="unused",
            question="Original question",
            append_user_message=False,
            cancel_event=control,
        )
        self.assertTrue(result.cancelled)
        self.assertEqual(_RetryRepository.appended, [])

    def test_retry_success_replaces_partial_error_and_keeps_selection_context(self) -> None:
        with (
            patch.object(AIChatPanel, "refresh_settings"),
            patch.object(AIChatPanel, "load_initial_chat"),
        ):
            panel = AIChatPanel(
                1,
                repository=object(),
                settings_repository=_PanelSettings,
                summary_repository=object(),
            )
        panel.active_conversation_id = 42
        panel.model.addItem("original-model")
        panel.model.setCurrentText("original-model")
        panel.attach_selection(
            {
                "selectedText": "Exact quoted selection",
                "location": {"segments": [{"page": 7}]},
            }
        )
        panel.composer.setPlainText("Explain this selection")
        emitted: list[tuple[object, ...]] = []
        panel.send_requested.connect(lambda *args: emitted.append(args))
        panel._submit()
        panel.append_stream_chunk(
            42,
            "gemini",
            "original-model",
            "Incomplete response",
        )
        panel.finish_request(
            error="The provider disconnected.",
            conversation_id=42,
            retryable=True,
            partial_message_id=91,
        )

        retry = panel.findChild(QPushButton, "aiInlineRetry")
        self.assertIsNotNone(retry)
        provider_index = panel.provider.findData("custom")
        self.assertGreaterEqual(provider_index, 0)
        panel.provider.setCurrentIndex(provider_index)
        panel.model.setEditText("current-model")
        QTest.mouseClick(retry, Qt.MouseButton.LeftButton)

        self.assertEqual(len(emitted), 2)
        retried = emitted[-1]
        self.assertEqual(retried[1:3], ("custom", "current-model"))
        self.assertEqual(retried[3], "Explain this selection")
        self.assertEqual(retried[4:6], ("Exact quoted selection", 7))
        self.assertEqual(retried[7]["partial_message_id"], 91)
        self.assertTrue(retried[8])
        self.assertEqual(len(panel.findChildren(QLabel, "aiMessageText")), 1)

        panel.append_stream_chunk(42, "custom", "current-model", "Recovered answer")
        panel.finish_request(
            SimpleNamespace(
                conversation_id=42,
                reply="Recovered answer",
                provider="custom",
                model="current-model",
                citations=(),
                metadata_valid=True,
            )
        )
        QTest.qWait(20)
        self.assertEqual(panel.findChildren(QWidget, "aiErrorRow"), [])
        self.assertIsNone(panel.findChild(QPushButton, "aiInlineRetry"))
        assistant_text = [
            message.document().toPlainText()
            for message in panel.findChildren(MarkdownMessage)
        ]
        self.assertEqual(assistant_text, ["Recovered answer"])
        self.assertEqual(len(panel.findChildren(QLabel, "aiMessageText")), 1)
        panel.close()

    def test_retry_failure_keeps_one_error_and_original_group_context(self) -> None:
        with (
            patch.object(AIChatPanel, "refresh_settings"),
            patch.object(AIChatPanel, "load_initial_chat"),
        ):
            panel = AIChatPanel(
                11,
                repository=object(),
                settings_repository=_PanelSettings,
                summary_repository=object(),
            )
        panel.active_conversation_id = 77
        panel.model.addItem("model")
        panel.model.setCurrentText("model")
        original_papers = [
            {
                "id": 11,
                "title": "Paper A",
                "file_path": "a.pdf",
                "file_hash": "hash-a",
                "alias_index": 1,
            },
            {
                "id": 12,
                "title": "Paper B",
                "file_path": "b.pdf",
                "file_hash": "hash-b",
                "alias_index": 2,
            },
        ]
        panel.set_workspace_papers(original_papers, {11: "P1", 12: "P2"})
        panel.set_group_mode(True)
        panel.composer.setPlainText("Compare their methods")
        emitted: list[tuple[object, ...]] = []
        panel.send_requested.connect(lambda *args: emitted.append(args))
        panel._submit()
        panel.finish_request(
            error="First failure",
            conversation_id=77,
            retryable=True,
        )

        panel.set_workspace_papers(
            [
                {"id": 21, "title": "Unrelated C"},
                {"id": 22, "title": "Unrelated D"},
            ],
            {21: "P1", 22: "P2"},
        )
        retry = panel.findChild(QPushButton, "aiInlineRetry")
        self.assertIsNotNone(retry)
        QTest.mouseClick(retry, Qt.MouseButton.LeftButton)
        self.assertEqual(emitted[-1][7]["workspace_papers"], original_papers)
        self.assertTrue(emitted[-1][7]["use_workspace_context"])
        self.assertTrue(emitted[-1][8])

        panel.finish_request(
            error="Second failure",
            conversation_id=77,
            retryable=True,
        )
        QTest.qWait(20)
        self.assertEqual(len(panel.findChildren(QWidget, "aiErrorRow")), 1)
        self.assertEqual(len(panel.findChildren(QPushButton, "aiInlineRetry")), 1)
        self.assertEqual(len(panel.findChildren(QLabel, "aiMessageText")), 1)
        panel.close()

    def test_retry_updates_saved_partial_response_without_duplicate_history(self) -> None:
        class _Repository:
            appended: list[tuple[object, ...]] = []
            updated: list[tuple[object, ...]] = []
            saved: list[tuple[object, ...]] = []

            @staticmethod
            def get_conversation(conversation_id: int, _paper_id: int):
                return {"id": conversation_id, "conversation_type": "solo"}

            @staticmethod
            def list_messages(_conversation_id: int):
                return [
                    {
                        "id": 8,
                        "role": "user",
                        "content": "Explain this selection",
                        "selected_text": "Exact quoted selection",
                        "selected_page": 7,
                    },
                    {
                        "id": 91,
                        "role": "assistant",
                        "content": "Incomplete response",
                    },
                ]

            @classmethod
            def append_message(cls, *args, **kwargs):
                cls.appended.append((*args, kwargs))
                return 92

            @classmethod
            def update_assistant_message(
                cls, message_id, conversation_id, content, **kwargs
            ):
                cls.updated.append(
                    (message_id, conversation_id, content, dict(kwargs))
                )
                return True

            @classmethod
            def save_message_result(cls, *args, **kwargs):
                cls.saved.append((*args, kwargs))

            @staticmethod
            def get_remote_state(*_args):
                return None

            @staticmethod
            def set_remote_state(*_args):
                return None

        class _Provider:
            reusable_document_reference = False

            def __init__(self) -> None:
                self.message = ""
                self.local_history: list[dict[str, object]] = []

            @staticmethod
            def prepare_document(path: Path) -> RemoteDocument:
                return RemoteDocument(file_id=path.name, local_path=str(path))

            @staticmethod
            def validate_document(_document: RemoteDocument) -> bool:
                return True

            def send_message(self, *, message, local_history, **_kwargs):
                self.message = message
                self.local_history = list(local_history)
                return AIReply("Recovered answer", "")

        class _Resolver:
            @staticmethod
            def resolve_result(result):
                return result

        provider = _Provider()
        service = AIChatService(
            repository=_Repository,
            citation_resolver_factory=lambda _path: _Resolver(),
        )
        service._provider = lambda _provider, api_key=None: provider
        control = AIRequestControl()
        control.partial_message_id = 91
        with tempfile.TemporaryDirectory() as temporary:
            pdf_path = Path(temporary) / "paper.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n% retry fixture\n")
            result = service.send_message(
                paper_id=1,
                conversation_id=42,
                provider="openai",
                model="current-model",
                pdf_path=pdf_path,
                file_hash="hash",
                question="Explain this selection",
                selected_text="Exact quoted selection",
                selected_page=7,
                append_user_message=False,
                cancel_event=control,
            )

        self.assertEqual(_Repository.appended, [])
        self.assertEqual(
            _Repository.updated,
            [
                (
                    91,
                    42,
                    "Recovered answer",
                    {"provider": "openai", "model": "current-model"},
                )
            ],
        )
        self.assertEqual(result.assistant_message_id, 91)
        self.assertEqual(_Repository.saved[0][0], 91)
        self.assertEqual(provider.local_history, [])
        self.assertIn("Exact quoted selection", provider.message)
        self.assertIn("page 7", provider.message)

    def test_history_popup_is_tall_fixed_footer_and_hover_geometry_is_stable(self) -> None:
        panel = _HistoryPanel()
        panel.show()
        popup = ChatHistoryPopup(panel)
        popup.show_below(panel.anchor)
        QTest.qWait(30)

        self.assertEqual(popup.height(), 480)
        self.assertTrue(popup.title.isVisible())
        self.assertTrue(popup.new_chat_button.isVisible())
        self.assertGreater(popup.scroll.height(), 350)
        self.assertGreater(
            popup.new_chat_button.geometry().top(),
            popup.scroll.geometry().bottom(),
        )

        rows = popup.findChildren(QWidget, "aiHistoryRow")
        self.assertEqual(rows[0].height(), 52)
        self.assertTrue(all(row.height() == 34 for row in rows[1:]))
        chips = popup.findChildren(QPushButton, "aiHistoryMemberChip")
        self.assertEqual([chip.text() for chip in chips], ["P1", "P2", "P3"])
        self.assertTrue(all(chip.height() == 17 for chip in chips))

        bar = popup.scroll.verticalScrollBar()
        bar.setValue(bar.maximum() // 2)
        QTest.qWait(10)
        scroll_value = bar.value()
        row_geometries = [row.geometry() for row in rows]
        for widget in (
            popup.findChild(QPushButton, "aiHistoryChatButton"),
            chips[0],
            popup.findChild(QPushButton, "aiHistoryDeleteButton"),
        ):
            self.assertIsNotNone(widget)
            QTest.mouseMove(widget, widget.rect().center())
            QTest.qWait(5)
        self.assertEqual(bar.value(), scroll_value)
        self.assertEqual([row.geometry() for row in rows], row_geometries)

        popup.close()
        panel.close()


if __name__ == "__main__":
    unittest.main()
