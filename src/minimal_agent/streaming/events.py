"""Strict public events written to SQLite and exposed through SSE."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from minimal_agent.schemas import PendingStreamEvent


class _PublicEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_seq: int | None = Field(default=None, ge=0)

    def to_pending(self) -> PendingStreamEvent:
        content = self.model_dump(mode="json", exclude={"type", "source_seq"})
        return PendingStreamEvent(
            event_type=self.type,
            payload=content,
            source_seq=self.source_seq,
        )


class RunStartedEvent(_PublicEvent):
    type: Literal["run.started"] = "run.started"
    run_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)


class TurnStartedEvent(_PublicEvent):
    type: Literal["turn.started"] = "turn.started"
    run_id: str = Field(min_length=1)
    step_no: int = Field(ge=1)


class TurnCompletedEvent(_PublicEvent):
    type: Literal["turn.completed"] = "turn.completed"
    run_id: str = Field(min_length=1)
    step_no: int = Field(ge=1)
    outcome: Literal["tool_call", "final_answer", "failed"]


class ReasoningDeltaEvent(_PublicEvent):
    type: Literal["reasoning.delta"] = "reasoning.delta"
    item_id: str = Field(min_length=1)
    delta: str
    source: Literal[
        "reasoning_text",
        "reasoning_content",
        "reasoning_summary",
        "think_tag",
        "lifecycle",
    ] = "reasoning_summary"
    content_type: Literal["raw", "summary", "activity"] = "summary"


class ReasoningStartedEvent(_PublicEvent):
    type: Literal["reasoning.started"] = "reasoning.started"
    item_id: str = Field(min_length=1)
    source: Literal[
        "reasoning_text",
        "reasoning_content",
        "reasoning_summary",
        "think_tag",
        "lifecycle",
    ] = "lifecycle"
    content_type: Literal["raw", "summary", "activity"] = "activity"


class ReasoningDoneEvent(_PublicEvent):
    type: Literal["reasoning.done"] = "reasoning.done"
    item_id: str = Field(min_length=1)
    text: str | None = None
    source: Literal[
        "reasoning_text",
        "reasoning_content",
        "reasoning_summary",
        "think_tag",
        "lifecycle",
    ] = "reasoning_summary"
    content_type: Literal["raw", "summary", "activity"] = "summary"
    completed: bool = True


class AssistantDeltaEvent(_PublicEvent):
    type: Literal["assistant.delta"] = "assistant.delta"
    item_id: str = Field(min_length=1)
    delta: str


class AssistantDoneEvent(_PublicEvent):
    type: Literal["assistant.done"] = "assistant.done"
    item_id: str = Field(min_length=1)
    text: str


class ToolCallDeltaEvent(_PublicEvent):
    type: Literal["tool_call.delta"] = "tool_call.delta"
    item_id: str = Field(min_length=1)
    call_id: str = Field(min_length=1)
    delta: str


class ToolCallReadyEvent(_PublicEvent):
    type: Literal["tool_call.ready"] = "tool_call.ready"
    item_id: str = Field(min_length=1)
    call_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    arguments: dict[str, JsonValue]


class ToolStartedEvent(_PublicEvent):
    type: Literal["tool.started"] = "tool.started"
    call_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    arguments: dict[str, JsonValue]


class ToolCompletedEvent(_PublicEvent):
    type: Literal["tool.completed"] = "tool.completed"
    call_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    result: JsonValue


class ToolFailedEvent(_PublicEvent):
    type: Literal["tool.failed"] = "tool.failed"
    call_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    error: dict[str, JsonValue]


class RunCompletedEvent(_PublicEvent):
    type: Literal["run.completed"] = "run.completed"
    run_id: str = Field(min_length=1)
    message_id: str = Field(min_length=1)


class RunFailedEvent(_PublicEvent):
    type: Literal["run.failed"] = "run.failed"
    run_id: str = Field(min_length=1)
    error_code: str = Field(min_length=1)
    message: str


type PublicStreamEvent = Annotated[
    RunStartedEvent
    | TurnStartedEvent
    | TurnCompletedEvent
    | ReasoningDeltaEvent
    | ReasoningStartedEvent
    | ReasoningDoneEvent
    | AssistantDeltaEvent
    | AssistantDoneEvent
    | ToolCallDeltaEvent
    | ToolCallReadyEvent
    | ToolStartedEvent
    | ToolCompletedEvent
    | ToolFailedEvent
    | RunCompletedEvent
    | RunFailedEvent,
    Field(discriminator="type"),
]

DELTA_EVENT_TYPES = frozenset({"reasoning.delta", "assistant.delta", "tool_call.delta"})
BARRIER_EVENT_TYPES = frozenset(
    {
        "run.started",
        "turn.started",
        "turn.completed",
        "reasoning.started",
        "reasoning.done",
        "assistant.done",
        "tool_call.ready",
        "tool.started",
        "tool.completed",
        "tool.failed",
        "run.completed",
        "run.failed",
    }
)
TERMINAL_EVENT_TYPES = frozenset({"run.completed", "run.failed"})
