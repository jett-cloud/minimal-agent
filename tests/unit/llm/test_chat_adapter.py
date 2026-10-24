from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

from minimal_agent.llm import (
    LLMRequest,
    OpenAIChatCompletionsLLM,
    ReasoningDelta,
    ResponseCompleted,
    RetryPolicy,
    TextDone,
    ToolArgumentsDone,
    ToolCallStarted,
)


def _chunk(
    *,
    chunk_id: str = "chat_1",
    delta: object | None = None,
    finish_reason: str | None = None,
    choices: list[object] | None = None,
    usage: object | None = None,
) -> SimpleNamespace:
    if choices is None:
        choices = [
            SimpleNamespace(
                index=0,
                delta=delta or SimpleNamespace(),
                finish_reason=finish_reason,
            )
        ]
    return SimpleNamespace(id=chunk_id, choices=choices, usage=usage)


class _ChatAPI:
    def __init__(self, chunks: list[object]) -> None:
        self.chunks = chunks
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> AsyncIterator[object]:
        self.calls.append(kwargs)

        async def stream() -> AsyncIterator[object]:
            for chunk in self.chunks:
                yield chunk

        return stream()


class _SDK:
    def __init__(self, chunks: list[object]) -> None:
        self.chat = SimpleNamespace(completions=_ChatAPI(chunks))


@pytest.mark.asyncio
async def test_chat_adapter_maps_reasoning_content_text_and_usage() -> None:
    sdk = _SDK(
        [
            _chunk(delta=SimpleNamespace(reasoning_content="plan ")),
            _chunk(delta=SimpleNamespace(reasoning_content="done", content="answer")),
            _chunk(delta=SimpleNamespace(), finish_reason="stop"),
            _chunk(
                choices=[],
                usage=SimpleNamespace(prompt_tokens=4, completion_tokens=3, total_tokens=7),
            ),
        ]
    )
    adapter = OpenAIChatCompletionsLLM(
        sdk,
        model="compatible-model",
        retry_policy=RetryPolicy(max_retries=0),
    )
    request = LLMRequest(
        instructions="test",
        input=[{"role": "user", "content": "hello"}],
        reasoning_effort="low",
        max_output_tokens=64,
    )

    events = [event async for event in adapter.stream(request)]

    assert "".join(event.delta for event in events if isinstance(event, ReasoningDelta)) == (
        "plan done"
    )
    assert next(event for event in events if isinstance(event, TextDone)).text == "answer"
    completed = next(event for event in events if isinstance(event, ResponseCompleted))
    assert completed.total_tokens == 7
    call = sdk.chat.completions.calls[0]
    assert call["messages"][0] == {"role": "system", "content": "test"}
    assert call["reasoning_effort"] == "low"
    assert call["max_tokens"] == 64


@pytest.mark.asyncio
async def test_chat_adapter_assembles_tool_arguments_and_converts_history() -> None:
    first = SimpleNamespace(
        index=0,
        id="call_1",
        function=SimpleNamespace(name="calculator", arguments='{"expression":'),
    )
    second = SimpleNamespace(
        index=0,
        id=None,
        function=SimpleNamespace(name=None, arguments='"2+2"}'),
    )
    sdk = _SDK(
        [
            _chunk(delta=SimpleNamespace(tool_calls=[first])),
            _chunk(delta=SimpleNamespace(tool_calls=[second])),
            _chunk(delta=SimpleNamespace(), finish_reason="tool_calls"),
        ]
    )
    adapter = OpenAIChatCompletionsLLM(
        sdk,
        model="compatible-model",
        retry_policy=RetryPolicy(max_retries=0),
    )
    request = LLMRequest(
        instructions="test",
        input=[
            {"role": "user", "content": "calculate"},
            {
                "type": "function_call",
                "call_id": "old_call",
                "name": "calculator",
                "arguments": '{"expression":"1+1"}',
                "reasoning_content": "prior reasoning",
            },
            {"type": "function_call_output", "call_id": "old_call", "output": "2"},
        ],
        tools=[
            {
                "type": "function",
                "name": "calculator",
                "description": "calculate",
                "parameters": {
                    "type": "object",
                    "properties": {"expression": {"type": "string"}},
                    "required": ["expression"],
                    "additionalProperties": False,
                },
                "strict": True,
            }
        ],
    )

    events = [event async for event in adapter.stream(request)]

    started = next(event for event in events if isinstance(event, ToolCallStarted))
    done = next(event for event in events if isinstance(event, ToolArgumentsDone))
    assert started.call_id == "call_1"
    assert done.arguments == '{"expression":"2+2"}'
    call = sdk.chat.completions.calls[0]
    assert call["tools"][0]["function"]["name"] == "calculator"
    assert call["messages"][2]["reasoning_content"] == "prior reasoning"
    assert call["messages"][3] == {
        "role": "tool",
        "tool_call_id": "old_call",
        "content": "2",
    }
