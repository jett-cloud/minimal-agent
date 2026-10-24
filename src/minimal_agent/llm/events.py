"""Provider-independent events emitted by an LLM streaming adapter."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

type ReasoningSource = Literal[
    "reasoning_text",
    "reasoning_content",
    "reasoning_summary",
    "think_tag",
    "lifecycle",
]
type ReasoningContentType = Literal["raw", "summary", "activity"]


class _Event(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_sequence: int | None = None


class ResponseStarted(_Event):
    type: Literal["response.started"] = "response.started"
    response_id: str


class TextDelta(_Event):
    type: Literal["text.delta"] = "text.delta"
    item_id: str
    delta: str


class TextDone(_Event):
    type: Literal["text.done"] = "text.done"
    item_id: str
    text: str


class ReasoningStarted(_Event):
    type: Literal["reasoning.started"] = "reasoning.started"
    item_id: str
    source: ReasoningSource = "lifecycle"
    content_type: ReasoningContentType = "activity"


class ReasoningDelta(_Event):
    type: Literal["reasoning.delta"] = "reasoning.delta"
    item_id: str
    delta: str
    source: ReasoningSource = "reasoning_summary"
    content_type: ReasoningContentType = "summary"


class ReasoningDone(_Event):
    type: Literal["reasoning.done"] = "reasoning.done"
    item_id: str
    text: str | None
    source: ReasoningSource = "reasoning_summary"
    content_type: ReasoningContentType = "summary"
    completed: bool = True


class ContinuationItem(_Event):
    """Opaque provider output that must be replayed before a tool result."""

    type: Literal["continuation.item"] = "continuation.item"
    item: dict[str, JsonValue]


class ToolCallStarted(_Event):
    type: Literal["tool_call.started"] = "tool_call.started"
    item_id: str
    call_id: str
    name: str


class ToolArgumentsDelta(_Event):
    type: Literal["tool_call.arguments.delta"] = "tool_call.arguments.delta"
    item_id: str
    call_id: str
    delta: str


class ToolArgumentsDone(_Event):
    type: Literal["tool_call.arguments.done"] = "tool_call.arguments.done"
    item_id: str
    call_id: str
    name: str
    arguments: str


class RefusalDelta(_Event):
    type: Literal["refusal.delta"] = "refusal.delta"
    item_id: str
    delta: str


class RefusalDone(_Event):
    type: Literal["refusal.done"] = "refusal.done"
    item_id: str
    refusal: str


class RequestRetrying(_Event):
    type: Literal["request.retrying"] = "request.retrying"
    attempt: int
    max_attempts: int
    error_code: str
    delay_seconds: float


class ResponseCompleted(_Event):
    type: Literal["response.completed"] = "response.completed"
    response_id: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


class ResponseIncomplete(_Event):
    type: Literal["response.incomplete"] = "response.incomplete"
    response_id: str
    reason: str | None = None


class ResponseFailed(_Event):
    type: Literal["response.failed"] = "response.failed"
    response_id: str
    error_code: str
    message: str


type LLMEvent = Annotated[
    ResponseStarted
    | TextDelta
    | TextDone
    | ReasoningStarted
    | ReasoningDelta
    | ReasoningDone
    | ContinuationItem
    | ToolCallStarted
    | ToolArgumentsDelta
    | ToolArgumentsDone
    | RefusalDelta
    | RefusalDone
    | RequestRetrying
    | ResponseCompleted
    | ResponseIncomplete
    | ResponseFailed,
    Field(discriminator="type"),
]

TERMINAL_EVENT_TYPES = frozenset({"response.completed", "response.incomplete", "response.failed"})


def is_terminal_event(event: LLMEvent) -> bool:
    return event.type in TERMINAL_EVENT_TYPES
