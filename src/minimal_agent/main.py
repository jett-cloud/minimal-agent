"""FastAPI application entry point."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel

from minimal_agent import __version__
from minimal_agent.api.errors import install_error_handlers
from minimal_agent.api.events import router as events_router
from minimal_agent.api.orders import router as orders_router
from minimal_agent.api.runs import router as runs_router
from minimal_agent.api.sessions import router as sessions_router
from minimal_agent.config import Settings, get_settings
from minimal_agent.llm import LLMClient
from minimal_agent.services import ApplicationServices, build_services
from minimal_agent.tools import ToolRegistry


class HealthResponse(BaseModel):
    """Health endpoint response."""

    status: str
    service: str
    version: str
    environment: str


def create_app(
    *,
    settings: Settings | None = None,
    llm: LLMClient | None = None,
    tools: ToolRegistry | None = None,
    services: ApplicationServices | None = None,
) -> FastAPI:
    """Create one application with explicit, replaceable process services."""

    selected_settings = settings or (services.settings if services is not None else get_settings())
    selected_services = services or build_services(
        selected_settings,
        llm=llm,
        tools=tools,
    )

    @asynccontextmanager
    async def lifespan(_application: FastAPI) -> AsyncIterator[None]:
        await selected_services.initialize()
        await selected_services.supervisor.recover_interrupted()
        try:
            yield
        finally:
            await selected_services.close()

    application = FastAPI(
        title="Minimal Agent",
        version=__version__,
        lifespan=lifespan,
    )
    application.state.services = selected_services
    application.dependency_overrides[get_settings] = lambda: selected_settings
    install_error_handlers(application)
    application.include_router(sessions_router)
    application.include_router(runs_router)
    application.include_router(events_router)
    application.include_router(orders_router)

    @application.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(
            status="ok",
            service="minimal-agent",
            version=__version__,
            environment=selected_settings.app_env,
        )

    return application


app = create_app()
