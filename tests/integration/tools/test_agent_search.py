from pathlib import Path

import pytest

from minimal_agent.tools import ToolRegistry, create_mainboard_search_tool
from tests.integration.tools.helpers import run_tool_scenario

_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.asyncio
async def test_agent_search_success_is_persisted_and_returned_to_model(tmp_path: Path) -> None:
    # 验证 Agent 的搜索结果会持久化并返回模型。
    stream, messages, llm = await run_tool_scenario(
        tmp_path,
        tool_name="search",
        arguments='{"query":"ROG STRIX B650E-I","brand":"ASUS","limit":2}',
        registry=ToolRegistry([create_mainboard_search_tool(_ROOT / "data/mainboards.json")]),
    )

    assert "event: tool.completed" in stream
    assert messages[2]["content"]["result"]["data"]["count"] > 0
    assert "ROG STRIX B650E-I" in str(llm.requests[1].input)
