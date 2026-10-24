"""Framework-free ReAct loop connecting context, LLM, tools, storage, and SSE."""

import asyncio
import json
from dataclasses import dataclass
from time import monotonic
from typing import Any

from minimal_agent.agent.accumulators import StepAccumulator
from minimal_agent.agent.compression import ContextCompressionError
from minimal_agent.agent.context import ContextBuilder, ContextDataError, ContextLimitExceeded
from minimal_agent.agent.decisions import FinalAnswerDecision, ToolCallDecision
from minimal_agent.agent.errors import AgentLoopError, MaxStepsExceeded
from minimal_agent.llm import (
    LLMClient,
    LLMError,
    RequestRetrying,
    ResponseStarted,
    is_terminal_event,
)
from minimal_agent.schemas import MessageKind, MessageRole, RunRecord, RunStatus
from minimal_agent.storage import (
    MessageRepository,
    RunRepository,
    StreamEventRepository,
    TraceRepository,
)
from minimal_agent.streaming import (
    AssistantDoneEvent,
    BufferedEventWriter,
    EventPublisher,
    RunStartedEvent,
    ToolCallReadyEvent,
    ToolCompletedEvent,
    ToolFailedEvent,
    ToolStartedEvent,
    TurnCompletedEvent,
    TurnStartedEvent,
)
from minimal_agent.tools import ToolRegistry

_MAX_ERROR_MESSAGE_LENGTH = 1000


@dataclass(frozen=True, slots=True)
class _FinalOutcome:
    run: RunRecord
    decision: FinalAnswerDecision


