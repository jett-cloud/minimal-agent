import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from minimal_agent.schemas import MessageKind, MessageRole, PendingStreamEvent, RunStatus
from minimal_agent.storage import (
    ActiveRunExistsError,
    Database,
    InvalidStateError,
    MessageRepository,
    NotFoundError,
    RunRepository,
    SessionRepository,
    StreamEventRepository,
    TraceRepository,
)


def test_messages_are_sequenced_and_isolated_by_session(database: Database) -> None:
    # 验证消息按序编号且按会话隔离。
    sessions = SessionRepository(database)
    messages = MessageRepository(database)
    first = sessions.create(session_id="session_a")
    second = sessions.create(session_id="session_b")

    a1 = messages.append(
        first.id,
        role=MessageRole.USER,
        kind=MessageKind.MESSAGE,
        content={"text": "a1"},
    )
    b1 = messages.append(
        second.id,
        role=MessageRole.USER,
        kind=MessageKind.MESSAGE,
        content={"text": "b1"},
    )
    a2 = messages.append(
        first.id,
        role=MessageRole.ASSISTANT,
        kind=MessageKind.MESSAGE,
        content={"text": "a2"},
    )

    assert (a1.seq, a2.seq, b1.seq) == (1, 2, 1)
    assert [item.content["text"] for item in messages.list_after(first.id)] == ["a1", "a2"]
    assert [item.content["text"] for item in messages.list_after(second.id)] == ["b1"]


def test_concurrent_message_appends_allocate_unique_sequences(database: Database) -> None:
    # 验证并发追加消息会分配唯一序号。
    sessions = SessionRepository(database)
    messages = MessageRepository(database)
    session = sessions.create()

    def append(index: int) -> int:
        return messages.append(
            session.id,
            role=MessageRole.USER,
            kind=MessageKind.MESSAGE,
            content={"index": index},
        ).seq

    with ThreadPoolExecutor(max_workers=8) as executor:
        sequences = list(executor.map(append, range(20)))

    assert sorted(sequences) == list(range(1, 21))
    assert [message.seq for message in messages.list_after(session.id)] == list(range(1, 21))


def test_summary_coverage_must_advance_and_stay_within_messages(database: Database) -> None:
    # 验证摘要覆盖范围必须推进且不能超出消息。
    sessions = SessionRepository(database)
    messages = MessageRepository(database)
    session = sessions.create()
    for index in range(3):
        messages.append(
            session.id,
            role=MessageRole.USER,
            kind=MessageKind.MESSAGE,
            content={"text": str(index)},
        )

    first_summary = sessions.update_summary(
        session.id,
        summary="first",
        from_seq=1,
        through_seq=1,
    )

    assert first_summary.summary_through_seq == 1
    with pytest.raises(InvalidStateError, match="advance"):
        sessions.update_summary(
            session.id,
            summary="duplicate",
            from_seq=1,
            through_seq=1,
        )
    with pytest.raises(InvalidStateError, match="do not exist"):
        sessions.update_summary(
            session.id,
            summary="future",
            from_seq=1,
            through_seq=4,
        )

    advanced = sessions.update_summary(
        session.id,
        summary="advanced",
        from_seq=1,
        through_seq=2,
    )
    assert advanced.summary == "advanced"
    assert advanced.summary_through_seq == 2


def test_session_title_update_and_delete_cascade(database: Database) -> None:
    # 验证会话标题更新和删除级联。
    sessions = SessionRepository(database)
    messages = MessageRepository(database)
    runs = RunRepository(database)
    traces = TraceRepository(database)
    events = StreamEventRepository(database)
    session = sessions.create(title="Old")
    user_message = messages.append(
        session.id,
        role=MessageRole.USER,
        kind=MessageKind.MESSAGE,
        content={"text": "hello"},
    )
    run = runs.create(session.id, user_message_id=user_message.id)
    traces.append(run.id, step_no=0, event_type="run.started", payload={})
    events.append_batch(
        run.id,
        [PendingStreamEvent(event_type="run.started", payload={"run_id": run.id})],
    )

    updated = sessions.update_title(session.id, title="  New title  ")

    assert updated.title == "New title"
    with pytest.raises(ActiveRunExistsError):
        sessions.delete(session.id)

    runs.fail(run.id, error_code="TEST", error_message="done")
    sessions.delete(session.id)

    assert sessions.get(session.id) is None
    assert messages.get(user_message.id) is None
    assert runs.get(run.id) is None
    assert traces.list_for_run(run.id) == []
    assert events.list_after_id(run.id) == []


