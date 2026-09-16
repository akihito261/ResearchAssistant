from __future__ import annotations

import html
import json
import re
import socket
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.translation.base import TranslationError, TranslationProvider


class MyMemoryTranslationProvider(TranslationProvider):
    """Small no-key provider backed by MyMemory's public REST endpoint."""

    ENDPOINT = "https://api.mymemory.translated.net/get"
    MAX_QUERY_BYTES = 450
    LANGUAGE_PATTERN = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z]{2,4})?$")

    def __init__(self, timeout_seconds: float = 10.0) -> None:
        self.timeout_seconds = max(1.0, float(timeout_seconds))

    def translate(
        self,
        text: str,
        *,
        source_language: str = "en",
        target_language: str = "vi",
    ) -> str:
        normalized_text = text.strip()
        if not normalized_text:
            return ""
        self._validate_language(source_language)
        self._validate_language(target_language)

        translated_chunks = [
            self._translate_chunk(chunk, source_language, target_language)
            for chunk in self._split_utf8(normalized_text)
        ]
        return " ".join(chunk.strip() for chunk in translated_chunks if chunk.strip())

    def _translate_chunk(
        self,
        text: str,
        source_language: str,
        target_language: str,
    ) -> str:
        query = urlencode(
            {
                "q": text,
                "langpair": f"{source_language}|{target_language}",
                "mt": "1",
            }
        )
        request = Request(
            f"{self.ENDPOINT}?{query}",
            headers={
                "Accept": "application/json",
                "User-Agent": "ResearchAssistant/1.0",
            },
        )

        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            raise TranslationError(
                f"Translation service returned HTTP {error.code}."
            ) from error
        except (URLError, TimeoutError, socket.timeout) as error:
            raise TranslationError(
                "Translation service is unavailable. Check your Internet connection."
            ) from error
        except (UnicodeDecodeError, json.JSONDecodeError, OSError) as error:
            raise TranslationError(
                "Translation service returned an unreadable response."
            ) from error

        status = payload.get("responseStatus")
        response_data = payload.get("responseData") or {}
        translated_text = response_data.get("translatedText")
        if status not in (None, 200) or not isinstance(translated_text, str):
            details = payload.get("responseDetails") or "Translation failed."
            raise TranslationError(str(details))

        return html.unescape(translated_text)

    @classmethod
    def _split_utf8(cls, text: str) -> list[str]:
        if len(text.encode("utf-8")) <= cls.MAX_QUERY_BYTES:
            return [text]

        chunks: list[str] = []
        current = ""
        for token in re.split(r"(\s+)", text):
            if not token:
                continue
            candidate = current + token
            if len(candidate.encode("utf-8")) <= cls.MAX_QUERY_BYTES:
                current = candidate
                continue

            if current.strip():
                chunks.append(current.strip())
                current = ""

            if len(token.encode("utf-8")) <= cls.MAX_QUERY_BYTES:
                current = token.lstrip()
                continue

            token_chunk = ""
            for character in token:
                candidate = token_chunk + character
                if len(candidate.encode("utf-8")) > cls.MAX_QUERY_BYTES:
                    if token_chunk:
                        chunks.append(token_chunk)
                    token_chunk = character
                else:
                    token_chunk = candidate
            current = token_chunk

        if current.strip():
            chunks.append(current.strip())
        return chunks

    @classmethod
    def _validate_language(cls, language: str) -> None:
        if not cls.LANGUAGE_PATTERN.fullmatch(language):
            raise ValueError(f"Invalid language code: {language}")
