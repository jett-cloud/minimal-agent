from pathlib import Path

import httpx
import pytest

from minimal_agent.tools import (
    CALCULATOR_TOOL,
    ToolRegistry,
    ToolRetrySemantics,
    ToolSideEffect,
    create_mainboard_search_tool,
    create_weather_tool,
)

_ROOT = Path(__file__).resolve().parents[2]
_REGISTERED_TOOL_TESTS = {
    "calculator": "tests/unit/tools/test_calculator.py",
    "search": "tests/unit/tools/test_search.py",
    "weather": "tests/unit/tools/test_weather.py",
}


@pytest.mark.asyncio
async def test_every_registered_tool_has_strict_schema_semantics_and_test_module() -> None:
    weather_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(500))
    )
    try:
        registry = ToolRegistry(
            [
                CALCULATOR_TOOL,
                create_mainboard_search_tool(_ROOT / "data/mainboards.json"),
                create_weather_tool(weather_client),
            ]
        )
        assert set(registry.names) == set(_REGISTERED_TOOL_TESTS)
        for name in registry.names:
            tool = registry.get(name)
            assert tool is not None
            assert (_ROOT / _REGISTERED_TOOL_TESTS[name]).is_file()
            assert isinstance(tool.side_effect, ToolSideEffect)
            assert isinstance(tool.retry_semantics, ToolRetrySemantics)
            schema = tool.parameters_schema()
            assert schema["additionalProperties"] is False
            assert set(schema["required"]) == set(schema["properties"])

        assert registry.get("calculator").side_effect is ToolSideEffect.PURE
        assert registry.get("search").side_effect is ToolSideEffect.READ
        assert registry.get("weather").side_effect is ToolSideEffect.READ
    finally:
        await weather_client.aclose()
