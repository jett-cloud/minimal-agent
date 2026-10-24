"""Session and user-message HTTP routes."""

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from minimal_agent.api.errors import APIError
from minimal_agent.dependencies import get_application_services
from minimal_agent.schemas import (
    APIErrorResponse,
    CreateMessageRequest,
    CreateSessionRequest,
    MessageRecord,
    RunAcceptedResponse,
    SessionRecord,
)
from minimal_agent.services import ApplicationServices
from minimal_agent.storage import ActiveRunExistsError

router = APIRouter(prefix="/v1/sessions", tags=["sessions"])
_ERROR_RESPONSES = {
    404: {"model": APIErrorResponse, "description": "Session not found"},
    409: {"model": APIErrorResponse, "description": "Session already has a running Run"},
    422: {"model": APIErrorResponse, "description": "Request validation failed"},
    503: {"model": APIErrorResponse, "description": "Agent runtime unavailable"},
}


@router.post(
    "",
    response_model=SessionRecord,
    status_code=status.HTTP_201_CREATED,
    responses={422: _ERROR_RESPONSES[422]},
)
async def create_session(
    services: Annotated[ApplicationServices, Depends(get_application_services)],
    request: CreateSessionRequest | None = None,
) -> SessionRecord:
    payload = request or CreateSessionRequest()
    return await asyncio.to_thread(services.sessions.create, title=payload.title)


@router.get("", response_model=list[SessionRecord], responses={422: _ERROR_RESPONSES[422]})
async def list_sessions(
    services: Annotated[ApplicationServices, Depends(get_application_services)],
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[SessionRecord]:
    return await asyncio.to_thread(services.sessions.list, limit=limit, offset=offset)


@router.get(
    "/{session_id}",
    response_model=SessionRecord,
    responses={404: _ERROR_RESPONSES[404]},
)
async def get_session(
    session_id: str,
    services: Annotated[ApplicationServices, Depends(get_application_services)],
) -> SessionRecord:
    session = await asyncio.to_thread(services.sessions.get, session_id)
    if session is None:
        raise APIError(404, "SESSION_NOT_FOUND", "Session not found")
    return session


@router.get(
    "/{session_id}/messages",
    response_model=list[MessageRecord],
    responses={404: _ERROR_RESPONSES[404], 422: _ERROR_RESPONSES[422]},
)
async def list_messages(
    session_id: str,
    services: Annotated[ApplicationServices, Depends(get_application_services)],
    after_seq: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> list[MessageRecord]:
    if await asyncio.to_thread(services.sessions.get, session_id) is None:
        raise APIError(404, "SESSION_NOT_FOUND", "Session not found")
    return await asyncio.to_thread(
        services.messages.list_after,
        session_id,
        after_seq=after_seq,
        limit=limit,
    )


@router.post(
    "/{session_id}/messages",
    response_model=RunAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses=_ERROR_RESPONSES,
)
async def create_message(
    session_id: str,
    request: CreateMessageRequest,
    services: Annotated[ApplicationServices, Depends(get_application_services)],
) -> RunAcceptedResponse:
    if await asyncio.to_thread(services.sessions.get, session_id) is None:
        raise APIError(404, "SESSION_NOT_FOUND", "Session not found")
    if len(request.text) > services.settings.user_message_max_chars:
        raise APIError(
            422,
            "MESSAGE_TOO_LONG",
            "User message exceeds the configured character limit",
            details={"max_chars": services.settings.user_message_max_chars},
        )
    if services.agent_unavailable_reason is not None:
        raise APIError(
            503,
            "AGENT_UNAVAILABLE",
            "Agent runtime is not available",
            details={"reason": services.agent_unavailable_reason},
        )

    try:
        user_message, run = await asyncio.to_thread(
            services.runs.create_with_user_message,
            session_id,
            text=request.text,
        )
    except ActiveRunExistsError as error:
        raise APIError(
            409,
            "SESSION_BUSY",
            "Session already has a running Run",
        ) from error

    try:
        services.supervisor.start(run.id)
    except Exception as error:
        await asyncio.to_thread(
            services.runs.fail_with_error,
            run.id,
            error_code="RUN_START_FAILED",
            error_message="Agent Run could not be started",
        )
        await services.publisher.notify_committed(run.id)
        raise APIError(503, "RUN_START_FAILED", "Agent Run could not be started") from error

    return RunAcceptedResponse(
        run_id=run.id,
        user_message_id=user_message.id,
        events_url=f"/v1/runs/{run.id}/events",
    )
