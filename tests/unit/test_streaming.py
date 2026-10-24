import asyncio

import pytest
from pydantic import ValidationError

from minimal_agent.schemas import MessageKind, MessageRole, PendingStreamEvent
from minimal_agent.storage import (
    Database,
    MessageRepository,
    RunRepository,
    SessionRepository,
    StreamEventRepository,
)
from minimal_agent.streaming import (
    AssistantDeltaEvent,
    BufferedEventWriter,
    EventPublisher,
    ReasoningDeltaEvent,
    RunCompletedEvent,
    RunStartedEvent,
    replay_run_events,
)


def _create_run(
    database: Database,
) -> tuple[str, RunRepository, StreamEventRepository]:
    sessions = SessionRepository(database)
    messages = MessageRepository(database)
    runs = RunRepository(database)
    session = sessions.create()
    user = messages.append(
        session.id,
        role=MessageRole.USER,
        kind=MessageKind.MESSAGE,
        content={"text": "hello"},
    )
    run = runs.create(session.id, user_message_id=user.id)
    return run.id, runs, StreamEventRepository(database)


def test_public_event_schema_is_strict_and_converts_to_pending() -> None:
    event = AssistantDeltaEvent(item_id="item_1", delta="你", source_seq=4)

    assert event.to_pending() == PendingStreamEvent(
        event_type="assistant.delta",
        payload={"item_id": "item_1", "delta": "你"},
        source_seq=4,
    )
    with pytest.raises(ValidationError):
        AssistantDeltaEvent(item_id="item_1", delta="x", unexpected=True)  # type: ignore[call-arg]


@pytest.mark.asyncio
async def test_writer_preserves_order_and_only_merges_adjacent_matching_deltas(
    database: Database,
) -> None:
    run_id, _, events = _create_run(database)
    publisher = EventPublisher()
    writer = BufferedEventWriter(
        run_id,
        store=events,
        publisher=publisher,
        flush_bytes=100_000,
        flush_interval_seconds=10,
    )

    await writer.publish(AssistantDeltaEvent(item_id="assistant_1", delta="你", source_seq=1))
    await writer.publish(AssistantDeltaEvent(item_id="assistant_1", delta="好", source_seq=2))
    await writer.publish(ReasoningDeltaEvent(item_id="reasoning_1", delta="r"))
    await writer.publish(AssistantDeltaEvent(item_id="assistant_1", delta="!", source_seq=3))
    await writer.flush()
    await writer.close()

    stored = events.list_after_id(run_id)
    assert [event.seq for event in stored] == [1, 2, 3]
    assert [event.event_type for event in stored] == [
        "assistant.delta",
        "reasoning.delta",
        "assistant.delta",
    ]
    assert [event.payload["delta"] for event in stored] == ["你好", "r", "!"]
    assert stored[0].source_seq == 1
    assert publisher.revision(run_id) == 1


@pytest.mark.asyncio
async def test_writer_flushes_at_byte_threshold(database: Database) -> None:
    run_id, _, events = _create_run(database)
    publisher = EventPublisher()
    writer = BufferedEventWriter(
        run_id,
        store=events,
        publisher=publisher,
        flush_bytes=8192,
        flush_interval_seconds=10,
    )
    revision = publisher.revision(run_id)

    await writer.publish(AssistantDeltaEvent(item_id="item_1", delta="x" * 9000))
    changed = await publisher.wait_for_change(
        run_id,
        after_revision=revision,
        timeout_seconds=1,
    )

    assert changed == 1
    assert events.list_after_id(run_id)[0].payload["delta"] == "x" * 9000
    await writer.close()


@pytest.mark.asyncio
async def test_writer_flushes_after_interval(database: Database) -> None:
    run_id, _, events = _create_run(database)
    publisher = EventPublisher()
    writer = BufferedEventWriter(
        run_id,
        store=events,
        publisher=publisher,
        flush_bytes=100_000,
        flush_interval_seconds=0.02,
    )

    await writer.publish(AssistantDeltaEvent(item_id="item_1", delta="small"))
    changed = await publisher.wait_for_change(
        run_id,
        after_revision=0,
        timeout_seconds=1,
    )

    assert changed == 1
    assert [event.payload["delta"] for event in events.list_after_id(run_id)] == ["small"]
    await writer.close()


