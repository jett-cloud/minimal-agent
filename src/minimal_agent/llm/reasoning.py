"""Provider-independent reasoning selection and incremental think-tag parsing."""

from collections.abc import AsyncIterator
from typing import Literal

from minimal_agent.llm.base import LLMClient, LLMRequest, LLMStreamError
from minimal_agent.llm.events import (
    LLMEvent,
    ReasoningDelta,
    ReasoningDone,
    ReasoningStarted,
    ResponseCompleted,
    ResponseIncomplete,
    TextDelta,
    TextDone,
    ToolCallStarted,
    is_terminal_event,
)

type ReasoningCapture = Literal["auto", "structured", "tagged", "both"]
type ReasoningOutput = Literal["auto", "raw", "summary", "off"]


class ReasoningAwareLLM:
    """Decorate an LLM client with source arbitration and think-tag extraction."""

    def __init__(
        self,
        inner: LLMClient,
        *,
        capture: ReasoningCapture = "auto",
        output: ReasoningOutput = "auto",
        think_tag_enabled: bool = True,
        think_tag_name: str = "think",
    ) -> None:
        self._inner = inner
        self._capture = capture
        self._output = output
        self._think_tag_enabled = think_tag_enabled
        self._think_tag_name = think_tag_name

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMEvent]:
        parser = _ThinkTagParser(self._think_tag_name) if self._think_tag_enabled else None
        selected: Literal["structured", "tagged"] | None = None
        saw_tool_call = False
        terminal_seen = False
        published_reasoning_items: set[str] = set()

        async for event in self._inner.stream(request):
            if isinstance(event, ToolCallStarted):
                saw_tool_call = True

            if isinstance(event, (ReasoningStarted, ReasoningDelta, ReasoningDone)):
                allowed, selected = self._select_reasoning(event, selected)
                if allowed:
                    yield event
                    if isinstance(event, ReasoningStarted):
                        published_reasoning_items.add(event.item_id)
                    elif isinstance(event, ReasoningDone):
                        published_reasoning_items.discard(event.item_id)
                elif (
                    isinstance(event, ReasoningDone) and event.item_id in published_reasoning_items
                ):
                    fallback = ReasoningDone(
                        item_id=event.item_id,
                        text=None,
                        source="lifecycle",
                        content_type="activity",
                        source_sequence=event.source_sequence,
                    )
                    fallback_allowed, selected = self._select_reasoning(fallback, selected)
                    if fallback_allowed:
                        yield fallback
                    published_reasoning_items.discard(event.item_id)
                continue

            if parser is not None and isinstance(event, TextDelta):
                for mapped in parser.feed(event):
                    if isinstance(mapped, (ReasoningStarted, ReasoningDelta, ReasoningDone)):
                        allowed, selected = self._select_reasoning(mapped, selected)
                        if allowed:
                            yield mapped
                    else:
                        yield mapped
                continue

            if parser is not None and isinstance(event, TextDone):
                for mapped in parser.finish_text(event):
                    if isinstance(mapped, (ReasoningStarted, ReasoningDelta, ReasoningDone)):
                        allowed, selected = self._select_reasoning(mapped, selected)
                        if allowed:
                            yield mapped
                    else:
                        yield mapped
                continue

            if is_terminal_event(event):
                if parser is not None:
                    for mapped in parser.finish_without_text_done():
                        if isinstance(mapped, (ReasoningStarted, ReasoningDelta, ReasoningDone)):
                            allowed, selected = self._select_reasoning(mapped, selected)
                            if allowed:
                                yield mapped
                        else:
                            yield mapped
                for item_id in sorted(published_reasoning_items):
                    lifecycle_done = ReasoningDone(
                        item_id=item_id,
                        text=None,
                        source="lifecycle",
                        content_type="activity",
                        source_sequence=event.source_sequence,
                    )
                    allowed, selected = self._select_reasoning(lifecycle_done, selected)
                    if allowed:
                        yield lifecycle_done
                published_reasoning_items.clear()
                if (
                    isinstance(event, ResponseCompleted)
                    and parser is not None
                    and parser.incomplete
                    and not parser.answer_text
                    and not saw_tool_call
                ):
                    yield ResponseIncomplete(
                        response_id=event.response_id,
                        reason="think_tag_not_closed",
                        source_sequence=event.source_sequence,
                    )
                else:
                    yield event
                terminal_seen = True
                continue

            yield event

        if parser is not None and not terminal_seen:
            for mapped in parser.finish_without_text_done():
                if isinstance(mapped, (ReasoningStarted, ReasoningDelta, ReasoningDone)):
                    allowed, selected = self._select_reasoning(mapped, selected)
                    if allowed:
                        yield mapped
                else:
                    yield mapped

    def _select_reasoning(
        self,
        event: ReasoningStarted | ReasoningDelta | ReasoningDone,
        selected: Literal["structured", "tagged"] | None,
    ) -> tuple[bool, Literal["structured", "tagged"] | None]:
        if self._output == "off":
            return False, selected
        if event.content_type == "raw" and self._output == "summary":
            return False, selected
        if event.content_type == "summary" and self._output == "raw":
            return False, selected

        source_kind: Literal["structured", "tagged"] | None
        if event.source == "think_tag":
            source_kind = "tagged"
        elif event.source == "lifecycle":
            source_kind = None
        else:
            source_kind = "structured"

        if source_kind == "structured" and self._capture == "tagged":
            return False, selected
        if source_kind == "tagged" and self._capture == "structured":
            return False, selected
        if self._capture == "both" or source_kind is None:
            return True, selected
        if self._capture in {"structured", "tagged"}:
            return True, selected

        has_content = (isinstance(event, ReasoningDelta) and bool(event.delta)) or (
            isinstance(event, ReasoningDone) and bool(event.text)
        )
        if isinstance(event, ReasoningStarted) and source_kind == "tagged":
            has_content = True
        if selected is None and has_content and source_kind is not None:
            selected = source_kind
        return selected is None or selected == source_kind, selected


