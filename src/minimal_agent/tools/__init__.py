"""Built-in tools and the framework-free registry."""

from minimal_agent.tools.base import (
    ToolDefinition,
    ToolError,
    ToolExecutionError,
    ToolResult,
    ToolRetrySemantics,
    ToolSideEffect,
)
from minimal_agent.tools.calculator import CALCULATOR_TOOL, CalculatorArguments, calculate
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
    "ToolDefinition",
    "ToolError",
    "ToolExecutionError",
    "ToolRegistry",
    "ToolResult",
    "ToolRetrySemantics",
    "ToolSideEffect",
    "WeatherArguments",
    "calculate",
    "create_mainboard_search_tool",
    "create_open_meteo_http_client",
    "create_weather_tool",
]
