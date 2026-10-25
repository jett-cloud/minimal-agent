"""Read models for persisted reasoning history."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ReasoningSource = Literal[
    "reasoning_text",
    "reasoning_content",
    "reasoning_summary",
    "think_tag",
    "lifecycle",
]
ReasoningContentType = Literal["raw", "summary", "activity"]


class ReasoningHistoryItem(BaseModel):
    """One reconstructed reasoning stream from a completed or interrupted Run."""

    model_config = ConfigDict(frozen=True)

    item_id: str = Field(min_length=1)
    source: ReasoningSource
    content_type: ReasoningContentType
    text: str
    completed: bool
    first_event_id: int = Field(ge=1)
    last_event_id: int = Field(ge=1)