class _ThinkTagParser:
    def __init__(self, tag_name: str) -> None:
        self._opening = f"<{tag_name}>"
        self._closing = f"</{tag_name}>"
        self._state: Literal["probing", "reasoning", "answer"] = "probing"
        self._item_id: str | None = None
        self._probe = ""
        self._reasoning_tail = ""
        self._raw: list[str] = []
        self._answer: list[str] = []
        self._reasoning: list[str] = []
        self._text_done = False
        self._reasoning_done = False
        self.incomplete = False

    @property
    def answer_text(self) -> str:
        return "".join(self._answer)

    def feed(self, event: TextDelta) -> tuple[LLMEvent, ...]:
        self._claim(event.item_id)
        self._raw.append(event.delta)
        return tuple(self._consume(event.delta, event.source_sequence))

    def finish_text(self, event: TextDone) -> tuple[LLMEvent, ...]:
        self._claim(event.item_id)
        streamed = "".join(self._raw)
        events: list[LLMEvent] = []
        if streamed:
            if streamed != event.text:
                raise LLMStreamError(
                    "INVALID_STREAM_EVENT",
                    "Completed assistant text does not match streamed deltas",
                    retryable=False,
                )
        else:
            self._raw.append(event.text)
            events.extend(self._consume(event.text, event.source_sequence))
        events.extend(self._finalize_pending(event.source_sequence))
        self._text_done = True
        events.append(
            TextDone(
                item_id=event.item_id,
                text=self.answer_text,
                source_sequence=event.source_sequence,
            )
        )
        return tuple(events)

    def finish_without_text_done(self) -> tuple[LLMEvent, ...]:
        if self._text_done or self._item_id is None:
            return ()
        return tuple(self._finalize_pending(None))

    def _consume(self, text: str, source_sequence: int | None) -> list[LLMEvent]:
        if not text:
            return []
        if self._state == "answer":
            return [self._answer_delta(text, source_sequence)]
        if self._state == "reasoning":
            return self._consume_reasoning(text, source_sequence)

        self._probe += text
        candidate = self._probe.lstrip()
        if not candidate:
            return []
        if candidate.startswith(self._opening):
            leading_length = len(self._probe) - len(candidate)
            remainder = self._probe[leading_length + len(self._opening) :]
            self._probe = ""
            self._state = "reasoning"
            events: list[LLMEvent] = [
                ReasoningStarted(
                    item_id=self._required_item_id(),
                    source="think_tag",
                    content_type="raw",
                    source_sequence=source_sequence,
                )
            ]
            events.extend(self._consume_reasoning(remainder, source_sequence))
            return events
        if self._opening.startswith(candidate):
            return []

        self._state = "answer"
        answer = self._probe
        self._probe = ""
        return [self._answer_delta(answer, source_sequence)]

    def _consume_reasoning(self, text: str, source_sequence: int | None) -> list[LLMEvent]:
        combined = self._reasoning_tail + text
        closing_index = combined.find(self._closing)
        if closing_index >= 0:
            reasoning_text = combined[:closing_index]
            answer_text = combined[closing_index + len(self._closing) :]
            self._reasoning_tail = ""
            events = self._reasoning_delta(reasoning_text, source_sequence)
            events.append(self._reasoning_done_event(source_sequence, completed=True))
            self._state = "answer"
            if answer_text:
                events.append(self._answer_delta(answer_text, source_sequence))
            return events

        hold = max(0, len(self._closing) - 1)
        if len(combined) <= hold:
            self._reasoning_tail = combined
            return []
        emit = combined[:-hold]
        self._reasoning_tail = combined[-hold:]
        return self._reasoning_delta(emit, source_sequence)

    def _finalize_pending(self, source_sequence: int | None) -> list[LLMEvent]:
        if self._state == "probing" and self._probe:
            self._state = "answer"
            answer = self._probe
            self._probe = ""
            return [self._answer_delta(answer, source_sequence)]
        if self._state == "reasoning" and not self._reasoning_done:
            events = self._reasoning_delta(self._reasoning_tail, source_sequence)
            self._reasoning_tail = ""
            self.incomplete = True
            events.append(self._reasoning_done_event(source_sequence, completed=False))
            return events
        return []

    def _answer_delta(self, text: str, source_sequence: int | None) -> TextDelta:
        self._answer.append(text)
        return TextDelta(
            item_id=self._required_item_id(),
            delta=text,
            source_sequence=source_sequence,
        )

    def _reasoning_delta(self, text: str, source_sequence: int | None) -> list[LLMEvent]:
        if not text:
            return []
        self._reasoning.append(text)
        return [
            ReasoningDelta(
                item_id=self._required_item_id(),
                delta=text,
                source="think_tag",
                content_type="raw",
                source_sequence=source_sequence,
            )
        ]

    def _reasoning_done_event(
        self, source_sequence: int | None, *, completed: bool
    ) -> ReasoningDone:
        self._reasoning_done = True
        return ReasoningDone(
            item_id=self._required_item_id(),
            text="".join(self._reasoning),
            source="think_tag",
            content_type="raw",
            completed=completed,
            source_sequence=source_sequence,
        )

    def _claim(self, item_id: str) -> None:
        if self._item_id is None:
            self._item_id = item_id
        elif self._item_id != item_id:
            raise LLMStreamError(
                "INVALID_STREAM_EVENT",
                "Think-tag parser received multiple assistant text items",
                retryable=False,
            )

    def _required_item_id(self) -> str:
        if self._item_id is None:
            raise RuntimeError("Think-tag parser has no text item")
        return self._item_id
