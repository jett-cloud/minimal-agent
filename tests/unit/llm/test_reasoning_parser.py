import pytest

from minimal_agent.llm import (
    FakeLLM,
    LLMRequest,
    ReasoningAwareLLM,
    ReasoningDelta,
    ReasoningDone,
    ReasoningStarted,
    ResponseCompleted,
    ResponseIncomplete,
    ResponseStarted,
    TextDelta,
    TextDone,
)


def _request() -> LLMRequest:
    return LLMRequest(instructions="test", input=[{"role": "user", "content": "hello"}])


async def _events(script, **settings):
    client = ReasoningAwareLLM(FakeLLM([script]), **settings)
    return [event async for event in client.stream(_request())]


@pytest.mark.asyncio
async def test_think_tag_is_parsed_across_chunks_and_removed_from_answer() -> None:
    raw = "<think>plan carefully</think>final answer"
    events = await _events(
        [
            ResponseStarted(response_id="r1"),
            TextDelta(item_id="m1", delta="  <thi"),
            TextDelta(item_id="m1", delta="nk>plan care"),
            TextDelta(item_id="m1", delta="fully</thi"),
            TextDelta(item_id="m1", delta="nk>final answer"),
            TextDone(item_id="m1", text="  " + raw),
            ResponseCompleted(response_id="r1"),
        ]
    )

    assert any(isinstance(event, ReasoningStarted) for event in events)
    reasoning = [event for event in events if isinstance(event, ReasoningDelta)]
    assert "".join(event.delta for event in reasoning) == "plan carefully"
    done = next(event for event in events if isinstance(event, ReasoningDone))
    assert done.source == "think_tag"
    assert done.completed is True
    answer = [event for event in events if isinstance(event, TextDelta)]
    assert "".join(event.delta for event in answer) == "final answer"
    assert next(event for event in events if isinstance(event, TextDone)).text == "final answer"


@pytest.mark.asyncio
async def test_literal_think_tag_after_answer_is_not_parsed() -> None:
    text = "Example: <think>literal</think>"
    events = await _events(
        [
            TextDelta(item_id="m1", delta=text),
            TextDone(item_id="m1", text=text),
            ResponseCompleted(response_id="r1"),
        ]
    )

    assert not any(isinstance(event, ReasoningDelta) for event in events)
    assert next(event for event in events if isinstance(event, TextDone)).text == text


@pytest.mark.asyncio
async def test_auto_capture_keeps_first_reasoning_source_without_duplicate() -> None:
    raw = "<think>tag duplicate</think>answer"
    events = await _events(
        [
            ReasoningStarted(item_id="reason1", source="reasoning_content", content_type="raw"),
            ReasoningDelta(
                item_id="reason1",
                delta="structured",
                source="reasoning_content",
                content_type="raw",
            ),
            ReasoningDone(
                item_id="reason1",
                text="structured",
                source="reasoning_content",
                content_type="raw",
            ),
            TextDelta(item_id="m1", delta=raw),
            TextDone(item_id="m1", text=raw),
            ResponseCompleted(response_id="r1"),
        ],
        capture="auto",
    )

    deltas = [event for event in events if isinstance(event, ReasoningDelta)]
    assert [(event.source, event.delta) for event in deltas] == [
        ("reasoning_content", "structured")
    ]
    assert next(event for event in events if isinstance(event, TextDone)).text == "answer"


@pytest.mark.asyncio
async def test_both_capture_emits_structured_and_tagged_reasoning() -> None:
    raw = "<think>tagged</think>answer"
    events = await _events(
        [
            ReasoningDelta(
                item_id="reason1",
                delta="structured",
                source="reasoning_text",
                content_type="raw",
            ),
            TextDelta(item_id="m1", delta=raw),
            TextDone(item_id="m1", text=raw),
            ResponseCompleted(response_id="r1"),
        ],
        capture="both",
    )

    assert [
        (event.source, event.delta) for event in events if isinstance(event, ReasoningDelta)
    ] == [
        ("reasoning_text", "structured"),
        ("think_tag", "tagged"),
    ]


@pytest.mark.asyncio
async def test_unclosed_think_tag_becomes_incomplete_response() -> None:
    raw = "<think>unfinished"
    events = await _events(
        [
            TextDelta(item_id="m1", delta=raw),
            TextDone(item_id="m1", text=raw),
            ResponseCompleted(response_id="r1"),
        ]
    )

    done = next(event for event in events if isinstance(event, ReasoningDone))
    assert done.completed is False
    assert not any(isinstance(event, ResponseCompleted) for event in events)
    incomplete = next(event for event in events if isinstance(event, ResponseIncomplete))
    assert incomplete.reason == "think_tag_not_closed"


@pytest.mark.asyncio
async def test_reasoning_output_off_still_removes_think_text_from_answer() -> None:
    raw = "<think>hidden by configuration</think>visible"
    events = await _events(
        [
            TextDelta(item_id="m1", delta=raw),
            TextDone(item_id="m1", text=raw),
            ResponseCompleted(response_id="r1"),
        ],
        output="off",
    )

    assert not any(
        isinstance(event, (ReasoningStarted, ReasoningDelta, ReasoningDone)) for event in events
    )
    assert next(event for event in events if isinstance(event, TextDone)).text == "visible"


@pytest.mark.asyncio
async def test_lifecycle_only_reasoning_gets_a_matching_done_event() -> None:
    events = await _events(
        [
            ReasoningStarted(item_id="reason1"),
            TextDelta(item_id="m1", delta="answer"),
            TextDone(item_id="m1", text="answer"),
            ResponseCompleted(response_id="r1"),
        ]
    )

    done = next(event for event in events if isinstance(event, ReasoningDone))
    assert done.item_id == "reason1"
    assert done.source == "lifecycle"
    assert done.content_type == "activity"
    assert done.text is None
