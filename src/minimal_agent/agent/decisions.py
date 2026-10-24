"""The two valid outcomes of one ReAct model step."""

from dataclasses import dataclass

from pydantic import JsonValue


@dataclass(frozen=True, slots=True)
class FinalAnswerDecision:
    item_id: str
    text: str
    done_event_emitted: bool


@dataclass(frozen=True, slots=True)
class ToolCallDecision:
    item_id: str
    call_id: str
    name: str
    arguments_json: str
    source_seq: int | None
    continuation_items: tuple[dict[str, JsonValue], ...] = ()
    reasoning_content: str | None = None


type StepDecision = FinalAnswerDecision | ToolCallDecision
