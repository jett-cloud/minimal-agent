"""Trace and durable stream-event persistence models."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class TraceRecord(BaseModel):
    """A diagnostic event that is not included in model context."""

    model_config = ConfigDict(frozen=True)

    id: int
    run_id: str
    step_no: int
    event_type: str
    payload: dict[str, Any]
    duration_ms: int | None
    created_at: datetime


class PendingStreamEvent(BaseModel):
    """A normalized event waiting for durable sequence allocation."""

    model_config = ConfigDict(frozen=True)

    event_type: str = Field(min_length=1, max_length=100)
    payload: dict[str, Any]
    source_seq: int | None = Field(default=None, ge=0)


class StreamEventRecord(PendingStreamEvent):
    """A durable SSE event ordered both globally and within its run."""

    id: int
    run_id: str
    seq: int
    created_at: datetime
