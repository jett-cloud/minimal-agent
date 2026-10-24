import asyncio
from collections.abc import AsyncIterator, Sequence

import pytest

from minimal_agent.agent import AgentLoop, ContextBuilder, RunSupervisor
from minimal_agent.llm import (
    ContinuationItem,
    FakeLLM,
    LLMError,
    LLMEvent,
    LLMRequest,
    ReasoningDelta,
    ReasoningDone,
    ReasoningStarted,
    ResponseCompleted,
    ResponseStarted,
    TextDelta,
    TextDone,
    ToolArgumentsDelta,
    ToolArgumentsDone,
    ToolCallStarted,
)
from minimal_agent.schemas import MessageKind, MessageRecord, MessageRole, RunStatus
from minimal_agent.storage import (
    Database,
    MessageRepository,
    RunRepository,
    SessionRepository,
    StreamEventRepository,
    TraceRepository,
)
from minimal_agent.streaming import EventPublisher
from minimal_agent.tools import CALCULATOR_TOOL, ToolRegistry


class _SmallCounter:
    def count_request(self, _request: LLMRequest) -> int:
        return 1


class _FixedCounter:
    def __init__(self, tokens: int) -> None:
        self.tokens = tokens

    def count_request(self, _request: LLMRequest) -> int:
        return self.tokens


class _NeverSummarize:
    async def summarize(
        self,
        *,
        existing_summary: str | None,
        messages: Sequence[MessageRecord],
    ) -> str:
        raise AssertionError((existing_summary, messages))


class _HangingLLM:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def stream(self, _request: LLMRequest) -> AsyncIterator[LLMEvent]:
        self.started.set()
        await asyncio.Event().wait()
        if False:
            yield ResponseCompleted(response_id="unreachable")


def _final_response(text: str = "final answer", *, suffix: str = "1") -> list[LLMEvent]:
    return [
        ResponseStarted(response_id=f"response_{suffix}"),
        TextDelta(item_id=f"message_{suffix}", delta=text),
        TextDone(item_id=f"message_{suffix}", text=text),
        ResponseCompleted(
            response_id=f"response_{suffix}",
            input_tokens=10,
            output_tokens=3,
            total_tokens=13,
        ),
    ]