class AgentLoop:
    def __init__(
        self,
        *,
        llm: LLMClient,
        context: ContextBuilder,
        tools: ToolRegistry,
        messages: MessageRepository,
        runs: RunRepository,
        traces: TraceRepository,
        stream_events: StreamEventRepository,
        publisher: EventPublisher,
        max_steps: int = 8,
        run_timeout_seconds: float = 60.0,
        stream_flush_bytes: int = 8192,
        stream_flush_interval_seconds: float = 0.1,
        stream_queue_maxsize: int = 256,
    ) -> None:
        if max_steps < 1 or run_timeout_seconds <= 0:
            raise ValueError("Agent limits must be positive")
        self._llm = llm
        self._context = context
        self._tools = tools
        self._messages = messages
        self._runs = runs
        self._traces = traces
        self._stream_events = stream_events
        self._publisher = publisher
        self._max_steps = max_steps
        self._run_timeout_seconds = run_timeout_seconds
        self._stream_flush_bytes = stream_flush_bytes
        self._stream_flush_interval_seconds = stream_flush_interval_seconds
        self._stream_queue_maxsize = stream_queue_maxsize

    async def execute(self, run_id: str) -> RunRecord:
        run = await asyncio.to_thread(self._runs.require, run_id)
        if run.status != RunStatus.RUNNING:
            raise AgentLoopError("RUN_NOT_RUNNING", f"Run is not running: {run_id}")
        writer = BufferedEventWriter(
            run_id,
            store=self._stream_events,
            publisher=self._publisher,
            flush_bytes=self._stream_flush_bytes,
            flush_interval_seconds=self._stream_flush_interval_seconds,
            queue_maxsize=self._stream_queue_maxsize,
        )
        await writer.publish(RunStartedEvent(run_id=run.id, session_id=run.session_id))
        await self._trace(
            run.id,
            step_no=0,
            event_type="run.started",
            payload={"session_id": run.session_id},
        )

        terminal: RunRecord | None = None
        try:
            async with asyncio.timeout(self._run_timeout_seconds):
                outcome = await self._run_steps(run, writer)
            terminal = await self._commit_final_answer(outcome)
        except TimeoutError:
            terminal = await self._fail_run(
                run.id,
                writer,
                code="RUN_TIMEOUT",
                message=f"Agent exceeded the {self._run_timeout_seconds:g} second time limit",
                exception_type="TimeoutError",
            )
        except asyncio.CancelledError:
            await asyncio.shield(
                self._fail_run(
                    run.id,
                    writer,
                    code="RUN_CANCELLED",
                    message="Agent task was cancelled before completion",
                    exception_type="CancelledError",
                )
            )
            raise
        except Exception as error:
            code, message = _normalize_error(error)
            terminal = await self._fail_run(
                run.id,
                writer,
                code=code,
                message=message,
                exception_type=type(error).__name__,
            )
        finally:
            try:
                await writer.close()
            except Exception as close_error:
                await self._trace(
                    run.id,
                    step_no=(await asyncio.to_thread(self._runs.require, run.id)).loop_count,
                    event_type="stream.close_failed",
                    payload={"exception_type": type(close_error).__name__},
                )
                if terminal is None:
                    raise

        if terminal is None:
            raise RuntimeError("AgentLoop exited without a terminal Run")
        return terminal

    async def _run_steps(
        self,
        initial_run: RunRecord,
        writer: BufferedEventWriter,
    ) -> _FinalOutcome:
        for step_no in range(1, self._max_steps + 1):
            run = await asyncio.to_thread(self._runs.increment_loop, initial_run.id)
            await writer.publish(TurnStartedEvent(run_id=run.id, step_no=step_no))
            await self._trace(
                run.id,
                step_no=step_no,
                event_type="turn.started",
                payload={},
            )

            prepared = await self._context.prepare(
                session_id=run.session_id,
                current_user_message_id=run.user_message_id,
                tools=self._tools,
            )
            await self._trace(
                run.id,
                step_no=step_no,
                event_type="context.prepared",
                payload={
                    "estimated_input_tokens": prepared.estimated_input_tokens,
                    "trigger_tokens": prepared.trigger_tokens,
                    "compression_applied": prepared.compression_applied,
                    "summary_through_seq": prepared.summary_through_seq,
                    "requested_tools": list(prepared.requested_tools),
                },
            )

            accumulator = StepAccumulator()
            llm_started = monotonic()
            async for event in self._llm.stream(prepared.request):
                await accumulator.consume(event, writer)
                if isinstance(event, ResponseStarted):
                    await self._trace(
                        run.id,
                        step_no=step_no,
                        event_type="llm.started",
                        payload={"response_id": event.response_id},
                    )
                elif isinstance(event, RequestRetrying):
                    await self._trace(
                        run.id,
                        step_no=step_no,
                        event_type="llm.retrying",
                        payload=accumulator.retry_notices[-1],
                    )
                if is_terminal_event(event):
                    break
            decision = accumulator.decision()
            await self._trace(
                run.id,
                step_no=step_no,
                event_type="llm.completed",
                payload={
                    "response_id": accumulator.response_id,
                    "decision": (
                        "tool_call" if isinstance(decision, ToolCallDecision) else "final_answer"
                    ),
                    "usage": accumulator.usage,
                },
                duration_ms=_elapsed_ms(llm_started),
            )

            if isinstance(decision, ToolCallDecision):
                await self._handle_tool_call(run, step_no, decision, writer)
                await writer.publish(
                    TurnCompletedEvent(run_id=run.id, step_no=step_no, outcome="tool_call")
                )
                await self._trace(
                    run.id,
                    step_no=step_no,
                    event_type="turn.completed",
                    payload={"outcome": "tool_call"},
                )
                continue

            return await self._prepare_final_answer(run, step_no, decision, writer)

        raise MaxStepsExceeded(self._max_steps)

    async def _handle_tool_call(
        self,
        run: RunRecord,
        step_no: int,
        decision: ToolCallDecision,
        writer: BufferedEventWriter,
    ) -> None:
        await asyncio.to_thread(
            self._messages.append,
            run.session_id,
            run_id=run.id,
            role=MessageRole.ASSISTANT,
            kind=MessageKind.TOOL_CALL,
            content={
                "call_id": decision.call_id,
                "name": decision.name,
                "arguments": decision.arguments_json,
                "continuation_items": list(decision.continuation_items),
                "reasoning_content": decision.reasoning_content,
            },
        )
        arguments = _parse_arguments(decision.arguments_json)
        if isinstance(arguments, dict):
            await writer.publish(
                ToolCallReadyEvent(
                    item_id=decision.item_id,
                    call_id=decision.call_id,
                    name=decision.name,
                    arguments=arguments,
                    source_seq=decision.source_seq,
                )
            )
            await writer.publish(
                ToolStartedEvent(
                    call_id=decision.call_id,
                    name=decision.name,
                    arguments=arguments,
                )
            )
            tool_started = monotonic()
            result = await self._tools.execute(decision.name, arguments)
        else:
            tool_started = monotonic()
            result = await self._tools.execute_json(decision.name, decision.arguments_json)

        await asyncio.to_thread(
            self._messages.append,
            run.session_id,
            run_id=run.id,
            role=MessageRole.TOOL,
            kind=MessageKind.TOOL_RESULT,
            content={"call_id": decision.call_id, "result": result.model_dump(mode="json")},
        )
        duration_ms = _elapsed_ms(tool_started)
        if result.ok:
            await writer.publish(
                ToolCompletedEvent(
                    call_id=decision.call_id,
                    name=decision.name,
                    result=result.data,
                )
            )
            trace_payload: dict[str, Any] = {
                "call_id": decision.call_id,
                "tool_name": decision.name,
                "ok": True,
            }
            trace_type = "tool.completed"
        else:
            if result.error is None:
                raise RuntimeError("Failed ToolResult has no error")
            await writer.publish(
                ToolFailedEvent(
                    call_id=decision.call_id,
                    name=decision.name,
                    error=result.error.model_dump(mode="json"),
                )
            )
            trace_payload = {
                "call_id": decision.call_id,
                "tool_name": decision.name,
                "ok": False,
                "error": result.error.model_dump(mode="json"),
            }
            trace_type = "tool.failed"
        await self._trace(
            run.id,
            step_no=step_no,
            event_type=trace_type,
            payload=trace_payload,
            duration_ms=duration_ms,
        )

    async def _prepare_final_answer(
        self,
        run: RunRecord,
        step_no: int,
        decision: FinalAnswerDecision,
        writer: BufferedEventWriter,
    ) -> _FinalOutcome:
        if not decision.done_event_emitted:
            await writer.publish(AssistantDoneEvent(item_id=decision.item_id, text=decision.text))
        await writer.publish(
            TurnCompletedEvent(run_id=run.id, step_no=step_no, outcome="final_answer")
        )
        await self._trace(
            run.id,
            step_no=step_no,
            event_type="turn.completed",
            payload={"outcome": "final_answer"},
        )
        await writer.flush()
        return _FinalOutcome(run=run, decision=decision)

    async def _commit_final_answer(self, outcome: _FinalOutcome) -> RunRecord:
        completed, _, _ = await asyncio.to_thread(
            self._runs.complete_with_final_answer,
            outcome.run.id,
            text=outcome.decision.text,
        )
        await self._publisher.notify_committed(outcome.run.id)
        return completed

    async def _fail_run(
        self,
        run_id: str,
        writer: BufferedEventWriter,
        *,
        code: str,
        message: str,
        exception_type: str,
    ) -> RunRecord:
        current = await asyncio.to_thread(self._runs.require, run_id)
        if current.status != RunStatus.RUNNING:
            return current
        safe_message = message[:_MAX_ERROR_MESSAGE_LENGTH]
        await self._trace(
            run_id,
            step_no=current.loop_count,
            event_type="exception",
            payload={
                "error_code": code,
                "message": safe_message,
                "exception_type": exception_type,
            },
        )
        try:
            await writer.flush()
        except Exception as flush_error:
            await self._trace(
                run_id,
                step_no=current.loop_count,
                event_type="stream.flush_failed",
                payload={"exception_type": type(flush_error).__name__},
            )
        failed, _, _ = await asyncio.to_thread(
            self._runs.fail_with_error,
            run_id,
            error_code=code,
            error_message=safe_message,
        )
        await self._publisher.notify_committed(run_id)
        return failed

    async def _trace(
        self,
        run_id: str,
        *,
        step_no: int,
        event_type: str,
        payload: dict[str, Any],
        duration_ms: int | None = None,
    ) -> None:
        await asyncio.to_thread(
            self._traces.append,
            run_id,
            step_no=step_no,
            event_type=event_type,
            payload=payload,
            duration_ms=duration_ms,
        )


def _parse_arguments(arguments_json: str) -> dict[str, Any] | None:
    try:
        value = json.loads(arguments_json)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _elapsed_ms(started: float) -> int:
    return max(0, round((monotonic() - started) * 1000))


def _normalize_error(error: Exception) -> tuple[str, str]:
    if isinstance(error, (AgentLoopError, LLMError)):
        return error.code, error.message
    if isinstance(error, (ContextLimitExceeded, ContextDataError, ContextCompressionError)):
        return error.code, str(error)
    return "INTERNAL_ERROR", "Agent failed unexpectedly"
