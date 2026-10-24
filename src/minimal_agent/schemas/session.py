"""Session persistence models."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class SessionRecord(BaseModel):
    """A persisted, independently scoped conversation."""

    model_config = ConfigDict(frozen=True)

    id: str
    title: str | None
    summary: str | None
    summary_from_seq: int | None
    summary_through_seq: int | None
    created_at: datetime
    updated_at: datetime
