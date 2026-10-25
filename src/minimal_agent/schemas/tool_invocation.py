"""Durable logical tool-call models."""

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict


class ToolInvocationStatus(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


class ToolInvocationRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    run_id: str
    session_id: str
    step_no: int
    provider_call_id: str
    tool_name: str
    arguments_json: str
    arguments_hash: str
    idempotency_key: str | None
    retry_semantics: Literal["safe", "with_idempotency_key", "never"]
    status: ToolInvocationStatus
    attempt_count: int
    result: dict[str, object] | None
    error: dict[str, object] | None
    tool_call_message_id: str
    result_message_id: str | None
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None
