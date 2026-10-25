import asyncio
import threading
from decimal import Decimal

import pytest
from pydantic import BaseModel, ConfigDict

from minimal_agent.schemas import MessageKind, MessageRole, ToolInvocationStatus
from minimal_agent.storage import (
    Database,
    MessageRepository,
    OrderRepository,
    RunRepository,
    SessionRepository,
    ToolInvocationRepository,
)
from minimal_agent.tools import (
    DurableToolExecutor,
    ToolDefinition,
    ToolExecutionContext,
    ToolRegistry,
    ToolResultUnknownError,
    ToolRetrySemantics,
    ToolSideEffect,
)


class ValueArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str


def _run(database: Database) -> tuple[str, str]:
    session = SessionRepository(database).create()
    message = MessageRepository(database).append(
        session.id,
        role=MessageRole.USER,
        kind=MessageKind.MESSAGE,
        content={"text": "test"},
    )
    run = RunRepository(database).create(session.id, user_message_id=message.id)
    RunRepository(database).increment_loop(run.id)
    return session.id, run.id


async def _prepare(
    executor: DurableToolExecutor,
    *,
    session_id: str,
    run_id: str,
    name: str,
    arguments_json: str = '{"value":"fixed"}',
):
    return await executor.prepare(
        run_id=run_id,
        session_id=session_id,
        step_no=1,
        provider_call_id="provider-call-1",
        tool_name=name,
        arguments_json=arguments_json,
        tool_call_content={
            "call_id": "provider-call-1",
            "name": name,
            "arguments": arguments_json,
        },
    )


@pytest.mark.asyncio
async def test_safe_timeout_retries_once_with_same_logical_call(database: Database) -> None:
    # 验证 safe 工具超时后只重试一次并复用逻辑调用。
    attempts: list[tuple[str, int, str]] = []
    first_attempt_started = asyncio.Event()
    release_first_attempt = asyncio.Event()

    async def handler(arguments: ValueArguments, context: ToolExecutionContext):
        attempts.append((context.invocation_id, context.attempt_no, arguments.value))
        if context.attempt_no == 1:
            first_attempt_started.set()
            await release_first_attempt.wait()
        return {"value": arguments.value}

    tool = ToolDefinition(
        name="safe_write",
        description="Test safe retry.",
        args_model=ValueArguments,
        handler=handler,
        timeout_seconds=0.01,
        retry_semantics=ToolRetrySemantics.SAFE,
    )
    invocations = ToolInvocationRepository(database)
    executor = DurableToolExecutor(ToolRegistry([tool]), invocations)
    session_id, run_id = _run(database)

    outcome = await executor.execute(
        await _prepare(executor, session_id=session_id, run_id=run_id, name=tool.name)
    )

    assert outcome.result.ok
    assert [attempt[1] for attempt in attempts] == [1, 2]
    assert attempts[0][0] == attempts[1][0] == outcome.invocation.id
    assert outcome.invocation.status is ToolInvocationStatus.SUCCEEDED
    assert outcome.invocation.attempt_count == 2
    assert outcome.invocation.result_message_id is not None
    assert first_attempt_started.is_set()


@pytest.mark.asyncio
async def test_idempotency_retry_cannot_create_duplicate_order(database: Database) -> None:
    # 验证带幂等键重试不会创建重复订单。
    repository = OrderRepository(database)
    attempt_lock = threading.Lock()
    attempt_count = 0
    keys: list[str | None] = []
    release_first_attempt = threading.Event()
    first_attempt_finished = threading.Event()

    def handler(arguments: ValueArguments, context: ToolExecutionContext):
        nonlocal attempt_count
        with attempt_lock:
            attempt_count += 1
            current_attempt = attempt_count
            keys.append(context.idempotency_key)
        if current_attempt == 1:
            release_first_attempt.wait(timeout=5)
        try:
            record = repository.create(
                customer_name="Alice",
                product_name=arguments.value,
                quantity=1,
                unit_price=Decimal("10.00"),
                idempotency_key=context.idempotency_key or "missing",
            )
            return record.model_dump(mode="json")
        finally:
            if current_attempt == 1:
                first_attempt_finished.set()

    tool = ToolDefinition(
        name="idempotent_order",
        description="Create one idempotent order.",
        args_model=ValueArguments,
        handler=handler,
        timeout_seconds=0.2,
        side_effect=ToolSideEffect.WRITE,
        retry_semantics=ToolRetrySemantics.WITH_IDEMPOTENCY_KEY,
    )
    invocations = ToolInvocationRepository(database)
    executor = DurableToolExecutor(ToolRegistry([tool]), invocations)
    session_id, run_id = _run(database)

    outcome = await executor.execute(
        await _prepare(executor, session_id=session_id, run_id=run_id, name=tool.name)
    )
    release_first_attempt.set()
    assert await asyncio.to_thread(first_attempt_finished.wait, 2)

    assert outcome.result.ok
    assert len(repository.list()) == 1
    assert len(keys) == 2 and keys[0] == keys[1]
    assert keys[0] == outcome.invocation.idempotency_key