def test_session_update_and_delete_require_existing_session(database: Database) -> None:
    # 验证更新和删除会话要求目标存在。
    sessions = SessionRepository(database)

    with pytest.raises(NotFoundError):
        sessions.update_title("missing", title="new")
    with pytest.raises(NotFoundError):
        sessions.delete("missing")


def test_only_one_run_can_be_running_per_session(database: Database) -> None:
    # 验证每个会话最多只有一个运行中任务。
    sessions = SessionRepository(database)
    messages = MessageRepository(database)
    runs = RunRepository(database)
    session = sessions.create()
    first_user = messages.append(
        session.id,
        role=MessageRole.USER,
        kind=MessageKind.MESSAGE,
        content={"text": "first"},
    )
    second_user = messages.append(
        session.id,
        role=MessageRole.USER,
        kind=MessageKind.MESSAGE,
        content={"text": "second"},
    )

    first_run = runs.create(session.id, user_message_id=first_user.id)
    with pytest.raises(ActiveRunExistsError):
        runs.create(session.id, user_message_id=second_user.id)

    assert messages.get(second_user.id).run_id is None
    failed = runs.fail(first_run.id, error_code="TEST_FAILURE", error_message="expected")
    second_run = runs.create(session.id, user_message_id=second_user.id)

    assert failed.status is RunStatus.FAILED
    assert second_run.status is RunStatus.RUNNING


def test_user_message_and_run_are_created_atomically(database: Database) -> None:
    # 验证用户消息和任务原子创建。
    sessions = SessionRepository(database)
    messages = MessageRepository(database)
    runs = RunRepository(database)
    session = sessions.create()

    user, run = runs.create_with_user_message(session.id, text="hello")

    assert user.run_id == run.id
    assert run.user_message_id == user.id
    assert messages.get(user.id) == user

    with pytest.raises(ActiveRunExistsError):
        runs.create_with_user_message(session.id, text="must not be orphaned")

    assert [message.content["text"] for message in messages.list_after(session.id)] == ["hello"]


def test_run_completion_requires_its_own_assistant_message(database: Database) -> None:
    # 验证任务完成必须关联自身的助手消息。
    sessions = SessionRepository(database)
    messages = MessageRepository(database)
    runs = RunRepository(database)
    session = sessions.create()
    user_message = messages.append(
        session.id,
        role=MessageRole.USER,
        kind=MessageKind.MESSAGE,
        content={"text": "question"},
    )
    run = runs.create(session.id, user_message_id=user_message.id)
    run = runs.increment_loop(run.id)
    final_message = messages.append(
        session.id,
        run_id=run.id,
        role=MessageRole.ASSISTANT,
        kind=MessageKind.MESSAGE,
        content={"text": "answer"},
    )

    completed = runs.complete(run.id, final_message_id=final_message.id)

    assert completed.status is RunStatus.COMPLETED
    assert completed.loop_count == 1
    assert completed.final_message_id == final_message.id
    assert completed.finished_at is not None


def test_message_cannot_reference_run_from_another_session(database: Database) -> None:
    # 验证消息不能引用其他会话的任务。
    sessions = SessionRepository(database)
    messages = MessageRepository(database)
    runs = RunRepository(database)
    first = sessions.create()
    second = sessions.create()
    user_message = messages.append(
        first.id,
        role=MessageRole.USER,
        kind=MessageKind.MESSAGE,
        content={"text": "question"},
    )
    run = runs.create(first.id, user_message_id=user_message.id)

    with pytest.raises(InvalidStateError, match="same session"):
        messages.append(
            second.id,
            run_id=run.id,
            role=MessageRole.TOOL,
            kind=MessageKind.TOOL_RESULT,
            content={"ok": True},
        )


