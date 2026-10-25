"""Durable tool execution with fixed logical calls and one safe retry."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from pydantic import BaseModel

from minimal_agent.schemas import ToolInvocationRecord, ToolInvocationStatus
from minimal_agent.storage.tool_invocations import ToolInvocationRepository
from minimal_agent.tools.base import (
    ToolDefinition,
    ToolExecutionContext,
    ToolResult,
    ToolResultUnknownError,
    ToolRetrySemantics,
)
from minimal_agent.tools.registry import ToolRegistry

type InvocationCallback = Callable[[ToolInvocationRecord], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class PreparedToolInvocation:
    record: ToolInvocationRecord
    tool: ToolDefinition[Any] | None
    arguments: BaseModel | None
    immediate_result: ToolResult | None = None


@dataclass(frozen=True, slots=True)
class DurableToolOutcome:
    invocation: ToolInvocationRecord
    result: ToolResult


class DurableToolExecutor:
    """Own retry policy while the registry only validates and invokes handlers."""

    def __init__(
        self,
        registry: ToolRegistry,
        invocations: ToolInvocationRepository,
        *,
        max_attempts: int = 2,
    ) -> None:
        if max_attempts != 2:
            raise ValueError("Durable tool execution currently requires exactly two attempts")
        self.registry = registry
        self.invocations = invocations
        self.max_attempts = max_attempts

    async def prepare(
        self,
        *,
        run_id: str,
        session_id: str,
        step_no: int,
        provider_call_id: str,
        tool_name: str,
        arguments_json: str,
        tool_call_content: dict[str, Any],
    ) -> PreparedToolInvocation:
        tool = self.registry.get(tool_name)
        parsed: object
        validation_error: ToolResult | None = None
        arguments: BaseModel | None = None
        try:
            parsed = json.loads(arguments_json)
        except json.JSONDecodeError as error:
            parsed = None
            validation_error = ToolResult.failure(
                "INVALID_ARGUMENTS",
                "Tool arguments are not valid JSON",
                details={"position": error.pos},
            )
        if tool is None:
            validation_error = ToolResult.failure(
                "TOOL_NOT_FOUND",
                f"Tool is not registered: {tool_name}",
                details={"tool_name": tool_name},
            )
            semantics = ToolRetrySemantics.NEVER
        else:
            semantics = tool.retry_semantics
            if validation_error is None:
                if not isinstance(parsed, dict):
                    validation_error = ToolResult.failure(
                        "INVALID_ARGUMENTS", "Tool arguments must be a JSON object"
                    )
                else:
                    arguments, validation_error = self.registry.validate_arguments(tool, parsed)

        canonical_json = arguments_json
        if arguments is not None:
            canonical_json = json.dumps(
                arguments.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        idempotency_key = (
            f"toolop_{uuid4().hex}"
            if semantics is ToolRetrySemantics.WITH_IDEMPOTENCY_KEY
            else None
        )
        record = await asyncio.to_thread(
            self.invocations.create_with_tool_call,
            run_id=run_id,
            session_id=session_id,
            step_no=step_no,
            provider_call_id=provider_call_id,
            tool_name=tool_name,
            arguments_json=canonical_json,
            retry_semantics=semantics.value,
            idempotency_key=idempotency_key,
            tool_call_content=tool_call_content,
        )
        if validation_error is not None:
            record = await asyncio.to_thread(
                self.invocations.complete_with_tool_result, record.id, validation_error
            )
        return PreparedToolInvocation(record, tool, arguments, validation_error)

    async def execute(
        self,
        prepared: PreparedToolInvocation,
        *,
        on_attempt: InvocationCallback | None = None,
        on_retry: InvocationCallback | None = None,
        on_unknown: InvocationCallback | None = None,
    ) -> DurableToolOutcome:
        if prepared.immediate_result is not None:
            return DurableToolOutcome(prepared.record, prepared.immediate_result)
        if prepared.tool is None or prepared.arguments is None:
            raise RuntimeError("Executable invocation is missing its tool or arguments")
        return await self._execute(
            prepared.record,
            prepared.tool,
            prepared.arguments,
            on_attempt=on_attempt,
            on_retry=on_retry,
            on_unknown=on_unknown,
        )

    async def resume(
        self,
        invocation_id: str,
        *,
        on_attempt: InvocationCallback | None = None,
        on_retry: InvocationCallback | None = None,
        on_unknown: InvocationCallback | None = None,
    ) -> DurableToolOutcome:
        record = await asyncio.to_thread(self.invocations.require, invocation_id)
        if record.status in (ToolInvocationStatus.SUCCEEDED, ToolInvocationStatus.FAILED):
            if record.result is None:
                raise RuntimeError("Completed invocation has no persisted result")
            return DurableToolOutcome(record, ToolResult.model_validate(record.result))
        tool = self.registry.get(record.tool_name)
        if tool is None:
            raise ToolResultUnknownError("Registered tool is unavailable during recovery")
        parsed = json.loads(record.arguments_json)
        arguments, error = self.registry.validate_arguments(tool, parsed)
        if error is not None or arguments is None:
            raise ToolResultUnknownError("Persisted tool arguments cannot be recovered")
        if record.status is ToolInvocationStatus.PENDING and record.attempt_count > 0:
            record = await asyncio.to_thread(
                self.invocations.mark_unknown,
                record.id,
                error={
                    "code": "PROCESS_INTERRUPTED",
                    "message": "Process stopped while the tool result was unknown",
                },
            )
            if on_unknown is not None:
                await on_unknown(record)
        return await self._execute(
            record,
            tool,
            arguments,
            on_attempt=on_attempt,
            on_retry=on_retry,
            on_unknown=on_unknown,
        )

    async def _execute(
        self,
        record: ToolInvocationRecord,
        tool: ToolDefinition[Any],
        arguments: BaseModel,
        *,
        on_attempt: InvocationCallback | None,
        on_retry: InvocationCallback | None,
        on_unknown: InvocationCallback | None,
    ) -> DurableToolOutcome:
        while record.attempt_count < self.max_attempts:
            if record.status is ToolInvocationStatus.UNKNOWN:
                if not self._may_retry(record, tool):
                    raise ToolResultUnknownError()
                if on_retry is not None:
                    await on_retry(record)
            record = await asyncio.to_thread(self.invocations.start_attempt, record.id)
            if on_attempt is not None:
                await on_attempt(record)
            context = ToolExecutionContext(
                invocation_id=record.id,
                provider_call_id=record.provider_call_id,
                idempotency_key=record.idempotency_key,
                attempt_no=record.attempt_count,
            )
            try:
                if tool.timeout_seconds is None:
                    result = await self.registry.invoke_validated(tool, arguments, context=context)
                else:
                    async with asyncio.timeout(tool.timeout_seconds):
                        result = await self.registry.invoke_validated(
                            tool, arguments, context=context
                        )
            except TimeoutError:
                record = await asyncio.to_thread(
                    self.invocations.mark_unknown,
                    record.id,
                    error={
                        "code": "TOOL_TIMEOUT",
                        "message": ("Tool call timed out before a definite result was received"),
                        "attempt_no": record.attempt_count,
                    },
                )
                if on_unknown is not None:
                    await on_unknown(record)
                continue
            record = await asyncio.to_thread(
                self.invocations.complete_with_tool_result, record.id, result
            )
            return DurableToolOutcome(record, result)
        raise ToolResultUnknownError()

    @staticmethod
    def _may_retry(record: ToolInvocationRecord, tool: ToolDefinition[Any]) -> bool:
        if tool.retry_semantics is ToolRetrySemantics.SAFE:
            return True
        return (
            tool.retry_semantics is ToolRetrySemantics.WITH_IDEMPOTENCY_KEY
            and record.idempotency_key is not None
        )
