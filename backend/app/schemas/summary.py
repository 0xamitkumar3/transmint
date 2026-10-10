"""Pydantic schemas for summary requests, LLM output and API responses."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SummaryType(str, Enum):
    BRIEF = "brief"
    DETAILED = "detailed"
    MEETING_NOTES = "meeting_notes"


class SummaryRequest(BaseModel):
    """Summarize either a stored transcription or raw transcript text (exactly one)."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"transcription_id": "3f2b0c9e8a7d4c1b9e6f5a4d3c2b1a09", "summary_type": "meeting_notes"},
                {"text": "Welcome to today's lecture on entropy...", "summary_type": "brief", "output_language": "English"},
            ]
        }
    )

    transcription_id: str | None = Field(default=None, min_length=1, max_length=64)
    text: str | None = Field(default=None, description="Transcript text to summarize directly.")
    summary_type: SummaryType = SummaryType.BRIEF
    output_language: str | None = Field(
        default=None,
        max_length=40,
        pattern=r"^[A-Za-z][A-Za-z \-()]*$",
        description="Language for the summary (e.g. 'English'). Defaults to the transcript language.",
    )

    @field_validator("text", "output_language", "transcription_id", mode="before")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @model_validator(mode="after")
    def _exactly_one_source(self) -> SummaryRequest:
        if (self.transcription_id is None) == (self.text is None):
            raise ValueError("Provide exactly one of 'transcription_id' or 'text'.")
        return self


# ---------------------------------------------------------------------------
# Structured content returned by the LLM (validated before it is stored).
# ---------------------------------------------------------------------------


class Concept(BaseModel):
    term: str = Field(min_length=1)
    explanation: str = ""


class ActionItem(BaseModel):
    task: str = Field(min_length=1)
    owner: str | None = None
    deadline: str | None = None


class SummaryContent(BaseModel):
    """The structured summary. Lists are empty when the transcript does not support them."""

    overview: str = Field(min_length=1)
    key_points: list[str] = Field(default_factory=list)
    important_concepts: list[Concept] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list, description="Only explicitly confirmed decisions.")
    suggestions: list[str] = Field(default_factory=list, description="Proposals not confirmed as decisions.")
    action_items: list[ActionItem] = Field(default_factory=list)
    follow_up_questions: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(
        default_factory=list, description="Parts of the transcript that were unclear or inaudible."
    )

    @field_validator(
        "key_points", "decisions", "suggestions", "follow_up_questions", "uncertainties", mode="before"
    )
    @classmethod
    def _clean_str_list(cls, value: object) -> object:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(v).strip() for v in value if v is not None and str(v).strip()]
        return value


class SummaryResponse(BaseModel):
    id: str
    transcription_id: str | None = None
    summary_type: SummaryType
    output_language: str | None = None
    model: str
    chunk_count: int = Field(ge=1, description="Number of transcript chunks summarized.")
    content: SummaryContent
    created_at: datetime
