"""Run state and diagnostic Trace HTTP routes."""

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends

from minimal_agent.api.errors import APIError
from minimal_agent.dependencies import get_application_services
from minimal_agent.schemas import APIErrorResponse, RunRecord, TraceRecord
from minimal_agent.services import ApplicationServices

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
