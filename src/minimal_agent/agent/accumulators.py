"""Validate an LLM stream and reduce it to one ReAct decision."""

from dataclasses import dataclass, field

from minimal_agent.agent.decisions import (
    FinalAnswerDecision,
    StepDecision,
    ToolCallDecision,
)
from minimal_agent.agent.errors import AgentLoopError
from minimal_agent.llm import (
    ContinuationItem,
    LLMEvent,
    ReasoningDelta,
    ReasoningDone,
    ReasoningStarted,
    RefusalDelta,
    RefusalDone,
    RequestRetrying,
    ResponseCompleted,
    ResponseFailed,
    ResponseIncomplete,
    ResponseStarted,
    TextDelta,
    TextDone,
    ToolArgumentsDelta,
    ToolArgumentsDone,
    ToolCallStarted,
)
from minimal_agent.streaming import (
    AssistantDeltaEvent,
    AssistantDoneEvent,
    BufferedEventWriter,
    ReasoningDeltaEvent,
    ReasoningDoneEvent,
    ReasoningStartedEvent,
    ToolCallDeltaEvent,
)


@dataclass(slots=True)
class _TextState:
    item_id: str | None = None
    deltas: list[str] = field(default_factory=list)
    done_text: str | None = None
    done_emitted: bool = False


@dataclass(slots=True)
class _ToolState:
    item_id: str
    call_id: str
    name: str
    deltas: list[str] = field(default_factory=list)
    arguments: str | None = None
    source_seq: int | None = None


