from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import logging
import json
from pathlib import Path
import re
from threading import Event, Lock
from time import monotonic
from typing import Any, Callable

from app.ai import (
    AIDocumentUnavailableError,
    AIStateUnavailableError,
    AISummaryFormatError,
    RemoteDocument,
    create_provider,
)
from app.ai.custom_config import load_custom_api_config
from app.database.ai_repository import AIRepository
from app.database.settings_repository import SettingsRepository
from app.services.ai_credential_service import AICredentialService
from app.services.library_path_service import resolve_paper_path
from app.services.pdf_service import calculate_sha256
from app.services.citation_service import (
    AISummaryResult,
    Citation,
    CitationResolver,
    NormalizedAIResult,
    parse_chat_result,
    parse_summary_result,
)


LOGGER = logging.getLogger(__name__)


_CHAT_GROUNDING_INSTRUCTION = """

Return the user-visible answer first. Ground claims only in the attached original
English PDF. Never fill missing facts with outside knowledge or guesses. If only
part of the question is answered by the paper, answer that part and explicitly
say what is missing. Label any reasonable inference as an inference.

At the very end, append exactly one invisible metadata block in this form:
<!--RA_RESULT
{"support_status":"supported|partially_supported|not_found","citations":[{"claim":"the exact user-visible sentence or paragraph supported by this source","page_hint":5,"section":"III-B or null","evidence":"a short exact English evidence quote"}]}
RA_RESULT-->

Use only one of the three support_status values. Include citation metadata for
each supported factual bullet or compact factual block, especially blocks with
numbers, percentages, units, datasets, hardware/platforms, algorithms/methods,
baselines, experimental settings, comparisons, measured speed, latency, energy,
memory, or explicit author claims. One strong source may support several closely
related facts in the same block; do not create citations merely for grammatical
sentence boundaries. Do not cite a clearly labelled inference as direct paper evidence.
Every citation needs a short, distinctive, exact English evidence excerpt (normally
about 6-12 words) from the PDF; never invent evidence or quote long passages. For
not_found, use an empty citation list and plainly say the paper does not provide the
requested information. The claim value must copy the exact complete user-visible
bullet, sentence, or paragraph from your answer that the citation supports, so the UI
can place its source badge directly after it. Reuse the same evidence when it genuinely
supports more than one claim. For greetings or other non-factual conversation, an
empty citation list is fine.
When a claim contains LaTeX, JSON-escape every backslash (for example
"\\\\hat") so the RA_RESULT block always remains valid JSON.
Do not show or explain the metadata block to the user.
""".strip()

_MULTI_PAPER_GROUNDING_INSTRUCTION = """
This is an explicit comparison across all attached papers. The PDFs are attached
with request-local source labels. The Workspace papers map below is the authority
for each attachment's stable P alias and integer paper_id. Compare only those
papers and keep each paper's claims distinct. Every citation object in RA_RESULT must additionally
include the exact integer paper_id from this list. Never attribute evidence from
one paper to another. Emit one flat citation object per source directly inside
the citations array. Never combine multiple paper_ids, pages, evidence excerpts,
or destinations into one citation object, nested array, sources object, or
citation cluster. Do not write visible citation markers such as [P1], [P1.P2],
or [P1 · p.6]; the application renders independent clickable badges from the
RA_RESULT metadata.

For every paper-grounded comparison or factual claim, include its evidence in
RA_RESULT. Each citation object must contain exactly one paper_alias (for example
"P1"), the matching integer paper_id, one page_hint, one short exact English
evidence quote, and the exact claim text. If a claim compares two papers, emit two
separate citation objects - one for each paper. Never omit citations merely because
the answer uses more than one attached PDF. Keep the visible answer to at most
ten compact factual blocks (normally under 900 words) unless the user explicitly
requests more detail. Completing the full RA_RESULT metadata trailer has priority
over extra prose.
""".strip()

_SUMMARY_PROMPT_TEMPLATE = """Create a concise research-note summary of the attached
original English paper. Write every content field in __OUTPUT_LANGUAGE__ while
preserving model names, datasets, benchmarks, metrics, acronyms, algorithms,
formulae, technical terms that would lose accuracy in translation, and numeric
values accurately.

Return one JSON object only. Its structure is language-independent. The top-level
object must contain all nine canonical English keys exactly as written below.
Every one of these nine values must be a string. Never translate, capitalize,
rename, or omit these keys:
problem, contribution, method, dataset, baseline, results, limitations,
unclear_points, ideas.

Also return `_citation_metadata`, keyed by those same nine canonical names. Each
metadata value uses this shape:
{"support_status":"supported|partially_supported|not_found","citations":[{"claim":"exact sentence or paragraph copied from the canonical string field","page_hint":1,"section":null,"evidence":"short exact English quote"}]}

Only the nine string values and "claim" use __OUTPUT_LANGUAGE__. JSON keys,
support_status values, and citation property names must remain canonical English.

Rules:
- Keep every field concise. Problem, Contribution, Dataset, and Baseline should
  normally be one or two short sentences or bullets. Method and Results may use
  up to three short sentences or bullets. Keep the remaining fields brief.
- Do not invent missing facts. If the paper does not provide a field, still return
  that canonical key; state in the requested output language that the paper does
  not provide the information, set not_found, and use an empty citation list.
- Use partially_supported when only some requested detail is evidenced and say
  what is missing.
- Add citations only for the most important supported claims in Problem,
  Contribution, Method, and Results. Other fields should normally use an empty
  citation list.
- Use at most one citation in each of those four fields. Its English evidence
  must be a short distinctive excerpt of about 6-12 words, never a sentence,
  paragraph, abstract passage, or long verbatim quotation.
- Every citation claim must exactly copy the complete sentence or paragraph in the
  field that it supports. Reuse one claim value when several sources support it.
- Prefer one strong source for each important claim and never repeat
  near-identical evidence across fields.
- Limitations must be explicitly stated or clearly evidenced by the paper.
- Ideas may contain AI inference, but every idea must be clearly labelled as a
  suggested idea or inference in the requested output language, never as an
  author contribution.
- Ideas normally need no citation. If a source is necessary as inspiration or
  context, include at most one and never present it as proof the authors proposed it.
- Do not wrap the JSON in Markdown and do not add text outside the JSON.
"""

