"""Application service graph and owned-client lifecycle."""

from dataclasses import dataclass
from typing import Any

import httpx

from minimal_agent.agent import (
    AgentLoop,
    ContextBuilder,
    HeuristicTokenCounter,
    LLMSessionSummarizer,
    RunSupervisor,
    load_prompt,
)
from minimal_agent.config import Settings
from minimal_agent.llm import (
    LLMClient,
    ModelContextWindow,
    ModelContextWindowError,
    OpenAIChatCompletionsLLM,
    OpenAIModelContextWindow,
    OpenAIResponsesLLM,
    ReasoningAwareLLM,
    RetryPolicy,
    StaticModelContextWindow,
    create_openai_sdk_client,
)
from minimal_agent.storage import (
    Database,
    MessageRepository,
    OrderRepository,
    RunRepository,
    SessionRepository,
    StreamEventRepository,
    ToolInvocationRepository,
    TraceRepository,
)
from minimal_agent.streaming import EventPublisher
from minimal_agent.tools import (
    CALCULATOR_TOOL,
    DurableToolExecutor,
    ToolRegistry,
    create_mainboard_search_tool,
    create_open_meteo_http_client,
    create_order_tools,
    create_weather_tool,
)


@dataclass(slots=True)
class ApplicationServices:
    settings: Settings
    database: Database
    sessions: SessionRepository
    messages: MessageRepository
    runs: RunRepository
    traces: TraceRepository
    stream_events: StreamEventRepository
    orders: OrderRepository
    tool_invocations: ToolInvocationRepository
    publisher: EventPublisher
    supervisor: RunSupervisor
    agent_unavailable_reason: str | None = None
    model_context_window: ModelContextWindow | None = None
    weather_http_client: httpx.AsyncClient | None = None
    llm_sdk_client: Any | None = None

    async def initialize(self) -> None:
        """Resolve remote model metadata before accepting Agent Runs."""

        if self.agent_unavailable_reason is not None or self.model_context_window is None:
            return
        try:
            await self.model_context_window.get()
        except ModelContextWindowError as error:
            self.agent_unavailable_reason = f"{error.code}: {error.message}"
        except Exception as error:
            self.agent_unavailable_reason = (
                f"Model context window discovery failed ({type(error).__name__})"
            )

    async def close(self) -> None:
        await self.supervisor.shutdown(grace_seconds=self.settings.agent_shutdown_grace_seconds)
        if self.weather_http_client is not None:
            await self.weather_http_client.aclose()
        if self.llm_sdk_client is not None:
            await self.llm_sdk_client.close()


