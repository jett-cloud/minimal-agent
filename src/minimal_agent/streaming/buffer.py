"""Bounded single-writer buffering for durable stream events."""

import asyncio
import json
from dataclasses import dataclass
from typing import Protocol

from minimal_agent.schemas import PendingStreamEvent, StreamEventRecord
from minimal_agent.streaming.events import (
    BARRIER_EVENT_TYPES,
    DELTA_EVENT_TYPES,
    TERMINAL_EVENT_TYPES,
    PublicStreamEvent,
)
from minimal_agent.streaming.publisher import EventPublisher


class StreamEventStore(Protocol):
    def append_batch(
        self,
        run_id: str,
        events: list[PendingStreamEvent],
    ) -> list[StreamEventRecord]: ...


@dataclass(slots=True)
class _FlushCommand:
    completed: asyncio.Future[None]


class _StopCommand:
    pass


_STOP = _StopCommand()
type _QueueItem = PublicStreamEvent | _FlushCommand | _StopCommand


class BufferedEventWriter:
    """Persist one Run's events in order through one background writer task."""

    def __init__(
        self,
        run_id: str,
        *,
        store: StreamEventStore,
        publisher: EventPublisher,
        flush_bytes: int = 8192,
        flush_interval_seconds: float = 0.1,
        queue_maxsize: int = 256,
    ) -> None:
        if not run_id:
            raise ValueError("run_id must not be empty")
        if flush_bytes < 1 or flush_interval_seconds <= 0 or queue_maxsize < 1:
            raise ValueError("Stream buffer limits must be positive")
        self._run_id = run_id
        self._store = store
        self._publisher = publisher
        self._flush_bytes = flush_bytes
        self._flush_interval_seconds = flush_interval_seconds
        self._queue: asyncio.Queue[_QueueItem] = asyncio.Queue(maxsize=queue_maxsize)
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self._terminal_enqueued = False

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("Event writer is closed")
        if self._task is None:
            self._task = asyncio.create_task(
                self._run(),
                name=f"stream-writer:{self._run_id}",
            )

    async def publish(self, event: PublicStreamEvent) -> None:
        if self._closed:
            raise RuntimeError("Event writer is closed")
        if self._terminal_enqueued:
            raise RuntimeError("Cannot publish after a terminal event")
        await self.start()
        self._raise_if_failed()
        if event.type in TERMINAL_EVENT_TYPES:
            self._terminal_enqueued = True
        await self._queue.put(event)

    async def flush(self) -> None:
        if self._closed:
            raise RuntimeError("Event writer is closed")
        await self.start()
        self._raise_if_failed()
        completed = asyncio.get_running_loop().create_future()
        await self._queue.put(_FlushCommand(completed))
        await completed

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._task is None:
            return
        self._raise_if_failed()
        await self._queue.put(_STOP)
        await self._task

    def _raise_if_failed(self) -> None:
        if self._task is not None and self._task.done():
            self._task.result()

    async def _run(self) -> None:
        pending: list[PendingStreamEvent] = []
        deadline: float | None = None
        loop = asyncio.get_running_loop()

        while True:
            timeout = None if deadline is None else max(0.0, deadline - loop.time())
            try:
                if timeout is None:
                    item = await self._queue.get()
                else:
                    item = await asyncio.wait_for(self._queue.get(), timeout=timeout)
            except TimeoutError:
                await self._commit(pending)
                pending.clear()
                deadline = None
                continue

            try:
                if isinstance(item, _StopCommand):
                    await self._commit(pending)
                    return
                if isinstance(item, _FlushCommand):
                    try:
                        await self._commit(pending)
                    except BaseException as error:
                        item.completed.set_exception(error)
                        raise
                    else:
                        item.completed.set_result(None)
                    finally:
                        pending.clear()
                        deadline = None
                    continue

                _append_or_merge(pending, item.to_pending())
                if deadline is None:
                    deadline = loop.time() + self._flush_interval_seconds
                if (
                    _batch_size_bytes(pending) >= self._flush_bytes
                    or item.type in BARRIER_EVENT_TYPES
                ):
                    await self._commit(pending)
                    pending.clear()
                    deadline = None
            finally:
                self._queue.task_done()

    async def _commit(self, pending: list[PendingStreamEvent]) -> None:
        if not pending:
            return
        records = await asyncio.to_thread(self._store.append_batch, self._run_id, list(pending))
        if records:
            await self._publisher.notify_committed(self._run_id)


def _append_or_merge(
    pending: list[PendingStreamEvent],
    event: PendingStreamEvent,
) -> None:
    if pending and _can_merge(pending[-1], event):
        previous = pending[-1]
        payload = dict(previous.payload)
        payload["delta"] = str(payload["delta"]) + str(event.payload["delta"])
        pending[-1] = PendingStreamEvent(
            event_type=previous.event_type,
            payload=payload,
            source_seq=previous.source_seq,
        )
        return
    pending.append(event)


def _can_merge(previous: PendingStreamEvent, current: PendingStreamEvent) -> bool:
    if previous.event_type != current.event_type or current.event_type not in DELTA_EVENT_TYPES:
        return False
    return (
        previous.payload.get("item_id") == current.payload.get("item_id")
        and previous.payload.get("call_id") == current.payload.get("call_id")
        and previous.payload.get("source") == current.payload.get("source")
        and previous.payload.get("content_type") == current.payload.get("content_type")
        and isinstance(previous.payload.get("delta"), str)
        and isinstance(current.payload.get("delta"), str)
    )


def _batch_size_bytes(events: list[PendingStreamEvent]) -> int:
    return sum(
        len(
            json.dumps(
                event.model_dump(mode="json"),
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        )
        for event in events
    )
