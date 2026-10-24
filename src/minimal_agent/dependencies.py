"""FastAPI dependencies backed by one application-owned service graph."""

from fastapi import Request

from minimal_agent.agent import RunSupervisor
from minimal_agent.api.errors import APIError
from minimal_agent.services import ApplicationServices
from minimal_agent.storage import (
    Database,
    MessageRepository,
    RunRepository,
    SessionRepository,
    StreamEventRepository,
    TraceRepository,
)
from minimal_agent.streaming import EventPublisher


def get_application_services(request: Request) -> ApplicationServices:
    services = getattr(request.app.state, "services", None)
    if not isinstance(services, ApplicationServices):
        raise APIError(503, "APP_NOT_READY", "Application services are not ready")
    return services


def get_database(request: Request) -> Database:
    return get_application_services(request).database


def get_session_repository(request: Request) -> SessionRepository:
    return get_application_services(request).sessions


def get_message_repository(request: Request) -> MessageRepository:
    return get_application_services(request).messages


def get_run_repository(request: Request) -> RunRepository:
    return get_application_services(request).runs


def get_trace_repository(request: Request) -> TraceRepository:
    return get_application_services(request).traces


def get_stream_event_repository(request: Request) -> StreamEventRepository:
    return get_application_services(request).stream_events


def get_event_publisher(request: Request) -> EventPublisher:
    return get_application_services(request).publisher


def get_run_supervisor(request: Request) -> RunSupervisor:
    services = get_application_services(request)
    if services.agent_unavailable_reason is not None:
        raise APIError(
            503,
            "AGENT_UNAVAILABLE",
            "Agent runtime is not available",
            details={"reason": services.agent_unavailable_reason},
        )
    return services.supervisor