_OUTPUT_LANGUAGE_NAMES = {
    "vi": "natural Vietnamese (vi)",
    "en": "natural English (en)",
    "fr": "natural French (fr)",
    "de": "natural German (de)",
    "ja": "natural Japanese (ja)",
    "ko": "natural Korean (ko)",
    "zh-cn": "natural Simplified Chinese (zh-CN)",
}


def _summary_prompt(output_language: str) -> str:
    code = str(output_language or "vi").strip() or "vi"
    language = _OUTPUT_LANGUAGE_NAMES.get(
        code.casefold(),
        f'the language identified by code "{code}"',
    )
    return _SUMMARY_PROMPT_TEMPLATE.replace("__OUTPUT_LANGUAGE__", language)


@dataclass(frozen=True)
class AIChatResult:
    conversation_id: int
    reply: str
    provider: str
    model: str
    assistant_message_id: int | None = None
    remote_state_id: str | None = None
    cancelled: bool = False
    support_status: str = "not_found"
    citations: tuple[Citation, ...] = ()
    metadata_valid: bool = True


@dataclass
class AIRequestControl:
    """Thread-safe cancellation and partial-message coordination."""

    event: Event = field(default_factory=Event)
    lock: Lock = field(default_factory=Lock)
    partial_message_id: int | None = None
    _closer: Callable[[], None] | None = None

    def set(self) -> None:
        self.event.set()
        with self.lock:
            closer = self._closer
        if closer is not None:
            try:
                closer()
            except Exception:
                pass

    def is_set(self) -> bool:
        return self.event.is_set()

    def register_closer(self, closer: Callable[[], None]) -> None:
        with self.lock:
            self._closer = closer
        if self.is_set():
            self.set()

    def clear_closer(self) -> None:
        with self.lock:
            self._closer = None


class _ChunkBatcher:
    """Emit the first delta immediately, then batch worker-to-UI traffic."""

    INTERVAL_SECONDS = 0.045

    def __init__(self, callback: Callable[[str], None], cancel_event: Any) -> None:
        self.callback = callback
        self.cancel_event = cancel_event
        self.pending: list[str] = []
        self.last_emit = 0.0

    def add(self, chunk: str) -> None:
        if not chunk or self.cancel_event.is_set():
            return
        now = monotonic()
        if self.last_emit == 0.0:
            self.callback(chunk)
            self.last_emit = now
            return
        self.pending.append(chunk)
        if now - self.last_emit >= self.INTERVAL_SECONDS:
            self.flush()

    def flush(self) -> None:
        if not self.pending or self.cancel_event.is_set():
            self.pending.clear()
            return
        value = "".join(self.pending)
        self.pending.clear()
        self.callback(value)
        self.last_emit = monotonic()


def _expired(value: str | None) -> bool:
    if not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed <= datetime.now(timezone.utc)
    except ValueError:
        return True


_FACTUAL_BLOCK_TERMS = (
    "dataset", "benchmark", "baseline", "algorithm", "method", "model",
    "hardware", "platform", "device", "processor", "gpu", "cpu", "arm",
    "accuracy", "performance", "speed", "latency", "energy", "memory",
    "throughput", "frame", "author", "experiment", "comparison",
)


def _factual_block_count(content: str) -> int:
    """Return a conservative diagnostic estimate; never drives source display."""
    blocks = re.split(
        r"\n\s*\n|\n(?=\s*(?:[-*\u2022]|\d+[.)])\s+)",
        str(content or ""),
    )
    count = 0
    for block in blocks:
        normalized = " ".join(block.casefold().split())
        if not normalized:
            continue
        if re.search(r"\d", normalized) or any(
            term in normalized for term in _FACTUAL_BLOCK_TERMS
        ):
            count += 1
    return count


