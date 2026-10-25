"""OpenAI-compatible Chat Completions streaming adapter."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from minimal_agent.llm.base import LLMRequest, LLMStreamError
from minimal_agent.llm.events import (
    LLMEvent,
    ReasoningDelta,
    ReasoningDone,
    ReasoningStarted,
    RefusalDelta,
    RefusalDone,
    RequestRetrying,
    ResponseCompleted,
    ResponseIncomplete,
    ResponseStarted,
    TextDelta,
    TextDone,
    ToolArgumentsDelta,
    ToolArgumentsDone,
    ToolCallStarted,
    is_terminal_event,
)
from minimal_agent.llm.openai_client import _normalize_error
from minimal_agent.llm.retry import RetryPolicy

type Sleep = Callable[[float], Awaitable[None]]


@dataclass(slots=True)
class _ChatToolState:
    index: int
    item_id: str
    call_id: str = ""
    name: str = ""
    arguments: list[str] = field(default_factory=list)
    emitted_characters: int = 0
    started: bool = False


class ChatCompletionsEventMapper:
    """Normalize one-choice Chat Completions chunks into the internal event protocol."""

    def __init__(self) -> None:
        self.response_id: str | None = None
        self._sequence = 0
        self._text: list[str] = []
        self._refusal: list[str] = []
        self._reasoning: list[str] = []
        self._tools: dict[int, _ChatToolState] = {}
        self._finish_reason: str | None = None
        self._usage: object | None = None

    def map(self, chunk: object) -> tuple[LLMEvent, ...]:
        self._sequence += 1
        sequence = self._sequence
        events: list[LLMEvent] = []
        chunk_id = _optional_text(getattr(chunk, "id", None))
        if self.response_id is None and chunk_id:
            self.response_id = chunk_id
            events.append(ResponseStarted(response_id=chunk_id, source_sequence=sequence))

        usage = getattr(chunk, "usage", None)
        if usage is not None:
            self._usage = usage

        choices = getattr(chunk, "choices", None) or []
        if not choices:
            return tuple(events)
        if len(choices) != 1 or getattr(choices[0], "index", 0) != 0:
            raise LLMStreamError(
                "INVALID_STREAM_EVENT",
                "Chat adapter requires exactly one completion choice",
                retryable=False,
            )

        choice = choices[0]
        delta = getattr(choice, "delta", None)
        if delta is not None:
            reasoning = _extra_text(delta, "reasoning_content")
            if reasoning is not None:
                if not self._reasoning:
                    events.append(
                        ReasoningStarted(
                            item_id=self._reasoning_item_id(),
                            source="lifecycle",
                            content_type="activity",
                            source_sequence=sequence,
                        )
                    )
                self._reasoning.append(reasoning)
                events.append(
                    ReasoningDelta(
                        item_id=self._reasoning_item_id(),
                        delta=reasoning,
                        source="reasoning_content",
                        content_type="raw",
                        source_sequence=sequence,
                    )
                )

            content = _optional_text(getattr(delta, "content", None))
            if content is not None:
                self._text.append(content)
                events.append(
                    TextDelta(
                        item_id=self._message_item_id(),
                        delta=content,
                        source_sequence=sequence,
                    )
                )

            refusal = _optional_text(getattr(delta, "refusal", None))
            if refusal is not None:
                self._refusal.append(refusal)
                events.append(
                    RefusalDelta(
                        item_id=self._message_item_id(),
                        delta=refusal,
                        source_sequence=sequence,
                    )
                )

            for tool_delta in getattr(delta, "tool_calls", None) or []:
                events.extend(self._map_tool_delta(tool_delta, sequence=sequence))

        finish_reason = getattr(choice, "finish_reason", None)
        if isinstance(finish_reason, str):
            self._finish_reason = finish_reason
        return tuple(events)

    def finish(self) -> tuple[LLMEvent, ...]:
        if self.response_id is None:
            raise LLMStreamError(
                "LLM_STREAM_INTERRUPTED",
                "Chat stream ended before a response identifier was received",
                retryable=True,
            )
        sequence = self._sequence + 1
        events: list[LLMEvent] = []
        if self._reasoning:
            events.append(
                ReasoningDone(
                    item_id=self._reasoning_item_id(),
                    text="".join(self._reasoning),
                    source="reasoning_content",
                    content_type="raw",
                    source_sequence=sequence,
                )
            )
        if self._text:
            events.append(
                TextDone(
                    item_id=self._message_item_id(),
                    text="".join(self._text),
                    source_sequence=sequence,
                )
            )
        if self._refusal:
            events.append(
                RefusalDone(
                    item_id=self._message_item_id(),
                    refusal="".join(self._refusal),
                    source_sequence=sequence,
                )
            )
        for state in self._tools.values():
            if not state.started:
                raise LLMStreamError(
                    "INVALID_STREAM_EVENT",
                    "Chat tool arguments completed without call metadata",
                    retryable=False,
                )
            events.append(
                ToolArgumentsDone(
                    item_id=state.item_id,
                    call_id=state.call_id,
                    name=state.name,
                    arguments="".join(state.arguments),
                    source_sequence=sequence,
                )
            )

        if self._finish_reason in {"length", "content_filter"}:
            events.append(
                ResponseIncomplete(
                    response_id=self.response_id,
                    reason=self._finish_reason,
                    source_sequence=sequence,
                )
            )
            return tuple(events)
        if self._finish_reason not in {"stop", "tool_calls", "function_call"}:
            raise LLMStreamError(
                "LLM_STREAM_INTERRUPTED",
                "Chat stream ended without a supported finish reason",
                retryable=True,
            )

        prompt_tokens = _optional_int(getattr(self._usage, "prompt_tokens", None))
        completion_tokens = _optional_int(getattr(self._usage, "completion_tokens", None))
        total_tokens = _optional_int(getattr(self._usage, "total_tokens", None))
        events.append(
            ResponseCompleted(
                response_id=self.response_id,
                input_tokens=prompt_tokens,
                output_tokens=completion_tokens,
                total_tokens=total_tokens,
                source_sequence=sequence,
            )
        )
        return tuple(events)

    def _map_tool_delta(self, tool_delta: object, *, sequence: int) -> list[LLMEvent]:
        index = getattr(tool_delta, "index", None)
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            raise LLMStreamError(
                "INVALID_STREAM_EVENT",
                "Chat tool call index is invalid",
                retryable=False,
            )
        state = self._tools.setdefault(
            index,
            _ChatToolState(index=index, item_id=f"{self._response_id()}:tool:{index}"),
        )
        call_id = _optional_text(getattr(tool_delta, "id", None))
        if call_id:
            if not state.call_id:
                state.call_id = call_id
            elif call_id != state.call_id:
                state.call_id += call_id
        function = getattr(tool_delta, "function", None)
        if function is not None:
            name = _optional_text(getattr(function, "name", None))
            if name:
                if not state.name:
                    state.name = name
                elif name != state.name:
                    state.name += name
            arguments = _optional_text(getattr(function, "arguments", None))
            if arguments is not None:
                state.arguments.append(arguments)

        events: list[LLMEvent] = []
        if not state.started and state.call_id and state.name:
            state.started = True
            events.append(
                ToolCallStarted(
                    item_id=state.item_id,
                    call_id=state.call_id,
                    name=state.name,
                    source_sequence=sequence,
                )
            )
        joined = "".join(state.arguments)
        if state.started and len(joined) > state.emitted_characters:
            delta = joined[state.emitted_characters :]
            state.emitted_characters = len(joined)
            events.append(
                ToolArgumentsDelta(
                    item_id=state.item_id,
                    call_id=state.call_id,
                    delta=delta,
                    source_sequence=sequence,
                )
            )
        return events

    def _response_id(self) -> str:
        if self.response_id is None:
            raise LLMStreamError(
                "INVALID_STREAM_EVENT",
                "Chat content arrived before the response identifier",
                retryable=False,
            )
        return self.response_id

    def _message_item_id(self) -> str:
        return f"{self._response_id()}:message:0"

    def _reasoning_item_id(self) -> str:
        return f"{self._response_id()}:reasoning:0"


class OpenAIChatCompletionsLLM:
    """Stream an OpenAI-compatible Chat Completions endpoint with bounded retries."""

    def __init__(
        self,
        client: Any,
        *,
        model: str,
        retry_policy: RetryPolicy | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if not model.strip():
            raise ValueError("LLM model must not be empty")
        self._client = client
        self._model = model.strip()
        self._retry_policy = retry_policy or RetryPolicy()
        self._sleep = sleep

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMEvent]:
        for attempt in range(1, self._retry_policy.max_attempts + 1):
            emitted_semantic_event = False
            saw_terminal = False
            mapper = ChatCompletionsEventMapper()
            try:
                stream = await self._client.chat.completions.create(**self._request_kwargs(request))
                async for chunk in stream:
                    for event in mapper.map(chunk):
                        emitted_semantic_event = True
                        yield event
                for event in mapper.finish():
                    emitted_semantic_event = True
                    saw_terminal = saw_terminal or is_terminal_event(event)
                    yield event
                if not saw_terminal:
                    raise LLMStreamError(
                        "LLM_STREAM_INTERRUPTED",
                        "Chat stream ended without a terminal event",
                        retryable=True,
                    )
                return
            except asyncio.CancelledError:
                raise
            except Exception as error:
                normalized = _normalize_error(error, attempts=attempt)
                can_retry = (
                    normalized.retryable
                    and not emitted_semantic_event
                    and attempt < self._retry_policy.max_attempts
                )
                if not can_retry:
                    raise normalized from error
                delay = self._retry_policy.delay_for_retry(attempt)
                yield RequestRetrying(
                    attempt=attempt + 1,
                    max_attempts=self._retry_policy.max_attempts,
                    error_code=normalized.code,
                    delay_seconds=delay,
                )
                await self._sleep(delay)

    def _request_kwargs(self, request: LLMRequest) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": _chat_messages(request),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if request.tools:
            kwargs["tools"] = [_chat_tool(tool) for tool in request.tools]
            kwargs["tool_choice"] = request.tool_choice
            kwargs["parallel_tool_calls"] = False
        if request.max_output_tokens is not None:
            kwargs["max_tokens"] = request.max_output_tokens
        if request.reasoning_effort is not None:
            kwargs["reasoning_effort"] = request.reasoning_effort
        return kwargs


def _chat_messages(request: LLMRequest) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [{"role": "system", "content": request.instructions}]
    for item in request.input:
        item_type = item.get("type")
        role = item.get("role")
        if isinstance(role, str):
            messages.append({"role": role, "content": item.get("content", "")})
        elif item_type == "function_call":
            call_id = item.get("call_id")
            name = item.get("name")
            arguments = item.get("arguments")
            if not all(isinstance(value, str) for value in (call_id, name, arguments)):
                raise ValueError("Function call context item is malformed")
            assistant: dict[str, Any] = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {"name": name, "arguments": arguments},
                    }
                ],
            }
            reasoning_content = item.get("reasoning_content")
            if isinstance(reasoning_content, str) and reasoning_content:
                assistant["reasoning_content"] = reasoning_content
            messages.append(assistant)
        elif item_type == "function_call_output":
            call_id = item.get("call_id")
            output = item.get("output")
            if not isinstance(call_id, str) or not isinstance(output, str):
                raise ValueError("Function result context item is malformed")
            messages.append({"role": "tool", "tool_call_id": call_id, "content": output})
        elif item_type == "reasoning":
            continue
        else:
            raise ValueError("Unsupported context item for Chat Completions")
    return messages


def _chat_tool(tool: dict[str, Any]) -> dict[str, Any]:
    if tool.get("type") != "function":
        raise ValueError("Chat Completions only supports function tools")
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool["description"],
            "parameters": tool["parameters"],
            "strict": tool.get("strict", True),
        },
    }


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _extra_text(value: object, name: str) -> str | None:
    direct = getattr(value, name, None)
    if isinstance(direct, str):
        return direct
    model_extra = getattr(value, "model_extra", None)
    extra = model_extra.get(name) if isinstance(model_extra, dict) else None
    return extra if isinstance(extra, str) else None


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