def build_services(
    settings: Settings,
    *,
    llm: LLMClient | None = None,
    tools: ToolRegistry | None = None,
) -> ApplicationServices:
    """Build one explicit process-local dependency graph without global registries."""

    database = Database(settings.database_path)
    database.initialize()
    sessions = SessionRepository(database)
    messages = MessageRepository(database)
    runs = RunRepository(database)
    traces = TraceRepository(database)
    stream_events = StreamEventRepository(database)
    orders = OrderRepository(database)
    tool_invocations = ToolInvocationRepository(database)
    publisher = EventPublisher()
    supervisor = RunSupervisor(None, runs=runs, publisher=publisher)
    services = ApplicationServices(
        settings=settings,
        database=database,
        sessions=sessions,
        messages=messages,
        runs=runs,
        traces=traces,
        stream_events=stream_events,
        orders=orders,
        tool_invocations=tool_invocations,
        publisher=publisher,
        supervisor=supervisor,
    )

    configuration_error = _agent_configuration_error(settings, injected_llm=llm)
    if configuration_error is not None:
        services.agent_unavailable_reason = configuration_error
        return services

    try:
        resolved_llm = llm or _create_llm(settings, services)
        if llm is not None:
            if settings.injected_llm_context_window_tokens is None:
                raise ValueError("Injected LLM context window is not configured")
            model_context_window: ModelContextWindow = StaticModelContextWindow(
                settings.injected_llm_context_window_tokens
            )
        else:
            if services.llm_sdk_client is None or settings.llm_model is None:
                raise RuntimeError("Real LLM services were not initialized")
            model_context_window = OpenAIModelContextWindow(
                services.llm_sdk_client.models,
                model=settings.llm_model,
            )
        services.model_context_window = model_context_window
        resolved_tools = tools or _create_tools(settings, services)
        tool_executor = DurableToolExecutor(resolved_tools, tool_invocations)
        context = ContextBuilder(
            sessions=sessions,
            messages=messages,
            runs=runs,
            token_counter=HeuristicTokenCounter(),
            summarizer=LLMSessionSummarizer(
                resolved_llm,
                prompt=load_prompt(settings.summarize_prompt_path),
            ),
            system_prompt=load_prompt(settings.system_prompt_path),
            model_context_window=model_context_window,
            trigger_ratio=settings.context_trigger_ratio,
            recent_turns_to_keep=settings.recent_turns_to_keep,
            reasoning_effort=settings.llm_reasoning_effort,
            reasoning_summary=settings.llm_reasoning_summary,
        )
        supervisor.set_loop(
            AgentLoop(
                llm=resolved_llm,
                context=context,
                tools=resolved_tools,
                messages=messages,
                runs=runs,
                traces=traces,
                stream_events=stream_events,
                publisher=publisher,
                tool_executor=tool_executor,
                max_steps=settings.agent_max_steps,
                stream_flush_bytes=settings.stream_flush_bytes,
                stream_flush_interval_seconds=settings.stream_flush_interval_ms / 1000,
                stream_queue_maxsize=settings.stream_queue_maxsize,
            )
        )
    except Exception as error:
        services.agent_unavailable_reason = (
            f"Agent runtime initialization failed ({type(error).__name__})"
        )
    return services


def _agent_configuration_error(
    settings: Settings,
    *,
    injected_llm: LLMClient | None,
) -> str | None:
    if injected_llm is not None:
        if settings.injected_llm_context_window_tokens is None:
            return "Injected LLM context window must be configured"
        return None
    if settings.llm_api_key is None or not settings.llm_api_key.get_secret_value().strip():
        return "LLM_API_KEY must be configured"
    if settings.llm_model is None or not settings.llm_model.strip():
        return "LLM_MODEL must be configured"
    return None


def _create_llm(settings: Settings, services: ApplicationServices) -> LLMClient:
    if settings.llm_api_key is None or settings.llm_model is None:
        raise ValueError("LLM credentials are not configured")
    sdk_client = create_openai_sdk_client(
        settings.llm_api_key,
        base_url=str(settings.llm_base_url) if settings.llm_base_url is not None else None,
        timeout_seconds=settings.llm_timeout_seconds,
    )
    services.llm_sdk_client = sdk_client
    adapter_type = (
        OpenAIResponsesLLM if settings.llm_api_style == "responses" else OpenAIChatCompletionsLLM
    )
    adapter = adapter_type(
        sdk_client,
        model=settings.llm_model,
        retry_policy=RetryPolicy(max_retries=settings.llm_max_retries),
    )
    return ReasoningAwareLLM(
        adapter,
        capture=settings.llm_reasoning_capture,
        output=settings.llm_reasoning_output,
        think_tag_enabled=settings.llm_think_tag_enabled,
        think_tag_name=settings.llm_think_tag_name,
    )


def _create_tools(settings: Settings, services: ApplicationServices) -> ToolRegistry:
    weather_http_client = create_open_meteo_http_client(settings.weather_http_timeout_seconds)
    services.weather_http_client = weather_http_client
    return ToolRegistry(
        [
            CALCULATOR_TOOL,
            create_mainboard_search_tool(settings.mainboard_data_path),
            create_weather_tool(weather_http_client),
            *create_order_tools(
                services.orders, timeout_seconds=settings.order_tool_timeout_seconds
            ),
        ]
    )