@pytest.mark.asyncio
async def test_terminal_barrier_flushes_pending_and_rejects_later_events(
    database: Database,
) -> None:
    run_id, _, events = _create_run(database)
    publisher = EventPublisher()
    writer = BufferedEventWriter(
        run_id,
        store=events,
        publisher=publisher,
        flush_bytes=100_000,
        flush_interval_seconds=10,
    )

    await writer.publish(AssistantDeltaEvent(item_id="item_1", delta="answer"))
    await writer.publish(RunCompletedEvent(run_id=run_id, message_id="message_1"))
    await publisher.wait_for_change(run_id, after_revision=0, timeout_seconds=1)

    assert [event.event_type for event in events.list_after_id(run_id)] == [
        "assistant.delta",
        "run.completed",
    ]
    with pytest.raises(RuntimeError, match="terminal"):
        await writer.publish(AssistantDeltaEvent(item_id="item_1", delta="late"))
    await writer.close()


@pytest.mark.asyncio
async def test_publisher_wakes_all_subscribers() -> None:
    publisher = EventPublisher()
    waiters = [
        asyncio.create_task(publisher.wait_for_change("run_1", after_revision=0, timeout_seconds=1))
        for _ in range(2)
    ]
    await publisher.notify_committed("run_1")

    assert await asyncio.gather(*waiters) == [1, 1]


@pytest.mark.asyncio
async def test_replay_resumes_after_global_event_id_without_duplicates(database: Database) -> None:
    run_id, runs, events = _create_run(database)
    records = events.append_batch(
        run_id,
        [
            RunStartedEvent(run_id=run_id, session_id="session_1").to_pending(),
            AssistantDeltaEvent(item_id="item_1", delta="hello").to_pending(),
            RunCompletedEvent(run_id=run_id, message_id="message_1").to_pending(),
        ],
    )

    chunks = [
        chunk
        async for chunk in replay_run_events(
            run_id,
            after_id=records[0].id,
            events=events,
            runs=runs,
            publisher=EventPublisher(),
            heartbeat_seconds=1,
            batch_size=1,
        )
    ]

    assert len(chunks) == 2
    assert f"id: {records[1].id}\n" in chunks[0]
    assert "event: assistant.delta" in chunks[0]
    assert "event: run.completed" in chunks[1]
    assert f"id: {records[0].id}\n" not in chunks[0]


@pytest.mark.asyncio
async def test_heartbeat_is_not_persisted(database: Database) -> None:
    run_id, runs, events = _create_run(database)
    stream = replay_run_events(
        run_id,
        after_id=0,
        events=events,
        runs=runs,
        publisher=EventPublisher(),
        heartbeat_seconds=0.01,
    )

    assert await anext(stream) == ": heartbeat\n\n"
    await stream.aclose()
    assert events.list_after_id(run_id) == []


@pytest.mark.asyncio
async def test_disconnected_reader_does_not_cancel_writer(database: Database) -> None:
    run_id, runs, events = _create_run(database)
    publisher = EventPublisher()
    writer = BufferedEventWriter(run_id, store=events, publisher=publisher)

    async def disconnected() -> bool:
        return True

    stream = replay_run_events(
        run_id,
        after_id=0,
        events=events,
        runs=runs,
        publisher=publisher,
        heartbeat_seconds=1,
        is_disconnected=disconnected,
    )
    with pytest.raises(StopAsyncIteration):
        await anext(stream)

    await writer.publish(AssistantDeltaEvent(item_id="item_1", delta="still running"))
    await writer.publish(RunCompletedEvent(run_id=run_id, message_id="message_1"))
    await writer.close()
    assert [event.event_type for event in events.list_after_id(run_id)] == [
        "assistant.delta",
        "run.completed",
    ]