def _tool_response(
    arguments: str,
    *,
    call_id: str = "call_1",
    name: str = "calculator",
) -> list[LLMEvent]:
    midpoint = max(1, len(arguments) // 2)
    return [
        ResponseStarted(response_id=f"response_{call_id}"),
        ToolCallStarted(item_id=f"item_{call_id}", call_id=call_id, name=name),
        ToolArgumentsDelta(
            item_id=f"item_{call_id}",
            call_id=call_id,
            delta=arguments[:midpoint],
        ),
        ToolArgumentsDelta(
            item_id=f"item_{call_id}",
            call_id=call_id,
            delta=arguments[midpoint:],
        ),
        ToolArgumentsDone(
            item_id=f"item_{call_id}",
            call_id=call_id,
            name=name,
            arguments=arguments,
        ),
        ResponseCompleted(response_id=f"response_{call_id}"),
    ]


def _create_running_run(
    database: Database,
) -> tuple[str, str, MessageRepository, RunRepository, TraceRepository, StreamEventRepository]:
    sessions = SessionRepository(database)
    messages = MessageRepository(database)
    runs = RunRepository(database)
    session = sessions.create()
    user = messages.append(
        session.id,
        role=MessageRole.USER,
        kind=MessageKind.MESSAGE,
        content={"text": "please help"},
    )
    run = runs.create(session.id, user_message_id=user.id)
    return (
        session.id,
        run.id,
        messages,
        runs,
        TraceRepository(database),
        StreamEventRepository(database),
    )


def _make_loop(
    database: Database,
    llm: object,
    *,
    tools: ToolRegistry | None = None,
    max_steps: int = 8,
    timeout: float = 2,
    counter: object | None = None,
    context_window: int = 10_000,
) -> tuple[
    AgentLoop, str, MessageRepository, RunRepository, TraceRepository, StreamEventRepository
]:
    session_id, run_id, messages, runs, traces, events = _create_running_run(database)
    context = ContextBuilder(
        sessions=SessionRepository(database),
        messages=messages,
        runs=runs,
        token_counter=counter or _SmallCounter(),  # type: ignore[arg-type]
        summarizer=_NeverSummarize(),
        system_prompt="test system prompt",
        context_window_tokens=context_window,
    )
    loop = AgentLoop(
        llm=llm,  # type: ignore[arg-type]
        context=context,
        tools=tools or ToolRegistry(),
        messages=messages,
        runs=runs,
        traces=traces,
        stream_events=events,
        publisher=EventPublisher(),
        max_steps=max_steps,
        run_timeout_seconds=timeout,
        stream_flush_interval_seconds=0.01,
    )
    assert SessionRepository(database).require(session_id).id == session_id
    return loop, run_id, messages, runs, traces, events


@pytest.mark.asyncio
async def test_agent_loop_completes_plain_answer_with_trace_and_terminal(
    database: Database,
) -> None:
    llm = FakeLLM([_final_response("你好")])
    loop, run_id, messages, runs, traces, events = _make_loop(database, llm)

    result = await loop.execute(run_id)

    assert result.status is RunStatus.COMPLETED
    assert result.loop_count == 1
    assert messages.get(result.final_message_id).content == {"text": "你好"}
    assert [event.event_type for event in events.list_after_id(run_id)] == [
        "run.started",
        "turn.started",
        "assistant.delta",
        "assistant.done",
        "turn.completed",
        "run.completed",
    ]
    trace_types = [trace.event_type for trace in traces.list_for_run(run_id)]
    assert trace_types == [
        "run.started",
        "turn.started",
        "context.prepared",
        "llm.started",
        "llm.completed",
        "turn.completed",
        "run.completed",
    ]
    assert runs.require(run_id).final_message_id == result.final_message_id


@pytest.mark.asyncio
async def test_agent_loop_executes_tool_then_rebuilds_context(database: Database) -> None:
    llm = FakeLLM(
        [
            _tool_response('{"expression":"1+2"}'),
            _final_response("结果是 3", suffix="2"),
        ]
    )
    loop, run_id, messages, _, traces, events = _make_loop(
        database,
        llm,
        tools=ToolRegistry([CALCULATOR_TOOL]),
    )

    result = await loop.execute(run_id)

    assert result.status is RunStatus.COMPLETED
    assert result.loop_count == 2
    stored = messages.list_after(result.session_id)
    assert [message.kind for message in stored] == [
        MessageKind.MESSAGE,
        MessageKind.TOOL_CALL,
        MessageKind.TOOL_RESULT,
        MessageKind.MESSAGE,
    ]
    assert stored[2].content["result"] == {
        "ok": True,
        "data": {"expression": "1+2", "result": "3"},
        "error": None,
    }
    assert [item["type"] for item in llm.requests[1].input if "type" in item] == [
        "function_call",
        "function_call_output",
    ]
    stream = events.list_after_id(run_id)
    tool_deltas = [event for event in stream if event.event_type == "tool_call.delta"]
    assert len(tool_deltas) == 1
    assert tool_deltas[0].payload["delta"] == '{"expression":"1+2"}'
    assert "tool.completed" in [trace.event_type for trace in traces.list_for_run(run_id)]


@pytest.mark.asyncio
async def test_agent_loop_persists_reasoning_events_without_polluting_final_answer(
    database: Database,
) -> None:
    llm = FakeLLM(
        [
            [
                ResponseStarted(response_id="response_1"),
                ReasoningStarted(item_id="reason_1", source="reasoning_text", content_type="raw"),
                ReasoningDelta(
                    item_id="reason_1",
                    delta="分析过程",
                    source="reasoning_text",
                    content_type="raw",
                ),
                ReasoningDone(
                    item_id="reason_1",
                    text="分析过程",
                    source="reasoning_text",
                    content_type="raw",
                ),
                TextDelta(item_id="message_1", delta="最终回答"),
                TextDone(item_id="message_1", text="最终回答"),
                ResponseCompleted(response_id="response_1"),
            ]
        ]
    )
    loop, run_id, messages, _, _, events = _make_loop(database, llm)

    result = await loop.execute(run_id)

    assert messages.get(result.final_message_id).content == {"text": "最终回答"}
    stored = events.list_after_id(run_id)
    assert [event.event_type for event in stored if event.event_type.startswith("reasoning.")] == [
        "reasoning.started",
        "reasoning.delta",
        "reasoning.done",
    ]
    delta = next(event for event in stored if event.event_type == "reasoning.delta")
    assert delta.payload == {
        "item_id": "reason_1",
        "delta": "分析过程",
        "source": "reasoning_text",
        "content_type": "raw",
    }


@pytest.mark.asyncio
async def test_reasoning_continuation_and_chat_content_are_replayed_after_tool(
    database: Database,
) -> None:
    tool_events = _tool_response('{"expression":"2+2"}')
    tool_events.insert(
        1,
        ReasoningDelta(
            item_id="reason_1",
            delta="need calculator",
            source="reasoning_content",
            content_type="raw",
        ),
    )
    tool_events.insert(
        2,
        ContinuationItem(item={"type": "reasoning", "id": "reason_1", "summary": []}),
    )
    llm = FakeLLM([tool_events, _final_response("4", suffix="2")])
    loop, run_id, messages, _, _, _ = _make_loop(
        database,
        llm,
        tools=ToolRegistry([CALCULATOR_TOOL]),
    )

    result = await loop.execute(run_id)

    tool_call = next(
        message
        for message in messages.list_after(result.session_id)
        if message.kind is MessageKind.TOOL_CALL
    )
    assert tool_call.content["reasoning_content"] == "need calculator"
    second_input = llm.requests[1].input
    assert second_input[1] == {"type": "reasoning", "id": "reason_1", "summary": []}
    assert second_input[2]["type"] == "function_call"
    assert second_input[2]["reasoning_content"] == "need calculator"


@pytest.mark.asyncio
async def test_invalid_tool_arguments_are_returned_to_next_model_step(database: Database) -> None:
    llm = FakeLLM([_tool_response("{}"), _final_response("参数不合法", suffix="2")])
    loop, run_id, messages, _, traces, events = _make_loop(
        database,
        llm,
        tools=ToolRegistry([CALCULATOR_TOOL]),
    )

    result = await loop.execute(run_id)

    assert result.status is RunStatus.COMPLETED
    tool_result = next(
        message
        for message in messages.list_after(result.session_id)
        if message.role is MessageRole.TOOL
    )
    assert tool_result.content["result"]["error"]["code"] == "INVALID_ARGUMENTS"
    second_request = str(llm.requests[1].input)
    assert "INVALID_ARGUMENTS" in second_request
    assert "tool.failed" in [event.event_type for event in events.list_after_id(run_id)]
    assert "tool.failed" in [trace.event_type for trace in traces.list_for_run(run_id)]


@pytest.mark.asyncio
async def test_max_steps_fails_without_infinite_loop(database: Database) -> None:
    llm = FakeLLM(
        [
            _tool_response('{"expression":"1+1"}', call_id="call_1"),
            _tool_response('{"expression":"2+2"}', call_id="call_2"),
        ]
    )
    loop, run_id, messages, runs, traces, events = _make_loop(
        database,
        llm,
        tools=ToolRegistry([CALCULATOR_TOOL]),
        max_steps=2,
    )

    result = await loop.execute(run_id)

    assert result.status is RunStatus.FAILED
    assert result.loop_count == 2
    assert result.error_code == "MAX_STEPS_EXCEEDED"
    assert runs.require(run_id).status is RunStatus.FAILED
    assert messages.list_after(result.session_id)[-1].kind is MessageKind.SYSTEM_ERROR
    assert events.list_after_id(run_id)[-1].event_type == "run.failed"
    assert [trace.event_type for trace in traces.list_for_run(run_id)][-2:] == [
        "exception",
        "run.failed",
    ]


@pytest.mark.asyncio
async def test_parallel_tool_calls_are_rejected_even_if_provider_ignores_setting(
    database: Database,
) -> None:
    llm = FakeLLM(
        [
            [
                ResponseStarted(response_id="response_1"),
                ToolCallStarted(item_id="item_1", call_id="call_1", name="calculator"),
                ToolCallStarted(item_id="item_2", call_id="call_2", name="calculator"),
            ]
        ]
    )
    loop, run_id, _, _, traces, events = _make_loop(
        database,
        llm,
        tools=ToolRegistry([CALCULATOR_TOOL]),
    )

    result = await loop.execute(run_id)

    assert result.status is RunStatus.FAILED
    assert result.error_code == "MULTIPLE_TOOL_CALLS"
    assert events.list_after_id(run_id)[-1].event_type == "run.failed"
    exception = next(
        trace for trace in traces.list_for_run(run_id) if trace.event_type == "exception"
    )
    assert exception.payload["error_code"] == "MULTIPLE_TOOL_CALLS"


@pytest.mark.asyncio
async def test_total_run_timeout_becomes_durable_failure(database: Database) -> None:
    llm = _HangingLLM()
    loop, run_id, _, runs, traces, events = _make_loop(database, llm, timeout=0.2)

    result = await loop.execute(run_id)

    assert llm.started.is_set()
    assert result.status is RunStatus.FAILED
    assert result.error_code == "RUN_TIMEOUT"
    assert runs.require(run_id).status is RunStatus.FAILED
    assert events.list_after_id(run_id)[-1].event_type == "run.failed"
    assert any(
        trace.payload.get("error_code") == "RUN_TIMEOUT" for trace in traces.list_for_run(run_id)
    )


@pytest.mark.asyncio
async def test_context_limit_and_llm_errors_become_traced_run_failures(
    database: Database,
) -> None:
    unused_llm = FakeLLM([_final_response()])
    context_loop, context_run_id, _, _, context_traces, _ = _make_loop(
        database,
        unused_llm,
        counter=_FixedCounter(800),
        context_window=1_000,
    )

    context_result = await context_loop.execute(context_run_id)

    assert context_result.error_code == "CONTEXT_LIMIT_EXCEEDED"
    assert unused_llm.requests == []
    assert any(
        trace.event_type == "exception" and trace.payload["error_code"] == "CONTEXT_LIMIT_EXCEEDED"
        for trace in context_traces.list_for_run(context_run_id)
    )

    failing_llm = FakeLLM(
        [LLMError("LLM_UPSTREAM_FAILED", "upstream unavailable", retryable=False)]
    )
    llm_loop, llm_run_id, _, _, llm_traces, _ = _make_loop(database, failing_llm)

    llm_result = await llm_loop.execute(llm_run_id)

    assert llm_result.error_code == "LLM_UPSTREAM_FAILED"
    assert any(
        trace.event_type == "exception" and trace.payload["message"] == "upstream unavailable"
        for trace in llm_traces.list_for_run(llm_run_id)
    )


@pytest.mark.asyncio
async def test_supervisor_recovers_interrupted_run_with_terminal_event(database: Database) -> None:
    fake = FakeLLM([_final_response()])
    loop, run_id, _, runs, _, events = _make_loop(database, fake)
    publisher = EventPublisher()
    supervisor = RunSupervisor(loop, runs=runs, publisher=publisher)

    recovered = await supervisor.recover_interrupted()

    assert recovered == 1
    assert runs.require(run_id).status is RunStatus.FAILED
    assert runs.require(run_id).error_code == "PROCESS_RESTARTED"
    assert events.list_after_id(run_id)[-1].event_type == "run.failed"
    assert publisher.revision(run_id) == 1


@pytest.mark.asyncio
async def test_supervisor_holds_and_cleans_tasks(database: Database) -> None:
    llm = FakeLLM([_final_response()])
    loop, run_id, _, runs, _, _ = _make_loop(database, llm)
    supervisor = RunSupervisor(loop, runs=runs, publisher=EventPublisher())

    task = supervisor.start(run_id)
    assert run_id in supervisor.active_run_ids
    result = await task
    await asyncio.sleep(0)

    assert result.status is RunStatus.COMPLETED
    assert run_id not in supervisor.active_run_ids


@pytest.mark.asyncio
async def test_supervisor_shutdown_cancels_and_finalizes_long_run(database: Database) -> None:
    llm = _HangingLLM()
    loop, run_id, _, runs, _, events = _make_loop(database, llm, timeout=10)
    supervisor = RunSupervisor(loop, runs=runs, publisher=EventPublisher())
    supervisor.start(run_id)
    await llm.started.wait()

    await supervisor.shutdown(grace_seconds=0)

    assert runs.require(run_id).status is RunStatus.FAILED
    assert runs.require(run_id).error_code == "RUN_CANCELLED"
    assert events.list_after_id(run_id)[-1].event_type == "run.failed"
    assert supervisor.active_run_ids == ()