class AIChatService:
    RECENT_MESSAGE_COUNT = 8
    COMPACT_THRESHOLD = 10

    def __init__(
        self,
        *,
        repository: Any = AIRepository,
        credential_service: Any = AICredentialService,
        citation_resolver_factory: Any = CitationResolver,
        settings_repository: Any = SettingsRepository,
    ) -> None:
        self.repository = repository
        self.credential_service = credential_service
        self.citation_resolver_factory = citation_resolver_factory
        self.settings_repository = settings_repository
        self._local_document_cache: dict[tuple[str, str, str], RemoteDocument] = {}

    def _provider(
        self,
        provider: str,
        api_key: str | None = None,
        provider_config: Mapping[str, object] | None = None,
    ):
        key = api_key if api_key is not None else self.credential_service.get_api_key(provider)
        config = None
        if provider.strip().lower() == "custom":
            if provider_config is not None:
                config = dict(provider_config)
                return create_provider(provider, key, custom_config=config)
            config = load_custom_api_config(self.settings_repository)
        return create_provider(provider, key, custom_config=config)

    def test_connection(
        self,
        provider: str,
        api_key: str | None = None,
        provider_config: Mapping[str, object] | None = None,
    ) -> tuple[str, list[str]]:
        client = self._provider(provider, api_key, provider_config)
        return client.test_connection()

    @staticmethod
    def _memory_line(message: dict[str, Any]) -> str:
        role = "User" if message.get("role") == "user" else "Assistant"
        if role == "Assistant" and message.get("provider"):
            provider = str(message.get("provider") or "").title()
            model = str(message.get("model") or "").strip()
            role = f"Assistant ({provider}{f' / {model}' if model else ''})"
        content_value = str(message.get("content") or "")
        if message.get("role") == "user" and message.get("selected_text"):
            content_value = AIChatService._message(
                content_value,
                str(message.get("selected_text") or ""),
                message.get("selected_page"),
            )
        content = " ".join(content_value.split())
        if len(content) > 500:
            content = content[:497].rstrip() + "…"
        return f"{role}: {content}"

    def _context_history(
        self,
        conversation: dict[str, Any],
        history: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        summary = str(conversation.get("rolling_summary") or "").strip()
        checkpoint = int(conversation.get("memory_through_message_id") or 0)
        recent = history

        if len(history) > self.COMPACT_THRESHOLD:
            recent = history[-self.RECENT_MESSAGE_COUNT :]
            compactable = [
                message
                for message in history[: -self.RECENT_MESSAGE_COUNT]
                if int(message["id"]) > checkpoint
            ]
            if compactable:
                addition = "\n".join(self._memory_line(item) for item in compactable)
                summary = "\n".join(part for part in (summary, addition) if part)
                if len(summary) > 12_000:
                    summary = (
                        summary[:4_000].rstrip()
                        + "\n[…older memory compacted…]\n"
                        + summary[-7_500:].lstrip()
                    )
                checkpoint = int(compactable[-1]["id"])
                self.repository.update_memory(
                    int(conversation["id"]),
                    summary,
                    checkpoint,
                )

        context: list[dict[str, Any]] = []
        if summary:
            context.append(
                {
                    "role": "user",
                    "content": (
                        "Conversation memory from earlier turns. Treat it as context, "
                        "not as a new question:\n" + summary
                    ),
                }
            )
        for item in recent:
            content = str(item.get("content") or "")
            if item.get("role") == "user" and item.get("selected_text"):
                content = self._message(
                    content,
                    str(item.get("selected_text") or ""),
                    item.get("selected_page"),
                )
            context.append({"role": item.get("role"), "content": content})
        return context

    def _remote_document(
        self,
        client: Any,
        *,
        paper_id: int,
        provider: str,
        pdf_path: Path,
        file_hash: str,
        force_upload: bool = False,
        operation: str = "",
    ) -> RemoteDocument:
        if not bool(getattr(client, "reusable_document_reference", True)):
            cache_key = (provider, str(pdf_path.resolve(strict=False)), file_hash)
            if not force_upload:
                cached = self._local_document_cache.get(cache_key)
                if cached is not None and client.validate_document(cached):
                    if operation == "summary":
                        LOGGER.info("[SUMMARY] pdf_reference=reused_local")
                    LOGGER.debug(
                        "Reusing prepared local AI document (paper_id=%s, provider=%s)",
                        paper_id,
                        provider,
                    )
                    return cached
            prepared = client.prepare_document(pdf_path)
            self._local_document_cache[cache_key] = prepared
            if operation == "summary":
                LOGGER.info("[SUMMARY] pdf_reference=prepared_local")
            LOGGER.debug(
                "Prepared local AI document (paper_id=%s, provider=%s)",
                paper_id,
                provider,
            )
            return prepared
        cache_identity = str(
            getattr(client, "document_cache_identity", "") or ""
        ).strip()
        cached_source_hash = (
            f"{file_hash}:{cache_identity}"
            if provider == "gemini" and cache_identity
            else file_hash
        )
        stored = None if force_upload else self.repository.get_document_ref(paper_id, provider)
        reuploading = bool(force_upload)
        if stored and str(stored["source_file_hash"]) != cached_source_hash:
            if provider == "gemini":
                LOGGER.warning(
                    "Cached Gemini file inaccessible with current credentials; "
                    "re-uploading paper."
                )
                self.repository.delete_document_ref(paper_id, provider)
                reuploading = True
            stored = None
        if (
            stored
            and str(stored["source_file_hash"]) == cached_source_hash
            and not _expired(stored.get("expires_at"))
        ):
            cached_document = RemoteDocument(
                file_id=str(stored["remote_file_id"]),
                uri=stored.get("remote_uri"),
                mime_type=str(stored.get("mime_type") or "application/pdf"),
                expires_at=stored.get("expires_at"),
            )
            if provider != "gemini" or client.validate_document(cached_document):
                if operation == "summary":
                    LOGGER.info("[SUMMARY] pdf_reference=reused_remote")
                LOGGER.debug(
                    "Reusing remote AI document reference (paper_id=%s, provider=%s)",
                    paper_id,
                    provider,
                )
                return cached_document
            LOGGER.warning(
                "Cached Gemini file inaccessible with current credentials; "
                "re-uploading paper."
            )
            self.repository.delete_document_ref(paper_id, provider)
            reuploading = True

        uploaded = client.prepare_document(pdf_path)
        if operation == "summary":
            LOGGER.info("[SUMMARY] pdf_reference=uploaded_remote")
        self.repository.upsert_document_ref(
            paper_id,
            provider,
            cached_source_hash,
            uploaded.file_id,
            remote_uri=uploaded.uri,
            mime_type=uploaded.mime_type,
            expires_at=uploaded.expires_at,
        )
        LOGGER.debug(
            "Uploaded AI document reference (paper_id=%s, provider=%s)",
            paper_id,
            provider,
        )
        if provider == "gemini" and reuploading:
            LOGGER.info("Gemini file re-uploaded successfully.")
        return uploaded

    @staticmethod
    def _message(question: str, selected_text: str | None, selected_page: int | None) -> str:
        if not selected_text:
            return question
        page = f" on PDF page {selected_page}" if selected_page else ""
        return (
            f"The user attached this exact selection{page}:\n"
            f"---\n{selected_text}\n---\n"
            f"Question:\n{question}"
        )

    @staticmethod
    def _grounded_message(message: str, response_language: str | None = None) -> str:
        raw_language = str(response_language or "").strip()
        language_code = raw_language.casefold()
        language_instruction = None
        if language_code == "en":
            language_instruction = "Answer in English."
        elif language_code == "vi":
            language_instruction = (
                "Answer in Vietnamese. Preserve useful technical terms and acronyms "
                "in English where appropriate."
            )
        elif re.fullmatch(r"[a-z]{2,3}(?:-[a-z]{2,4})?", language_code):
            language = _OUTPUT_LANGUAGE_NAMES.get(
                language_code,
                f'the language identified by code "{raw_language}"',
            )
            language_instruction = (
                f"Answer in {language}. Preserve useful technical terms and acronyms "
                "in English where appropriate."
            )
        parts = [message, _CHAT_GROUNDING_INSTRUCTION]
        if language_instruction:
            parts.append(language_instruction)
        return "\n\n".join(parts)

    @staticmethod
    def _verification_failed(result: NormalizedAIResult) -> NormalizedAIResult:
        return replace(
            result,
            citations=tuple(
                replace(citation, verification_status="verification_error")
                for citation in result.citations
            ),
        )

    @staticmethod
    def _requires_paper_evidence(question: str) -> bool:
        normalized = " ".join(question.casefold().strip(" .!?…").split())
        return normalized not in {
            "hi", "hello", "hey", "chào", "xin chào", "cảm ơn", "thanks", "thank you"
        }

    @staticmethod
    def _generate(
        client: Any,
        *,
        model: str,
        message: str,
        document: RemoteDocument | list[RemoteDocument],
        previous_state_id: str | None,
        local_history: list[dict[str, Any]],
        on_chunk: Callable[[str], None] | None,
        cancel_event: Any,
    ):
        if on_chunk is not None and hasattr(client, "stream_message"):
            batcher = _ChunkBatcher(on_chunk, cancel_event)
            try:
                return client.stream_message(
                    model=model,
                    message=message,
                    document=document,
                    previous_state_id=previous_state_id,
                    local_history=local_history,
                    on_chunk=batcher.add,
                    cancel_event=cancel_event,
                )
            finally:
                batcher.flush()
        return client.send_message(
            model=model,
            message=message,
            document=document,
            previous_state_id=previous_state_id,
            local_history=local_history,
        )

    @staticmethod
    def _request_document_contexts(
        conversation: Mapping[str, Any],
        *,
        paper_id: int,
        pdf_path: Path,
        file_hash: str,
        workspace_papers: list[Mapping[str, Any]] | None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Build sources without letting current tab state redefine a saved group."""
        is_group_conversation = (
            str(conversation.get("conversation_type") or "solo").casefold()
            == "group"
        )
        candidates: list[Mapping[str, Any]] = []
        if is_group_conversation:
            # Persisted active membership and alias_index are authoritative on
            # history restore. Never add the current tab to this collection.
            candidates = [
                item
                for item in (conversation.get("members") or [])
                if isinstance(item, Mapping)
            ]
        elif workspace_papers and len(workspace_papers) >= 2:
            candidates = [
                item for item in workspace_papers if isinstance(item, Mapping)
            ]

        contexts: list[dict[str, Any]] = []
        seen_ids: set[int] = set()
        seen_aliases: set[int] = set()
        for position, item in enumerate(candidates, start=1):
            try:
                context_id = int(item.get("id", 0))
            except (TypeError, ValueError):
                continue
            if context_id < 1 or context_id in seen_ids:
                continue
            try:
                alias_index = int(item.get("alias_index") or position)
            except (TypeError, ValueError):
                alias_index = position
            if alias_index < 1 or alias_index in seen_aliases:
                if is_group_conversation:
                    raise ValueError(
                        "The comparison chat has invalid persisted paper aliases."
                    )
                alias_index = position
                while alias_index in seen_aliases:
                    alias_index += 1
            raw_pdf_path = item.get("pdf_path")
            raw_stored_path = item.get("file_path")
            context_path = (
                Path(str(raw_pdf_path))
                if raw_pdf_path
                else resolve_paper_path(str(raw_stored_path or ""))
            )
            seen_ids.add(context_id)
            seen_aliases.add(alias_index)
            contexts.append(
                {
                    "id": context_id,
                    "title": str(item.get("title") or "Untitled paper"),
                    "pdf_path": context_path,
                    "file_hash": str(item.get("file_hash") or ""),
                    "alias_index": alias_index,
                    "alias": f"P{alias_index}",
                }
            )

        if is_group_conversation and len(contexts) < 2:
            raise ValueError("A comparison chat needs at least two active papers.")
        if len(contexts) < 2:
            contexts = [
                {
                    "id": int(paper_id),
                    "title": "Current paper",
                    "pdf_path": Path(pdf_path),
                    "file_hash": str(file_hash or ""),
                    "alias_index": 1,
                    "alias": "P1",
                }
            ]
        return contexts, len(contexts) >= 2

    @staticmethod
    def _label_documents(
        documents: list[RemoteDocument],
        contexts: list[dict[str, Any]],
        *,
        is_multi_paper: bool,
    ) -> list[RemoteDocument]:
        if not is_multi_paper:
            return documents
        return [
            replace(
                document,
                source_alias=str(context["alias"]),
                source_title=str(context["title"]),
            )
            for document, context in zip(documents, contexts, strict=True)
        ]

    def persist_partial_response(
        self,
        control: AIRequestControl,
        *,
        conversation_id: int,
        provider: str,
        model: str,
        content: str,
    ) -> int | None:
        value = content.rstrip()
        if not value:
            return None
        with control.lock:
            if control.partial_message_id is None:
                control.partial_message_id = self.repository.append_message(
                    conversation_id,
                    "assistant",
                    value,
                    provider=provider,
                    model=model,
                )
            else:
                self.repository.update_assistant_message(
                    control.partial_message_id,
                    conversation_id,
                    value,
                    provider=provider,
                    model=model,
                )
            return control.partial_message_id

    def send_message(
        self,
        *,
        paper_id: int,
        conversation_id: int,
        provider: str,
        model: str,
        pdf_path: Path,
        file_hash: str,
        question: str,
        selected_text: str | None = None,
        selected_page: int | None = None,
        response_language: str | None = None,
        workspace_papers: list[Mapping[str, Any]] | None = None,
        append_user_message: bool = True,
        cancel_event: Any = None,
        on_chunk: Callable[[str], None] | None = None,
    ) -> AIChatResult:
        provider = provider.strip().lower()
        model = model.strip()
        question = question.strip()
        if cancel_event is None:
            cancel_event = AIRequestControl()
        if not model:
            raise ValueError("Choose or enter an AI model in the AI sidebar.")
        if not question:
            raise ValueError("Enter a question for the paper.")
        conversation = self.repository.get_conversation(conversation_id, paper_id)
        if conversation is None:
            raise ValueError("The selected AI chat no longer exists.")
        conversation_id = int(conversation["id"])
        history = self.repository.list_messages(conversation_id)
        if append_user_message:
            self.repository.append_message(
                conversation_id,
                "user",
                question,
                selected_text=selected_text,
                selected_page=selected_page,
            )
        else:
            # Retry uses the original persisted user turn and excludes that
            # failed turn (plus any saved partial response) from provider
            # history so the question is sent exactly once.
            for index in range(len(history) - 1, -1, -1):
                item = history[index]
                if (
                    str(item.get("role") or "") == "user"
                    and str(item.get("content") or "").strip() == question
                ):
                    history = history[:index]
                    break
        if cancel_event is not None and cancel_event.is_set():
            return AIChatResult(
                conversation_id, "", provider, model, cancelled=True
            )
        local_context = self._context_history(conversation, history)
        contexts, is_multi_paper = self._request_document_contexts(
            conversation,
            paper_id=paper_id,
            pdf_path=pdf_path,
            file_hash=file_hash,
            workspace_papers=workspace_papers,
        )
        for context in contexts:
            context_path = context["pdf_path"]
            if not context_path.is_file():
                raise FileNotFoundError(
                    f"The original English PDF no longer exists:\n{context_path}"
                )
            context["file_hash"] = (
                str(context["file_hash"]).strip()
                or calculate_sha256(context_path)
            )
        client = self._provider(provider)
        documents = self._label_documents([
            self._remote_document(
                client,
                paper_id=int(context["id"]),
                provider=provider,
                pdf_path=context["pdf_path"],
                file_hash=str(context["file_hash"]),
            )
            for context in contexts
        ], contexts, is_multi_paper=is_multi_paper)
        document: RemoteDocument | list[RemoteDocument] = (
            documents if is_multi_paper else documents[0]
        )
        if cancel_event is not None and cancel_event.is_set():
            return AIChatResult(
                conversation_id, "", provider, model, cancelled=True
            )
        message = self._grounded_message(
            self._message(question, selected_text, selected_page),
            response_language,
        )
        if is_multi_paper:
            paper_map = "\n".join(
                f"Attachment {position} = {context['alias']}: "
                f"paper_id={context['id']} · "
                f"{context['title']}"
                for position, context in enumerate(contexts, start=1)
            )
            message = (
                f"{message}\n\n{_MULTI_PAPER_GROUNDING_INSTRUCTION}\n"
                f"Workspace papers:\n{paper_map}"
            )
        remote_state_id = (
            None
            if is_multi_paper
            else self.repository.get_remote_state(conversation_id, provider, model)
        )
        try:
            reply = self._generate(
                client,
                model=model,
                message=message,
                document=document,
                previous_state_id=remote_state_id,
                local_history=local_context,
                on_chunk=on_chunk,
                cancel_event=cancel_event,
            )
        except AIStateUnavailableError:
            self.repository.set_remote_state(
                conversation_id, provider, model, None
            )
            if cancel_event is not None and cancel_event.is_set():
                return AIChatResult(
                    conversation_id, "", provider, model, cancelled=True
                )
            reply = self._generate(
                client,
                model=model,
                message=message,
                document=document,
                previous_state_id=None,
                local_history=local_context,
                on_chunk=on_chunk,
                cancel_event=cancel_event,
            )
        except AIDocumentUnavailableError:
            if provider == "gemini":
                LOGGER.warning(
                    "Cached Gemini file inaccessible with current credentials; "
                    "re-uploading paper."
                )
            for context in contexts:
                self.repository.delete_document_ref(int(context["id"]), provider)
            self.repository.set_remote_state(
                conversation_id, provider, model, None
            )
            documents = self._label_documents([
                self._remote_document(
                    client,
                    paper_id=int(context["id"]),
                    provider=provider,
                    pdf_path=context["pdf_path"],
                    file_hash=str(context["file_hash"]),
                    force_upload=True,
                )
                for context in contexts
            ], contexts, is_multi_paper=is_multi_paper)
            document = documents if is_multi_paper else documents[0]
            if cancel_event is not None and cancel_event.is_set():
                return AIChatResult(
                    conversation_id, "", provider, model, cancelled=True
                )
            reply = self._generate(
                client,
                model=model,
                message=message,
                document=document,
                previous_state_id=None,
                local_history=local_context,
                on_chunk=on_chunk,
                cancel_event=cancel_event,
            )
        if cancel_event is not None and cancel_event.is_set():
            return AIChatResult(
                conversation_id,
                reply.text,
                provider,
                model,
                assistant_message_id=getattr(
                    cancel_event, "partial_message_id", None
                ),
                cancelled=True,
            )
        paper_aliases = (
            {
                str(context["alias"]): int(context["id"])
                for context in contexts
            }
            if is_multi_paper
            else None
        )
        normalized = parse_chat_result(
            reply.text, paper_aliases=paper_aliases
        )
        alias_by_paper = {
            int(value): alias
            for alias, value in (paper_aliases or {}).items()
        }
        normalized = replace(
            normalized,
            citations=tuple(
                replace(
                    citation,
                    conversation_id=int(conversation_id),
                    alias=alias_by_paper.get(int(citation.paper_id or 0)),
                )
                for citation in normalized.citations
            ),
        )
        if not is_multi_paper:
            normalized = replace(
                normalized,
                citations=tuple(
                    replace(
                        citation,
                        paper_id=int(paper_id),
                        conversation_id=int(conversation_id),
                    )
                    for citation in normalized.citations
                ),
            )
        if is_multi_paper:
            paths = {
                int(context["id"]): context["pdf_path"]
                for context in contexts
            }
            resolvers: dict[int, Any] = {}
            resolved_citations: list[Citation] = []
            for citation in normalized.citations:
                citation_paper_id = int(citation.paper_id or 0)
                citation_path = paths.get(citation_paper_id)
                if citation_path is None:
                    resolved_citations.append(
                        replace(
                            citation,
                            verification_status="invalid_paper",
                        )
                    )
                    continue
                try:
                    resolver = resolvers.get(citation_paper_id)
                    if resolver is None:
                        resolver = self.citation_resolver_factory(citation_path)
                        resolvers[citation_paper_id] = resolver
                    resolved_citations.extend(
                        resolver.resolve_all((citation,))
                    )
                except (OSError, RuntimeError, ValueError):
                    # One unreadable paper must not discard verified sources
                    # from every other paper in the same group response.
                    resolved_citations.append(
                        replace(
                            citation,
                            verification_status="verification_error",
                        )
                    )
            normalized = replace(
                normalized, citations=tuple(resolved_citations)
            )
        else:
            try:
                normalized = self.citation_resolver_factory(
                    contexts[0]["pdf_path"]
                ).resolve_result(normalized)
            except (OSError, RuntimeError, ValueError):
                normalized = self._verification_failed(normalized)
        if (
            self._requires_paper_evidence(question)
            and normalized.support_status != "not_found"
            and not any(citation.verified for citation in normalized.citations)
        ):
            normalized = replace(normalized, metadata_valid=False)
        factual_blocks = _factual_block_count(normalized.content)
        verified_count = sum(
            citation.verified for citation in normalized.citations
        )
        verification_not_found = sum(
            citation.verification_status == "not_found"
            for citation in normalized.citations
        )
        verification_errors = sum(
            citation.verification_status == "verification_error"
            for citation in normalized.citations
        )
        invalid_evidence = normalized.invalid_citation_count + sum(
            citation.verification_status == "invalid_evidence"
            for citation in normalized.citations
        )
        cited_claims = {
            " ".join(citation.claim_text.casefold().split())
            for citation in normalized.citations
            if citation.claim_text.strip()
        }
        LOGGER.info(
            "[CITATION] factual_blocks=%d metadata_claims=%d verified=%d "
            "rendered=pending metadata_missing=%d uncovered_factual_blocks=%d "
            "invalid_evidence=%d verification_not_found=%d "
            "verification_error=%d duplicate=%d",
            factual_blocks,
            normalized.metadata_claim_count,
            verified_count,
            int(not normalized.metadata_present),
            max(0, factual_blocks - len(cited_claims)),
            invalid_evidence,
            verification_not_found,
            verification_errors,
            normalized.duplicate_citation_count,
        )
        assistant_message_id = None
        partial_message_id = (
            getattr(cancel_event, "partial_message_id", None)
            if cancel_event is not None
            else None
        )
        update_assistant = getattr(
            self.repository, "update_assistant_message", None
        )
        if partial_message_id is not None and callable(update_assistant):
            if update_assistant(
                int(partial_message_id),
                conversation_id,
                normalized.content,
                provider=provider,
                model=model,
            ):
                assistant_message_id = int(partial_message_id)
        if assistant_message_id is None:
            assistant_message_id = self.repository.append_message(
                conversation_id,
                "assistant",
                normalized.content,
                provider=provider,
                model=model,
            )
        save_result = getattr(self.repository, "save_message_result", None)
        if callable(save_result):
            save_result(
                assistant_message_id,
                paper_id,
                normalized.support_status,
                [citation.as_dict() for citation in normalized.citations],
                metadata_valid=normalized.metadata_valid,
            )
        if cancel_event is not None and cancel_event.is_set():
            self.repository.delete_message(
                assistant_message_id,
                conversation_id,
            )
            return AIChatResult(
                conversation_id, "", provider, model, cancelled=True
            )
        self.repository.set_remote_state(
            conversation_id,
            provider,
            model,
            None if is_multi_paper else reply.remote_state_id,
        )
        return AIChatResult(
            conversation_id=conversation_id,
            reply=normalized.content,
            provider=provider,
            model=model,
            assistant_message_id=assistant_message_id,
            remote_state_id=(None if is_multi_paper else reply.remote_state_id or None),
            support_status=normalized.support_status,
            citations=normalized.citations,
            metadata_valid=normalized.metadata_valid,
        )

    def generate_summary(
        self,
        *,
        paper_id: int,
        provider: str,
        model: str,
        pdf_path: Path,
        file_hash: str,
        output_language: str = "vi",
        cancel_event: Any = None,
    ) -> AISummaryResult:
        total_started = monotonic()
        provider = provider.strip().lower()
        model = model.strip()
        if cancel_event is None:
            cancel_event = AIRequestControl()
        if not model:
            raise ValueError("Choose or enter an AI model in the AI sidebar.")
        if not pdf_path.is_file():
            raise FileNotFoundError(
                f"The original English PDF no longer exists:\n{pdf_path}"
            )
        prepare_started = monotonic()
        file_hash = file_hash.strip() or calculate_sha256(pdf_path)
        summary_prompt = _summary_prompt(output_language)
        client = self._provider(provider)
        LOGGER.info("[SUMMARY] provider=%s model=%s", provider, model)
        document = self._remote_document(
            client,
            paper_id=paper_id,
            provider=provider,
            pdf_path=pdf_path,
            file_hash=file_hash,
            operation="summary",
        )
        prepare_seconds = monotonic() - prepare_started
        if cancel_event.is_set():
            raise RuntimeError("Summary generation was cancelled.")
        generation_started = monotonic()
        generation_requests = 0

        def request_summary(active_document: RemoteDocument, prompt: str):
            nonlocal generation_requests
            generation_requests += 1
            try:
                native_summary = getattr(client, "generate_summary", None)
                if callable(native_summary):
                    return native_summary(
                        model=model,
                        message=prompt,
                        document=active_document,
                        cancel_event=cancel_event,
                    )
                return self._generate(
                    client,
                    model=model,
                    message=prompt,
                    document=active_document,
                    previous_state_id=None,
                    local_history=[],
                    on_chunk=lambda _chunk: None,
                    cancel_event=cancel_event,
                )
            except (AIDocumentUnavailableError, AISummaryFormatError):
                raise
            except Exception:
                LOGGER.exception(
                    "[SUMMARY] parsing_failure=provider_error provider=%s model=%s",
                    provider,
                    model,
                )
                LOGGER.info(
                    "[SUMMARY] timing outcome=provider_error prepare=%.3fs "
                    "ai_parse=%.3fs total=%.3fs",
                    prepare_seconds,
                    monotonic() - generation_started,
                    monotonic() - total_started,
                )
                raise

        def parse_reply(reply: Any) -> AISummaryResult:
            raw_output = reply if isinstance(reply, Mapping) else getattr(reply, "text", None)
            LOGGER.info(
                "[SUMMARY] provider_result_type=%s validator_input_type=%s "
                "extracted_output_length=%d",
                type(reply).__name__,
                type(raw_output).__name__,
                len(str(raw_output or "")),
            )
            return parse_summary_result(raw_output, provider=provider, model=model)

        try:
            reply = request_summary(document, summary_prompt)
        except AIDocumentUnavailableError:
            # One bounded recovery for a remote reference that became invalid
            # after its stored expiry/hash checks passed.
            if provider == "gemini":
                LOGGER.warning(
                    "Cached Gemini file inaccessible with current credentials; "
                    "re-uploading paper."
                )
            self.repository.delete_document_ref(paper_id, provider)
            document = self._remote_document(
                client,
                paper_id=paper_id,
                provider=provider,
                pdf_path=pdf_path,
                file_hash=file_hash,
                force_upload=True,
                operation="summary",
            )
            if cancel_event.is_set():
                raise RuntimeError("Summary generation was cancelled.")
            try:
                reply = request_summary(document, summary_prompt)
            except AISummaryFormatError as error:
                LOGGER.warning(
                    "[SUMMARY] parsing_failure=%s provider=%s retryable=%s",
                    error.category,
                    provider,
                    error.retryable,
                )
                raise ValueError("Could not generate the paper summary.") from None
        except AISummaryFormatError as error:
            LOGGER.warning(
                "[SUMMARY] parsing_failure=%s provider=%s retryable=%s",
                error.category,
                provider,
                error.retryable,
            )
            LOGGER.info(
                "[SUMMARY] timing outcome=%s prepare=%.3fs ai_parse=%.3fs total=%.3fs",
                error.category,
                prepare_seconds,
                monotonic() - generation_started,
                monotonic() - total_started,
            )
            if error.category == "truncated_response":
                raise ValueError("The AI summary response was truncated.") from None
            raise ValueError("Could not generate the paper summary.") from None
        if cancel_event.is_set():
            raise RuntimeError("Summary generation was cancelled.")
        parse_seconds = 0.0

        parse_started = monotonic()
        try:
            result = parse_reply(reply)
        except AISummaryFormatError as error:
            parse_seconds += monotonic() - parse_started
            LOGGER.warning(
                "[SUMMARY] parsing_failure=%s paper_id=%s provider=%s "
                "missing_keys=%s retryable=%s",
                error.category,
                paper_id,
                provider,
                list(error.missing_keys),
                error.retryable,
            )
            if not error.retryable:
                LOGGER.info(
                    "[SUMMARY] timing outcome=%s prepare=%.3fs ai_parse=%.3fs total=%.3fs",
                    error.category,
                    prepare_seconds,
                    monotonic() - generation_started,
                    monotonic() - total_started,
                )
                if error.category == "truncated_response":
                    raise ValueError("The AI summary response was truncated.") from None
                raise ValueError("Could not generate the paper summary.") from None
            retry_prompt = (
                summary_prompt
                + "\n\nReturn the result strictly according to the required structured schema."
            )
            try:
                reply = request_summary(document, retry_prompt)
            except AISummaryFormatError as retry_error:
                LOGGER.warning(
                    "[SUMMARY] parsing_failure=%s format_retry_failed=1 "
                    "paper_id=%s provider=%s",
                    retry_error.category,
                    paper_id,
                    provider,
                )
                if retry_error.category == "truncated_response":
                    raise ValueError(
                        "The AI summary response was truncated."
                    ) from None
                raise ValueError("Could not generate the paper summary.") from None
            if cancel_event.is_set():
                raise RuntimeError("Summary generation was cancelled.")
            parse_started = monotonic()
            try:
                result = parse_reply(reply)
            except AISummaryFormatError as retry_error:
                parse_seconds += monotonic() - parse_started
                LOGGER.warning(
                    "[SUMMARY] parsing_failure=%s format_retry_failed=1 "
                    "paper_id=%s provider=%s missing_keys=%s",
                    retry_error.category,
                    paper_id,
                    provider,
                    list(retry_error.missing_keys),
                )
                LOGGER.info(
                    "[SUMMARY] timing outcome=%s prepare=%.3fs ai_parse=%.3fs total=%.3fs",
                    retry_error.category,
                    prepare_seconds,
                    monotonic() - generation_started,
                    monotonic() - total_started,
                )
                raise ValueError(
                    "The AI returned an invalid summary format."
                ) from None
        parse_seconds += monotonic() - parse_started
        generation_seconds = max(
            0.0, monotonic() - generation_started - parse_seconds
        )
        citation_started = monotonic()
        try:
            resolved_result = self.citation_resolver_factory(pdf_path).resolve_summary(result)
        except (OSError, RuntimeError, ValueError):
            LOGGER.exception(
                "Summary citation verification failed "
                "(paper_id=%s, provider=%s)",
                paper_id,
                provider,
            )
            fields = {
                name: replace(
                    field,
                    citations=tuple(
                        replace(citation, verification_status="verification_error")
                        for citation in field.citations
                    ),
                )
                for name, field in result.fields.items()
            }
            resolved_result = replace(result, fields=fields)
        citation_seconds = monotonic() - citation_started
        total_seconds = monotonic() - total_started
        LOGGER.info(
            "Summary timing paper_id=%s provider=%s requests=%d "
            "prepare=%.3fs ai=%.3fs parse=%.3fs citation=%.3fs total=%.3fs",
            paper_id,
            provider,
            generation_requests,
            prepare_seconds,
            generation_seconds,
            parse_seconds,
            citation_seconds,
            total_seconds,
        )
        return resolved_result

    def generate_research_summary(
        self,
        *,
        paper_id: int,
        provider: str,
        model: str,
        pdf_path: Path,
        file_hash: str,
        cancel_event: Any = None,
    ) -> dict[str, Any]:
        """Generate the global Research Profile, separate from sidebar Summary."""
        provider = provider.strip().lower()
        model = model.strip()
        if not model:
            raise ValueError("Choose an AI model before generating a Research Profile.")
        if not pdf_path.is_file():
            raise FileNotFoundError(f"The original English PDF no longer exists:\n{pdf_path}")
        control = cancel_event or AIRequestControl()
        source_hash = file_hash.strip() or calculate_sha256(pdf_path)
        client = self._provider(provider)
        document = self._remote_document(
            client,
            paper_id=int(paper_id),
            provider=provider,
            pdf_path=pdf_path,
            file_hash=source_hash,
            operation="research_summary",
        )
        prompt = """Create a comprehensive retrieval-oriented Research Profile for this paper.
This is an internal search index, not the user-facing Summary sidebar. Use about
500-800 useful words total; be specific and information-dense. Return exactly
one JSON object with these keys: title, research_problem, motivation, core_idea,
method_architecture, models_algorithms, datasets, experiments, important_results,
main_contributions, limitations, possible_applications, terminology,
search_keywords_en, search_keywords_vi. Values must be strings, except both
search_keywords fields must be arrays of concise strings. Do not use Markdown
fences and do not add prose outside JSON. Never invent facts absent from the PDF."""
        reply = self._generate(
            client,
            model=model,
            message=prompt,
            document=document,
            previous_state_id=None,
            local_history=[],
            on_chunk=lambda _chunk: None,
            cancel_event=control,
        )
        raw = str(getattr(reply, "text", "") or "").strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I | re.S).strip()
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("The AI returned an invalid Research Profile format.")
        try:
            value = json.loads(raw[start : end + 1])
        except json.JSONDecodeError as error:
            raise ValueError("The AI returned an invalid Research Profile format.") from error
        required = {
            "title", "research_problem", "motivation", "core_idea",
            "method_architecture", "models_algorithms", "datasets", "experiments",
            "important_results", "main_contributions", "limitations",
            "possible_applications", "terminology", "search_keywords_en",
            "search_keywords_vi",
        }
        if not isinstance(value, dict) or not required.issubset(value):
            raise ValueError("The AI Research Profile is missing required fields.")
        for key in required - {"search_keywords_en", "search_keywords_vi"}:
            if not isinstance(value.get(key), str):
                raise ValueError("The AI Research Profile contains invalid fields.")
        for key in ("search_keywords_en", "search_keywords_vi"):
            if not isinstance(value.get(key), list):
                raise ValueError("The AI Research Profile contains invalid keywords.")
            value[key] = [str(item).strip() for item in value[key] if str(item).strip()]
        return value

    def discard_response(self, result: object) -> None:
        """Remove a response completed after its UI request was cancelled."""
        if not isinstance(result, AIChatResult):
            return
        if result.assistant_message_id is not None:
            self.repository.delete_message(
                result.assistant_message_id,
                result.conversation_id,
            )
            if result.remote_state_id:
                self.repository.delete_remote_state_if_matches(
                    result.conversation_id,
                    result.provider,
                    result.model,
                    result.remote_state_id,
                )
