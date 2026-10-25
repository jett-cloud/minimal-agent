from pathlib import Path

import httpx
import pytest

from minimal_agent.storage import Database, OrderRepository
from minimal_agent.tools import (
    CALCULATOR_TOOL,
    ToolRegistry,
    ToolRetrySemantics,
    ToolSideEffect,
    create_mainboard_search_tool,
    create_order_tools,
    create_weather_tool,
)

_ROOT = Path(__file__).resolve().parents[2]
_REGISTERED_TOOL_TESTS = {
    "calculator": "tests/unit/tools/test_calculator.py",
    "search": "tests/unit/tools/test_search.py",
    "weather": "tests/unit/tools/test_weather.py",
    "order_create": "tests/unit/tools/test_orders.py",
    "order_get": "tests/unit/tools/test_orders.py",
    "order_list": "tests/unit/tools/test_orders.py",
    "order_update": "tests/unit/tools/test_orders.py",
    "order_delete": "tests/unit/tools/test_orders.py",
}


@pytest.mark.asyncio
async def test_every_registered_tool_has_strict_schema_semantics_and_test_module(
    tmp_path: Path,
) -> None:
    # 验证所有已注册工具都有严格 Schema、重试语义和对应测试模块。
    order_database = Database(tmp_path / "contract-orders.db")
    order_database.initialize()
    weather_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(500))
    )
    try:
        registry = ToolRegistry(
            [
                CALCULATOR_TOOL,
                create_mainboard_search_tool(_ROOT / "data/mainboards.json"),
                create_weather_tool(weather_client),
                *create_order_tools(OrderRepository(order_database)),
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
        assert registry.get("order_create").side_effect is ToolSideEffect.WRITE
        assert (
            registry.get("order_update").retry_semantics is ToolRetrySemantics.WITH_IDEMPOTENCY_KEY
        )
    finally:
        await weather_client.aclose()
