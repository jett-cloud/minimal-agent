"""Message persistence models."""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class MessageKind(StrEnum):
    MESSAGE = "message"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    SYSTEM_ERROR = "system_error"


class MessageRecord(BaseModel):
    """A complete semantic message used to rebuild model context."""

    model_config = ConfigDict(frozen=True)

    id: str
    session_id: str
    run_id: str | None
    seq: int
    role: MessageRole
    kind: MessageKind
    content: dict[str, Any]
    created_at: datetime
