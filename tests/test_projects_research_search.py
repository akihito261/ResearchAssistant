from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import app.database.database as database
from app.ai.custom_provider import CustomAPIProvider
from app.ai.custom_config import load_custom_api_config
from app.ai.factory import create_provider
from app.database.ai_repository import AIRepository
from app.database.ai_search_repository import AISearchRepository
from app.database.collection_repository import CollectionRepository
from app.database.paper_repository import PaperRepository
from app.database.project_repository import ProjectRepository
from app.database.research_summary_repository import ResearchSummaryRepository
from app.database.settings_repository import SettingsRepository
from app.services.project_ai_search_service import ProjectAISearchService
from app.ui.project_ai_search_dialog import inline_reference_markdown


class _TextProvider:
    def send_text_message(self, **_kwargs):
        return SimpleNamespace(
            text=(
                '{"answer":"Paper A matches [1].",'
                '"references":[{"ref":1,"paper_id":1},'
                '{"ref":2,"paper_id":999}]}'
            )
        )


class _AIService:
    def _provider(self, _provider):
        return _TextProvider()


class _CapturingProvider:
    def __init__(self, text: str) -> None:
        self.text = text
        self.message = ""

    def send_text_message(self, **kwargs):
        self.message = str(kwargs["message"])
        return SimpleNamespace(text=self.text)


class _ConfiguredAIService:
    def __init__(self, provider: _CapturingProvider) -> None:
        self.provider = provider

    def _provider(self, _provider):
        return self.provider


class _SummaryRows:
    rows = []

    @classmethod
    def list_ready_for_project(cls, _project_id):
        return list(cls.rows)


class _LargeSummaryRows:
    shortlist_calls = []

    @classmethod
    def list_ready_for_project(cls, _project_id):
        return [
            {"paper_id": index, "title": f"Paper {index}", "search_text": "bulk"}
            for index in range(1, 42)
        ]

    @classmethod
    def shortlist_for_project(cls, project_id, question, *, limit):
        cls.shortlist_calls.append((project_id, question, limit))
        return [
            {"paper_id": 31, "title": "ROI Shortlist", "search_text": "ROI compression"}
        ]


class ProjectResearchSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.old_data_dir = database.DATA_DIR
        self.old_database_path = database.DATABASE_PATH
        database.DATA_DIR = self.root / "data"
        database.DATABASE_PATH = database.DATA_DIR / "research.db"
        database.init_database()
        self.default_project = ProjectRepository.list_all()[0]
        self.other_project = ProjectRepository.create("Project B")
        self.paper_a = PaperRepository.add_paper(
            "Paper A", "Author A", 2025, None,
            str(self.root / "a.pdf"), "hash-a", 1,
        )
        self.paper_b = PaperRepository.add_paper(
            "Paper B", "Author B", 2026, None,
            str(self.root / "b.pdf"), "hash-b", 1,
        )

    def tearDown(self) -> None:
        database.DATA_DIR = self.old_data_dir
        database.DATABASE_PATH = self.old_database_path
        self.temporary.cleanup()

    def test_project_scope_activity_workspace_and_group_membership(self) -> None:
        project_a = int(self.default_project["id"])
        project_b = int(self.other_project["id"])
        ProjectRepository.add_paper(project_a, self.paper_a)
        ProjectRepository.remove_paper(project_a, self.paper_b)
        ProjectRepository.add_paper(project_b, self.paper_a)
        ProjectRepository.add_paper(project_b, self.paper_b)
        self.assertEqual(
            [int(row["id"]) for row in PaperRepository.list_papers(project_id=project_a)],
            [self.paper_a],
        )
        self.assertFalse(
            ProjectRepository.record_activity(
                project_a, self.paper_a, seconds=119, interactions=3
            )["transitioned_to_reading"]
        )
        self.assertTrue(
            ProjectRepository.record_activity(
                project_a, self.paper_a, seconds=1
            )["transitioned_to_reading"]
        )
        self.assertEqual(ProjectRepository.status(project_b, self.paper_a), "Unread")
        ProjectRepository.save_workspace(project_b, [self.paper_b, self.paper_a], self.paper_a)
        self.assertEqual(
            ProjectRepository.load_workspace(project_b),
            ([self.paper_b, self.paper_a], self.paper_a),
        )
        with self.assertRaises(ValueError):
            AIRepository.create_group_conversation(
                [self.paper_a, self.paper_b], project_id=project_a
            )
        conversation = AIRepository.create_group_conversation(
            [self.paper_a, self.paper_b], project_id=project_b
        )
        self.assertEqual(int(conversation["project_id"]), project_b)

    def test_research_summary_and_search_are_project_scoped(self) -> None:
        project_a = int(self.default_project["id"])
        project_b = int(self.other_project["id"])
        ProjectRepository.add_paper(project_a, self.paper_a)
        ProjectRepository.add_paper(project_b, self.paper_b)
        self.assertTrue(ResearchSummaryRepository.claim_generation(self.paper_a, "hash-a"))
        ResearchSummaryRepository.save(
            self.paper_a,
            {"title": "Paper A", "core_idea": "A searchable method"},
            source_hash="hash-a", provider="gemini", model="model-a",
        )
        self.assertEqual(
            [int(row["paper_id"]) for row in ResearchSummaryRepository.list_ready_for_project(project_a)],
            [self.paper_a],
        )
        self.assertEqual(ResearchSummaryRepository.list_ready_for_project(project_b), [])
        service = ProjectAISearchService(ai_service=_AIService())
        result = service.search(
            project_id=project_a,
            conversation_id=None,
            question="Which paper matches?",
            provider="gemini",
            model="model-a",
        )
        self.assertEqual(result.references[0]["paper_id"], self.paper_a)
        self.assertEqual(len(result.references), 1)
        restored = AISearchRepository.list_messages(result.conversation_id)
        self.assertEqual(restored[-1]["references"][0]["paper_id"], self.paper_a)
        self.assertEqual(
            int(AISearchRepository.list_conversations(project_a)[0]["project_id"]),
            project_a,
        )

    def test_custom_api_uses_runtime_name_url_and_headers(self) -> None:
        provider = create_provider(
            "custom",
            "secret",
            custom_config={
                "name": "Lab Gateway",
                "base_url": "https://gateway.example/v1",
                "headers": {"X-Workspace": "research"},
            },
        )
        self.assertIsInstance(provider, CustomAPIProvider)
        self.assertEqual(provider.display_name, "Lab Gateway")
        self.assertEqual(
            str(provider.client.base_url).rstrip("/"),
            "https://gateway.example/v1",
        )
        provider.client = SimpleNamespace(
            models=SimpleNamespace(
                list=lambda: SimpleNamespace(
                    data=[SimpleNamespace(id="model-b"), SimpleNamespace(id="model-a")]
                )
            )
        )
        message, models = provider.test_connection()
        self.assertIn("Lab Gateway", message)
        self.assertEqual(models, ["model-a", "model-b"])

    def test_collections_with_same_name_are_isolated_by_project(self) -> None:
        project_a = int(self.default_project["id"])
        project_b = int(self.other_project["id"])
        ProjectRepository.add_paper(project_a, self.paper_a)
        ProjectRepository.add_paper(project_b, self.paper_b)
        method_a = CollectionRepository.create("Method", project_id=project_a)
        method_b = CollectionRepository.create("Method", project_id=project_b)
        self.assertNotEqual(method_a, method_b)
        CollectionRepository.add_to_paper(
            self.paper_a, method_a, project_id=project_a
        )
        CollectionRepository.add_to_paper(
            self.paper_b, method_b, project_id=project_b
        )
        self.assertEqual(
            [row["name"] for row in CollectionRepository.list_all(project_a)],
            ["Method", "Neural Codec", "ROI Compression", "Video Compression"],
        )
        self.assertEqual(
            [row["name"] for row in CollectionRepository.list_all(project_b)],
            ["Method"],
        )
        with self.assertRaises(ValueError):
            CollectionRepository.add_to_paper(
                self.paper_a, method_b, project_id=project_a
            )
        self.assertEqual(
            [int(row["id"]) for row in PaperRepository.list_papers(
                project_id=project_a, search="Method"
            )],
            [self.paper_a],
        )
        self.assertEqual(
            [int(row["id"]) for row in PaperRepository.list_papers(
                project_id=project_b, collection_id=method_b
            )],
            [self.paper_b],
        )
        self.assertEqual(
            PaperRepository.list_papers(
                project_id=project_a, collection_id=method_b
            ),
            [],
        )

    def test_search_references_are_inline_and_map_directly_to_paper_ids(self) -> None:
        markdown, mapping = inline_reference_markdown(
            "Paper A supports the result [1], while Paper B differs.",
            [
                {"ref": 1, "paper_id": self.paper_a, "title": "Paper A"},
                {"ref": 2, "paper_id": self.paper_b, "title": "Paper B"},
            ],
        )
        self.assertEqual(mapping, {1: self.paper_a, 2: self.paper_b})
        self.assertIn(
            f"[[1]](ra-paper://paper/{self.paper_a})", markdown
        )
        self.assertIn(
            f"Paper B [[2]](ra-paper://paper/{self.paper_b})", markdown
        )

    def test_fresh_custom_api_defaults_do_not_overwrite_saved_config(self) -> None:
        fresh = load_custom_api_config(SettingsRepository)
        self.assertEqual(fresh["name"], "Vilao")
        self.assertEqual(fresh["base_url"], "https://api.vilao.ai/v1")
        self.assertEqual(fresh["format"], "openai_compatible")
        self.assertEqual(fresh["headers"], {})

        SettingsRepository.set("custom_api_name", "Lab Gateway")
        SettingsRepository.set(
            "custom_api_base_url", "https://gateway.example/v1"
        )
        SettingsRepository.set_json("custom_api_headers", {"X-Lab": "one"})
        saved = load_custom_api_config(SettingsRepository)
        self.assertEqual(saved["name"], "Lab Gateway")
        self.assertEqual(saved["base_url"], "https://gateway.example/v1")
        self.assertEqual(saved["headers"], {"X-Lab": "one"})

    def test_search_deduplicates_candidates_and_citations_by_paper_id(self) -> None:
        project_id = int(self.default_project["id"])
        ProjectRepository.add_paper(project_id, self.paper_a)
        ProjectRepository.add_paper(project_id, self.paper_b)
        _SummaryRows.rows = [
            {
                "paper_id": self.paper_a,
                "title": "Paper A",
                "search_text": "Method A",
            },
            {
                "paper_id": self.paper_a,
                "title": "Paper A duplicate join row",
                "search_text": "Method A duplicate",
            },
            {
                "paper_id": self.paper_b,
                "title": "Paper B",
                "search_text": "Method B",
            },
        ]
        provider = _CapturingProvider(
            '{"answer":"Paper A [1], [3]. Paper A again [3]. Paper B [2].",'
            f'"references":[{{"ref":1,"paper_id":{self.paper_a}}},'
            f'{{"ref":3,"paper_id":{self.paper_a}}},'
            f'{{"ref":2,"paper_id":{self.paper_b}}}]}}'
        )
        service = ProjectAISearchService(
            summary_repository=_SummaryRows,
            ai_service=_ConfiguredAIService(provider),
        )
        result = service.search(
            project_id=project_id,
            conversation_id=None,
            question="Compare them",
            provider="custom",
            model="model",
        )
        self.assertEqual(
            [(row["ref"], row["paper_id"]) for row in result.references],
            [(1, self.paper_a), (2, self.paper_b)],
        )
        self.assertEqual(result.answer.count("[1]"), 2)
        self.assertNotIn("[3]", result.answer)
        self.assertEqual(provider.message.count("Title: Paper A\n"), 1)
        self.assertEqual(provider.message.count("internal_source_id=R1"), 1)

    def test_search_numbers_sources_by_first_answer_appearance(self) -> None:
        project_id = int(self.default_project["id"])
        _SummaryRows.rows = [
            {"paper_id": self.paper_a, "title": "Paper A", "search_text": "A"},
            {"paper_id": self.paper_b, "title": "Paper B", "search_text": "B"},
        ]
        provider = _CapturingProvider(
            '{"answer":"Paper B [[R2]]. Paper A [[R1]]. Paper B again [[R2]].",'
            '"references":[{"source_id":"R1"},{"source_id":"R2"}]}'
        )
        result = ProjectAISearchService(
            summary_repository=_SummaryRows,
            ai_service=_ConfiguredAIService(provider),
        ).search(
            project_id=project_id,
            conversation_id=None,
            question="Compare",
            provider="custom",
            model="model",
        )
        self.assertEqual(
            [(row["ref"], row["paper_id"]) for row in result.references],
            [(1, self.paper_b), (2, self.paper_a)],
        )
        self.assertEqual(result.answer, "Paper B [1]. Paper A [2]. Paper B again [1].")

    def test_search_uses_project_fts_shortlist_above_threshold(self) -> None:
        project_id = int(self.default_project["id"])
        _LargeSummaryRows.shortlist_calls = []
        provider = _CapturingProvider('{"answer":"ROI Shortlist.","references":[]}')
        ProjectAISearchService(
            summary_repository=_LargeSummaryRows,
            ai_service=_ConfiguredAIService(provider),
        ).search(
            project_id=project_id,
            conversation_id=None,
            question="ROI compression",
            provider="custom",
            model="model",
        )
        self.assertEqual(
            _LargeSummaryRows.shortlist_calls,
            [(project_id, "ROI compression", 20)],
        )
        self.assertIn("Title: ROI Shortlist", provider.message)
        self.assertNotIn("Title: Paper 1\n", provider.message)

    def test_research_profile_lifecycle_and_project_fts_shortlist(self) -> None:
        project_a = int(self.default_project["id"])
        project_b = int(self.other_project["id"])
        ProjectRepository.add_paper(project_a, self.paper_a)
        ProjectRepository.add_paper(project_b, self.paper_b)
        self.assertTrue(
            ResearchSummaryRepository.claim_generation(self.paper_a, "hash-a")
        )
        pending = ResearchSummaryRepository.get(self.paper_a)
        self.assertEqual(pending["profile_status"], "pending")
        ResearchSummaryRepository.fail(self.paper_a, "provider unavailable")
        self.assertFalse(
            ResearchSummaryRepository.claim_generation(self.paper_a, "hash-a")
        )
        self.assertTrue(
            ResearchSummaryRepository.claim_generation(
                self.paper_a, "hash-a", retry_failed=True
            )
        )
        ResearchSummaryRepository.save(
            self.paper_a,
            {
                "title": "Paper A",
                "research_problem": "semantic ROI compression",
                "search_keywords_en": ["ROI", "compression"],
                "search_keywords_vi": ["vùng quan tâm", "nén"],
            },
            source_hash="hash-a",
            provider="custom",
            model="model-a",
        )
        ready = ResearchSummaryRepository.get(self.paper_a)
        self.assertEqual(ready["profile_status"], "ready")
        self.assertTrue(ready["generated_at"])
        shortlisted = ResearchSummaryRepository.shortlist_for_project(
            project_a, "ROI compression"
        )
        self.assertEqual([row["paper_id"] for row in shortlisted], [self.paper_a])
        self.assertEqual(
            ResearchSummaryRepository.shortlist_for_project(
                project_b, "ROI compression"
            ),
            [],
        )
        self.assertTrue(
            ResearchSummaryRepository.mark_stale_if_source_changed(
                self.paper_a, "new-hash"
            )
        )
        self.assertEqual(
            ResearchSummaryRepository.get(self.paper_a)["profile_status"],
            "stale",
        )
        self.assertEqual(
            ResearchSummaryRepository.shortlist_for_project(
                project_a, "ROI compression"
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
