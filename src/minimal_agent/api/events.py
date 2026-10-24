"""SSE endpoint backed by durable stream events."""

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import StreamingResponse

from minimal_agent.api.errors import APIError
from minimal_agent.config import Settings, get_settings
from minimal_agent.dependencies import (
    get_event_publisher,
    get_run_repository,
    get_stream_event_repository,
)
from minimal_agent.schemas import APIErrorResponse
from minimal_agent.storage import RunRepository, StreamEventRepository
from minimal_agent.streaming import EventPublisher, replay_run_events

router = APIRouter(prefix="/v1/runs", tags=["run-events"])


@router.get(
    "/{run_id}/events",
    response_class=StreamingResponse,
    responses={
        400: {"model": APIErrorResponse, "description": "Invalid resume cursor"},
        404: {"model": APIErrorResponse, "description": "Run not found"},
        422: {"model": APIErrorResponse, "description": "Invalid query parameter"},
    },
)
async def get_run_events(
    run_id: str,
    request: Request,
    events: Annotated[StreamEventRepository, Depends(get_stream_event_repository)],
    runs: Annotated[RunRepository, Depends(get_run_repository)],
    publisher: Annotated[EventPublisher, Depends(get_event_publisher)],
    settings: Annotated[Settings, Depends(get_settings)],
    after_id: Annotated[int | None, Query(ge=0)] = None,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    """Replay a Run's committed events and follow it until its terminal state."""

    if await asyncio.to_thread(runs.get, run_id) is None:
        raise APIError(404, "RUN_NOT_FOUND", "Run not found")
    resume_after = _resume_after_id(after_id, last_event_id)
    stream = replay_run_events(
        run_id,
        after_id=resume_after,
        events=events,
        runs=runs,
        publisher=publisher,
        heartbeat_seconds=settings.sse_heartbeat_seconds,
        is_disconnected=request.is_disconnected,
    )
    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _resume_after_id(after_id: int | None, last_event_id: str | None) -> int:
    if after_id is not None:
        return after_id
    if last_event_id is None:
        return 0
    try:
        parsed = int(last_event_id)
    except ValueError as error:
        raise APIError(
            400,
            "INVALID_LAST_EVENT_ID",
            "Last-Event-ID must be a non-negative integer",
        ) from error
    if parsed < 0:
        raise APIError(
            400,
            "INVALID_LAST_EVENT_ID",
            "Last-Event-ID must be a non-negative integer",
        )
    return parsed