@pytest.mark.asyncio
async def test_handler_timeout_after_commit_replays_same_order(database: Database) -> None:
    # 验证 handler 在业务提交后自行报告超时时，执行器仍按未知结果使用原键重试。
    repository = OrderRepository(database)
    attempts: list[ToolExecutionContext] = []
    created_ids: list[str] = []

    def handler(arguments: ValueArguments, context: ToolExecutionContext):
        attempts.append(context)
        record = repository.create(
            customer_name="Alice",
            product_name=arguments.value,
            quantity=1,
            unit_price=Decimal("10.00"),
            idempotency_key=context.idempotency_key or "missing",
        )
        created_ids.append(record.id)
        if context.attempt_no == 1:
            raise TimeoutError("response was lost after commit")
        return record.model_dump(mode="json")

    tool = ToolDefinition(
        name="commit_then_timeout",
        description="Create an order and simulate a lost committed response.",
        args_model=ValueArguments,
        handler=handler,
        timeout_seconds=1,
        side_effect=ToolSideEffect.WRITE,
        retry_semantics=ToolRetrySemantics.WITH_IDEMPOTENCY_KEY,
    )
    invocations = ToolInvocationRepository(database)
    executor = DurableToolExecutor(ToolRegistry([tool]), invocations)
    session_id, run_id = _run(database)

    outcome = await executor.execute(
        await _prepare(executor, session_id=session_id, run_id=run_id, name=tool.name)
    )

    assert outcome.result.ok
    assert [context.attempt_no for context in attempts] == [1, 2]
    assert attempts[0].idempotency_key == attempts[1].idempotency_key
    assert created_ids == [created_ids[0], created_ids[0]]
    assert outcome.result.data["id"] == created_ids[0]
    assert len(repository.list()) == 1
    assert outcome.invocation.status is ToolInvocationStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_never_retry_leaves_unknown_and_does_not_run_twice(database: Database) -> None:
    # 验证 never 工具超时后保持未知且不会再次执行。
    attempts = 0
    attempt_started = asyncio.Event()
    release_attempt = asyncio.Event()

    async def handler(arguments: ValueArguments):
        nonlocal attempts
        attempts += 1
        attempt_started.set()
        await release_attempt.wait()
        return arguments.value

    tool = ToolDefinition(
        name="unsafe_write",
        description="Test no retry.",
        args_model=ValueArguments,
        handler=handler,
        timeout_seconds=0.01,
        side_effect=ToolSideEffect.WRITE,
        retry_semantics=ToolRetrySemantics.NEVER,
    )
    invocations = ToolInvocationRepository(database)
    executor = DurableToolExecutor(ToolRegistry([tool]), invocations)
    session_id, run_id = _run(database)
    prepared = await _prepare(executor, session_id=session_id, run_id=run_id, name=tool.name)

    with pytest.raises(ToolResultUnknownError):
        await executor.execute(prepared)

    persisted = invocations.require(prepared.record.id)
    assert attempts == 1
    assert persisted.status is ToolInvocationStatus.UNKNOWN
    assert persisted.attempt_count == 1
    assert persisted.result_message_id is None
    assert attempt_started.is_set()


@pytest.mark.asyncio
async def test_resume_reuses_persisted_parameters_and_key(database: Database) -> None:
    # 验证恢复调用时复用持久化参数和幂等键。
    contexts: list[ToolExecutionContext] = []

    async def handler(arguments: ValueArguments, context: ToolExecutionContext):
        contexts.append(context)
        return {"value": arguments.value}

    tool = ToolDefinition(
        name="recoverable",
        description="Test recovery.",
        args_model=ValueArguments,
        handler=handler,
        retry_semantics=ToolRetrySemantics.WITH_IDEMPOTENCY_KEY,
    )
    invocations = ToolInvocationRepository(database)
    executor = DurableToolExecutor(ToolRegistry([tool]), invocations)
    session_id, run_id = _run(database)
    prepared = await _prepare(executor, session_id=session_id, run_id=run_id, name=tool.name)
    invocations.start_attempt(prepared.record.id)
    invocations.mark_unknown(
        prepared.record.id,
        error={"code": "PROCESS_INTERRUPTED", "message": "result unknown"},
    )

    outcome = await executor.resume(prepared.record.id)

    assert outcome.result.data == {"value": "fixed"}
    assert contexts[0].invocation_id == prepared.record.id
    assert contexts[0].idempotency_key == prepared.record.idempotency_key
    assert contexts[0].attempt_no == 2
