"""Replay committed stream events as resumable Server-Sent Events."""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable

from minimal_agent.schemas import RunStatus, StreamEventRecord
from minimal_agent.storage import RunRepository, StreamEventRepository
from minimal_agent.streaming.events import TERMINAL_EVENT_TYPES
from minimal_agent.streaming.publisher import EventPublisher

type DisconnectCheck = Callable[[], Awaitable[bool]]


async def _never_disconnected() -> bool:
    return False


def encode_sse(event: StreamEventRecord) -> str:
    data = json.dumps(
        event.payload,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    return f"id: {event.id}\nevent: {event.event_type}\ndata: {data}\n\n"


async def replay_run_events(
    run_id: str,
    *,
    after_id: int,
    events: StreamEventRepository,
    runs: RunRepository,
    publisher: EventPublisher,
    heartbeat_seconds: float = 15.0,
    batch_size: int = 200,
    is_disconnected: DisconnectCheck = _never_disconnected,
) -> AsyncIterator[str]:
    """Replay from SQLite, then wait for commits without coupling to the producer task."""

    if after_id < 0 or heartbeat_seconds <= 0 or not 1 <= batch_size <= 1000:
        raise ValueError("Invalid SSE replay limits")
    cursor = after_id
    revision = publisher.revision(run_id)

    while True:
        if await is_disconnected():
            return
        batch = await asyncio.to_thread(
            events.list_after_id,
            run_id,
            after_id=cursor,
            limit=batch_size,
        )
        for event in batch:
            cursor = event.id
            yield encode_sse(event)
            if event.event_type in TERMINAL_EVENT_TYPES:
                return
        if len(batch) == batch_size:
            continue

        run = await asyncio.to_thread(runs.get, run_id)
        if run is None or run.status != RunStatus.RUNNING:
            final_batch = await asyncio.to_thread(
                events.list_after_id,
                run_id,
                after_id=cursor,
                limit=batch_size,
            )
            for event in final_batch:
                cursor = event.id
                yield encode_sse(event)
                if event.event_type in TERMINAL_EVENT_TYPES:
                    return
            return
        if await is_disconnected():
            return

        next_revision = await publisher.wait_for_change(
            run_id,
            after_revision=revision,
            timeout_seconds=heartbeat_seconds,
        )
        if next_revision == revision:
            if await is_disconnected():
                return
            yield ": heartbeat\n\n"
        revision = next_revision