def test_trace_and_stream_events_are_ordered_per_run(database: Database) -> None:
    # 验证 Trace 和流事件在任务内保持顺序。
    sessions = SessionRepository(database)
    messages = MessageRepository(database)
    runs = RunRepository(database)
    traces = TraceRepository(database)
    events = StreamEventRepository(database)
    session = sessions.create()
    user_message = messages.append(
        session.id,
        role=MessageRole.USER,
        kind=MessageKind.MESSAGE,
        content={"text": "hello"},
    )
    run = runs.create(session.id, user_message_id=user_message.id)

    traces.append(
        run.id,
        step_no=0,
        event_type="run.started",
        payload={"session_id": session.id},
        duration_ms=2,
    )
    first_batch = events.append_batch(
        run.id,
        [
            PendingStreamEvent(event_type="run.started", payload={"run_id": run.id}),
            PendingStreamEvent(
                event_type="assistant.delta",
                payload={"text": "你"},
                source_seq=4,
            ),
        ],
    )
    second_batch = events.append_batch(
        run.id,
        [PendingStreamEvent(event_type="assistant.delta", payload={"text": "好"}, source_seq=5)],
    )

    assert [trace.event_type for trace in traces.list_for_run(run.id)] == ["run.started"]
    assert [event.seq for event in [*first_batch, *second_batch]] == [1, 2, 3]
    assert [
        event.payload["text"] for event in events.list_after_id(run.id, after_id=first_batch[0].id)
    ] == ["你", "好"]


def test_startup_recovery_marks_only_running_runs_failed(database: Database) -> None:
    # 验证启动恢复只将运行中任务标记为失败。
    sessions = SessionRepository(database)
    messages = MessageRepository(database)
    runs = RunRepository(database)

    completed_session = sessions.create()
    completed_user = messages.append(
        completed_session.id,
        role=MessageRole.USER,
        kind=MessageKind.MESSAGE,
        content={"text": "done"},
    )
    completed_run = runs.create(completed_session.id, user_message_id=completed_user.id)
    completed_message = messages.append(
        completed_session.id,
        run_id=completed_run.id,
        role=MessageRole.ASSISTANT,
        kind=MessageKind.MESSAGE,
        content={"text": "done"},
    )
    runs.complete(completed_run.id, final_message_id=completed_message.id)

    running_session = sessions.create()
    running_user = messages.append(
        running_session.id,
        role=MessageRole.USER,
        kind=MessageKind.MESSAGE,
        content={"text": "interrupted"},
    )
    running_run = runs.create(running_session.id, user_message_id=running_user.id)

    changed = runs.mark_running_failed(
        error_code="PROCESS_RESTARTED",
        error_message="Application restarted",
    )

    assert changed == 1
    assert runs.get(completed_run.id).status is RunStatus.COMPLETED
    assert runs.get(running_run.id).status is RunStatus.FAILED


def test_final_answer_run_and_terminal_event_commit_atomically(database: Database) -> None:
    # 验证最终回答、任务和终态事件原子提交。
    sessions = SessionRepository(database)
    messages = MessageRepository(database)
    runs = RunRepository(database)
    events = StreamEventRepository(database)
    session = sessions.create()
    user = messages.append(
        session.id,
        role=MessageRole.USER,
        kind=MessageKind.MESSAGE,
        content={"text": "question"},
    )
    run = runs.create(session.id, user_message_id=user.id)
    runs.increment_loop(run.id)

    completed, answer, terminal = runs.complete_with_final_answer(run.id, text="answer")

    assert completed.status is RunStatus.COMPLETED
    assert completed.final_message_id == answer.id
    assert messages.get(answer.id).content == {"text": "answer"}
    assert terminal.event_type == "run.completed"
    assert terminal.payload["message_id"] == answer.id
    assert events.list_after_id(run.id)[-1] == terminal
    assert TraceRepository(database).list_for_run(run.id)[-1].event_type == "run.completed"


def test_terminal_event_insert_failure_rolls_back_answer_and_run(database: Database) -> None:
    # 验证终态事件写入失败会回滚回答和任务。
    sessions = SessionRepository(database)
    messages = MessageRepository(database)
    runs = RunRepository(database)
    events = StreamEventRepository(database)
    session = sessions.create()
    user = messages.append(
        session.id,
        role=MessageRole.USER,
        kind=MessageKind.MESSAGE,
        content={"text": "question"},
    )
    run = runs.create(session.id, user_message_id=user.id)
    with database.connection() as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_completed_event
            BEFORE INSERT ON stream_events
            WHEN NEW.event_type = 'run.completed'
            BEGIN
                SELECT RAISE(ABORT, 'terminal rejected');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="terminal rejected"):
        runs.complete_with_final_answer(run.id, text="must roll back")

    assert runs.require(run.id).status is RunStatus.RUNNING
    assert [message.id for message in messages.list_after(session.id)] == [user.id]
    assert events.list_after_id(run.id) == []
