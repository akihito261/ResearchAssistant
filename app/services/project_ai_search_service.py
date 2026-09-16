from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from app.database.ai_search_repository import AISearchRepository
from app.database.research_summary_repository import ResearchSummaryRepository
from app.services.ai_chat_service import AIChatService


LOGGER = logging.getLogger(__name__)


class ProjectSearchResponseError(ValueError):
    """A provider response that cannot satisfy the Search result contract."""


def _rewrite_reference_numbers(
    answer: str, aliases: dict[int, int]
) -> str:
    """Rewrite model reference numbers without cascading replacements."""
    if not aliases:
        return answer

    def replace(match: re.Match[str]) -> str:
        old = int(match.group(1))
        canonical = aliases.get(old)
        return f"[{canonical}]" if canonical is not None else ""

    rewritten = re.sub(r"\[(\d+)\]", replace, answer)
    return re.sub(r"(\[\d+\])(?:\s*[,;]\s*\1)+", r"\1", rewritten)


@dataclass(frozen=True)
class ProjectSearchResult:
    conversation_id: int
    answer: str
    references: tuple[dict[str, Any], ...]
    provider: str
    model: str


class ProjectAISearchService:
    """Text-only project search over already generated Research Profiles."""

    def __init__(
        self,
        *,
        repository: Any = AISearchRepository,
        summary_repository: Any = ResearchSummaryRepository,
        ai_service: AIChatService | None = None,
    ) -> None:
        self.repository = repository
        self.summary_repository = summary_repository
        self.ai_service = ai_service or AIChatService()

    def search(
        self,
        *,
        project_id: int,
        conversation_id: int | None,
        question: str,
        provider: str,
        model: str,
        append_user_message: bool = True,
    ) -> ProjectSearchResult:
        question = str(question).strip()
        if not question:
            raise ValueError("Enter a question for AI Search.")
        if conversation_id is None:
            conversation_id = self.repository.create_conversation(project_id)
        elif self.repository.get_conversation(project_id, conversation_id) is None:
            raise ValueError("The AI Search conversation no longer exists.")
        history = self.repository.list_messages(conversation_id)
        if append_user_message:
            self.repository.append_message(conversation_id, "user", question)
        elif (
            history
            and history[-1].get("role") == "user"
            and str(history[-1].get("content") or "").strip() == question
        ):
            # The failed user turn is already durable; the prompt carries the
            # question separately, so do not feed it twice during manual Retry.
            history = history[:-1]

        raw_summaries = self.summary_repository.list_ready_for_project(project_id)
        if len(raw_summaries) > 40:
            shortlisted = self.summary_repository.shortlist_for_project(
                project_id, question, limit=20
            )
            raw_summaries = shortlisted or raw_summaries[:20]
        summaries: list[dict[str, Any]] = []
        candidate_ids: set[int] = set()
        for raw_summary in raw_summaries:
            row = dict(raw_summary)
            paper_id = int(row["paper_id"])
            if paper_id in candidate_ids:
                continue
            candidate_ids.add(paper_id)
            summaries.append(row)
        if not summaries:
            raise ValueError(
                "This project has no Research Profiles yet. Read or chat with a paper first."
            )

        allowed: dict[int, dict[str, Any]] = {}
        source_ids: dict[str, int] = {}
        source_blocks: list[str] = []
        for index, row in enumerate(summaries, start=1):
            paper_id = int(row["paper_id"])
            source_id = f"R{index}"
            allowed[paper_id] = row
            source_ids[source_id] = paper_id
            source_blocks.append(
                f"[{source_id}] internal_source_id={source_id}\n"
                f"Title: {row['title']}\n{row['search_text']}"
            )
        prompt = (
            "Research Profile index for the current project:\n\n"
            + "\n\n---\n\n".join(source_blocks)
            + "\n\nUser search question:\n"
            + question
            + "\n\nReturn exactly one JSON object and no Markdown fence: "
              '{"answer":"natural answer placing [[R1]] immediately after a cited paper title",'
              '"references":[{"source_id":"R1"}]}. '
              "Use only internal R identifiers supplied above. Repeat the same R marker "
              "when citing the same paper again. Never invent an R identifier. The app, "
              "not you, assigns final visible citation numbers."
        )
        client = self.ai_service._provider(provider)
        reply = client.send_text_message(
            model=model,
            message=prompt,
            local_history=[
                {"role": row["role"], "content": row["content"]}
                for row in history[-12:]
            ],
            system_instruction=(
                "You search only the supplied Research Profile index. Never invent papers "
                "or use outside knowledge. Answer in the user's language."
            ),
        )
        raw = str(reply.text or "").strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I | re.S).strip()
        start, end = raw.find("{"), raw.rfind("}")
        try:
            parsed = json.loads(raw[start : end + 1])
        except (json.JSONDecodeError, ValueError) as error:
            LOGGER.warning(
                "AI Search structured response was invalid: %s; raw=%r",
                error,
                raw[:1200],
            )
            raise ProjectSearchResponseError(
                "AI Search returned an invalid structured response."
            ) from None
        answer = str(parsed.get("answer") or "").strip() if isinstance(parsed, dict) else ""
        raw_refs = parsed.get("references", []) if isinstance(parsed, dict) else []
        references: list[dict[str, Any]] = []
        paper_numbers: dict[int, int] = {}
        ref_aliases: dict[int, int] = {}
        ordered_raw_refs: list[dict[str, Any]] = []
        uses_internal_sources = bool(
            re.search(r"\[\[R\d+\]\]", answer, flags=re.I)
        )
        if isinstance(raw_refs, list):
            declared_by_source: dict[str, dict[str, Any]] = {}
            for raw_ref in raw_refs:
                if not isinstance(raw_ref, dict):
                    continue
                source_id = str(raw_ref.get("source_id") or "").upper()
                if source_id and source_id not in declared_by_source:
                    declared_by_source[source_id] = raw_ref
                    uses_internal_sources = True

            # Visible numbering belongs to the application and follows first
            # appearance in the answer, not the provider's metadata order.
            seen_source_ids: set[str] = set()
            for source_id in re.findall(r"\[\[(R\d+)\]\]", answer, flags=re.I):
                source_id = source_id.upper()
                if source_id in seen_source_ids or source_id not in source_ids:
                    continue
                seen_source_ids.add(source_id)
                ordered_raw_refs.append(
                    declared_by_source.get(source_id, {"source_id": source_id})
                )
            for raw_ref in raw_refs:
                if not isinstance(raw_ref, dict):
                    continue
                source_id = str(raw_ref.get("source_id") or "").upper()
                if source_id and source_id in seen_source_ids:
                    continue
                if source_id:
                    seen_source_ids.add(source_id)
                ordered_raw_refs.append(raw_ref)

            for raw_ref in ordered_raw_refs:
                try:
                    source_id = str(raw_ref.get("source_id") or "").upper()
                    if source_id:
                        paper_id = int(source_ids[source_id])
                        ref = int(source_id[1:])
                    else:
                        ref = int(raw_ref.get("ref"))
                        paper_id = int(raw_ref.get("paper_id"))
                except (TypeError, ValueError):
                    continue
                except KeyError:
                    continue
                if ref < 1 or paper_id not in allowed:
                    continue
                canonical = paper_numbers.get(paper_id)
                if canonical is None:
                    canonical = len(paper_numbers) + 1
                    paper_numbers[paper_id] = canonical
                    references.append(
                        {
                            "ref": canonical,
                            "paper_id": paper_id,
                            "title": str(allowed[paper_id]["title"]),
                        }
                    )
                ref_aliases[ref] = canonical
        if not answer:
            raise ProjectSearchResponseError(
                "AI Search returned an empty answer."
            )
        def replace_internal_source(match: re.Match[str]) -> str:
            source_id = match.group(1).upper()
            paper_id = source_ids.get(source_id)
            canonical = paper_numbers.get(paper_id) if paper_id is not None else None
            return f"[{canonical}]" if canonical is not None else ""

        answer = re.sub(
            r"\[\[(R\d+)\]\]", replace_internal_source, answer, flags=re.I
        )
        if not uses_internal_sources:
            answer = _rewrite_reference_numbers(answer, ref_aliases)
        self.repository.append_message(
            conversation_id,
            "assistant",
            answer,
            provider=provider,
            model=model,
            references=references,
        )
        return ProjectSearchResult(
            conversation_id=int(conversation_id),
            answer=answer,
            references=tuple(references),
            provider=provider,
            model=model,
        )
