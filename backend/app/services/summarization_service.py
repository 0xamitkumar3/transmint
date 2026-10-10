"""Transcript summarization with Groq, including chunking and hierarchical merging."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Protocol

from pydantic import ValidationError

from app.core.errors import (
    DependencyUnavailableError,
    InvalidInputError,
    SummarizationError,
    UpstreamRateLimitError,
    UpstreamTimeoutError,
)
from app.schemas.summary import SummaryContent, SummaryType
from app.services.prompts import build_chunk_messages, build_merge_messages, build_repair_message

logger = logging.getLogger(__name__)

MAX_MERGE_DEPTH = 6


# ---------------------------------------------------------------------------
# LLM client
# ---------------------------------------------------------------------------


class LLMClient(Protocol):
    model: str

    def check_available(self) -> None:
        """Raise `DependencyUnavailableError` if the client cannot be used."""

    def complete_json(self, messages: list[dict[str, str]], *, max_tokens: int) -> str:
        """Return the raw text of a chat completion that should contain a JSON object."""


class GroqClient:
    """Groq chat-completions client that maps SDK errors to Transmint errors."""

    def __init__(
        self,
        api_key: str | None,
        model: str,
        *,
        timeout: float = 60.0,
        max_retries: int = 2,
        temperature: float = 0.2,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self.temperature = temperature
        self._client = None

    def check_available(self) -> None:
        if not self.api_key:
            raise DependencyUnavailableError(
                "Summarization is unavailable: GROQ_API_KEY is not configured. "
                "Create a key at https://console.groq.com/keys and set it in backend/.env."
            )

    def _get_client(self):  # noqa: ANN202 - groq.Groq, imported lazily
        self.check_available()
        if self._client is None:
            try:
                from groq import Groq
            except ImportError as exc:
                raise DependencyUnavailableError("The 'groq' package is not installed.") from exc
            # The SDK retries 429/5xx/connection errors with exponential backoff.
            self._client = Groq(api_key=self.api_key, timeout=self.timeout, max_retries=self.max_retries)
        return self._client

    def complete_json(self, messages: list[dict[str, str]], *, max_tokens: int) -> str:
        client = self._get_client()
        import groq
        try:
            response = client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=self.temperature,
                max_completion_tokens=max_tokens,
                response_format={"type": "json_object"},
            )
        except groq.AuthenticationError as exc:
            raise DependencyUnavailableError("Groq rejected the API key. Check GROQ_API_KEY.") from exc
        except groq.RateLimitError as exc:
            retry_after = exc.response.headers.get("retry-after") if exc.response is not None else None
            headers = {"Retry-After": retry_after} if retry_after else None
            raise UpstreamRateLimitError(
                "Groq rate limit reached. Please wait and try again.", headers=headers
            ) from exc
        except groq.APITimeoutError as exc:
            raise UpstreamTimeoutError("The summarization request to Groq timed out.") from exc
        except groq.APIConnectionError as exc:
            raise SummarizationError("Could not connect to the Groq API.") from exc
        except groq.NotFoundError as exc:
            raise DependencyUnavailableError(
                f"Groq model '{self.model}' is not available. Set GROQ_MODEL to a supported model "
                "(see https://console.groq.com/docs/models)."
            ) from exc
        except groq.APIStatusError as exc:
            raise SummarizationError(f"Groq API error (HTTP {exc.status_code}).") from exc

        choice = response.choices[0] if response.choices else None
        content = choice.message.content if choice and choice.message else None
        if not content:
            raise SummarizationError("Groq returned an empty response.")
        if choice.finish_reason == "length":
            raise SummarizationError("Groq response was truncated; try a 'brief' summary or a smaller chunk size.")
        return content


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------


def chunk_text(text: str, max_chars: int, overlap_chars: int = 0) -> list[str]:
    """Split text into chunks of at most `max_chars`, preferring sentence boundaries.

    Consecutive chunks share up to `overlap_chars` of context so ideas spanning a
    boundary are not lost.
    """
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_chars:
        return [text] if text else []
    overlap_chars = min(overlap_chars, max_chars // 4)

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            window = text[start:end]
            # Prefer a sentence end in the last 30% of the window, then any whitespace.
            floor = int(len(window) * 0.7)
            cut = max(window.rfind(p, floor) for p in (". ", "? ", "! "))
            if cut != -1:
                end = start + cut + 1
            else:
                space = window.rfind(" ", floor)
                if space != -1:
                    end = start + space
        chunks.append(text[start:end].strip())
        if end >= len(text):
            break
        next_start = end - overlap_chars
        if overlap_chars:
            # Start the overlap on a word boundary.
            space = text.find(" ", next_start, end)
            next_start = space + 1 if space != -1 else next_start
        start = max(next_start, start + 1)
    return [c for c in chunks if c]


def parse_summary_json(raw: str) -> SummaryContent:
    """Extract and validate a `SummaryContent` object from raw model output."""
    cleaned = raw.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found")
    data = json.loads(cleaned[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("top-level JSON value is not an object")
    return SummaryContent.model_validate(data)


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def drop_unsupported_details(content: SummaryContent, source_text: str) -> SummaryContent:
    """Null out action-item owners/deadlines that do not literally appear in the source.

    A safety net against the model inventing names or dates.
    """
    haystack = f" {_normalize(source_text)} "
    for item in content.action_items:
        if item.owner and f" {_normalize(item.owner)} " not in haystack:
            logger.info("Dropping unsupported action-item owner %r", item.owner)
            item.owner = None
        if item.deadline:
            words = [w for w in _normalize(item.deadline).split() if w not in {"by", "on", "before", "until", "the"}]
            if not words or f" {' '.join(words)} " not in haystack:
                logger.info("Dropping unsupported action-item deadline %r", item.deadline)
                item.deadline = None
    return content


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


@dataclass
class SummaryOutcome:
    content: SummaryContent
    chunk_count: int
    model: str


class SummarizationService:
    """Summarizes transcripts. Long inputs are chunked and merged hierarchically."""

    def __init__(
        self,
        llm: LLMClient,
        *,
        chunk_chars: int = 12_000,
        overlap_chars: int = 300,
        max_transcript_chars: int = 400_000,
        final_max_tokens: int = 4096,
        partial_max_tokens: int = 2048,
    ) -> None:
        self.llm = llm
        self.chunk_chars = chunk_chars
        self.overlap_chars = overlap_chars
        self.max_transcript_chars = max_transcript_chars
        self.final_max_tokens = final_max_tokens
        self.partial_max_tokens = partial_max_tokens

    def check_available(self) -> None:
        self.llm.check_available()

    def validate_text(self, text: str | None) -> str:
        text = (text or "").strip()
        if not text:
            raise InvalidInputError("Transcript text is empty; there is nothing to summarize.")
        if len(text) < 20 or not re.search(r"\w", text):
            raise InvalidInputError("Transcript text is too short to summarize.")
        if len(text) > self.max_transcript_chars:
            raise InvalidInputError(
                f"Transcript is too long ({len(text):,} characters; limit {self.max_transcript_chars:,})."
            )
        return text

    def summarize(
        self, text: str, summary_type: SummaryType, output_language: str | None = None
    ) -> SummaryOutcome:
        text = self.validate_text(text)
        self.check_available()
        chunks = chunk_text(text, self.chunk_chars, self.overlap_chars)

        if len(chunks) == 1:
            messages = build_chunk_messages(
                chunks[0], index=1, total=1, summary_type=summary_type, output_language=output_language
            )
            content = self._complete(messages, self.final_max_tokens)
        else:
            logger.info("Summarizing long transcript in %d chunks", len(chunks))
            partials = [
                self._complete(
                    build_chunk_messages(
                        chunk, index=i, total=len(chunks), summary_type=summary_type,
                        output_language=output_language,
                    ),
                    self.partial_max_tokens,
                )
                for i, chunk in enumerate(chunks, start=1)
            ]
            content = self._merge(partials, summary_type, output_language)

        return SummaryOutcome(
            content=drop_unsupported_details(content, text), chunk_count=len(chunks), model=self.llm.model
        )

    def _merge(
        self, partials: list[SummaryContent], summary_type: SummaryType, output_language: str | None
    ) -> SummaryContent:
        """Merge partial notes, grouping them so no single prompt exceeds the chunk budget."""
        level = partials
        for _ in range(MAX_MERGE_DEPTH):
            dumped = [p.model_dump() for p in level]
            groups = self._group_by_size(dumped)
            if len(groups) == 1:
                return self._complete(
                    build_merge_messages(groups[0], summary_type=summary_type,
                                         output_language=output_language, final=True),
                    self.final_max_tokens,
                )
            level = [
                self._complete(
                    build_merge_messages(group, summary_type=summary_type,
                                         output_language=output_language, final=False),
                    self.partial_max_tokens,
                )
                for group in groups
            ]
        raise SummarizationError("Transcript is too long to merge into a single summary.")

    def _group_by_size(self, items: list[dict]) -> list[list[dict]]:
        groups: list[list[dict]] = [[]]
        size = 0
        for item in items:
            item_size = len(json.dumps(item, ensure_ascii=False))
            # Always put at least two items per group so every level shrinks.
            if groups[-1] and size + item_size > self.chunk_chars and len(groups[-1]) >= 2:
                groups.append([])
                size = 0
            groups[-1].append(item)
            size += item_size
        return groups

    def _complete(self, messages: list[dict[str, str]], max_tokens: int) -> SummaryContent:
        """Call the LLM and validate its JSON, asking once for a repair if it is malformed."""
        raw = self.llm.complete_json(messages, max_tokens=max_tokens)
        try:
            return parse_summary_json(raw)
        except (ValueError, ValidationError) as exc:
            error = _short_error(exc)
            logger.warning("Model returned invalid summary JSON (%s); requesting repair", error)
        repair = [*messages, {"role": "assistant", "content": raw[:8000]}, build_repair_message(error)]
        raw = self.llm.complete_json(repair, max_tokens=max_tokens)
        try:
            return parse_summary_json(raw)
        except (ValueError, ValidationError) as exc:
            raise SummarizationError(
                f"The model returned a summary in an invalid format ({_short_error(exc)})."
            ) from exc


def _short_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        first = exc.errors()[0]
        loc = ".".join(str(p) for p in first["loc"]) or "root"
        return f"{loc}: {first['msg']}"
    return str(exc)[:200]