class StepAccumulator:
    """Forward public deltas while retaining only enough state to validate the final action."""

    def __init__(self) -> None:
        self._text = _TextState()
        self._tool: _ToolState | None = None
        self._completed = False
        self.response_id: str | None = None
        self.usage: dict[str, int | None] | None = None
        self.retry_notices: list[dict[str, int | float | str]] = []
        self._continuation_items: list[dict[str, object]] = []
        self._reasoning_content: list[str] = []

    async def consume(self, event: LLMEvent, writer: BufferedEventWriter) -> None:
        if self._completed:
            raise AgentLoopError("INVALID_LLM_STREAM", "LLM emitted data after completion")

        if isinstance(event, ResponseStarted):
            if self.response_id is not None:
                raise AgentLoopError("INVALID_LLM_STREAM", "LLM response started more than once")
            self.response_id = event.response_id
            return

        if isinstance(event, TextDelta):
            self._claim_text_item(event.item_id)
            self._text.deltas.append(event.delta)
            await writer.publish(
                AssistantDeltaEvent(
                    item_id=event.item_id,
                    delta=event.delta,
                    source_seq=event.source_sequence,
                )
            )
            return

        if isinstance(event, TextDone):
            self._claim_text_item(event.item_id)
            streamed = "".join(self._text.deltas)
            if streamed and streamed != event.text:
                raise AgentLoopError(
                    "INVALID_LLM_STREAM",
                    "Completed assistant text does not match streamed deltas",
                )
            self._text.done_text = event.text
            self._text.done_emitted = True
            await writer.publish(
                AssistantDoneEvent(
                    item_id=event.item_id,
                    text=event.text,
                    source_seq=event.source_sequence,
                )
            )
            return

        if isinstance(event, RefusalDelta):
            self._claim_text_item(event.item_id)
            self._text.deltas.append(event.delta)
            await writer.publish(
                AssistantDeltaEvent(
                    item_id=event.item_id,
                    delta=event.delta,
                    source_seq=event.source_sequence,
                )
            )
            return

        if isinstance(event, RefusalDone):
            self._claim_text_item(event.item_id)
            streamed = "".join(self._text.deltas)
            if streamed and streamed != event.refusal:
                raise AgentLoopError(
                    "INVALID_LLM_STREAM",
                    "Completed refusal does not match streamed deltas",
                )
            self._text.done_text = event.refusal
            self._text.done_emitted = True
            await writer.publish(
                AssistantDoneEvent(
                    item_id=event.item_id,
                    text=event.refusal,
                    source_seq=event.source_sequence,
                )
            )
            return

        if isinstance(event, ReasoningStarted):
            await writer.publish(
                ReasoningStartedEvent(
                    item_id=event.item_id,
                    source=event.source,
                    content_type=event.content_type,
                    source_seq=event.source_sequence,
                )
            )
            return
        if isinstance(event, ReasoningDelta):
            if event.source == "reasoning_content":
                self._reasoning_content.append(event.delta)
            await writer.publish(
                ReasoningDeltaEvent(
                    item_id=event.item_id,
                    delta=event.delta,
                    source=event.source,
                    content_type=event.content_type,
                    source_seq=event.source_sequence,
                )
            )
            return
        if isinstance(event, ReasoningDone):
            await writer.publish(
                ReasoningDoneEvent(
                    item_id=event.item_id,
                    text=event.text,
                    source=event.source,
                    content_type=event.content_type,
                    completed=event.completed,
                    source_seq=event.source_sequence,
                )
            )
            return

        if isinstance(event, ContinuationItem):
            self._continuation_items.append(event.item)
            return

        if isinstance(event, ToolCallStarted):
            if self._tool is not None:
                raise AgentLoopError(
                    "MULTIPLE_TOOL_CALLS",
                    "MVP allows at most one tool call per model step",
                )
            self._tool = _ToolState(
                item_id=event.item_id,
                call_id=event.call_id,
                name=event.name,
            )
            return

        if isinstance(event, ToolArgumentsDelta):
            tool = self._require_tool(event.item_id, event.call_id)
            tool.deltas.append(event.delta)
            await writer.publish(
                ToolCallDeltaEvent(
                    item_id=event.item_id,
                    call_id=event.call_id,
                    delta=event.delta,
                    source_seq=event.source_sequence,
                )
            )
            return

        if isinstance(event, ToolArgumentsDone):
            tool = self._require_tool(event.item_id, event.call_id)
            if tool.name != event.name:
                raise AgentLoopError("INVALID_LLM_STREAM", "Tool name changed while streaming")
            streamed = "".join(tool.deltas)
            if streamed and streamed != event.arguments:
                raise AgentLoopError(
                    "INVALID_LLM_STREAM",
                    "Completed tool arguments do not match streamed deltas",
                )
            tool.arguments = event.arguments
            tool.source_seq = event.source_sequence
            return

        if isinstance(event, RequestRetrying):
            self.retry_notices.append(
                {
                    "attempt": event.attempt,
                    "max_attempts": event.max_attempts,
                    "error_code": event.error_code,
                    "delay_seconds": event.delay_seconds,
                }
            )
            return

        if isinstance(event, ResponseFailed):
            raise AgentLoopError(event.error_code, event.message)
        if isinstance(event, ResponseIncomplete):
            raise AgentLoopError(
                "LLM_RESPONSE_INCOMPLETE",
                f"LLM response was incomplete: {event.reason or 'unknown reason'}",
            )
        if isinstance(event, ResponseCompleted):
            self._completed = True
            self.usage = {
                "input_tokens": event.input_tokens,
                "output_tokens": event.output_tokens,
                "total_tokens": event.total_tokens,
            }

    def decision(self) -> StepDecision:
        if not self._completed:
            raise AgentLoopError(
                "LLM_STREAM_INTERRUPTED",
                "LLM stream ended without a completed response",
            )
        text = self._text.done_text
        if text is None and self._text.deltas:
            text = "".join(self._text.deltas)

        if self._tool is not None:
            if text:
                raise AgentLoopError(
                    "MULTIPLE_LLM_ACTIONS",
                    "A model step cannot contain both assistant text and a tool call",
                )
            if self._tool.arguments is None:
                raise AgentLoopError(
                    "INCOMPLETE_TOOL_CALL",
                    "Tool arguments did not complete and were discarded",
                )
            return ToolCallDecision(
                item_id=self._tool.item_id,
                call_id=self._tool.call_id,
                name=self._tool.name,
                arguments_json=self._tool.arguments,
                source_seq=self._tool.source_seq,
                continuation_items=tuple(self._continuation_items),
                reasoning_content="".join(self._reasoning_content) or None,
            )

        if not text or self._text.item_id is None:
            raise AgentLoopError("EMPTY_LLM_RESPONSE", "LLM completed without an answer")
        return FinalAnswerDecision(
            item_id=self._text.item_id,
            text=text,
            done_event_emitted=self._text.done_emitted,
        )

    def _claim_text_item(self, item_id: str) -> None:
        if self._text.item_id is None:
            self._text.item_id = item_id
        elif self._text.item_id != item_id:
            raise AgentLoopError(
                "MULTIPLE_LLM_ACTIONS",
                "MVP allows only one assistant output item per model step",
            )

    def _require_tool(self, item_id: str, call_id: str) -> _ToolState:
        if self._tool is None or self._tool.item_id != item_id or self._tool.call_id != call_id:
            raise AgentLoopError(
                "INVALID_LLM_STREAM",
                "Tool argument event has no matching started call",
            )
        return self._tool
