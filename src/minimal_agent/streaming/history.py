"""Reconstruct user-visible reasoning from durable stream events."""

from dataclasses import dataclass, field
from typing import Literal, cast

from minimal_agent.schemas import ReasoningHistoryItem, StreamEventRecord

_REASONING_EVENT_TYPES = (
    "reasoning.started",
    "reasoning.delta",
    "reasoning.done",
)
_SOURCES = {
    "reasoning_text",
    "reasoning_content",
    "reasoning_summary",
    "think_tag",
    "lifecycle",
}
_CONTENT_TYPES = {"raw", "summary", "activity"}


@dataclass(slots=True)
class _ReasoningState:
    item_id: str
    source: str
    content_type: str
    first_event_id: int
    last_event_id: int
    deltas: list[str] = field(default_factory=list)
    done_text: str | None = None
    completed: bool = False


def reasoning_event_types() -> tuple[str, ...]:
    return _REASONING_EVENT_TYPES


def reconstruct_reasoning(events: list[StreamEventRecord]) -> list[ReasoningHistoryItem]:
    """Group persisted reasoning events without copying them into message storage."""

    states: dict[tuple[str, str, str], _ReasoningState] = {}
    content_item_ids: set[str] = set()

    for event in events:
        if event.event_type not in _REASONING_EVENT_TYPES:
            continue
        payload = event.payload
        item_id = payload.get("item_id")
        source = payload.get("source", "lifecycle")
        content_type = payload.get("content_type", "activity")
        if (
            not isinstance(item_id, str)
            or not item_id
            or source not in _SOURCES
            or content_type not in _CONTENT_TYPES
        ):
            continue

        key = (item_id, str(source), str(content_type))
        state = states.get(key)
        if state is None:
            state = _ReasoningState(
                item_id=item_id,
                source=str(source),
                content_type=str(content_type),
                first_event_id=event.id,
                last_event_id=event.id,
            )
            states[key] = state
        state.last_event_id = event.id

        if content_type != "activity":
            content_item_ids.add(item_id)
        if event.event_type == "reasoning.delta":
            delta = payload.get("delta")
            if isinstance(delta, str):
                state.deltas.append(delta)
        elif event.event_type == "reasoning.done":
            text = payload.get("text")
            if isinstance(text, str):
                state.done_text = text
            state.completed = payload.get("completed") is not False

    history: list[ReasoningHistoryItem] = []
    for state in states.values():
        if state.content_type == "activity" and state.item_id in content_item_ids:
            continue
        source = cast(
            Literal[
                "reasoning_text",
                "reasoning_content",
                "reasoning_summary",
                "think_tag",
                "lifecycle",
            ],
            state.source,
        )
        content_type = cast(Literal["raw", "summary", "activity"], state.content_type)
        history.append(
            ReasoningHistoryItem(
                item_id=state.item_id,
                source=source,
                content_type=content_type,
                text=state.done_text if state.done_text is not None else "".join(state.deltas),
                completed=state.completed,
                first_event_id=state.first_event_id,
                last_event_id=state.last_event_id,
            )
        )
    return sorted(history, key=lambda item: item.first_event_id)
