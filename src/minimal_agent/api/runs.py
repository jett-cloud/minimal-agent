"""Run state and diagnostic Trace HTTP routes."""

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from minimal_agent.api.errors import APIError
from minimal_agent.dependencies import get_application_services
from minimal_agent.schemas import (
    APIErrorResponse,
    ReasoningHistoryItem,
    RunRecord,
    StreamEventRecord,
    TraceRecord,
)
from minimal_agent.services import ApplicationServices
from minimal_agent.streaming import reasoning_event_types, reconstruct_reasoning

router = APIRouter(prefix="/v1/runs", tags=["runs"])
_NOT_FOUND_RESPONSE = {404: {"model": APIErrorResponse, "description": "Run not found"}}


@router.get("/{run_id}", response_model=RunRecord, responses=_NOT_FOUND_RESPONSE)
async def get_run(
    run_id: str,
    services: Annotated[ApplicationServices, Depends(get_application_services)],
) -> RunRecord:
    run = await asyncio.to_thread(services.runs.get, run_id)
    if run is None:
        raise APIError(404, "RUN_NOT_FOUND", "Run not found")
    return run


@router.get(
    "/{run_id}/trace",
    response_model=list[TraceRecord],
    responses=_NOT_FOUND_RESPONSE,
)
async def get_run_trace(
    run_id: str,
    services: Annotated[ApplicationServices, Depends(get_application_services)],
) -> list[TraceRecord]:
    if await asyncio.to_thread(services.runs.get, run_id) is None:
        raise APIError(404, "RUN_NOT_FOUND", "Run not found")
    return await asyncio.to_thread(services.traces.list_for_run, run_id)


@router.get(
    "/{run_id}/reasoning",
    response_model=list[ReasoningHistoryItem],
    responses=_NOT_FOUND_RESPONSE,
)
async def get_run_reasoning(
    run_id: str,
    services: Annotated[ApplicationServices, Depends(get_application_services)],
) -> list[ReasoningHistoryItem]:
    """Return reasoning reconstructed from the Run's durable SSE events."""

    if await asyncio.to_thread(services.runs.get, run_id) is None:
        raise APIError(404, "RUN_NOT_FOUND", "Run not found")
    events = await asyncio.to_thread(
        services.stream_events.list_by_event_types,
        run_id,
        reasoning_event_types(),
    )
    return reconstruct_reasoning(events)


@router.get(
    "/{run_id}/event-history",
    response_model=list[StreamEventRecord],
    responses=_NOT_FOUND_RESPONSE,
)
async def get_run_event_history(
    run_id: str,
    services: Annotated[ApplicationServices, Depends(get_application_services)],
    after_id: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> list[StreamEventRecord]:
    """Return one page of persisted Run events without opening an SSE stream."""

    if await asyncio.to_thread(services.runs.get, run_id) is None:
        raise APIError(404, "RUN_NOT_FOUND", "Run not found")
    return await asyncio.to_thread(
        services.stream_events.list_after_id,
        run_id,
        after_id=after_id,
        limit=limit,
    )
