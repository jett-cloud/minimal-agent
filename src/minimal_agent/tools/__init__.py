"""Built-in tools and the framework-free registry."""

from minimal_agent.tools.base import (
    ToolDefinition,
    ToolError,
    ToolExecutionContext,
    ToolExecutionError,
    ToolResult,
    ToolResultUnknownError,
    ToolRetrySemantics,
    ToolSideEffect,
)
from minimal_agent.tools.calculator import CALCULATOR_TOOL, CalculatorArguments, calculate
from minimal_agent.tools.executor import (
    DurableToolExecutor,
    DurableToolOutcome,
    PreparedToolInvocation,
)
from minimal_agent.tools.orders import (
    OrderCreateArguments,
    OrderDeleteArguments,
    OrderGetArguments,
    OrderListArguments,
    OrderUpdateArguments,
    create_order_tools,
)
from minimal_agent.tools.registry import ToolRegistry
from minimal_agent.tools.search import (
    MainboardCatalog,
    MainboardDatasetError,
    MainboardRecord,
    MainboardSearchArguments,
    create_mainboard_search_tool,
)
from minimal_agent.tools.weather import (
    OpenMeteoWeatherClient,
    WeatherArguments,
    create_open_meteo_http_client,
    create_weather_tool,
)

__all__ = [
    "CALCULATOR_TOOL",
    "CalculatorArguments",
    "MainboardCatalog",
    "MainboardDatasetError",
    "MainboardRecord",
    "MainboardSearchArguments",
    "OpenMeteoWeatherClient",
    "OrderCreateArguments",
    "OrderDeleteArguments",
    "OrderGetArguments",
    "OrderListArguments",
    "OrderUpdateArguments",
    "ToolDefinition",
    "DurableToolExecutor",
    "DurableToolOutcome",
    "PreparedToolInvocation",
    "ToolError",
    "ToolExecutionError",
    "ToolExecutionContext",
    "ToolRegistry",
    "ToolResult",
    "ToolResultUnknownError",
    "ToolRetrySemantics",
    "ToolSideEffect",
    "WeatherArguments",
    "calculate",
    "create_mainboard_search_tool",
    "create_order_tools",
    "create_open_meteo_http_client",
    "create_weather_tool",
]
