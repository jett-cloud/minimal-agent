"""Agent run persistence models."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class RunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunRecord(BaseModel):
    """The durable lifecycle state for one user-triggered agent run."""

    model_config = ConfigDict(frozen=True)

    id: str
    session_id: str
    status: RunStatus
    loop_count: int
    user_message_id: str
    final_message_id: str | None
    error_code: str | None
    error_message: str | None
    started_at: datetime
    finished_at: datetime | None
