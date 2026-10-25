import sqlite3

import pytest

from minimal_agent.schemas import MessageKind, MessageRole, ToolInvocationStatus
from minimal_agent.storage import (
    Database,
    InvalidStateError,
    MessageRepository,
    RunRepository,
    SessionRepository,
    ToolInvocationRepository,
)
from minimal_agent.tools import ToolResult


def _running_run(database: Database) -> tuple[str, str]:
    session = SessionRepository(database).create()
    message = MessageRepository(database).append(
        session.id,
        role=MessageRole.USER,
        kind=MessageKind.MESSAGE,
        content={"text": "test durable tool boundary"},
    )
    run = RunRepository(database).create(session.id, user_message_id=message.id)
    RunRepository(database).increment_loop(run.id)
    return session.id, run.id


def _create_invocation(
    repository: ToolInvocationRepository,
    *,
    session_id: str,
    run_id: str,
    arguments_json: str = '{"value":"fixed"}',
):
    return repository.create_with_tool_call(
        run_id=run_id,
        session_id=session_id,
        step_no=1,
        provider_call_id="provider-call-1",
        tool_name="test_tool",
        arguments_json=arguments_json,
        retry_semantics="safe",
        idempotency_key=None,
        tool_call_content={
            "call_id": "provider-call-1",
            "name": "test_tool",
            "arguments": arguments_json,
        },
    )


def test_create_rolls_back_tool_call_message_when_invocation_insert_fails(
    database: Database,
) -> None:
    # 验证工具调用消息与逻辑调用记录位于同一事务。
    session_id, run_id = _running_run(database)
    repository = ToolInvocationRepository(database)
    with database.connection() as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_tool_invocation_insert
            BEFORE INSERT ON tool_invocations
            BEGIN
                SELECT RAISE(ABORT, 'injected invocation insert failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected invocation insert failure"):
        _create_invocation(repository, session_id=session_id, run_id=run_id)

    messages = MessageRepository(database).list_after(session_id)
    assert [message.kind for message in messages] == [MessageKind.MESSAGE]
    assert repository.latest_for_run(run_id) is None


def test_complete_rolls_back_tool_result_message_when_invocation_update_fails(
    database: Database,
) -> None:
    # 验证工具结果消息与逻辑调用终态位于同一事务。
    session_id, run_id = _running_run(database)
    repository = ToolInvocationRepository(database)
    invocation = _create_invocation(repository, session_id=session_id, run_id=run_id)
    repository.start_attempt(invocation.id)
    with database.connection() as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_tool_invocation_completion
            BEFORE UPDATE ON tool_invocations
            WHEN NEW.status IN ('succeeded', 'failed')
            BEGIN
                SELECT RAISE(ABORT, 'injected invocation completion failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected invocation completion failure"):
        repository.complete_with_tool_result(invocation.id, ToolResult.success({"value": "fixed"}))

    persisted = repository.require(invocation.id)
    messages = MessageRepository(database).list_after(session_id)
    assert persisted.status is ToolInvocationStatus.PENDING
    assert persisted.result is None
    assert persisted.result_message_id is None
    assert [message.kind for message in messages] == [
        MessageKind.MESSAGE,
        MessageKind.TOOL_CALL,
    ]


def test_provider_call_id_replays_same_call_and_rejects_changed_arguments(
    database: Database,
) -> None:
    # 验证同一 provider call 只能映射到一个固定逻辑调用。
    session_id, run_id = _running_run(database)
    repository = ToolInvocationRepository(database)

    first = _create_invocation(repository, session_id=session_id, run_id=run_id)
    replay = _create_invocation(repository, session_id=session_id, run_id=run_id)

    assert replay.id == first.id
    messages = MessageRepository(database).list_after(session_id)
    assert [message.kind for message in messages].count(MessageKind.TOOL_CALL) == 1

    with pytest.raises(InvalidStateError, match="reused for a different tool call"):
        _create_invocation(
            repository,
            session_id=session_id,
            run_id=run_id,
            arguments_json='{"value":"changed"}',
        )
